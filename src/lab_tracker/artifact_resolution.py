"""On-demand resolution of external artifact pointers.

An :class:`~lab_tracker.models.ExternalArtifactReference` is a *pointer* — a
``source_system`` + ``uri`` + ``content_hash`` — not bytes. This module turns
that pointer into a bounded, integrity-checked view of its content on demand, so
an assistant reasoning over the graph can pull content that was never captured in
the original metadata snapshot.

Design: ``docs/external-artifact-resolution-design.md`` and
``docs/data-store-registry-design.md``.

Key invariants:

* **Content hash is the integrity gate.** Every resolve recomputes the digest of
  the bytes it fetched and compares it to the reference's ``content_hash``. The
  result is tri-state — ``VERIFIED`` (bytes match the captured artifact),
  ``DRIFTED`` (reachable but the digest differs — the artifact moved or changed),
  or ``UNRESOLVED`` (no adapter, unreachable, denied, or unverifiable).
* **Bounding is independent of verification.** ``max_bytes`` (and an optional
  ``byte_range``) bound only the *returned* payload. The whole artifact is still
  streamed through the hasher, so a truncated read is still ``VERIFIED``.
* **Never hand over uncertified content.** If the digest cannot be recomputed and
  checked, the resolver returns ``UNRESOLVED`` rather than silently returning
  bytes that may not match what the graph reasoned about.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urljoin, urlsplit

from lab_tracker.bounded_subprocess import (
    DEFAULT_PROCESS_DEADLINE_SECONDS,
    DEFAULT_PROCESS_STDERR_LIMIT_BYTES,
    MAX_PROCESS_DEADLINE_SECONDS,
    BoundedSubprocessExecutor,
    ProcessDeadline,
    ProcessExecutionError,
    ProcessExecutor,
)
from lab_tracker.git_remote_policy import ApprovedGitRemote, GitRemotePolicy
from lab_tracker.local_file_access import (
    HandleBoundLocalFileAccess,
    LocalFileReader,
    LocalOpenFailure,
    LocalOpenFailureReason,
)
from lab_tracker.local_path_policy import LocalPathPolicy, native_local_path_from_uri
from lab_tracker.models import DataStore, ExternalArtifactReference, StoreKind
from lab_tracker.outbound_http import (
    DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS,
    MAX_OUTBOUND_HTTP_DEADLINE_SECONDS,
    OutboundHttpClient,
    OutboundHttpDeadline,
    OutboundHttpPolicy,
    OutboundHttpPolicyError,
    OutboundHttpTransportError,
    SafeHttpClient,
)

# Default cap on the bytes returned inline to a caller. Bounds payload size, not
# verification: the full artifact is always hashed regardless of this cap.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

# Hash algorithms we can recompute to verify a fetched artifact. A reference
# whose hash uses anything else (e.g. ``datalad-key:``) cannot be certified by
# recomputation and resolves as UNRESOLVED.
_VERIFIABLE_ALGORITHMS = frozenset(
    {"sha256", "sha1", "sha224", "sha384", "sha512", "md5"}
)

_LOCAL_SOURCE_SYSTEMS = frozenset({"local", "local_fs", "file"})
_HTTP_SCHEMES = frozenset({"http", "https"})

# Cap on the bytes an HTTP fetch will stream while verifying. The whole body must
# be hashed to certify it, so an artifact larger than this is refused
# (UNRESOLVED) rather than returned uncertified.
DEFAULT_MAX_FETCH_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_HTTP_REDIRECTS = 3
DEFAULT_SUBPROCESS_DEADLINE_SECONDS = DEFAULT_PROCESS_DEADLINE_SECONDS
MAX_SUBPROCESS_DEADLINE_SECONDS = MAX_PROCESS_DEADLINE_SECONDS

# Command metadata is small and machine-readable. These caps are intentionally
# independent so a noisy stderr cannot consume the stdout budget (or vice versa).
_PROCESS_METADATA_LIMIT_BYTES = 64 * 1024
_PROCESS_STDERR_LIMIT_BYTES = DEFAULT_PROCESS_STDERR_LIMIT_BYTES

# Protocols the git resolver's subprocess is allowed to use. Restricting this
# blocks `file://` (local reads) and `ext::`/`fd::` (arbitrary command execution)
# vectors that a malicious reference could otherwise trigger via git.
DEFAULT_GIT_ALLOW_PROTOCOL = "https:ssh:git"
_GIT_GENERIC_HTTP_REDIRECT_CONFIG = "http.followRedirects=false"
_GIT_HEALTH_FAILURE_DETAIL = "Git store health check failed."

_STREAM_CHUNK_SIZE = 1024 * 1024

# Recovery search: when a local artifact is missing at its URI (moved/renamed),
# optionally scan the resolver's allowed roots for a file whose content hash
# matches the reference, and return it VERIFIED instead of UNRESOLVED. Opt-in and
# bounded — see :class:`RecoveryPolicy` and :class:`LocalFilesystemResolver`.
DEFAULT_RECOVERY_MAX_FILES = 4096
DEFAULT_RECOVERY_MAX_BYTES = 512 * 1024 * 1024


def _uri_scheme(uri: str) -> str:
    try:
        return urlsplit(uri).scheme.lower()
    except ValueError:
        return ""


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


@dataclass(frozen=True)
class RecoveryPolicy:
    """Bounds a content-hash recovery scan of a resolver's allowed roots.

    Recovery only runs when ``enabled`` *and* the resolver was given explicit
    allowed roots to scope the walk. ``max_files`` caps how many candidate files
    the scan may examine and ``max_bytes`` how many bytes it may hash before
    giving up (falling back to UNRESOLVED, i.e. today's behaviour). The integrity
    gate is never relaxed: a recovered file is still verified by recomputing its
    full digest before its bytes are returned.
    """

    enabled: bool = False
    max_files: int = DEFAULT_RECOVERY_MAX_FILES
    max_bytes: int = DEFAULT_RECOVERY_MAX_BYTES


class ResolutionStatus(str, Enum):
    """Outcome of an attempt to dereference an external artifact pointer."""

    VERIFIED = "verified"
    DRIFTED = "drifted"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ResolvedArtifact:
    """The result of resolving an :class:`ExternalArtifactReference`.

    ``content`` holds bounded bytes only after integrity verification.  Drifted
    and unresolved results retain diagnostics but never retain artifact bytes.
    ``size_bytes`` is the *full* artifact size, which may exceed the returned
    payload when ``truncated`` is True.
    """

    status: ResolutionStatus
    source_system: str
    uri: str
    expected_hash: str
    fetched_at: datetime
    observed_hash: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    content: bytes | None = None
    truncated: bool = False
    detail: str | None = None

    def __post_init__(self) -> None:
        """Fail closed when an adapter marks uncertified bytes as verified."""

        if self.status is ResolutionStatus.VERIFIED:
            if self.observed_hash is None or not is_verifiable_hash(self.expected_hash):
                object.__setattr__(self, "status", ResolutionStatus.UNRESOLVED)
            elif parse_content_hash(self.observed_hash) != parse_content_hash(
                self.expected_hash
            ):
                object.__setattr__(self, "status", ResolutionStatus.DRIFTED)

        if self.status is not ResolutionStatus.VERIFIED and self.content is not None:
            object.__setattr__(self, "content", None)

    @property
    def is_verified(self) -> bool:
        return self.status is ResolutionStatus.VERIFIED

    @property
    def returned_bytes(self) -> int:
        return 0 if self.content is None else len(self.content)

    def to_json_dict(self) -> dict[str, object]:
        """Serialize the resolution envelope (without raw content bytes)."""

        return {
            "status": self.status.value,
            "source_system": self.source_system,
            "uri": _resolution_display_uri(self),
            "expected_hash": self.expected_hash,
            "observed_hash": self.observed_hash,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "returned_bytes": self.returned_bytes,
            "truncated": self.truncated,
            "fetched_at": self.fetched_at.isoformat(),
            "detail": self.detail,
        }


def _resolution_display_uri(result: ResolvedArtifact) -> str:
    scheme = _uri_scheme(result.uri)
    if result.status is ResolutionStatus.UNRESOLVED:
        if scheme == "rclone" or result.source_system.strip().lower() == "rclone":
            return "rclone://[redacted]"
        if result.uri.lstrip().lower().startswith(
            "git+"
        ) or result.source_system.strip().lower() == "git":
            return "git+[redacted]"
    is_http = (
        scheme in _HTTP_SCHEMES
        or result.source_system.strip().lower() in _HTTP_SCHEMES
        or result.uri.lstrip().lower().startswith(("http:", "https:"))
    )
    if not is_http:
        return result.uri
    if result.status is ResolutionStatus.UNRESOLVED:
        return "http(s)://[redacted]"
    try:
        parsed = urlsplit(result.uri)
        hostname = parsed.hostname
        if not hostname:
            raise ValueError
        port = parsed.port
    except ValueError:
        return "http(s)://[redacted]"
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme.lower()}://{authority}{parsed.path or '/'}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_content_hash(content_hash: str) -> tuple[str, str]:
    """Split a stored hash into ``(algorithm, hexdigest)``.

    Hashes are stored as ``algorithm:hexdigest`` (e.g. ``sha256:abc…``). A bare
    hex string with no prefix is assumed to be ``sha256``. Comparison is
    case-insensitive.
    """

    cleaned = (content_hash or "").strip()
    algorithm, separator, digest = cleaned.partition(":")
    if not separator:
        return "sha256", algorithm.lower()
    return algorithm.lower(), digest.lower()


def is_verifiable_hash(content_hash: str) -> bool:
    algorithm, digest = parse_content_hash(content_hash)
    return bool(digest) and algorithm in _VERIFIABLE_ALGORITHMS


class ArtifactResolver(ABC):
    """Dereferences external artifact pointers for a family of stores."""

    @abstractmethod
    def can_resolve(self, ref: ExternalArtifactReference) -> bool:
        """Return True when this resolver handles ``ref``'s store."""

    @abstractmethod
    def resolve(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Fetch a bounded view of ``ref`` and verify its content hash."""


class ResolverRegistry:
    """Dispatches a reference to the first resolver that can handle it."""

    def __init__(self, resolvers: Iterable[ArtifactResolver] | None = None) -> None:
        self._resolvers: list[ArtifactResolver] = list(resolvers or [])

    def register(self, resolver: ArtifactResolver) -> None:
        self._resolvers.append(resolver)

    def resolve(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        for resolver in self._resolvers:
            if resolver.can_resolve(ref):
                return resolver.resolve(ref, max_bytes=max_bytes, byte_range=byte_range)
        return _unresolved(
            ref,
            detail=f"No resolver registered for source_system '{ref.source_system}'.",
        )


def _unresolved(ref: ExternalArtifactReference, *, detail: str) -> ResolvedArtifact:
    return ResolvedArtifact(
        status=ResolutionStatus.UNRESOLVED,
        source_system=ref.source_system,
        uri=ref.uri,
        expected_hash=ref.content_hash,
        fetched_at=_now(),
        detail=detail,
    )


def unresolved(ref: ExternalArtifactReference, *, detail: str) -> ResolvedArtifact:
    """Public constructor for an UNRESOLVED result (e.g. a store lookup miss)."""

    return _unresolved(ref, detail=detail)


# Store kinds whose objects are fetched through the rclone adapter.
_RCLONE_STORE_KINDS = frozenset(
    {
        StoreKind.S3,
        StoreKind.GCS,
        StoreKind.AZURE_BLOB,
        StoreKind.DROPBOX,
        StoreKind.GDRIVE,
        StoreKind.BOX,
        StoreKind.ONEDRIVE,
        StoreKind.SSH,
        StoreKind.RCLONE,
    }
)


def _join_store_path(root: str, path: str) -> str:
    return f"{root.rstrip('/')}/{path.lstrip('/')}" if path else root


def store_relative_reference(
    store: DataStore, *, path: str, content_hash: str
) -> ExternalArtifactReference | None:
    """Translate a ``store://<name>/<path>`` locator into a concrete reference.

    Returns a reference one of the registered adapters can resolve, or ``None``
    when the store kind is not resolvable yet (``object_table``/``database`` need
    the deferred snapshot/query adapters) or the locator is malformed (a git
    locator needs a ``@<commit>`` pin). Credentials are never embedded — the
    rclone remote name comes from the store's ``credential_ref``, and git uses
    its own host-side credential helper keyed by the remote URL (``store.root``).
    """

    joined = _join_store_path(store.root, path)
    if store.kind is StoreKind.LOCAL_FS:
        return ExternalArtifactReference(
            source_system="local",
            uri=Path(joined).as_uri(),
            content_hash=content_hash,
        )
    if store.kind is StoreKind.HTTP:
        base = store.endpoint or store.root
        uri = f"{base.rstrip('/')}/{path.lstrip('/')}" if path else base
        return ExternalArtifactReference(
            source_system="http", uri=uri, content_hash=content_hash
        )
    if store.kind in _RCLONE_STORE_KINDS:
        remote = store.credential_ref or store.name
        return ExternalArtifactReference(
            source_system="rclone",
            uri=f"rclone://{remote}/{joined.lstrip('/')}",
            content_hash=content_hash,
        )
    if store.kind is StoreKind.GIT:
        # The locator carries the commit pin as a ``<path>@<commit>`` suffix; the
        # remote is the store root. Without a commit there is no snapshot to pin.
        file_path, sep, commit = path.rpartition("@")
        if not sep or not file_path or not commit:
            return None
        return ExternalArtifactReference(
            source_system="git",
            uri=f"git+{store.root}#{commit}:{file_path.lstrip('/')}",
            content_hash=content_hash,
        )
    return None


class StoreHealthStatus(str, Enum):
    """Outcome of probing a registered store's reachability."""

    HEALTHY = "healthy"
    UNREACHABLE = "unreachable"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class StoreHealth:
    status: StoreHealthStatus
    detail: str | None = None

    @property
    def is_healthy(self) -> bool:
        return self.status is StoreHealthStatus.HEALTHY

    def to_json_dict(self) -> dict[str, object]:
        return {"status": self.status.value, "detail": self.detail}


def check_store_health(
    store: DataStore,
    *,
    rclone_runner: RcloneRunner | None = None,
    git_runner: GitRunner | None = None,
    git_executor: ProcessExecutor | None = None,
    git_remote_policy: GitRemotePolicy | None = None,
    git_health_cwd: str | Path | None = None,
    git_binary: str = "git",
    git_allow_protocol: str | None = DEFAULT_GIT_ALLOW_PROTOCOL,
    git_deadline_seconds: float = DEFAULT_SUBPROCESS_DEADLINE_SECONDS,
    git_clock: Callable[[], float] = time.monotonic,
    http_client: object | None = None,
    http_timeout: float = 10.0,
) -> StoreHealth:
    """Probe whether a registered store is reachable from this host.

    Lightweight and read-only: a directory stat for ``local_fs``, an HTTP ``HEAD``
    for ``http``, ``rclone lsf`` for the cloud/remote kinds, and ``git ls-remote``
    for ``git``. ``object_table`` and ``database`` are reported ``unsupported``
    until their adapters land.
    """

    kind = store.kind
    if kind is StoreKind.LOCAL_FS:
        root = os.path.realpath(Path(store.root).expanduser())
        if os.path.isdir(root):
            return StoreHealth(StoreHealthStatus.HEALTHY)
        return StoreHealth(
            StoreHealthStatus.UNREACHABLE, f"Root directory not found: {store.root}"
        )

    if kind is StoreKind.HTTP:
        return _check_http_store_health(store, http_client=http_client, timeout=http_timeout)

    if kind in _RCLONE_STORE_KINDS:
        runner = rclone_runner or _subprocess_rclone_runner("rclone")
        remote = store.credential_ref or store.name
        target = f"{remote}:{store.root.lstrip('/')}"
        try:
            completed = runner(["lsf", "--max-depth", "1", target])
        except OSError as exc:
            return StoreHealth(StoreHealthStatus.UNREACHABLE, f"rclone is unavailable: {exc}")
        if completed.returncode == 0:
            return StoreHealth(StoreHealthStatus.HEALTHY)
        return StoreHealth(StoreHealthStatus.UNREACHABLE, _rclone_error_detail(completed))

    if kind is StoreKind.GIT:
        policy = (
            git_remote_policy
            if git_remote_policy is not None
            else GitRemotePolicy.deny_all()
        )
        approved = policy.authorize(store.root)
        if approved is None or git_health_cwd is None:
            return StoreHealth(
                StoreHealthStatus.UNREACHABLE,
                _GIT_HEALTH_FAILURE_DETAIL,
            )
        cwd = os.path.realpath(os.fspath(git_health_cwd))
        if not os.path.isdir(cwd) or (git_runner is not None and git_executor is not None):
            return StoreHealth(
                StoreHealthStatus.UNREACHABLE,
                _GIT_HEALTH_FAILURE_DETAIL,
            )
        try:
            deadline = ProcessDeadline.after(
                _validate_subprocess_deadline_seconds(git_deadline_seconds),
                clock=git_clock,
            )
            env = _git_environment(git_allow_protocol, cwd=cwd)
            config_args = _git_http_config_args(approved)
            executor = (
                git_executor
                if git_executor is not None
                else BoundedSubprocessExecutor()
            )
            preflight = _run_git_command(
                runner=git_runner,
                executor=executor,
                binary=git_binary,
                args=["ls-remote", "--get-url", "--", approved.subprocess_value],
                cwd=cwd,
                env=env,
                config_args=config_args,
                deadline=deadline,
                stdout_limit_bytes=_PROCESS_METADATA_LIMIT_BYTES,
            )
            if not _git_remote_preflight_matches(preflight, approved.subprocess_value):
                return StoreHealth(
                    StoreHealthStatus.UNREACHABLE,
                    _GIT_HEALTH_FAILURE_DETAIL,
                )
            completed = _run_git_command(
                runner=git_runner,
                executor=executor,
                binary=git_binary,
                args=["ls-remote", "--", approved.subprocess_value, "HEAD"],
                cwd=cwd,
                env=env,
                config_args=config_args,
                deadline=deadline,
                stdout_limit_bytes=_PROCESS_METADATA_LIMIT_BYTES,
            )
            deadline.check()
        except Exception:
            return StoreHealth(
                StoreHealthStatus.UNREACHABLE,
                _GIT_HEALTH_FAILURE_DETAIL,
            )
        if completed.returncode == 0:
            return StoreHealth(StoreHealthStatus.HEALTHY)
        return StoreHealth(
            StoreHealthStatus.UNREACHABLE,
            _GIT_HEALTH_FAILURE_DETAIL,
        )

    return StoreHealth(
        StoreHealthStatus.UNSUPPORTED,
        f"Health checks for '{kind.value}' stores are not supported yet.",
    )


def _check_http_store_health(
    store: DataStore, *, http_client: object | None, timeout: float
) -> StoreHealth:
    import httpx

    base = store.endpoint or store.root
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=timeout)
    try:
        response = client.head(base)
    except httpx.HTTPError as exc:
        return StoreHealth(StoreHealthStatus.UNREACHABLE, f"Cannot reach {base}: {exc}")
    finally:
        if owns_client:
            client.close()
    if response.status_code < 400 or response.status_code in (403, 405):
        # 403/405 mean the endpoint answered but refused HEAD — still reachable.
        return StoreHealth(StoreHealthStatus.HEALTHY)
    return StoreHealth(
        StoreHealthStatus.UNREACHABLE, f"HEAD {base} returned {response.status_code}."
    )


def _normalize_byte_range(byte_range: tuple[int, int] | None) -> tuple[int, int] | None:
    if byte_range is None:
        return None
    start, end = byte_range
    if start < 0 or end < 0:
        raise ValueError("byte_range bounds must be non-negative.")
    if end < start:
        raise ValueError("byte_range end must be >= start.")
    return start, end


class LocalFilesystemResolver(ArtifactResolver):
    """Resolves artifacts stored on this host's filesystem.

    Handles ``source_system`` of ``local``/``local_fs``/``file`` and ``file://``
    URIs. ``allowed_roots`` constrains which directories may be read; when set, a
    path that escapes every root (after symlink resolution) is refused as
    UNRESOLVED, so resolution cannot be used to read arbitrary host files.

    When ``recovery`` is enabled and allowed roots are configured, a reference
    whose file is missing at its URI (moved/renamed) triggers a file/byte-bounded
    content-hash search of those roots: a file whose recomputed digest matches
    the reference is returned VERIFIED instead of UNRESOLVED. Canonical
    pathname checks constrain the scan to the allowed roots and the content
    hash remains mandatory. Direct and recovery reads retain one validated
    descriptor through hashing, so a concurrent pathname replacement cannot
    redirect the bytes being verified.
    """

    def __init__(
        self,
        allowed_roots: Sequence[str | Path] | None = None,
        *,
        recovery: RecoveryPolicy | None = None,
        file_reader_factory: Callable[[LocalPathPolicy], LocalFileReader] | None = None,
    ) -> None:
        self._path_policy = LocalPathPolicy(allowed_roots)
        reader_factory = (
            HandleBoundLocalFileAccess
            if file_reader_factory is None
            else file_reader_factory
        )
        self._file_reader = reader_factory(self._path_policy)
        self._recovery_roots = self._path_policy.canonical_roots
        self._recovery = recovery or RecoveryPolicy()

    def can_resolve(self, ref: ExternalArtifactReference) -> bool:
        if ref.source_system.strip().lower() in _LOCAL_SOURCE_SYSTEMS:
            return True
        return _uri_scheme(ref.uri) == "file"

    def resolve(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        try:
            window = _normalize_byte_range(byte_range)
        except ValueError as exc:
            return _unresolved(ref, detail=str(exc))

        if not is_verifiable_hash(ref.content_hash):
            algorithm, _ = parse_content_hash(ref.content_hash)
            return _unresolved(
                ref,
                detail=f"Cannot verify content hash with algorithm '{algorithm}'.",
            )

        local_path = native_local_path_from_uri(ref.uri)
        if local_path is None:
            return _unresolved(
                ref,
                detail="Reference URI is not a local filesystem path.",
            )
        planned_path = self._path_policy.authorize_path(local_path)
        if planned_path is None:
            return _unresolved(
                ref,
                detail="Local artifact is not an authorized readable regular file.",
            )

        algorithm, _ = parse_content_hash(ref.content_hash)
        failure: LocalOpenFailure | None = None
        with self._file_reader.open_regular_file(planned_path) as opened:
            if isinstance(opened, LocalOpenFailure):
                failure = opened
            else:
                try:
                    content, total, truncated, observed = _hash_and_collect(
                        _read_stream_chunks(opened.stream),
                        algorithm=algorithm,
                        max_bytes=max_bytes,
                        window=window,
                    )
                except OSError:
                    return _unresolved(ref, detail="Failed to read local artifact.")

                content_type, _ = mimetypes.guess_type(opened.display_path)
                return _build_resolved(
                    ref,
                    observed=observed,
                    content=content,
                    total=total,
                    truncated=truncated,
                    content_type=content_type or "application/octet-stream",
                )

        if failure is not None and failure.reason is LocalOpenFailureReason.MISSING:
            recovered = self._recover_by_hash(
                ref,
                max_bytes=max_bytes,
                window=window,
                missing_path=planned_path,
            )
            if recovered is not None:
                return recovered
            return _unresolved(ref, detail="Local artifact not found.")
        if failure is not None and failure.reason is LocalOpenFailureReason.DENIED:
            return _unresolved(
                ref,
                detail="Local artifact is not an authorized readable regular file.",
            )
        return _unresolved(ref, detail="Failed to read local artifact.")

    def _recover_by_hash(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int,
        window: tuple[int, int] | None,
        missing_path: str,
    ) -> ResolvedArtifact | None:
        """Search the allowed roots for a file whose content matches ``ref``.

        Returns a VERIFIED result recovered from a different path, or ``None``
        when recovery is disabled, unscoped (no allowed roots), or nothing
        matches within the :class:`RecoveryPolicy` budget. Callers reach this
        only after the hash was confirmed verifiable and the URI's own file was
        missing, so a match is exactly as trustworthy as one found at the URI.
        """

        if not self._recovery.enabled or not self._recovery_roots:
            return None

        algorithm, expected_digest = parse_content_hash(ref.content_hash)
        target_name = os.path.basename(missing_path) or None

        considered = 0
        byte_budget = _CumulativeByteBudget(self._recovery.max_bytes)
        # Two passes so a moved file that kept its name is found first and
        # cheaply; skip the name-first pass when we have no basename to match.
        passes = (True, False) if target_name is not None else (False,)
        for prefer_name in passes:
            for candidate in self._iter_candidate_files(target_name, prefer_name):
                considered += 1
                if considered > self._recovery.max_files:
                    return None
                remaining_bytes = byte_budget.remaining
                if remaining_bytes <= 0:
                    return None
                planned_candidate = self._path_policy.authorize_path(candidate)
                if planned_candidate is None:
                    continue
                with self._file_reader.open_regular_file(planned_candidate) as opened:
                    if isinstance(opened, LocalOpenFailure):
                        continue
                    if opened.size_hint_bytes > remaining_bytes:
                        continue
                    try:
                        content, total, truncated, observed = _hash_and_collect(
                            _read_stream_chunks(
                                opened.stream,
                                byte_budget=byte_budget,
                            ),
                            algorithm=algorithm,
                            max_bytes=max_bytes,
                            window=window,
                            max_total=remaining_bytes,
                        )
                    except _FetchTooLarge:
                        # The candidate consumed the remaining logical budget
                        # before changing/growing past its same-handle size hint.
                        return None
                    except OSError:
                        continue
                    if parse_content_hash(observed)[1] != expected_digest:
                        continue
                    content_type, _ = mimetypes.guess_type(opened.display_path)
                    resolved = _build_resolved(
                        ref,
                        observed=observed,
                        content=content,
                        total=total,
                        truncated=truncated,
                        content_type=content_type or "application/octet-stream",
                    )
                    return replace(
                        resolved,
                        detail=(
                            f"Recovered from {opened.display_path} "
                            "(differs from reference URI)."
                        ),
                    )
        return None

    def _iter_candidate_files(
        self, target_name: str | None, prefer_name: bool
    ) -> Iterator[str]:
        """Yield files under the allowed roots for one recovery pass.

        ``prefer_name`` True yields only files whose basename equals
        ``target_name`` (the fast common case of a rename that kept the name);
        False yields the remainder. Symlinks are not followed during the walk.
        """

        for root in self._recovery_roots or ():
            for dirpath, directories, files in os.walk(
                root, topdown=True, followlinks=False
            ):
                self._path_policy.prune_walk_directories(dirpath, directories)
                for name in files:
                    is_match = target_name is not None and name == target_name
                    if prefer_name != is_match:
                        continue
                    yield os.path.join(dirpath, name)


class HttpResolver(ArtifactResolver):
    """Resolves artifacts addressed by ``http(s)`` URLs.

    Streams the full body through the hasher to verify it, capping the fetch at
    ``max_fetch_bytes``; an artifact larger than that is refused as UNRESOLVED
    rather than returned uncertified. Every destination is authorized before
    the request and the client connects only to the policy's vetted numeric
    addresses. Redirects are followed manually, with the complete policy
    reapplied to every hop.
    """

    def __init__(
        self,
        *,
        policy: OutboundHttpPolicy | None = None,
        client: OutboundHttpClient | None = None,
        deadline_seconds: float = DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        timeout: float | None = None,
        max_fetch_bytes: int = DEFAULT_MAX_FETCH_BYTES,
        max_redirects: int = DEFAULT_MAX_HTTP_REDIRECTS,
    ) -> None:
        if max_redirects < 0:
            raise ValueError("max_redirects must be non-negative.")
        if timeout is not None:
            if deadline_seconds != DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS:
                raise ValueError(
                    "Configure either deadline_seconds or the legacy timeout alias."
                )
            deadline_seconds = timeout
        if (
            not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0
            or deadline_seconds > MAX_OUTBOUND_HTTP_DEADLINE_SECONDS
        ):
            raise ValueError(
                "HTTP deadline must be finite, positive, and no greater than "
                f"{MAX_OUTBOUND_HTTP_DEADLINE_SECONDS:g} seconds."
            )
        self._policy = policy or OutboundHttpPolicy()
        self._client = client or SafeHttpClient(timeout=deadline_seconds)
        self._deadline_seconds = deadline_seconds
        self._clock = clock
        self._max_fetch_bytes = max_fetch_bytes
        self._max_redirects = max_redirects

    def can_resolve(self, ref: ExternalArtifactReference) -> bool:
        scheme = _uri_scheme(ref.uri)
        return scheme in _HTTP_SCHEMES or (
            not scheme and ref.source_system.lower() in _HTTP_SCHEMES
        )

    def resolve(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        try:
            window = _normalize_byte_range(byte_range)
        except ValueError as exc:
            return _unresolved(ref, detail=str(exc))

        if not is_verifiable_hash(ref.content_hash):
            algorithm, _ = parse_content_hash(ref.content_hash)
            return _unresolved(
                ref,
                detail=f"Cannot verify content hash with algorithm '{algorithm}'.",
            )

        algorithm, _ = parse_content_hash(ref.content_hash)
        deadline = OutboundHttpDeadline.after(
            self._deadline_seconds,
            clock=self._clock,
        )
        current_url = ref.uri
        seen_targets: set[str] = set()
        for redirect_count in range(self._max_redirects + 1):
            try:
                deadline.check()
                target = self._policy.authorize(current_url, deadline=deadline)
                deadline.check()
            except (OutboundHttpPolicyError, OutboundHttpTransportError):
                return _unresolved(
                    ref, detail="HTTP artifact fetch failed or was denied."
                )
            if target.absolute_url in seen_targets:
                return _unresolved(ref, detail="HTTP redirect loop was refused.")
            seen_targets.add(target.absolute_url)

            try:
                deadline.check()
                with self._client.open("GET", target, deadline=deadline) as response:
                    deadline.check()
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.get_header("location")
                        deadline.check()
                        if not location or redirect_count >= self._max_redirects:
                            return _unresolved(
                                ref, detail="HTTP redirect limit was exceeded."
                            )
                        try:
                            next_url = urljoin(target.absolute_url, location)
                            next_scheme = urlsplit(next_url).scheme.lower()
                        except ValueError:
                            return _unresolved(
                                ref,
                                detail="HTTP artifact fetch failed or was denied.",
                            )
                        if target.scheme == "https" and next_scheme == "http":
                            return _unresolved(
                                ref,
                                detail="HTTP artifact fetch failed or was denied.",
                            )
                        current_url = next_url
                        deadline.check()
                        continue
                    if not 200 <= response.status_code < 300:
                        return _unresolved(
                            ref,
                            detail=(
                                f"HTTP {response.status_code} fetching artifact."
                            ),
                        )
                    declared_size = _http_content_length(
                        response.get_header("content-length")
                    )
                    if (
                        declared_size is not None
                        and declared_size > self._max_fetch_bytes
                    ):
                        return _unresolved(
                            ref,
                            detail=str(_FetchTooLarge(self._max_fetch_bytes)),
                        )
                    content, total, truncated, observed = _hash_and_collect(
                        response.iter_bytes(),
                        algorithm=algorithm,
                        max_bytes=max_bytes,
                        window=window,
                        max_total=self._max_fetch_bytes,
                        budget_check=deadline.check,
                    )
                    deadline.check()
                    content_type = response.get_header("content-type")
                    deadline.check()
            except _FetchTooLarge as exc:
                return _unresolved(ref, detail=str(exc))
            except OutboundHttpTransportError:
                return _unresolved(
                    ref, detail="HTTP artifact fetch failed or was denied."
                )

            if content_type:
                content_type = content_type.split(";", 1)[0].strip()
            try:
                deadline.check()
                resolved = _build_resolved(
                    ref,
                    observed=observed,
                    content=content,
                    total=total,
                    truncated=truncated,
                    content_type=content_type or "application/octet-stream",
                )
                deadline.check()
                return resolved
            except OutboundHttpTransportError:
                return _unresolved(
                    ref, detail="HTTP artifact fetch failed or was denied."
                )
        return _unresolved(ref, detail="HTTP redirect limit was exceeded.")


@dataclass(frozen=True)
class RcloneCompleted:
    """Result of one rclone invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes


# A runner takes rclone argv (without the binary name) and returns its result.
RcloneRunner = Callable[[list[str]], RcloneCompleted]


class _LegacyProcessOutputExceeded(Exception):
    """A trusted injected test runner returned more data than its declared cap."""


def _validate_subprocess_deadline_seconds(value: float) -> float:
    if (
        not math.isfinite(value)
        or value <= 0
        or value > MAX_SUBPROCESS_DEADLINE_SECONDS
    ):
        raise ValueError(
            "Subprocess deadline must be finite and greater than 0, and no greater "
            f"than {MAX_SUBPROCESS_DEADLINE_SECONDS:g} seconds."
        )
    return value


def _subprocess_rclone_runner(binary: str) -> RcloneRunner:
    def run(args: list[str]) -> RcloneCompleted:
        import subprocess

        completed = subprocess.run(  # noqa: S603 - args are built, not shell
            [binary, *args],
            capture_output=True,
            check=False,
        )
        return RcloneCompleted(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return run


class RcloneResolver(ArtifactResolver):
    """Resolves artifacts via ``rclone``, the unifier for cloud and remote stores.

    One adapter covers S3, SFTP, Dropbox, Google Drive, Box, OneDrive and the
    rest of rclone's backends; credentials live host-side in rclone's own config,
    never in Lab Tracker. References are addressed as ``rclone://<remote>/<path>``
    (mapped to rclone's ``remote:path``). A ``runner`` may be injected for tests;
    otherwise rclone is invoked as a subprocess.

    The whole object is fetched (``rclone cat``) to certify its hash, so its size
    is checked first (``rclone size``) and an object larger than
    ``max_fetch_bytes`` is refused as UNRESOLVED rather than downloaded.

    ``allowed_remotes`` — ``None`` means unrestricted (library default); a list
    restricts resolution to those rclone remote *names*, and anything else
    resolves UNRESOLVED before any rclone subprocess runs. Without it, a
    reference can drive server-side ``rclone cat`` against ANY remote in the
    host's rclone config. ``registry_from_env`` denies all remotes unless
    ``LAB_TRACKER_RCLONE_ALLOWED_REMOTES`` is set — the same opt-in posture as
    the local resolver's allowed roots and the git resolver's remote allowlist.
    """

    def __init__(
        self,
        *,
        runner: RcloneRunner | None = None,
        executor: ProcessExecutor | None = None,
        binary: str = "rclone",
        allowed_remotes: Sequence[str] | None = None,
        max_fetch_bytes: int = DEFAULT_MAX_FETCH_BYTES,
        deadline_seconds: float = DEFAULT_SUBPROCESS_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if runner is not None and executor is not None:
            raise ValueError("Configure either runner or executor, not both.")
        self._runner = runner
        self._executor = executor or BoundedSubprocessExecutor()
        self._binary = binary
        self._allowed_remotes = (
            None if allowed_remotes is None else list(allowed_remotes)
        )
        self._max_fetch_bytes = max_fetch_bytes
        self._deadline_seconds = _validate_subprocess_deadline_seconds(
            deadline_seconds
        )
        self._clock = clock

    def can_resolve(self, ref: ExternalArtifactReference) -> bool:
        return _uri_scheme(ref.uri) == "rclone"

    def _remote_allowed(self, remote: str) -> bool:
        if self._allowed_remotes is None:
            return True
        return remote in self._allowed_remotes

    def resolve(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        try:
            window = _normalize_byte_range(byte_range)
        except ValueError as exc:
            return _unresolved(ref, detail=str(exc))

        if not is_verifiable_hash(ref.content_hash):
            algorithm, _ = parse_content_hash(ref.content_hash)
            return _unresolved(
                ref,
                detail=f"Cannot verify content hash with algorithm '{algorithm}'.",
            )

        target = self._rclone_target(ref.uri)
        if target is None:
            return _unresolved(ref, detail="Reference URI is not an rclone locator.")
        remote_name = target.partition(":")[0]
        if not self._remote_allowed(remote_name):
            return _unresolved(
                ref, detail="Remote is not in the rclone resolver allowlist."
            )

        algorithm, _ = parse_content_hash(ref.content_hash)
        deadline = ProcessDeadline.after(
            self._deadline_seconds,
            clock=self._clock,
        )
        collector = _HashCollector(
            algorithm=algorithm,
            max_bytes=max_bytes,
            window=window,
            max_total=self._max_fetch_bytes,
            budget_check=deadline.check,
        )
        try:
            size = self._object_size(target, deadline=deadline)
            if size is None:
                return _unresolved(ref, detail="rclone artifact resolution failed.")
            if size > self._max_fetch_bytes:
                return _unresolved(
                    ref,
                    detail=(
                        f"Artifact ({size} bytes) exceeds the "
                        f"{self._max_fetch_bytes}-byte fetch limit."
                    ),
                )
            cat = self._run_command(
                ["cat", target],
                deadline=deadline,
                stdout_limit_bytes=self._max_fetch_bytes,
                stdout_consumer=collector.consume,
            )
            if cat.returncode != 0:
                return _unresolved(
                    ref, detail="rclone artifact resolution failed."
                )
            content, total, truncated, observed = collector.finish()
            deadline.check()
        except (
            _FetchTooLarge,
            _LegacyProcessOutputExceeded,
            OSError,
            ProcessExecutionError,
            ValueError,
        ):
            return _unresolved(ref, detail="rclone artifact resolution failed.")

        try:
            content_type, _ = mimetypes.guess_type(target)
            resolved = _build_resolved(
                ref,
                observed=observed,
                content=content,
                total=total,
                truncated=truncated,
                content_type=content_type or "application/octet-stream",
            )
            deadline.check()
            return resolved
        except ProcessExecutionError:
            return _unresolved(ref, detail="rclone artifact resolution failed.")

    def _object_size(
        self,
        target: str,
        *,
        deadline: ProcessDeadline,
    ) -> int | None:
        completed = self._run_command(
            ["size", "--json", target],
            deadline=deadline,
            stdout_limit_bytes=_PROCESS_METADATA_LIMIT_BYTES,
        )
        if completed.returncode != 0:
            return None
        try:
            payload = json.loads(completed.stdout.decode("utf-8") or "{}")
        except (RecursionError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        size = payload.get("bytes")
        return size if type(size) is int and size >= 0 else None

    def _run_command(
        self,
        args: list[str],
        *,
        deadline: ProcessDeadline,
        stdout_limit_bytes: int,
        stdout_consumer: Callable[[bytes], None] | None = None,
    ) -> RcloneCompleted:
        if self._runner is not None:
            # This compatibility seam is for trusted, synchronous test runners.
            # Production always uses the preemptible bounded executor.
            deadline.check()
            completed = self._runner(args)
            deadline.check()
            if (
                len(completed.stdout) > stdout_limit_bytes
                or len(completed.stderr) > _PROCESS_STDERR_LIMIT_BYTES
            ):
                raise _LegacyProcessOutputExceeded
            stdout = completed.stdout
            if stdout_consumer is not None:
                stdout_consumer(stdout)
                deadline.check()
                stdout = b""
            return RcloneCompleted(completed.returncode, stdout, completed.stderr)

        completed = self._executor.run(
            [self._binary, *args],
            deadline=deadline,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=_PROCESS_STDERR_LIMIT_BYTES,
            stdout_consumer=stdout_consumer,
        )
        return RcloneCompleted(
            completed.returncode,
            completed.stdout,
            b"",
        )

    def _rclone_target(self, uri: str) -> str | None:
        try:
            parsed = urlsplit(uri)
        except ValueError:
            return None
        if parsed.scheme.lower() != "rclone" or not parsed.netloc:
            return None
        path = unquote(parsed.path).lstrip("/")
        if _has_control_characters(parsed.netloc) or _has_control_characters(path):
            return None
        return f"{parsed.netloc}:{path}"


def _rclone_error_detail(completed: RcloneCompleted) -> str:
    """Legacy store-health diagnostic; resolver subprocesses use generic errors."""

    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if stderr:
        return f"rclone failed: {stderr.splitlines()[-1]}"
    return f"rclone exited with status {completed.returncode}."


@dataclass(frozen=True)
class GitCompleted:
    """Result of one git invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes


# A runner takes git argv (without the binary name) and returns its result.
GitRunner = Callable[[list[str]], GitCompleted]


def _git_environment(
    allow_protocol: str | None,
    *,
    cwd: str,
) -> dict[str, str]:
    """Capture one non-interactive Git environment for a logical operation."""

    env = dict(os.environ)
    for variable in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        env.pop(variable, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    # The operation cwd may sit below an unrelated repository (notably under a
    # shared /tmp). Let Git discover the cache repository at ``cwd``, but never
    # walk into its parent and inherit repository-local URL rewrites or hooks.
    env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(os.path.realpath(cwd))
    if allow_protocol is None:
        env.pop("GIT_ALLOW_PROTOCOL", None)
    else:
        env["GIT_ALLOW_PROTOCOL"] = allow_protocol
    return env


def _git_http_config_args(approved: ApprovedGitRemote) -> list[str]:
    """Disable generic and approved-URL redirects before every Git subcommand."""

    args = ["-c", _GIT_GENERIC_HTTP_REDIRECT_CONFIG]
    if approved.scheme == "https":
        args.extend(
            [
                "-c",
                (
                    f"http.{approved.subprocess_value}."
                    "followRedirects=false"
                ),
            ]
        )
    return args


def _run_git_command(
    *,
    runner: GitRunner | None,
    executor: ProcessExecutor,
    binary: str,
    args: list[str],
    cwd: str,
    env: dict[str, str],
    config_args: Sequence[str],
    deadline: ProcessDeadline,
    stdout_limit_bytes: int,
    stdout_consumer: Callable[[bytes], None] | None = None,
) -> GitCompleted:
    """Run one Git command through the trusted runner or bounded production seam."""

    if runner is not None:
        # The callable seam is retained for deterministic tests and trusted callers.
        # Encoding the cwd in argv keeps its command interpretation identical.
        deadline.check()
        completed = runner([*config_args, "-C", cwd, *args])
        deadline.check()
        if (
            len(completed.stdout) > stdout_limit_bytes
            or len(completed.stderr) > _PROCESS_STDERR_LIMIT_BYTES
        ):
            raise _LegacyProcessOutputExceeded
        stdout = completed.stdout
        if stdout_consumer is not None:
            stdout_consumer(stdout)
            deadline.check()
            stdout = b""
        return GitCompleted(completed.returncode, stdout, completed.stderr)

    completed = executor.run(
        [binary, *config_args, *args],
        cwd=cwd,
        deadline=deadline,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=_PROCESS_STDERR_LIMIT_BYTES,
        stdout_consumer=stdout_consumer,
        env=env,
    )
    return GitCompleted(
        completed.returncode,
        completed.stdout,
        b"",
    )


def _git_remote_preflight_matches(
    completed: GitCompleted,
    canonical_remote: str,
) -> bool:
    """Require one exact, terminal UTF-8 line from ``ls-remote --get-url``."""

    if completed.returncode != 0:
        return False
    try:
        output = completed.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if output.endswith("\r\n"):
        line = output[:-2]
    elif output.endswith("\n"):
        line = output[:-1]
    else:
        return False
    return line == canonical_remote


class _GitReadError(Exception):
    """A git command failed while reading a blob; carries a caller-facing detail."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _parse_git_locator(uri: str) -> tuple[str, str, str] | None:
    """Split a ``git+<remote>#<commit>:<path>`` locator into its parts.

    Returns ``(remote, commit, path)`` or ``None`` when the URI is not a
    well-formed git locator. The remote may itself contain schemes, ``@``, and
    ``:`` (e.g. ``git@host:org/repo.git``); the commit and path live in the
    fragment after ``#`` so the remote is never ambiguous.
    """

    if not uri.startswith("git+"):
        return None
    remote, sep, fragment = uri[len("git+") :].partition("#")
    if not sep:
        return None
    commit, sep2, path = fragment.partition(":")
    if not sep2:
        return None
    if not remote or not commit or not path:
        return None
    if any(_has_control_characters(part) for part in (remote, commit, path)):
        return None
    # Guard against argument injection: a leading '-' could be read by git as an
    # option (e.g. remote='--upload-pack=<cmd>', which executes a command).
    if any(part.startswith("-") for part in (remote, commit, path)):
        return None
    return remote, commit, path


class GitResolver(ArtifactResolver):
    """Resolves artifacts pinned to a git commit.

    References are addressed as ``git+<remote>#<commit>:<path>`` (source_system
    ``git``). Credentials live host-side in git's own config/credential helper,
    never in Lab Tracker. The blob for ``<commit>:<path>`` is fetched into a
    per-remote cache and read with ``git cat-file``, then hashed to verify
    against the reference's ``content_hash``. A ``runner`` may be injected for
    tests; otherwise git is invoked as a subprocess and the resolver degrades to
    UNRESOLVED when the binary is absent.

    Security controls:

    * ``remote_policy`` structurally authorizes and canonically reconstructs a
      remote before cache or process work. The default policy denies everything.
    * ``allow_protocol`` — passed to git as ``GIT_ALLOW_PROTOCOL`` so ``file://``
      and ``ext::`` command-execution vectors are refused.
    * A bounded ``ls-remote --get-url`` preflight must return the exact canonical
      remote before the network-capable fetch runs, preventing host Git URL
      rewrite configuration from escaping the operator policy.
    * HTTP redirects are disabled generically and for the exact approved HTTPS
      URL on every Git invocation.
    * The blob's size is checked (``git cat-file -s``) before it is read, so an
      object larger than ``max_fetch_bytes`` is refused rather than buffered.
    * ``max_cache_bytes`` bounds the on-disk fetch cache; least-recently-used
      per-remote caches are evicted before a new fetch.
    """

    def __init__(
        self,
        *,
        runner: GitRunner | None = None,
        executor: ProcessExecutor | None = None,
        binary: str = "git",
        cache_root: str | Path | None = None,
        remote_policy: GitRemotePolicy | None = None,
        allow_protocol: str | None = DEFAULT_GIT_ALLOW_PROTOCOL,
        max_fetch_bytes: int = DEFAULT_MAX_FETCH_BYTES,
        max_cache_bytes: int | None = None,
        deadline_seconds: float = DEFAULT_SUBPROCESS_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if runner is not None and executor is not None:
            raise ValueError("Configure either runner or executor, not both.")
        self._runner = runner
        self._executor = executor
        self._binary = binary
        self._allow_protocol = allow_protocol
        self._cache_root = cache_root
        self._remote_policy = (
            remote_policy if remote_policy is not None else GitRemotePolicy.deny_all()
        )
        self._max_fetch_bytes = max_fetch_bytes
        self._max_cache_bytes = max_cache_bytes
        self._deadline_seconds = _validate_subprocess_deadline_seconds(
            deadline_seconds
        )
        self._clock = clock

    def can_resolve(self, ref: ExternalArtifactReference) -> bool:
        if ref.source_system.strip().lower() == "git":
            return True
        return ref.uri.startswith("git+")

    def resolve(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        try:
            window = _normalize_byte_range(byte_range)
        except ValueError as exc:
            return _unresolved(ref, detail=str(exc))

        if not is_verifiable_hash(ref.content_hash):
            algorithm, _ = parse_content_hash(ref.content_hash)
            return _unresolved(
                ref,
                detail=f"Cannot verify content hash with algorithm '{algorithm}'.",
            )

        locator = _parse_git_locator(ref.uri)
        if locator is None:
            return _unresolved(ref, detail="Reference URI is not a git locator.")
        remote, commit, path = locator
        approved = self._remote_policy.authorize(remote)
        if approved is None:
            return _unresolved(
                ref, detail="Remote is not in the git resolver allowlist."
            )

        algorithm, _ = parse_content_hash(ref.content_hash)
        try:
            deadline = ProcessDeadline.after(
                self._deadline_seconds,
                clock=self._clock,
            )
            executor = (
                self._executor
                if self._executor is not None
                else BoundedSubprocessExecutor()
            )
            deadline.check()
            cache = self._repo_cache(approved.subprocess_value)
            deadline.check()
            env = _git_environment(self._allow_protocol, cwd=cache)
            collector = _HashCollector(
                algorithm=algorithm,
                max_bytes=max_bytes,
                window=window,
                max_total=self._max_fetch_bytes,
                budget_check=deadline.check,
            )
            self._read_blob(
                approved,
                commit,
                path,
                cache=cache,
                executor=executor,
                env=env,
                deadline=deadline,
                stdout_consumer=collector.consume,
            )
            content, total, truncated, observed = collector.finish()
            deadline.check()
        except _GitReadError as exc:
            return _unresolved(ref, detail=exc.detail)
        except Exception:
            return _unresolved(ref, detail="Git artifact resolution failed.")

        try:
            content_type, _ = mimetypes.guess_type(path)
            resolved = _build_resolved(
                ref,
                observed=observed,
                content=content,
                total=total,
                truncated=truncated,
                content_type=content_type or "application/octet-stream",
            )
            deadline.check()
            return resolved
        except ProcessExecutionError:
            return _unresolved(ref, detail="Git artifact resolution failed.")

    def _read_blob(
        self,
        approved: ApprovedGitRemote,
        commit: str,
        path: str,
        *,
        cache: str,
        executor: ProcessExecutor,
        env: dict[str, str],
        deadline: ProcessDeadline,
        stdout_consumer: Callable[[bytes], None],
    ) -> None:
        remote = approved.subprocess_value
        deadline.check()
        rev = f"{commit}:{path}"
        config_args = _git_http_config_args(approved)
        # Idempotent: a fresh cache is initialised once, an existing one reused.
        initialized = self._run_command(
            ["init", "-q"],
            executor=executor,
            cwd=cache,
            env=env,
            config_args=config_args,
            deadline=deadline,
            stdout_limit_bytes=_PROCESS_METADATA_LIMIT_BYTES,
        )
        if initialized.returncode != 0:
            raise _GitReadError("Git artifact resolution failed.")
        preflight = self._run_command(
            ["ls-remote", "--get-url", "--", remote],
            executor=executor,
            cwd=cache,
            env=env,
            config_args=config_args,
            deadline=deadline,
            stdout_limit_bytes=_PROCESS_METADATA_LIMIT_BYTES,
        )
        if not _git_remote_preflight_matches(preflight, remote):
            raise _GitReadError("Git artifact resolution failed.")
        fetch = self._run_command(
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "--depth",
                "1",
                "--",
                remote,
                commit,
            ],
            executor=executor,
            cwd=cache,
            env=env,
            config_args=config_args,
            deadline=deadline,
            stdout_limit_bytes=_PROCESS_METADATA_LIMIT_BYTES,
        )
        if fetch.returncode != 0:
            raise _GitReadError("Git artifact resolution failed.")
        # Refuse an oversized object before reading it into memory.
        size_out = self._run_command(
            ["cat-file", "-s", rev],
            executor=executor,
            cwd=cache,
            env=env,
            config_args=config_args,
            deadline=deadline,
            stdout_limit_bytes=_PROCESS_METADATA_LIMIT_BYTES,
        )
        if size_out.returncode != 0:
            raise _GitReadError("Git artifact resolution failed.")
        try:
            size_text = size_out.stdout.decode("ascii").strip()
        except UnicodeDecodeError:
            raise _GitReadError("Git artifact resolution failed.") from None
        if not size_text.isdigit():
            raise _GitReadError("Git artifact resolution failed.")
        try:
            size = int(size_text)
        except ValueError:
            raise _GitReadError("Git artifact resolution failed.") from None
        if size > self._max_fetch_bytes:
            raise _GitReadError(
                f"Artifact ({size} bytes) exceeds the "
                f"{self._max_fetch_bytes}-byte fetch limit."
            )
        cat = self._run_command(
            ["cat-file", "blob", rev],
            executor=executor,
            cwd=cache,
            env=env,
            config_args=config_args,
            deadline=deadline,
            stdout_limit_bytes=self._max_fetch_bytes,
            stdout_consumer=stdout_consumer,
        )
        if cat.returncode != 0:
            raise _GitReadError("Git artifact resolution failed.")
        deadline.check()

    def _run_command(
        self,
        args: list[str],
        *,
        executor: ProcessExecutor,
        cwd: str,
        env: dict[str, str],
        config_args: Sequence[str],
        deadline: ProcessDeadline,
        stdout_limit_bytes: int,
        stdout_consumer: Callable[[bytes], None] | None = None,
    ) -> GitCompleted:
        return _run_git_command(
            runner=self._runner,
            executor=executor,
            binary=self._binary,
            args=args,
            cwd=cwd,
            env=env,
            config_args=config_args,
            deadline=deadline,
            stdout_limit_bytes=stdout_limit_bytes,
            stdout_consumer=stdout_consumer,
        )

    def _repo_cache(self, remote: str) -> str:
        base = os.fspath(self._cache_root) if self._cache_root else os.path.join(
            tempfile.gettempdir(), "lab-tracker-git-cache"
        )
        self._enforce_cache_quota(base)
        digest = hashlib.sha256(remote.encode("utf-8")).hexdigest()[:16]
        cache = os.path.join(base, digest)
        os.makedirs(cache, exist_ok=True)
        return cache

    def _enforce_cache_quota(self, base: str) -> None:
        """Evict least-recently-used per-remote caches until under the quota."""

        if not self._max_cache_bytes or not os.path.isdir(base):
            return
        entries: list[tuple[float, str, int]] = []
        total = 0
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if not os.path.isdir(path):
                continue
            size = _dir_size(path)
            entries.append((os.path.getmtime(path), path, size))
            total += size
        if total <= self._max_cache_bytes:
            return
        for _mtime, path, size in sorted(entries):  # oldest first
            if total <= self._max_cache_bytes:
                break
            shutil.rmtree(path, ignore_errors=True)
            total -= size


def _dir_size(path: str) -> int:
    """Best-effort total size of the files under ``path`` (for cache quota)."""

    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            with suppress(OSError):
                total += os.path.getsize(os.path.join(dirpath, name))
    return total


class _FetchTooLarge(Exception):
    """Raised when a streamed artifact exceeds the verification fetch cap."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"Artifact exceeds the {limit}-byte fetch limit.")
        self.limit = limit


class _CumulativeByteBudget:
    """Debit a shared logical byte allowance as candidate streams are read."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._consumed = 0

    @property
    def remaining(self) -> int:
        return self._limit - self._consumed

    def debit(self, size: int) -> None:
        next_consumed = self._consumed + size
        if next_consumed > self._limit:
            raise _FetchTooLarge(self._limit)
        self._consumed = next_consumed


def _http_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise OutboundHttpTransportError(
            "Outbound HTTP request failed."
        ) from None
    if parsed < 0:
        raise OutboundHttpTransportError("Outbound HTTP request failed.")
    return parsed


def _hash_and_collect(
    chunks: Iterable[bytes],
    *,
    algorithm: str,
    max_bytes: int,
    window: tuple[int, int] | None,
    max_total: int | None = None,
    budget_check: Callable[[], None] | None = None,
) -> tuple[bytes, int, bool, str]:
    """Stream ``chunks`` through a hasher, collecting the bounded returned bytes.

    Returns ``(content, total_size, truncated, observed_hash)``. The whole stream
    is hashed; ``content`` is either the first ``max_bytes`` (no window) or the
    requested ``[start, end)`` slice. Raises :class:`_FetchTooLarge` if the total
    exceeds ``max_total`` (so an oversized artifact is refused rather than
    returned uncertified).
    """

    collector = _HashCollector(
        algorithm=algorithm,
        max_bytes=max_bytes,
        window=window,
        max_total=max_total,
        budget_check=budget_check,
    )
    for chunk in chunks:
        collector.consume(chunk)
    return collector.finish()


class _HashCollector:
    """Incrementally hash a stream while retaining only the requested window."""

    def __init__(
        self,
        *,
        algorithm: str,
        max_bytes: int,
        window: tuple[int, int] | None,
        max_total: int | None = None,
        budget_check: Callable[[], None] | None = None,
    ) -> None:
        self._algorithm = algorithm
        self._hasher = hashlib.new(algorithm)
        self._collected = bytearray()
        self._total = 0
        self._max_bytes = max_bytes
        self._window = window
        self._max_total = max_total
        self._budget_check = budget_check

    def consume(self, chunk: bytes) -> None:
        """Consume one stream chunk, failing before accepting bytes over the cap."""

        self._check_budget()
        if not chunk:
            return
        next_total = self._total + len(chunk)
        if self._max_total is not None and next_total > self._max_total:
            raise _FetchTooLarge(self._max_total)

        chunk_start = self._total
        self._hasher.update(chunk)
        self._total = next_total
        if self._window is None:
            if len(self._collected) < self._max_bytes:
                self._collected.extend(
                    chunk[: self._max_bytes - len(self._collected)]
                )
        else:
            start, end = self._window
            lo = max(start, chunk_start)
            hi = min(end, self._total)
            if hi > lo:
                self._collected.extend(chunk[lo - chunk_start : hi - chunk_start])
        self._check_budget()

    def finish(self) -> tuple[bytes, int, bool, str]:
        """Return the collected view and digest after the complete stream."""

        self._check_budget()
        observed = f"{self._algorithm}:{self._hasher.hexdigest()}"
        self._check_budget()
        content = bytes(self._collected)
        return content, self._total, len(content) < self._total, observed

    def _check_budget(self) -> None:
        if self._budget_check is not None:
            self._budget_check()


def _read_stream_chunks(
    handle: BinaryIO,
    *,
    byte_budget: _CumulativeByteBudget | None = None,
) -> Iterable[bytes]:
    while True:
        chunk = handle.read(_STREAM_CHUNK_SIZE)
        if not chunk:
            break
        if byte_budget is not None:
            byte_budget.debit(len(chunk))
        yield chunk


def _build_resolved(
    ref: ExternalArtifactReference,
    *,
    observed: str,
    content: bytes,
    total: int,
    truncated: bool,
    content_type: str | None,
) -> ResolvedArtifact:
    """Verify a recomputed digest against the reference and build the result."""

    _, expected_digest = parse_content_hash(ref.content_hash)
    verified = parse_content_hash(observed)[1] == expected_digest
    return ResolvedArtifact(
        status=ResolutionStatus.VERIFIED if verified else ResolutionStatus.DRIFTED,
        source_system=ref.source_system,
        uri=ref.uri,
        expected_hash=ref.content_hash,
        observed_hash=observed,
        content_type=content_type or "application/octet-stream",
        size_bytes=total,
        content=content if verified else None,
        truncated=truncated,
        fetched_at=_now(),
        detail=None if verified else "Recomputed hash does not match content_hash.",
    )


def default_registry(
    *,
    allowed_roots: Sequence[str | Path] | None = None,
    recovery: RecoveryPolicy | None = None,
    http_policy: OutboundHttpPolicy | None = None,
    http_client: OutboundHttpClient | None = None,
    http_deadline_seconds: float = DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS,
    subprocess_deadline_seconds: float = DEFAULT_SUBPROCESS_DEADLINE_SECONDS,
    rclone_allowed_remotes: Sequence[str] | None = None,
    git_remote_policy: GitRemotePolicy | None = None,
    git_cache_root: str | Path | None = None,
    git_max_cache_bytes: int | None = None,
) -> ResolverRegistry:
    """Build a registry with the adapters available in this slice.

    Local-filesystem, HTTP(S), rclone, and git resolvers. The rclone and git
    adapters degrade to UNRESOLVED when their binary is absent, so including them
    is safe by default. Native store-backed adapters (s3, ssh, database) register
    here as they land. ``recovery`` opts the local resolver into content-hash
    recovery of missing files within ``allowed_roots``. ``http_policy`` is the
    shared outbound destination policy later reused by store-health probes.
    ``subprocess_deadline_seconds`` is one shared execution/verification budget
    for every command in a single rclone or Git resolution.
    ``git_remote_policy`` is one immutable structural policy shared with health
    composition; omission denies every Git remote.
    """

    return ResolverRegistry(
        [
            LocalFilesystemResolver(allowed_roots=allowed_roots, recovery=recovery),
            HttpResolver(
                policy=http_policy,
                client=http_client,
                deadline_seconds=http_deadline_seconds,
            ),
            RcloneResolver(
                allowed_remotes=rclone_allowed_remotes,
                deadline_seconds=subprocess_deadline_seconds,
            ),
            GitResolver(
                remote_policy=git_remote_policy,
                cache_root=git_cache_root,
                max_cache_bytes=git_max_cache_bytes,
                deadline_seconds=subprocess_deadline_seconds,
            ),
        ]
    )


# os.pathsep-separated list of directories the local resolver may read. When
# unset, the local resolver is restricted to *no* roots, so filesystem artifacts
# resolve as UNRESOLVED until an operator opts specific roots in. HTTP(S) uses
# the separate public-destination policy and conjunctive internal exceptions.
LAB_TRACKER_RESOLVER_ALLOWED_ROOTS_ENV = "LAB_TRACKER_RESOLVER_ALLOWED_ROOTS"

# Opt-in flag for content-hash recovery of missing local artifacts (off unless
# truthy). Recovery still only runs when allowed roots are configured. The two
# budget vars override the RecoveryPolicy defaults when set to a positive int.
LAB_TRACKER_RESOLVER_RECOVERY_ENV = "LAB_TRACKER_RESOLVER_RECOVERY"
LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES_ENV = "LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES"
LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES_ENV = "LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES"

# Comma-separated exact HTTP(S) origins and IP networks for internal artifact
# destinations. Both variables are required for an exception: the normalized
# scheme/host/effective-port origin must match exactly and every DNS answer must
# fall inside one configured CIDR. Without an exception, every answer must be a
# globally routable public address.
LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES_ENV = (
    "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES"
)
LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS_ENV = (
    "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS"
)
LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS_ENV = (
    "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS"
)
LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS_ENV = (
    "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS"
)

# Comma-separated allowlist of rclone remote NAMES the rclone resolver may read.
# When unset, registry_from_env denies all rclone remotes (references resolve
# UNRESOLVED) until an operator opts specific remotes in — without it, a
# reference could drive server-side `rclone cat` against any remote in the
# host's rclone config.
LAB_TRACKER_RCLONE_ALLOWED_REMOTES_ENV = "LAB_TRACKER_RCLONE_ALLOWED_REMOTES"

# Comma-separated structural Git grants. Each grant is parsed and canonicalized;
# candidates are authorized by scheme, host, effective port, optional SSH user,
# and repository path boundary. When unset, registry_from_env denies all remotes.
LAB_TRACKER_GIT_ALLOWED_REMOTES_ENV = "LAB_TRACKER_GIT_ALLOWED_REMOTES"
LAB_TRACKER_GIT_CACHE_ROOT_ENV = "LAB_TRACKER_GIT_CACHE_ROOT"
LAB_TRACKER_GIT_CACHE_MAX_BYTES_ENV = "LAB_TRACKER_GIT_CACHE_MAX_BYTES"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_flag(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY


def _env_positive_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def recovery_from_env() -> RecoveryPolicy:
    """Build a :class:`RecoveryPolicy` from the resolver recovery env vars."""

    return RecoveryPolicy(
        enabled=_env_flag(os.environ.get(LAB_TRACKER_RESOLVER_RECOVERY_ENV)),
        max_files=_env_positive_int(
            os.environ.get(LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES_ENV),
            DEFAULT_RECOVERY_MAX_FILES,
        ),
        max_bytes=_env_positive_int(
            os.environ.get(LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES_ENV),
            DEFAULT_RECOVERY_MAX_BYTES,
        ),
    )


def outbound_http_policy_from_env() -> OutboundHttpPolicy:
    """Build and validate the outbound HTTP policy from environment variables."""

    return outbound_http_policy_from_config(
        allowed_authorities=os.environ.get(
            LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES_ENV
        ),
        allowed_networks=os.environ.get(
            LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS_ENV
        ),
    )


def outbound_http_policy_from_config(
    *,
    allowed_authorities: str | None,
    allowed_networks: str | None,
) -> OutboundHttpPolicy:
    """Build and validate a policy from the application's loaded settings."""

    authorities = _strict_comma_separated_values(
        allowed_authorities,
        variable=LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES_ENV,
    )
    networks = _strict_comma_separated_values(
        allowed_networks,
        variable=LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS_ENV,
    )
    return OutboundHttpPolicy(
        allowed_authorities=authorities,
        allowed_networks=networks,
    )


def _strict_comma_separated_values(
    raw: str | None, *, variable: str
) -> list[str]:
    if raw is None or not raw.strip():
        return []
    values = [part.strip() for part in raw.split(",")]
    if any(not value for value in values):
        raise ValueError(f"{variable} contains an empty entry.")
    return values


def registry_from_env(
    *,
    http_policy: OutboundHttpPolicy | None = None,
    http_deadline_seconds: float | None = None,
    subprocess_deadline_seconds: float | None = None,
    git_remote_policy: GitRemotePolicy | None = None,
) -> ResolverRegistry:
    """Build the default registry, reading resolver config from the env."""

    raw = os.environ.get(LAB_TRACKER_RESOLVER_ALLOWED_ROOTS_ENV)
    allowed_roots = (
        [part for part in raw.split(os.pathsep) if part.strip()] if raw else []
    )
    raw_rclone = os.environ.get(LAB_TRACKER_RCLONE_ALLOWED_REMOTES_ENV)
    # Unset -> [] -> deny all rclone remotes (opt-in, mirroring allowed roots).
    rclone_allowed_remotes = (
        [part.strip() for part in raw_rclone.split(",") if part.strip()]
        if raw_rclone
        else []
    )
    if git_remote_policy is None:
        git_remote_policy = GitRemotePolicy.from_config(
            os.environ.get(LAB_TRACKER_GIT_ALLOWED_REMOTES_ENV),
            variable=LAB_TRACKER_GIT_ALLOWED_REMOTES_ENV,
        )
    git_cache_root = os.environ.get(LAB_TRACKER_GIT_CACHE_ROOT_ENV) or None
    git_max_cache_bytes = _env_positive_int(
        os.environ.get(LAB_TRACKER_GIT_CACHE_MAX_BYTES_ENV), 0
    )
    if http_deadline_seconds is None:
        raw_deadline = os.environ.get(
            LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS_ENV
        )
        try:
            http_deadline_seconds = (
                float(raw_deadline)
                if raw_deadline is not None
                else DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS
            )
        except ValueError:
            raise ValueError(
                f"{LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS_ENV} must be "
                "finite and positive."
            ) from None
    if (
        not math.isfinite(http_deadline_seconds)
        or http_deadline_seconds <= 0
        or http_deadline_seconds > MAX_OUTBOUND_HTTP_DEADLINE_SECONDS
    ):
        raise ValueError(
            f"{LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS_ENV} must be finite "
            f"and between 0 and {MAX_OUTBOUND_HTTP_DEADLINE_SECONDS:g} seconds."
        )
    if subprocess_deadline_seconds is None:
        raw_subprocess_deadline = os.environ.get(
            LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS_ENV
        )
        try:
            subprocess_deadline_seconds = (
                float(raw_subprocess_deadline)
                if raw_subprocess_deadline is not None
                else DEFAULT_SUBPROCESS_DEADLINE_SECONDS
            )
        except ValueError:
            raise ValueError(
                f"{LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS_ENV} must be "
                "finite and positive."
            ) from None
    try:
        subprocess_deadline_seconds = _validate_subprocess_deadline_seconds(
            subprocess_deadline_seconds
        )
    except ValueError:
        raise ValueError(
            f"{LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS_ENV} must be finite "
            f"and between 0 and {MAX_SUBPROCESS_DEADLINE_SECONDS:g} seconds."
        ) from None
    return default_registry(
        allowed_roots=allowed_roots,
        recovery=recovery_from_env(),
        http_policy=http_policy or outbound_http_policy_from_env(),
        http_deadline_seconds=http_deadline_seconds,
        subprocess_deadline_seconds=subprocess_deadline_seconds,
        rclone_allowed_remotes=rclone_allowed_remotes,
        git_remote_policy=git_remote_policy,
        git_cache_root=git_cache_root,
        git_max_cache_bytes=git_max_cache_bytes or None,
    )
