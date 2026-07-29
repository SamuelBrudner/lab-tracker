"""On-demand resolution of registered external-artifact store pointers.

An :class:`~lab_tracker.models.ExternalArtifactReference` is a *pointer* — a
``source_system`` + ``uri`` + ``content_hash`` — not bytes. The application
authorizes a canonical registered-store reference and prepares a narrow target;
this module turns that target into a bounded, integrity-checked view on demand.
Raw filesystem, URL, rclone, and Git locators remain inert metadata at the
public registry boundary. Concrete direct resolver methods are trusted internal
primitives only.

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
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TypeAlias
from urllib.parse import unquote, urlsplit

from lab_tracker.artifact_resolution_limits import (
    DEFAULT_MAX_BYTES,
    MAX_INLINE_ARTIFACT_BYTES,
    ArtifactContentBounds,
)
from lab_tracker.bounded_subprocess import (
    DEFAULT_PROCESS_DEADLINE_SECONDS,
    DEFAULT_PROCESS_STDERR_LIMIT_BYTES,
    MAX_PROCESS_DEADLINE_SECONDS,
    BoundedSubprocessExecutor,
    ProcessDeadline,
    ProcessExecutionError,
    ProcessExecutor,
)
from lab_tracker.git_process import (
    DEFAULT_GIT_ALLOW_PROTOCOL,
    GIT_GENERIC_HTTP_REDIRECT_CONFIG,
    GIT_PROCESS_METADATA_LIMIT_BYTES,
    GitCompleted,
    GitRunner,
)
from lab_tracker.git_process import (
    build_git_environment as _git_environment,
)
from lab_tracker.git_process import (
    git_http_config_args as _git_http_config_args,
)
from lab_tracker.git_process import (
    git_remote_preflight_matches as _git_remote_preflight_matches,
)
from lab_tracker.git_process import (
    run_git_command as _run_git_command,
)
from lab_tracker.git_remote_policy import (
    ApprovedGitRemote,
    GitRemoteAddress,
    GitRemotePolicy,
    parse_git_remote_address,
)
from lab_tracker.git_store_locator import (
    GitObjectFormat,
    PinnedGitPath,
    canonical_git_store_uri,
)
from lab_tracker.local_filesystem_authority import LocalFilesystemAuthority
from lab_tracker.local_filesystem_operations import (
    BoundedLocalFilesystemOperations,
)
from lab_tracker.local_filesystem_ports import (
    DirectLocalRecoveryScope,
    DirectLocalRegularFileTarget,
    EnumeratedLocalRegularFileTarget,
    LocalRecoveryCandidate,
    LocalRecoveryEnumerationOutcome,
    LocalRecoveryEnumerationResult,
    LocalRecoveryEnumerator,
    LocalRecoveryScope,
    LocalRegularFileReader,
    LocalRegularFileReadOutcome,
    LocalRegularFileReadResult,
    LocalRegularFileTarget,
    RegisteredLocalRecoveryScope,
    RegisteredLocalRegularFileTarget,
)
from lab_tracker.local_path_policy import (
    LocalPathPolicy,
    is_supported_absolute_local_root,
    native_local_path_from_uri,
)
from lab_tracker.local_resolution_budget import (
    DEFAULT_LOCAL_RECOVERY_MAX_DIRECTORIES,
    DEFAULT_LOCAL_RECOVERY_MAX_FILES,
    DEFAULT_LOCAL_RESOLUTION_MAX_READ_BYTES,
    MAX_LOCAL_RECOVERY_MAX_DIRECTORIES,
    MAX_LOCAL_RECOVERY_MAX_FILES,
    MAX_LOCAL_RESOLUTION_MAX_READ_BYTES,
    LocalResolutionBudget,
    LocalResolutionLimits,
)
from lab_tracker.local_store_locator import (
    LocalStoreLocator,
    PortableStorePath,
    canonical_local_store_uri,
    canonical_store_uri,
)
from lab_tracker.models import DataStore, ExternalArtifactReference, StoreKind
from lab_tracker.outbound_http import (
    DEFAULT_MAX_HTTP_REDIRECTS,
    DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS,
    HTTP_REDIRECT_STATUS_CODES,
    MAX_OUTBOUND_HTTP_DEADLINE_SECONDS,
    OutboundHttpClient,
    OutboundHttpDeadline,
    OutboundHttpPolicy,
    OutboundHttpPolicyError,
    OutboundHttpTransportError,
    RegisteredHttpPrefix,
    SafeHttpClient,
    resolve_direct_http_redirect,
)
from lab_tracker.rclone_remote_policy import (
    DEFAULT_RCLONE_REMOTE_POLICY_VARIABLE,
    RcloneRemotePolicy,
)
from lab_tracker.rclone_store_definition import (
    RegisteredRcloneStoreAddress,
    is_rclone_store_kind,
)
from lab_tracker.rclone_store_locator import (
    RcloneRemoteName,
    RegisteredRcloneRoot,
)
from lab_tracker.store_health import (
    GIT_STORE_HEALTH_FAILURE_DETAIL,
    HTTP_STORE_HEALTH_FAILURE_DETAIL,
    LOCAL_STORE_HEALTH_FAILURE_DETAIL,
    RCLONE_STORE_HEALTH_FAILURE_DETAIL,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)

# Hash algorithms we can recompute to verify a fetched artifact. A reference
# whose hash uses anything else (e.g. ``datalad-key:``) cannot be certified by
# recomputation and resolves as UNRESOLVED.
_VERIFIABLE_ALGORITHMS = frozenset({"sha256", "sha1", "sha224", "sha384", "sha512", "md5"})

_LOCAL_SOURCE_SYSTEMS = frozenset({"local", "local_fs", "file"})
_HTTP_SCHEMES = frozenset({"http", "https"})

# Cap on the bytes an HTTP fetch will stream while verifying. The whole body must
# be hashed to certify it, so an artifact larger than this is refused
# (UNRESOLVED) rather than returned uncertified.
DEFAULT_MAX_FETCH_BYTES = 64 * 1024 * 1024
DEFAULT_SUBPROCESS_DEADLINE_SECONDS = DEFAULT_PROCESS_DEADLINE_SECONDS
MAX_SUBPROCESS_DEADLINE_SECONDS = MAX_PROCESS_DEADLINE_SECONDS

# Command metadata is small and machine-readable. These caps are intentionally
# independent so a noisy stderr cannot consume the stdout budget (or vice versa).
_PROCESS_METADATA_LIMIT_BYTES = 64 * 1024
_PROCESS_STDERR_LIMIT_BYTES = DEFAULT_PROCESS_STDERR_LIMIT_BYTES
# Retain the established private name for callers that imported this module's
# former helper surface. Git execution itself now lives in ``git_process``.
_GIT_GENERIC_HTTP_REDIRECT_CONFIG = GIT_GENERIC_HTTP_REDIRECT_CONFIG

# Recovery search: when a local artifact is missing at its URI (moved/renamed),
# optionally scan the resolver's allowed roots for a file whose content hash
# matches the reference, and return it VERIFIED instead of UNRESOLVED. Opt-in and
# bounded — see :class:`RecoveryPolicy` and :class:`LocalFilesystemResolver`.
DEFAULT_RECOVERY_MAX_FILES = DEFAULT_LOCAL_RECOVERY_MAX_FILES
DEFAULT_RECOVERY_MAX_DIRECTORIES = DEFAULT_LOCAL_RECOVERY_MAX_DIRECTORIES
DEFAULT_RECOVERY_MAX_BYTES = DEFAULT_LOCAL_RESOLUTION_MAX_READ_BYTES


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
    allowed roots to scope the traversal. ``max_files`` caps returned unique
    candidate-file identities, ``max_directories`` caps admitted root/child
    directory attempts, and ``max_bytes`` caps accepted full-file bytes across
    the logical resolution. The integrity gate is never relaxed: a recovered
    file is still verified by recomputing its full digest before its bytes are
    returned.
    """

    enabled: bool = False
    max_files: int = DEFAULT_RECOVERY_MAX_FILES
    max_directories: int = DEFAULT_RECOVERY_MAX_DIRECTORIES
    max_bytes: int = DEFAULT_RECOVERY_MAX_BYTES

    def __post_init__(self) -> None:
        """Reject coercible values before they can weaken recovery bounds."""

        if type(self.enabled) is not bool:
            raise ValueError("Local recovery policy is invalid.")
        if (
            type(self.max_files) is not int
            or not 0 <= self.max_files <= MAX_LOCAL_RECOVERY_MAX_FILES
        ):
            raise ValueError("Local recovery policy is invalid.")
        if (
            type(self.max_directories) is not int
            or not 0 <= self.max_directories <= MAX_LOCAL_RECOVERY_MAX_DIRECTORIES
        ):
            raise ValueError("Local recovery policy is invalid.")
        if (
            type(self.max_bytes) is not int
            or not 1 <= self.max_bytes <= MAX_LOCAL_RESOLUTION_MAX_READ_BYTES
        ):
            raise ValueError("Local recovery policy is invalid.")


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

        if self.content is not None and (
            type(self.content) is not bytes or len(self.content) > MAX_INLINE_ARTIFACT_BYTES
        ):
            object.__setattr__(self, "status", ResolutionStatus.UNRESOLVED)
            object.__setattr__(self, "source_system", "artifact")
            object.__setattr__(self, "uri", "artifact://[redacted]")
            object.__setattr__(self, "expected_hash", "unavailable")
            object.__setattr__(self, "observed_hash", None)
            object.__setattr__(self, "content_type", None)
            object.__setattr__(self, "size_bytes", None)
            object.__setattr__(self, "content", None)
            object.__setattr__(self, "truncated", False)
            object.__setattr__(self, "fetched_at", _now())
            object.__setattr__(
                self,
                "detail",
                "Artifact content exceeded the inline byte limit.",
            )
            return

        if self.status is ResolutionStatus.VERIFIED:
            if self.observed_hash is None or not is_verifiable_hash(self.expected_hash):
                object.__setattr__(self, "status", ResolutionStatus.UNRESOLVED)
            elif parse_content_hash(self.observed_hash) != parse_content_hash(self.expected_hash):
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
        if (
            result.uri.lstrip().lower().startswith("git+")
            or result.source_system.strip().lower() == "git"
        ):
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


_LOCAL_STORE_TARGET_FACTORY_TOKEN = object()
_HTTP_STORE_TARGET_FACTORY_TOKEN = object()
_RCLONE_STORE_TARGET_FACTORY_TOKEN = object()
_GIT_STORE_TARGET_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class LocalStoreResolutionTarget:
    """Detached authority for resolving one logical local-store artifact.

    ``logical_reference`` is the user-facing identity retained in every result.
    ``store_root`` is the trusted, raw root read from the registered store, and
    ``locator`` has already passed the portable store-locator grammar.  No
    concrete host path is persisted in this target.
    """

    logical_reference: ExternalArtifactReference
    store_root: str
    locator: LocalStoreLocator

    def __init__(
        self,
        logical_reference: ExternalArtifactReference,
        store_root: str,
        locator: LocalStoreLocator,
        *,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _LOCAL_STORE_TARGET_FACTORY_TOKEN:
            raise TypeError("LocalStoreResolutionTarget must be built by its validated factory.")
        store_name = logical_reference.store_name
        canonical_uri = (
            canonical_local_store_uri(store_name, locator) if store_name is not None else None
        )
        if (
            not is_supported_absolute_local_root(store_root)
            or logical_reference.source_system != "store"
            or logical_reference.locator != locator.path
            or canonical_uri is None
            or logical_reference.uri != canonical_uri
        ):
            raise ValueError("Local store resolution target is inconsistent.")
        object.__setattr__(self, "logical_reference", logical_reference)
        object.__setattr__(self, "store_root", store_root)
        object.__setattr__(self, "locator", locator)


class ScopedLocalStoreResolver(ABC):
    """Narrow capability for resolving beneath a registered local-store root."""

    @abstractmethod
    def resolve_within_root(
        self,
        target: LocalStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Resolve ``target`` without broadening its registered-store authority."""


@dataclass(frozen=True, slots=True, init=False)
class HttpStoreResolutionTarget:
    """Detached authority for one artifact beneath a registered HTTP prefix.

    The concrete HTTP prefix is kept separate from the logical ``store://``
    identity. The portable locator is composed only through
    :class:`RegisteredHttpPrefix`, both here and at resolution time.
    """

    logical_reference: ExternalArtifactReference
    registered_prefix: RegisteredHttpPrefix
    locator: PortableStorePath

    def __init__(
        self,
        logical_reference: ExternalArtifactReference,
        registered_prefix: RegisteredHttpPrefix,
        locator: PortableStorePath,
        *,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _HTTP_STORE_TARGET_FACTORY_TOKEN:
            raise TypeError("HttpStoreResolutionTarget must be built by its validated factory.")
        store_name = logical_reference.store_name
        canonical_uri = canonical_store_uri(store_name, locator) if store_name is not None else None
        initial_url = registered_prefix.compose(locator)
        if (
            logical_reference.source_system != "store"
            or logical_reference.locator != locator.path
            or canonical_uri is None
            or logical_reference.uri != canonical_uri
            or initial_url is None
            or not registered_prefix.contains(initial_url)
        ):
            raise ValueError("HTTP store resolution target is inconsistent.")
        object.__setattr__(self, "logical_reference", logical_reference)
        object.__setattr__(self, "registered_prefix", registered_prefix)
        object.__setattr__(self, "locator", locator)


class ScopedHttpStoreResolver(ABC):
    """Narrow capability for resolving beneath a registered HTTP prefix."""

    @abstractmethod
    def resolve_within_http_store(
        self,
        target: HttpStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Resolve ``target`` while retaining its registered-prefix boundary."""


@dataclass(frozen=True, slots=True, init=False)
class RcloneStoreResolutionTarget:
    """Detached authority for one artifact beneath a registered rclone root."""

    logical_reference: ExternalArtifactReference
    remote: RcloneRemoteName
    registered_root: RegisteredRcloneRoot
    locator: PortableStorePath

    def __init__(
        self,
        logical_reference: ExternalArtifactReference,
        remote: RcloneRemoteName,
        registered_root: RegisteredRcloneRoot,
        locator: PortableStorePath,
        *,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _RCLONE_STORE_TARGET_FACTORY_TOKEN:
            raise TypeError("RcloneStoreResolutionTarget must be built by its validated factory.")
        store_name = logical_reference.store_name
        canonical_uri = canonical_store_uri(store_name, locator) if store_name is not None else None
        if (
            logical_reference.source_system != "store"
            or logical_reference.locator != locator.path
            or canonical_uri is None
            or logical_reference.uri != canonical_uri
            or registered_root.compose(remote, locator) is None
        ):
            raise ValueError("rclone store resolution target is inconsistent.")
        object.__setattr__(self, "logical_reference", logical_reference)
        object.__setattr__(self, "remote", remote)
        object.__setattr__(self, "registered_root", registered_root)
        object.__setattr__(self, "locator", locator)

    @property
    def argv_target(self) -> str:
        """Return the exact validated rclone positional target."""

        target = self.registered_root.compose(self.remote, self.locator)
        if target is None:  # pragma: no cover - constructor proves this invariant
            raise RuntimeError("Validated rclone target could not be recomposed.")
        return target


class ScopedRcloneStoreResolver(ABC):
    """Narrow capability for resolving beneath a registered rclone root."""

    @abstractmethod
    def resolve_within_rclone_store(
        self,
        target: RcloneStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Resolve ``target`` without reparsing its registered authority."""


@dataclass(frozen=True, slots=True, init=False)
class GitStoreResolutionTarget:
    """Detached immutable Git object beneath one registered remote."""

    logical_reference: ExternalArtifactReference
    remote: GitRemoteAddress
    pin: PinnedGitPath

    def __init__(
        self,
        logical_reference: ExternalArtifactReference,
        remote: GitRemoteAddress,
        pin: PinnedGitPath,
        *,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _GIT_STORE_TARGET_FACTORY_TOKEN:
            raise TypeError("GitStoreResolutionTarget must be built by its validated factory.")
        store_name = logical_reference.store_name
        canonical_uri = canonical_git_store_uri(store_name, pin) if store_name is not None else None
        if (
            not isinstance(remote, ApprovedGitRemote)
            or not isinstance(pin, PinnedGitPath)
            or logical_reference.source_system != "store"
            or logical_reference.locator != pin.locator
            or canonical_uri is None
            or logical_reference.uri != canonical_uri
        ):
            raise ValueError("Git store resolution target is inconsistent.")
        object.__setattr__(self, "logical_reference", logical_reference)
        object.__setattr__(self, "remote", remote)
        object.__setattr__(self, "pin", pin)


class ScopedGitStoreResolver(ABC):
    """Narrow capability for resolving one registered immutable Git object."""

    @abstractmethod
    def resolve_within_git_store(
        self,
        target: GitStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Resolve ``target`` without reconstructing a generic Git locator."""


PreparedArtifactResolutionTarget: TypeAlias = (
    LocalStoreResolutionTarget
    | HttpStoreResolutionTarget
    | RcloneStoreResolutionTarget
    | GitStoreResolutionTarget
    | ResolvedArtifact
)


@dataclass(frozen=True, slots=True)
class ArtifactResolutionIdentity:
    """Immutable primitive identity captured before untrusted resolver work."""

    source_system: str
    uri: str
    expected_hash: str
    precomputed: bool = False
    detail: str | None = None
    fetched_at: datetime | None = None


class ResolverRegistry:
    """Dispatch exact prepared store targets; reject raw references."""

    def __init__(self, resolvers: Iterable[ArtifactResolver] | None = None) -> None:
        self._resolvers: list[ArtifactResolver] = list(resolvers or [])

    def register(self, resolver: ArtifactResolver) -> None:
        self._resolvers.append(resolver)

    def resolve_prepared(
        self,
        target: PreparedArtifactResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Resolve an application-prepared target through its narrow capability."""

        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        runtime_target: object = target
        identity = snapshot_artifact_resolution_identity(runtime_target)
        if identity is None:
            return _sanitized_unresolved_result(
                "Prepared artifact resolution target is unsupported."
            )
        if type(runtime_target) is ResolvedArtifact:
            return sanitize_artifact_resolution_result(
                runtime_target,
                identity=identity,
                bounds=bounds,
            )
        if type(runtime_target) is LocalStoreResolutionTarget:
            return self.resolve_local_store(
                runtime_target,
                max_bytes=bounds.max_bytes,
                byte_range=bounds.byte_range,
            )
        if type(runtime_target) is HttpStoreResolutionTarget:
            return self.resolve_http_store(
                runtime_target,
                max_bytes=bounds.max_bytes,
                byte_range=bounds.byte_range,
            )
        if type(runtime_target) is RcloneStoreResolutionTarget:
            return self.resolve_rclone_store(
                runtime_target,
                max_bytes=bounds.max_bytes,
                byte_range=bounds.byte_range,
            )
        if type(runtime_target) is GitStoreResolutionTarget:
            return self.resolve_git_store(
                runtime_target,
                max_bytes=bounds.max_bytes,
                byte_range=bounds.byte_range,
            )
        return _sanitized_unresolved_result("Prepared artifact resolution target is unsupported.")

    def resolve(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Retain direct references as metadata without granting resolver authority.

        Application resolution is deliberately store-relative. Concrete
        adapters keep their direct methods as trusted implementation primitives,
        but the public registry never turns a persisted raw locator into host,
        network, credential, cache, or subprocess work.
        """

        ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        return _sanitized_unresolved_result(
            "Direct artifact references are metadata only.",
        )

    def resolve_local_store(
        self,
        target: LocalStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Dispatch only to a resolver that honors registered-store scoping."""

        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        identity = snapshot_artifact_resolution_identity(target)
        if identity is None:
            return _sanitized_unresolved_result(
                "Prepared artifact resolution target is unsupported."
            )
        for resolver in self._resolvers:
            if isinstance(resolver, ScopedLocalStoreResolver):
                result = resolver.resolve_within_root(
                    target,
                    max_bytes=bounds.max_bytes,
                    byte_range=bounds.byte_range,
                )
                return sanitize_artifact_resolution_result(
                    result,
                    identity=identity,
                    bounds=bounds,
                )
        return _unresolved_for_identity(
            identity,
            detail="No scoped local-store resolver is registered.",
        )

    def resolve_http_store(
        self,
        target: HttpStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Dispatch only to a resolver that honors registered HTTP scoping."""

        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        identity = snapshot_artifact_resolution_identity(target)
        if identity is None:
            return _sanitized_unresolved_result(
                "Prepared artifact resolution target is unsupported."
            )
        for resolver in self._resolvers:
            if isinstance(resolver, ScopedHttpStoreResolver):
                result = resolver.resolve_within_http_store(
                    target,
                    max_bytes=bounds.max_bytes,
                    byte_range=bounds.byte_range,
                )
                return sanitize_artifact_resolution_result(
                    result,
                    identity=identity,
                    bounds=bounds,
                )
        return _unresolved_for_identity(
            identity,
            detail="No scoped HTTP-store resolver is registered.",
        )

    def resolve_rclone_store(
        self,
        target: RcloneStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Dispatch only to a resolver that honors registered rclone scoping."""

        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        identity = snapshot_artifact_resolution_identity(target)
        if identity is None:
            return _sanitized_unresolved_result(
                "Prepared artifact resolution target is unsupported."
            )
        for resolver in self._resolvers:
            if isinstance(resolver, ScopedRcloneStoreResolver):
                result = resolver.resolve_within_rclone_store(
                    target,
                    max_bytes=bounds.max_bytes,
                    byte_range=bounds.byte_range,
                )
                return sanitize_artifact_resolution_result(
                    result,
                    identity=identity,
                    bounds=bounds,
                )
        return _unresolved_for_identity(
            identity,
            detail="No scoped rclone-store resolver is registered.",
        )

    def resolve_git_store(
        self,
        target: GitStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Dispatch only to a resolver for registered immutable Git objects."""

        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        identity = snapshot_artifact_resolution_identity(target)
        if identity is None:
            return _sanitized_unresolved_result(
                "Prepared artifact resolution target is unsupported."
            )
        for resolver in self._resolvers:
            if isinstance(resolver, ScopedGitStoreResolver):
                result = resolver.resolve_within_git_store(
                    target,
                    max_bytes=bounds.max_bytes,
                    byte_range=bounds.byte_range,
                )
                return sanitize_artifact_resolution_result(
                    result,
                    identity=identity,
                    bounds=bounds,
                )
        return _unresolved_for_identity(
            identity,
            detail="No scoped Git-store resolver is registered.",
        )


def resolver_registry_for_prepared_target(
    target: PreparedArtifactResolutionTarget,
    *,
    configured: ResolverRegistry | None,
) -> ResolverRegistry:
    """Select adapter configuration only for an exact scoped store target."""

    if not (
        type(target) is LocalStoreResolutionTarget
        or type(target) is HttpStoreResolutionTarget
        or type(target) is RcloneStoreResolutionTarget
        or type(target) is GitStoreResolutionTarget
    ):
        return ResolverRegistry()
    return configured if configured is not None else registry_from_env()


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


def _sanitized_unresolved_result(detail: str) -> ResolvedArtifact:
    """Build an opaque failure that cannot echo adapter-controlled fields."""

    return ResolvedArtifact(
        status=ResolutionStatus.UNRESOLVED,
        source_system="artifact",
        uri="artifact://[redacted]",
        expected_hash="unavailable",
        fetched_at=_now(),
        detail=detail,
    )


def _unresolved_for_identity(
    identity: ArtifactResolutionIdentity,
    *,
    detail: str,
) -> ResolvedArtifact:
    """Build a failure from the immutable pre-dispatch identity snapshot."""

    return ResolvedArtifact(
        status=ResolutionStatus.UNRESOLVED,
        source_system=identity.source_system,
        uri=identity.uri,
        expected_hash=identity.expected_hash,
        fetched_at=_now(),
        detail=detail,
    )


_SAFE_PRECOMPUTED_DETAILS = frozenset(
    {
        "Store artifact reference is invalid.",
        "Store artifact could not be resolved.",
    }
)


def sanitize_artifact_resolution_result(
    result: object,
    *,
    identity: ArtifactResolutionIdentity | None,
    bounds: ArtifactContentBounds,
) -> ResolvedArtifact:
    """Fail closed unless a result exactly matches its target and selected view.

    Registered adapters remain responsible for hashing the full artifact, which
    cannot be reconstructed from a truncated view. Their result objects still
    cross an application shape/identity boundary, so this postcondition is used
    both after registry dispatch and immediately before serialization. Whenever
    the returned view is the complete artifact, its digest is recomputed here too.
    """

    invalid = _sanitized_unresolved_result("Artifact resolver result could not be returned safely.")
    snapshot = _detached_safe_result(result)
    if snapshot is None or identity is None:
        return invalid

    if identity.precomputed:
        if _is_safe_precomputed_result(snapshot, identity):
            return snapshot
        return invalid

    if (
        snapshot.source_system != identity.source_system
        or snapshot.uri != identity.uri
        or snapshot.expected_hash != identity.expected_hash
    ):
        return invalid

    if snapshot.status is ResolutionStatus.UNRESOLVED:
        if snapshot.content is not None:
            return invalid
        return snapshot

    if (
        snapshot.observed_hash is None
        or snapshot.size_bytes is None
        or not is_verifiable_hash(identity.expected_hash)
        or not is_verifiable_hash(snapshot.observed_hash)
    ):
        return invalid

    observed_matches = parse_content_hash(snapshot.observed_hash) == parse_content_hash(
        identity.expected_hash
    )
    expected_returned_bytes = _selected_view_size(snapshot.size_bytes, bounds)
    expected_truncated = expected_returned_bytes < snapshot.size_bytes
    if snapshot.truncated is not expected_truncated:
        return invalid

    if snapshot.status is ResolutionStatus.VERIFIED:
        if (
            not observed_matches
            or snapshot.content is None
            or len(snapshot.content) != expected_returned_bytes
            or len(snapshot.content) > bounds.returned_allowance
            or (
                not snapshot.truncated
                and not _content_matches_expected_hash(
                    snapshot.content,
                    identity.expected_hash,
                )
            )
        ):
            return invalid
        return snapshot

    if (
        snapshot.status is not ResolutionStatus.DRIFTED
        or observed_matches
        or snapshot.content is not None
    ):
        return invalid
    return snapshot


def _detached_safe_result(result: object) -> ResolvedArtifact | None:
    """Copy exact safe fields so adapters cannot mutate accepted output later."""

    if type(result) is not ResolvedArtifact:
        return None

    try:
        status = result.status
        source_system = result.source_system
        uri = result.uri
        expected_hash = result.expected_hash
        fetched_at = result.fetched_at
        observed_hash = result.observed_hash
        content_type = result.content_type
        size_bytes = result.size_bytes
        content = result.content
        truncated = result.truncated
        detail = result.detail
    except Exception:
        return None
    if not (
        type(status) is ResolutionStatus
        and type(source_system) is str
        and type(uri) is str
        and type(expected_hash) is str
        and type(fetched_at) is datetime
        and fetched_at.tzinfo is not None
        and (observed_hash is None or type(observed_hash) is str)
        and (content_type is None or type(content_type) is str)
        and (size_bytes is None or (type(size_bytes) is int and size_bytes >= 0))
        and (content is None or type(content) is bytes)
        and type(truncated) is bool
        and (detail is None or type(detail) is str)
    ):
        return None

    try:
        if fetched_at.utcoffset() is None:
            return None
        normalized_fetched_at = fetched_at.astimezone(timezone.utc)
    except Exception:
        return None
    snapshot = ResolvedArtifact(
        status=status,
        source_system=source_system,
        uri=uri,
        expected_hash=expected_hash,
        fetched_at=normalized_fetched_at,
        observed_hash=observed_hash,
        content_type=content_type,
        size_bytes=size_bytes,
        content=content,
        truncated=truncated,
        detail=detail,
    )
    if (
        snapshot.status is not status
        or snapshot.source_system != source_system
        or snapshot.uri != uri
        or snapshot.expected_hash != expected_hash
        or snapshot.observed_hash != observed_hash
        or snapshot.content_type != content_type
        or snapshot.size_bytes != size_bytes
        or snapshot.content != content
        or snapshot.truncated is not truncated
        or snapshot.detail != detail
    ):
        return None
    return snapshot


def snapshot_artifact_resolution_identity(
    target: object,
) -> ArtifactResolutionIdentity | None:
    """Copy a target's primitive identity before calling untrusted code."""

    if type(target) is ResolvedArtifact:
        precomputed = _detached_safe_result(target)
        if precomputed is None or not _is_safe_precomputed_shape(precomputed):
            return None
        return ArtifactResolutionIdentity(
            source_system=precomputed.source_system,
            uri=precomputed.uri,
            expected_hash=precomputed.expected_hash,
            precomputed=True,
            detail=precomputed.detail,
            fetched_at=precomputed.fetched_at,
        )

    try:
        reference = _logical_reference_for_resolution_target(target)
        if reference is None:
            return None
        source_system = reference.source_system
        uri = reference.uri
        expected_hash = reference.content_hash
    except Exception:
        return None
    if not (type(source_system) is str and type(uri) is str and type(expected_hash) is str):
        return None
    return ArtifactResolutionIdentity(
        source_system=source_system,
        uri=uri,
        expected_hash=expected_hash,
    )


def _logical_reference_for_resolution_target(
    target: object,
) -> ExternalArtifactReference | None:
    if type(target) is ExternalArtifactReference:
        assert isinstance(target, ExternalArtifactReference)
        return target
    if type(target) is LocalStoreResolutionTarget:
        assert isinstance(target, LocalStoreResolutionTarget)
        return target.logical_reference
    if type(target) is HttpStoreResolutionTarget:
        assert isinstance(target, HttpStoreResolutionTarget)
        return target.logical_reference
    if type(target) is RcloneStoreResolutionTarget:
        assert isinstance(target, RcloneStoreResolutionTarget)
        return target.logical_reference
    if type(target) is GitStoreResolutionTarget:
        assert isinstance(target, GitStoreResolutionTarget)
        return target.logical_reference
    return None


def _is_safe_precomputed_shape(result: ResolvedArtifact) -> bool:
    """Recognize the only content-free static result shape prepared by the app."""

    return (
        result.status is ResolutionStatus.UNRESOLVED
        and result.source_system == "store"
        and result.uri == "store://[redacted]"
        and bool(result.expected_hash.strip())
        and result.observed_hash is None
        and result.content_type is None
        and result.size_bytes is None
        and result.content is None
        and result.truncated is False
        and result.detail in _SAFE_PRECOMPUTED_DETAILS
    )


def _is_safe_precomputed_result(
    result: ResolvedArtifact,
    identity: ArtifactResolutionIdentity,
) -> bool:
    """Tie a returned static failure to the exact pre-dispatch snapshot."""

    return (
        _is_safe_precomputed_shape(result)
        and result.source_system == identity.source_system
        and result.uri == identity.uri
        and result.expected_hash == identity.expected_hash
        and result.detail == identity.detail
        and result.fetched_at == identity.fetched_at
    )


def _selected_view_size(
    total_size: int,
    bounds: ArtifactContentBounds,
) -> int:
    """Return the exact number of bytes retained from a full artifact stream."""

    if bounds.byte_range is None:
        return min(total_size, bounds.max_bytes)
    start, end = bounds.byte_range
    retained_end = min(end, start + bounds.max_bytes)
    return max(0, min(total_size, retained_end) - start)


def _content_matches_expected_hash(content: bytes, expected_hash: str) -> bool:
    """Recheck integrity when the bounded response contains the full artifact."""

    algorithm, expected_digest = parse_content_hash(expected_hash)
    try:
        observed_digest = hashlib.new(algorithm, content).hexdigest()
    except ValueError:
        return False
    return observed_digest.lower() == expected_digest


def local_store_resolution_target(
    store: DataStore,
    *,
    locator: LocalStoreLocator,
    logical_reference: ExternalArtifactReference,
) -> LocalStoreResolutionTarget | None:
    """Build one internally consistent, store-scoped local resolution target."""

    if store.kind is not StoreKind.LOCAL_FS or logical_reference.store_name != store.name:
        return None
    try:
        return LocalStoreResolutionTarget(
            logical_reference=logical_reference,
            store_root=store.root,
            locator=locator,
            _factory_token=_LOCAL_STORE_TARGET_FACTORY_TOKEN,
        )
    except ValueError:
        return None


def http_store_resolution_target(
    store: DataStore,
    *,
    locator: PortableStorePath,
    logical_reference: ExternalArtifactReference,
) -> HttpStoreResolutionTarget | None:
    """Build one internally consistent, prefix-scoped HTTP resolution target."""

    if store.kind is not StoreKind.HTTP or logical_reference.store_name != store.name:
        return None
    registered_base = store.endpoint if store.endpoint is not None else store.root
    registered_prefix = RegisteredHttpPrefix.parse(registered_base)
    if registered_prefix is None:
        return None
    try:
        return HttpStoreResolutionTarget(
            logical_reference=logical_reference,
            registered_prefix=registered_prefix,
            locator=locator,
            _factory_token=_HTTP_STORE_TARGET_FACTORY_TOKEN,
        )
    except ValueError:
        return None


def rclone_store_resolution_target(
    store: DataStore,
    *,
    locator: PortableStorePath,
    logical_reference: ExternalArtifactReference,
) -> RcloneStoreResolutionTarget | None:
    """Build one internally consistent, root-scoped rclone target."""

    if not is_rclone_store_kind(store.kind) or logical_reference.store_name != store.name:
        return None
    address = RegisteredRcloneStoreAddress.parse(
        kind=store.kind,
        name=store.name,
        root=store.root,
        credential_ref=store.credential_ref,
    )
    if address is None:
        return None
    try:
        return RcloneStoreResolutionTarget(
            logical_reference=logical_reference,
            remote=address.remote,
            registered_root=address.root,
            locator=locator,
            _factory_token=_RCLONE_STORE_TARGET_FACTORY_TOKEN,
        )
    except ValueError:
        return None


def git_store_resolution_target(
    store: DataStore,
    *,
    pin: PinnedGitPath,
    logical_reference: ExternalArtifactReference,
) -> GitStoreResolutionTarget | None:
    """Build one internally consistent immutable registered-Git target."""

    if store.kind is not StoreKind.GIT or logical_reference.store_name != store.name:
        return None
    remote = parse_git_remote_address(store.root)
    if remote is None:
        return None
    try:
        return GitStoreResolutionTarget(
            logical_reference=logical_reference,
            remote=remote,
            pin=pin,
            _factory_token=_GIT_STORE_TARGET_FACTORY_TOKEN,
        )
    except ValueError:
        return None


def store_relative_reference(
    store: DataStore, *, path: str, content_hash: str
) -> (
    LocalStoreResolutionTarget
    | HttpStoreResolutionTarget
    | RcloneStoreResolutionTarget
    | GitStoreResolutionTarget
    | None
):
    """Translate one legacy store-relative path into a resolvable target.

    Registered local, HTTP, rclone, and Git stores yield scoped targets. Git
    additionally requires a full immutable object ID paired with the portable
    repository path. Unsupported adapters return ``None``.
    """

    if store.kind is StoreKind.LOCAL_FS:
        locator = LocalStoreLocator.parse_decoded(path)
        if locator is None:
            return None
        try:
            logical_reference = ExternalArtifactReference.for_local_store(
                store_name=store.name,
                locator=locator.path,
                content_hash=content_hash,
            )
        except ValueError:
            return None
        return local_store_resolution_target(
            store,
            locator=locator,
            logical_reference=logical_reference,
        )

    if store.kind is StoreKind.HTTP:
        locator = PortableStorePath.parse_decoded(path)
        canonical_uri = canonical_store_uri(store.name, locator) if locator is not None else None
        if locator is None or canonical_uri is None:
            return None
        logical_reference = ExternalArtifactReference(
            source_system="store",
            uri=canonical_uri,
            content_hash=content_hash,
            store_name=store.name,
            locator=locator.path,
        )
        return http_store_resolution_target(
            store,
            locator=locator,
            logical_reference=logical_reference,
        )

    if is_rclone_store_kind(store.kind):
        locator = PortableStorePath.parse_decoded(path)
        canonical_uri = canonical_store_uri(store.name, locator) if locator is not None else None
        if locator is None or canonical_uri is None:
            return None
        logical_reference = ExternalArtifactReference(
            source_system="store",
            uri=canonical_uri,
            content_hash=content_hash,
            store_name=store.name,
            locator=locator.path,
        )
        return rclone_store_resolution_target(
            store,
            locator=locator,
            logical_reference=logical_reference,
        )
    if store.kind is StoreKind.GIT:
        pin = PinnedGitPath.parse_decoded(path)
        if pin is None:
            return None
        try:
            logical_reference = ExternalArtifactReference.for_git_store(
                store_name=store.name,
                repository_path=pin.path.path,
                object_id=pin.object_id.value,
                content_hash=content_hash,
            )
        except ValueError:
            return None
        return git_store_resolution_target(
            store,
            pin=pin,
            logical_reference=logical_reference,
        )
    return None


def check_store_health(
    store: DataStore | StoreProbeTarget,
) -> StoreHealth:
    """Probe whether a registered store is reachable from this host.

    This transitional helper fails closed for local, HTTP, rclone, and Git
    stores; the runtime handles them only through dedicated policy-authorized
    adapters. ``object_table`` and ``database`` are reported ``unsupported``
    until their adapters land. Health transport, runner, executor, policy, and
    timeout keywords were intentionally removed so direct callers cannot mistake
    this compatibility path for a safe host-I/O probe.
    """

    kind = store.kind
    if kind is StoreKind.LOCAL_FS:
        return StoreHealth(
            StoreHealthStatus.UNREACHABLE,
            LOCAL_STORE_HEALTH_FAILURE_DETAIL,
        )

    if kind is StoreKind.HTTP:
        return StoreHealth(
            StoreHealthStatus.UNREACHABLE,
            HTTP_STORE_HEALTH_FAILURE_DETAIL,
        )

    if is_rclone_store_kind(kind):
        return StoreHealth(
            StoreHealthStatus.UNREACHABLE,
            RCLONE_STORE_HEALTH_FAILURE_DETAIL,
        )

    if kind is StoreKind.GIT:
        return StoreHealth(
            StoreHealthStatus.UNREACHABLE,
            GIT_STORE_HEALTH_FAILURE_DETAIL,
        )

    return StoreHealth(
        StoreHealthStatus.UNSUPPORTED,
        f"Health checks for '{kind.value}' stores are not supported yet.",
    )


class _RecoveryDisposition(Enum):
    MATCHED = "matched"
    EXHAUSTED = "exhausted"
    FAILED = "failed"


@dataclass(frozen=True)
class _RecoveryResult:
    disposition: _RecoveryDisposition
    artifact: ResolvedArtifact | None = None


def _valid_recovery_enumeration(
    result: LocalRecoveryEnumerationResult,
    *,
    scope: LocalRecoveryScope,
    policy: RecoveryPolicy,
    target_name: str | None,
) -> bool:
    if (
        type(result) is not LocalRecoveryEnumerationResult
        or type(result.outcome) is not LocalRecoveryEnumerationOutcome
        or type(result.candidates) is not tuple
        or len(result.candidates) > policy.max_files
        or type(result.directories_visited) is not int
        or not 0 <= result.directories_visited <= policy.max_directories
    ):
        return False
    if result.outcome is LocalRecoveryEnumerationOutcome.FAILED:
        return not result.candidates and result.directories_visited == 0

    seen: set[tuple[object, ...]] = set()
    fallback_seen = False
    for candidate in result.candidates:
        if (
            type(candidate) is not LocalRecoveryCandidate
            or type(candidate.name) is not str
            or not candidate.name
        ):
            return False
        target = candidate.target
        if type(scope) is DirectLocalRecoveryScope:
            if type(target) is not EnumeratedLocalRegularFileTarget:
                return False
            key: tuple[object, ...] = (
                target.root_index,
                *target.locator,
            )
        elif type(scope) is RegisteredLocalRecoveryScope:
            if (
                type(target) is not RegisteredLocalRegularFileTarget
                or target.store_root != scope.store_root
            ):
                return False
            key = (target.store_root, *target.locator)
        else:
            return False
        if not target.locator or candidate.name != target.locator[-1] or key in seen:
            return False
        seen.add(key)

        preferred = target_name is not None and candidate.name == target_name
        if fallback_seen and preferred:
            return False
        if not preferred:
            fallback_seen = True
    return True


class LocalFilesystemResolver(ArtifactResolver, ScopedLocalStoreResolver):
    """Resolve local artifacts through one bounded, pre-follow-safe broker."""

    def __init__(
        self,
        allowed_roots: Sequence[str | Path] | None = None,
        *,
        path_policy: LocalPathPolicy | None = None,
        recovery: RecoveryPolicy | None = None,
        file_reader: LocalRegularFileReader | None = None,
        recovery_enumerator: LocalRecoveryEnumerator | None = None,
        limits: LocalResolutionLimits | None = None,
        process_executor: ProcessExecutor | None = None,
    ) -> None:
        if recovery is not None and type(recovery) is not RecoveryPolicy:
            raise TypeError("Local recovery policy is invalid.")
        if limits is not None and type(limits) is not LocalResolutionLimits:
            raise TypeError("Local resolution limits are invalid.")
        resolved_recovery = (
            recovery
            if recovery is not None
            else RecoveryPolicy(
                max_bytes=(
                    limits.max_read_bytes
                    if limits is not None
                    else DEFAULT_RECOVERY_MAX_BYTES
                )
            )
        )
        resolved_limits = (
            limits
            if limits is not None
            else LocalResolutionLimits(max_read_bytes=resolved_recovery.max_bytes)
        )
        if resolved_recovery.max_bytes != resolved_limits.max_read_bytes:
            raise ValueError("Local recovery and resolution byte limits must match.")
        self._recovery = resolved_recovery
        self._limits = resolved_limits
        default_operations: BoundedLocalFilesystemOperations | None = None
        if file_reader is None or recovery_enumerator is None:
            if path_policy is not None:
                lexical_roots = path_policy.lexical_roots
            else:
                lexical_roots = (
                    None
                    if allowed_roots is None
                    else tuple(os.fspath(root) for root in allowed_roots)
                )
            authority = (
                LocalFilesystemAuthority.for_unscoped_library_compatibility()
                if lexical_roots is None
                else LocalFilesystemAuthority.from_roots(lexical_roots)
            )
            default_operations = BoundedLocalFilesystemOperations(
                authority=authority,
                executor=(
                    process_executor
                    if process_executor is not None
                    else BoundedSubprocessExecutor()
                ),
            )
        if file_reader is None:
            assert default_operations is not None
            self._file_reader: LocalRegularFileReader = default_operations
        else:
            self._file_reader = file_reader
        if recovery_enumerator is None:
            assert default_operations is not None
            self._recovery_enumerator: LocalRecoveryEnumerator = default_operations
        else:
            self._recovery_enumerator = recovery_enumerator
        if (
            self._recovery.enabled
            and type(self._recovery_enumerator) is BoundedLocalFilesystemOperations
        ):
            self._recovery_enumerator.validate_direct_recovery_configuration(
                max_files=self._recovery.max_files,
                max_directories=self._recovery.max_directories,
            )
        self._recovery_scope = DirectLocalRecoveryScope()

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
        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
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
        budget = LocalResolutionBudget(self._limits)
        return self._resolve_target(
            ref,
            target=DirectLocalRegularFileTarget(local_path),
            budget=budget,
            max_bytes=bounds.max_bytes,
            window=bounds.byte_range,
            mime_path=local_path,
            recovery_scope=self._recovery_scope,
            recovery_name=os.path.basename(local_path) or None,
            opaque_store_detail=False,
        )

    def resolve_within_root(
        self,
        target: LocalStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Resolve a store locator beneath a helper-retained nested scope."""

        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        ref = target.logical_reference
        if not is_verifiable_hash(ref.content_hash):
            algorithm, _ = parse_content_hash(ref.content_hash)
            return _unresolved(
                ref,
                detail=f"Cannot verify content hash with algorithm '{algorithm}'.",
            )

        budget = LocalResolutionBudget(self._limits)
        return self._resolve_target(
            ref,
            target=RegisteredLocalRegularFileTarget(
                target.store_root,
                target.locator.components,
            ),
            budget=budget,
            max_bytes=bounds.max_bytes,
            window=bounds.byte_range,
            mime_path=target.locator.path,
            recovery_scope=None,
            recovery_name=target.locator.components[-1],
            opaque_store_detail=True,
        )

    def _resolve_target(
        self,
        ref: ExternalArtifactReference,
        *,
        target: LocalRegularFileTarget,
        budget: LocalResolutionBudget,
        max_bytes: int,
        window: tuple[int, int] | None,
        mime_path: str,
        recovery_scope: LocalRecoveryScope | None,
        recovery_name: str | None,
        opaque_store_detail: bool,
    ) -> ResolvedArtifact:
        completed = self._read_and_collect(
            ref,
            target=target,
            budget=budget,
            max_bytes=max_bytes,
            window=window,
            mime_path=mime_path,
        )
        if isinstance(completed, ResolvedArtifact):
            return completed
        if completed is LocalRegularFileReadOutcome.DENIED:
            return _unresolved(
                ref,
                detail=(
                    "Local store artifact is not authorized."
                    if opaque_store_detail
                    else "Local artifact is not an authorized readable regular file."
                ),
            )
        if completed is not LocalRegularFileReadOutcome.MISSING:
            return _unresolved(ref, detail="Failed to read local artifact.")

        if opaque_store_detail:
            assert isinstance(target, RegisteredLocalRegularFileTarget)
            recovery_scope = RegisteredLocalRecoveryScope(target.store_root)
        recovered = self._recover_by_hash(
            ref,
            budget=budget,
            max_bytes=max_bytes,
            window=window,
            scope=recovery_scope,
            target_name=recovery_name,
            opaque_store_detail=opaque_store_detail,
        )
        if recovered.disposition is _RecoveryDisposition.MATCHED:
            assert recovered.artifact is not None
            return recovered.artifact
        if recovered.disposition is _RecoveryDisposition.FAILED:
            return _unresolved(ref, detail="Failed to read local artifact.")
        return _unresolved(
            ref,
            detail=(
                "Local store artifact not found."
                if opaque_store_detail
                else "Local artifact not found."
            ),
        )

    def _read_and_collect(
        self,
        ref: ExternalArtifactReference,
        *,
        target: LocalRegularFileTarget,
        budget: LocalResolutionBudget,
        max_bytes: int,
        window: tuple[int, int] | None,
        mime_path: str,
    ) -> ResolvedArtifact | LocalRegularFileReadOutcome:
        algorithm, _ = parse_content_hash(ref.content_hash)
        collector = _HashCollector(
            algorithm=algorithm,
            max_bytes=max_bytes,
            window=window,
            budget_check=budget.deadline.check,
        )
        try:
            result = self._file_reader.read_regular_file(
                target,
                budget=budget,
                stdout_consumer=collector.consume,
            )
        except Exception:
            budget.abort_terminal()
            return LocalRegularFileReadOutcome.FAILED
        except BaseException:
            budget.abort_terminal()
            raise
        if type(result) is not LocalRegularFileReadResult:
            budget.abort_terminal()
            return LocalRegularFileReadOutcome.FAILED
        if result.outcome is not LocalRegularFileReadOutcome.COMPLETE:
            if result.outcome is LocalRegularFileReadOutcome.FAILED:
                budget.abort_terminal()
            return result.outcome
        try:
            content, total, truncated, observed = collector.finish()
            if total != result.bytes_read:
                budget.abort_terminal()
                return LocalRegularFileReadOutcome.FAILED
            content_type, _ = mimetypes.guess_type(mime_path)
            return _build_resolved(
                ref,
                observed=observed,
                content=content,
                total=total,
                truncated=truncated,
                content_type=content_type or "application/octet-stream",
            )
        except Exception:
            budget.abort_terminal()
            return LocalRegularFileReadOutcome.FAILED
        except BaseException:
            budget.abort_terminal()
            raise

    def _recover_by_hash(
        self,
        ref: ExternalArtifactReference,
        *,
        budget: LocalResolutionBudget,
        max_bytes: int,
        window: tuple[int, int] | None,
        scope: LocalRecoveryScope | None,
        target_name: str | None,
        opaque_store_detail: bool,
    ) -> _RecoveryResult:
        if not self._recovery.enabled or scope is None:
            return _RecoveryResult(_RecoveryDisposition.EXHAUSTED)

        try:
            _, expected_digest = parse_content_hash(ref.content_hash)
            enumeration = self._recovery_enumerator.enumerate_recovery_candidates(
                scope,
                target_name=target_name,
                max_files=self._recovery.max_files,
                max_directories=self._recovery.max_directories,
                budget=budget,
            )
            if not _valid_recovery_enumeration(
                enumeration,
                scope=scope,
                policy=self._recovery,
                target_name=target_name,
            ):
                budget.abort_terminal()
                return _RecoveryResult(_RecoveryDisposition.FAILED)
            if enumeration.outcome is LocalRecoveryEnumerationOutcome.FAILED:
                budget.abort_terminal()
                return _RecoveryResult(_RecoveryDisposition.FAILED)

            budget.deadline.check()
            for candidate in enumeration.candidates:
                budget.deadline.check()
                attempt = self._read_and_collect(
                    ref,
                    target=candidate.target,
                    budget=budget,
                    max_bytes=max_bytes,
                    window=window,
                    mime_path=candidate.name,
                )
                if isinstance(attempt, ResolvedArtifact):
                    if (
                        attempt.observed_hash is not None
                        and parse_content_hash(attempt.observed_hash)[1] == expected_digest
                    ):
                        return _RecoveryResult(
                            _RecoveryDisposition.MATCHED,
                            replace(
                                attempt,
                                detail=(
                                    "Recovered within registered local store "
                                    "(differs from reference locator)."
                                    if opaque_store_detail
                                    else "Recovered within authorized local roots "
                                    "(differs from reference URI)."
                                ),
                            ),
                        )
                    continue
                if attempt is LocalRegularFileReadOutcome.FAILED:
                    budget.abort_terminal()
                    return _RecoveryResult(_RecoveryDisposition.FAILED)
                budget.deadline.check()
            if enumeration.outcome is LocalRecoveryEnumerationOutcome.LIMIT_REACHED:
                budget.abort_terminal()
                return _RecoveryResult(_RecoveryDisposition.FAILED)
        except Exception:
            budget.abort_terminal()
            return _RecoveryResult(_RecoveryDisposition.FAILED)
        except BaseException:
            budget.abort_terminal()
            raise
        return _RecoveryResult(_RecoveryDisposition.EXHAUSTED)


class HttpResolver(ArtifactResolver, ScopedHttpStoreResolver):
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
                raise ValueError("Configure either deadline_seconds or the legacy timeout alias.")
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
        self._policy = policy if policy is not None else OutboundHttpPolicy()
        self._client = client if client is not None else SafeHttpClient(timeout=deadline_seconds)
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
        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        return self._resolve_http_url(
            ref,
            initial_url=ref.uri,
            redirect_resolver=resolve_direct_http_redirect,
            bounds=bounds,
        )

    def resolve_within_http_store(
        self,
        target: HttpStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Resolve through a registered prefix without exposing its concrete URL."""

        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        initial_url = target.registered_prefix.compose(target.locator)
        if initial_url is None or not target.registered_prefix.contains(initial_url):
            return _unresolved(
                target.logical_reference,
                detail="HTTP artifact fetch failed or was denied.",
            )
        return self._resolve_http_url(
            target.logical_reference,
            initial_url=initial_url,
            redirect_resolver=target.registered_prefix.resolve_redirect,
            bounds=bounds,
        )

    def _resolve_http_url(
        self,
        ref: ExternalArtifactReference,
        *,
        initial_url: str,
        redirect_resolver: Callable[[str, str], str | None],
        bounds: ArtifactContentBounds,
    ) -> ResolvedArtifact:
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
        current_url = initial_url
        seen_targets: set[str] = set()
        for redirect_count in range(self._max_redirects + 1):
            try:
                deadline.check()
                target = self._policy.authorize(current_url, deadline=deadline)
                deadline.check()
            except (OutboundHttpPolicyError, OutboundHttpTransportError):
                return _unresolved(ref, detail="HTTP artifact fetch failed or was denied.")
            if target.absolute_url in seen_targets:
                return _unresolved(ref, detail="HTTP redirect loop was refused.")
            seen_targets.add(target.absolute_url)

            try:
                deadline.check()
                with self._client.open("GET", target, deadline=deadline) as response:
                    deadline.check()
                    if response.status_code in HTTP_REDIRECT_STATUS_CODES:
                        location = response.get_header("location")
                        deadline.check()
                        if not location or redirect_count >= self._max_redirects:
                            return _unresolved(ref, detail="HTTP redirect limit was exceeded.")
                        next_url = redirect_resolver(
                            target.absolute_url,
                            location,
                        )
                        if next_url is None:
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
                            detail=(f"HTTP {response.status_code} fetching artifact."),
                        )
                    declared_size = _http_content_length(response.get_header("content-length"))
                    if declared_size is not None and declared_size > self._max_fetch_bytes:
                        return _unresolved(
                            ref,
                            detail=str(_FetchTooLarge(self._max_fetch_bytes)),
                        )
                    content, total, truncated, observed = _hash_and_collect(
                        response.iter_bytes(),
                        algorithm=algorithm,
                        max_bytes=bounds.max_bytes,
                        window=bounds.byte_range,
                        max_total=self._max_fetch_bytes,
                        budget_check=deadline.check,
                    )
                    deadline.check()
                    content_type = response.get_header("content-type")
                    deadline.check()
            except _FetchTooLarge as exc:
                return _unresolved(ref, detail=str(exc))
            except OutboundHttpTransportError:
                return _unresolved(ref, detail="HTTP artifact fetch failed or was denied.")

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
                return _unresolved(ref, detail="HTTP artifact fetch failed or was denied.")
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
    if not math.isfinite(value) or value <= 0 or value > MAX_SUBPROCESS_DEADLINE_SECONDS:
        raise ValueError(
            "Subprocess deadline must be finite and greater than 0, and no greater "
            f"than {MAX_SUBPROCESS_DEADLINE_SECONDS:g} seconds."
        )
    return value


class RcloneResolver(ArtifactResolver, ScopedRcloneStoreResolver):
    """Resolves artifacts via ``rclone``, the unifier for cloud and remote stores.

    One adapter covers S3, SFTP, Dropbox, Google Drive, Box, OneDrive and the
    rest of rclone's backends; credentials live host-side in rclone's own config,
    never in Lab Tracker. References are addressed as ``rclone://<remote>/<path>``
    (mapped to rclone's ``remote:path``). A ``runner`` may be injected for tests;
    otherwise rclone is invoked as a subprocess.

    The whole object is fetched (``rclone cat``) to certify its hash, so its size
    is checked first (``rclone size``) and an object larger than
    ``max_fetch_bytes`` is refused as UNRESOLVED rather than downloaded.

    ``remote_policy`` is the only authority for rclone remote names. Omission
    denies every remote, including for direct library use. Runtime composition
    parses the strict configured policy once and shares that exact object with
    store health.
    """

    def __init__(
        self,
        *,
        runner: RcloneRunner | None = None,
        executor: ProcessExecutor | None = None,
        binary: str = "rclone",
        remote_policy: RcloneRemotePolicy | None = None,
        max_fetch_bytes: int = DEFAULT_MAX_FETCH_BYTES,
        deadline_seconds: float = DEFAULT_SUBPROCESS_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if runner is not None and executor is not None:
            raise ValueError("Configure either runner or executor, not both.")
        self._runner = runner
        self._executor = executor if executor is not None else BoundedSubprocessExecutor()
        self._binary = binary
        self._remote_policy = (
            remote_policy if remote_policy is not None else RcloneRemotePolicy.deny_all()
        )
        self._max_fetch_bytes = max_fetch_bytes
        self._deadline_seconds = _validate_subprocess_deadline_seconds(deadline_seconds)
        self._clock = clock

    def can_resolve(self, ref: ExternalArtifactReference) -> bool:
        return _uri_scheme(ref.uri) == "rclone"

    def resolve(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)

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
        if self._remote_policy.authorize(remote_name) is None:
            return _unresolved(ref, detail="Remote is not in the rclone resolver allowlist.")

        algorithm, _ = parse_content_hash(ref.content_hash)
        return self._resolve_validated_target(
            ref,
            target=target,
            algorithm=algorithm,
            window=bounds.byte_range,
            max_bytes=bounds.max_bytes,
        )

    def resolve_within_rclone_store(
        self,
        target: RcloneStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Resolve a prevalidated registered target without URI reparsing."""

        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        ref = target.logical_reference

        if not is_verifiable_hash(ref.content_hash):
            algorithm, _ = parse_content_hash(ref.content_hash)
            return _unresolved(
                ref,
                detail=f"Cannot verify content hash with algorithm '{algorithm}'.",
            )
        if self._remote_policy.authorize_name(target.remote) is None:
            return _unresolved(ref, detail="Remote is not in the rclone resolver allowlist.")

        algorithm, _ = parse_content_hash(ref.content_hash)
        return self._resolve_validated_target(
            ref,
            target=target.argv_target,
            algorithm=algorithm,
            window=bounds.byte_range,
            max_bytes=bounds.max_bytes,
        )

    def _resolve_validated_target(
        self,
        ref: ExternalArtifactReference,
        *,
        target: str,
        algorithm: str,
        window: tuple[int, int] | None,
        max_bytes: int,
    ) -> ResolvedArtifact:
        """Run the shared bounded size/cat/hash lifecycle for one exact target."""

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
                return _unresolved(ref, detail="rclone artifact resolution failed.")
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
            legacy_completed = self._runner(args)
            deadline.check()
            if (
                len(legacy_completed.stdout) > stdout_limit_bytes
                or len(legacy_completed.stderr) > _PROCESS_STDERR_LIMIT_BYTES
            ):
                raise _LegacyProcessOutputExceeded
            stdout = legacy_completed.stdout
            if stdout_consumer is not None:
                stdout_consumer(stdout)
                deadline.check()
                stdout = b""
            return RcloneCompleted(
                legacy_completed.returncode,
                stdout,
                legacy_completed.stderr,
            )

        process_result = self._executor.run(
            [self._binary, *args],
            deadline=deadline,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=_PROCESS_STDERR_LIMIT_BYTES,
            stdout_consumer=stdout_consumer,
        )
        return RcloneCompleted(
            process_result.returncode,
            process_result.stdout,
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


class GitResolver(ArtifactResolver, ScopedGitStoreResolver):
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
        self._deadline_seconds = _validate_subprocess_deadline_seconds(deadline_seconds)
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
        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)

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
            return _unresolved(ref, detail="Remote is not in the git resolver allowlist.")

        algorithm, _ = parse_content_hash(ref.content_hash)
        return self._resolve_authorized_target(
            ref,
            approved=approved,
            revision=commit,
            path=path,
            object_format=None,
            algorithm=algorithm,
            window=bounds.byte_range,
            max_bytes=bounds.max_bytes,
        )

    def resolve_within_git_store(
        self,
        target: GitStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Resolve a registered immutable object without generic URI parsing."""

        bounds = ArtifactContentBounds.from_resolver(max_bytes, byte_range)
        ref = target.logical_reference

        if not is_verifiable_hash(ref.content_hash):
            algorithm, _ = parse_content_hash(ref.content_hash)
            return _unresolved(
                ref,
                detail=f"Cannot verify content hash with algorithm '{algorithm}'.",
            )

        approved = self._remote_policy.authorize_address(target.remote)
        if approved is None:
            return _unresolved(ref, detail="Remote is not in the git resolver allowlist.")

        algorithm, _ = parse_content_hash(ref.content_hash)
        return self._resolve_authorized_target(
            ref,
            approved=approved,
            revision=target.pin.object_id.value,
            path=target.pin.path.path,
            object_format=target.pin.object_id.object_format,
            algorithm=algorithm,
            window=bounds.byte_range,
            max_bytes=bounds.max_bytes,
        )

    def _resolve_authorized_target(
        self,
        ref: ExternalArtifactReference,
        *,
        approved: ApprovedGitRemote,
        revision: str,
        path: str,
        object_format: GitObjectFormat | None,
        algorithm: str,
        window: tuple[int, int] | None,
        max_bytes: int,
    ) -> ResolvedArtifact:
        """Run the shared bounded Git lifecycle for one authorized target."""

        try:
            deadline = ProcessDeadline.after(
                self._deadline_seconds,
                clock=self._clock,
            )
            executor = self._executor if self._executor is not None else BoundedSubprocessExecutor()
            deadline.check()
            cache = self._repo_cache(
                approved.subprocess_value,
                object_format=object_format,
            )
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
                revision,
                path,
                cache=cache,
                executor=executor,
                env=env,
                deadline=deadline,
                stdout_consumer=collector.consume,
                object_format=object_format,
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
        object_format: GitObjectFormat | None = None,
    ) -> None:
        remote = approved.subprocess_value
        deadline.check()
        rev = f"{commit}:{path}"
        config_args = _git_http_config_args(approved)
        # Idempotent: a fresh cache is initialised once, an existing one reused.
        init_args = ["init", "-q"]
        if object_format is not None:
            init_args.append(f"--object-format={object_format}")
        initialized = self._run_command(
            init_args,
            executor=executor,
            cwd=cache,
            env=env,
            config_args=config_args,
            deadline=deadline,
            stdout_limit_bytes=GIT_PROCESS_METADATA_LIMIT_BYTES,
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
            stdout_limit_bytes=GIT_PROCESS_METADATA_LIMIT_BYTES,
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
            stdout_limit_bytes=GIT_PROCESS_METADATA_LIMIT_BYTES,
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
            stdout_limit_bytes=GIT_PROCESS_METADATA_LIMIT_BYTES,
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
                f"Artifact ({size} bytes) exceeds the {self._max_fetch_bytes}-byte fetch limit."
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

    def _repo_cache(
        self,
        remote: str,
        *,
        object_format: GitObjectFormat | None = None,
    ) -> str:
        base = (
            os.fspath(self._cache_root)
            if self._cache_root
            else os.path.join(tempfile.gettempdir(), "lab-tracker-git-cache")
        )
        self._enforce_cache_quota(base)
        if object_format is None:
            cache_identity = remote
            cache_prefix = ""
        else:
            cache_identity = f"{object_format}\0{remote}"
            cache_prefix = f"{object_format}-"
        digest = hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()[:16]
        cache = os.path.join(base, f"{cache_prefix}{digest}")
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


def _http_content_length(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise OutboundHttpTransportError("Outbound HTTP request failed.") from None
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
    capped ``[start, min(end, start + max_bytes))`` slice. Raises
    :class:`_FetchTooLarge` if the total exceeds ``max_total`` (so an oversized
    artifact is refused rather than returned uncertified).
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
        bounds = ArtifactContentBounds.from_resolver(max_bytes, window)
        self._algorithm = algorithm
        self._hasher = hashlib.new(algorithm)
        self._collected = bytearray()
        self._total = 0
        self._max_bytes = bounds.max_bytes
        if bounds.byte_range is None:
            self._window: tuple[int, int] | None = None
        else:
            start, end = bounds.byte_range
            self._window = (start, min(end, start + bounds.max_bytes))
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
                self._collected.extend(chunk[: self._max_bytes - len(self._collected)])
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
    local_path_policy: LocalPathPolicy | None = None,
    local_file_reader: LocalRegularFileReader | None = None,
    local_recovery_enumerator: LocalRecoveryEnumerator | None = None,
    local_resolution_limits: LocalResolutionLimits | None = None,
    recovery: RecoveryPolicy | None = None,
    http_policy: OutboundHttpPolicy | None = None,
    http_client: OutboundHttpClient | None = None,
    http_deadline_seconds: float = DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS,
    subprocess_deadline_seconds: float = DEFAULT_SUBPROCESS_DEADLINE_SECONDS,
    rclone_remote_policy: RcloneRemotePolicy | None = None,
    git_remote_policy: GitRemotePolicy | None = None,
    process_executor: ProcessExecutor | None = None,
    git_cache_root: str | Path | None = None,
    git_max_cache_bytes: int | None = None,
) -> ResolverRegistry:
    """Build a registry with the adapters available in this slice.

    Local-filesystem, HTTP(S), rclone, and git resolvers. The rclone and git
    adapters degrade to UNRESOLVED when their binary is absent, so including them
    is safe by default. Native store-backed adapters (s3, ssh, database) register
    here as they land. ``recovery`` opts the local resolver into content-hash
    recovery of missing files within ``allowed_roots``. ``http_policy`` and
    ``http_client`` are the outbound authority and pinned transport that runtime
    composition shares exactly with HTTP store-health probes.
    ``subprocess_deadline_seconds`` is one shared execution/verification budget
    for every command in a single rclone or Git resolution.
    ``rclone_remote_policy`` and ``git_remote_policy`` are immutable authorities
    shared with health composition; omission denies every corresponding remote.
    ``local_path_policy`` remains a library compatibility input; when supplied
    its configured roots take precedence over ``allowed_roots``. Runtime shares
    one bounded local broker across directory inspection, file reads, and
    recovery enumeration. One process executor is shared by the subprocess
    resolvers and health adapters.
    """

    shared_process_executor = (
        process_executor if process_executor is not None else BoundedSubprocessExecutor()
    )
    return ResolverRegistry(
        [
            LocalFilesystemResolver(
                allowed_roots=allowed_roots,
                path_policy=local_path_policy,
                file_reader=local_file_reader,
                recovery_enumerator=local_recovery_enumerator,
                limits=local_resolution_limits,
                recovery=recovery,
                process_executor=shared_process_executor,
            ),
            HttpResolver(
                policy=http_policy,
                client=http_client,
                deadline_seconds=http_deadline_seconds,
            ),
            RcloneResolver(
                remote_policy=rclone_remote_policy,
                executor=shared_process_executor,
                deadline_seconds=subprocess_deadline_seconds,
            ),
            GitResolver(
                remote_policy=git_remote_policy,
                executor=shared_process_executor,
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
# truthy). Recovery still only runs when allowed roots are configured. The
# three budget vars override the RecoveryPolicy defaults when set to a positive
# int.
LAB_TRACKER_RESOLVER_RECOVERY_ENV = "LAB_TRACKER_RESOLVER_RECOVERY"
LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES_ENV = "LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES"
LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES_ENV = (
    "LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES"
)
LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES_ENV = "LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES"

# Comma-separated exact HTTP(S) origins and IP networks for internal artifact
# resolution and store-health destinations. Both variables are required for an
# exception: the normalized scheme/host/effective-port origin must match exactly
# and every DNS answer must fall inside one configured CIDR. Without an
# exception, every answer must be a globally routable public address.
LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES_ENV = "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES"
LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS_ENV = "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS"
LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS_ENV = "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS"
LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS_ENV = (
    "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS"
)

# Comma-separated allowlist of rclone remote NAMES the rclone resolver may read.
# When unset, registry_from_env denies all rclone remotes (references resolve
# UNRESOLVED) until an operator opts specific remotes in — without it, a
# reference could drive server-side `rclone cat` against any remote in the
# host's rclone config.
LAB_TRACKER_RCLONE_ALLOWED_REMOTES_ENV = DEFAULT_RCLONE_REMOTE_POLICY_VARIABLE

# Comma-separated structural Git grants. Each grant is parsed and canonicalized;
# candidates are authorized by scheme, host, effective port, optional SSH user,
# and repository path boundary. When unset, registry_from_env denies all remotes.
LAB_TRACKER_GIT_ALLOWED_REMOTES_ENV = "LAB_TRACKER_GIT_ALLOWED_REMOTES"
LAB_TRACKER_GIT_CACHE_ROOT_ENV = "LAB_TRACKER_GIT_CACHE_ROOT"
LAB_TRACKER_GIT_CACHE_MAX_BYTES_ENV = "LAB_TRACKER_GIT_CACHE_MAX_BYTES"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})


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
        enabled=_strict_env_flag(
            os.environ.get(LAB_TRACKER_RESOLVER_RECOVERY_ENV),
            variable=LAB_TRACKER_RESOLVER_RECOVERY_ENV,
        ),
        max_files=_strict_env_positive_int(
            os.environ.get(LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES_ENV),
            DEFAULT_RECOVERY_MAX_FILES,
            variable=LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES_ENV,
            maximum=MAX_LOCAL_RECOVERY_MAX_FILES,
        ),
        max_directories=_strict_env_positive_int(
            os.environ.get(LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES_ENV),
            DEFAULT_RECOVERY_MAX_DIRECTORIES,
            variable=LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES_ENV,
            maximum=MAX_LOCAL_RECOVERY_MAX_DIRECTORIES,
        ),
        max_bytes=_strict_env_positive_int(
            os.environ.get(LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES_ENV),
            DEFAULT_RECOVERY_MAX_BYTES,
            variable=LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES_ENV,
            maximum=MAX_LOCAL_RESOLUTION_MAX_READ_BYTES,
        ),
    )


def _strict_env_flag(value: str | None, *, variable: str) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    raise ValueError(f"{variable} must be a boolean value.")


def _strict_env_positive_int(
    value: str | None,
    default: int,
    *,
    variable: str,
    maximum: int | None = None,
) -> int:
    if value is None:
        return default
    stripped = value.strip()
    if not stripped or not stripped.isascii() or not stripped.isdecimal():
        raise ValueError(f"{variable} must be a positive integer.")
    parsed = int(stripped)
    if parsed <= 0 or (maximum is not None and parsed > maximum):
        raise ValueError(f"{variable} must be a positive integer within its limit.")
    return parsed


def outbound_http_policy_from_env() -> OutboundHttpPolicy:
    """Build and validate the outbound HTTP policy from environment variables."""

    return outbound_http_policy_from_config(
        allowed_authorities=os.environ.get(LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES_ENV),
        allowed_networks=os.environ.get(LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS_ENV),
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


def _strict_comma_separated_values(raw: str | None, *, variable: str) -> list[str]:
    if raw is None or not raw.strip():
        return []
    values = [part.strip() for part in raw.split(",")]
    if any(not value for value in values):
        raise ValueError(f"{variable} contains an empty entry.")
    return values


def registry_from_env(
    *,
    local_path_policy: LocalPathPolicy | None = None,
    local_file_reader: LocalRegularFileReader | None = None,
    local_recovery_enumerator: LocalRecoveryEnumerator | None = None,
    local_resolution_limits: LocalResolutionLimits | None = None,
    recovery: RecoveryPolicy | None = None,
    http_policy: OutboundHttpPolicy | None = None,
    http_client: OutboundHttpClient | None = None,
    http_deadline_seconds: float | None = None,
    subprocess_deadline_seconds: float | None = None,
    rclone_remote_policy: RcloneRemotePolicy | None = None,
    git_remote_policy: GitRemotePolicy | None = None,
    process_executor: ProcessExecutor | None = None,
) -> ResolverRegistry:
    """Build the default registry, reading resolver config from the env."""

    if (
        local_path_policy is None
        and (local_file_reader is None or local_recovery_enumerator is None)
    ):
        local_path_policy = LocalPathPolicy.from_config(
            os.environ.get(LAB_TRACKER_RESOLVER_ALLOWED_ROOTS_ENV)
        )
    if rclone_remote_policy is None:
        rclone_remote_policy = RcloneRemotePolicy.from_config(
            os.environ.get(LAB_TRACKER_RCLONE_ALLOWED_REMOTES_ENV),
            variable=LAB_TRACKER_RCLONE_ALLOWED_REMOTES_ENV,
        )
    if git_remote_policy is None:
        git_remote_policy = GitRemotePolicy.from_config(
            os.environ.get(LAB_TRACKER_GIT_ALLOWED_REMOTES_ENV),
            variable=LAB_TRACKER_GIT_ALLOWED_REMOTES_ENV,
        )
    git_cache_root = os.environ.get(LAB_TRACKER_GIT_CACHE_ROOT_ENV) or None
    git_max_cache_bytes = _env_positive_int(os.environ.get(LAB_TRACKER_GIT_CACHE_MAX_BYTES_ENV), 0)
    if http_deadline_seconds is None:
        raw_deadline = os.environ.get(LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS_ENV)
        try:
            http_deadline_seconds = (
                float(raw_deadline)
                if raw_deadline is not None
                else DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS
            )
        except ValueError:
            raise ValueError(
                f"{LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS_ENV} must be finite and positive."
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
    recovery_policy = recovery if recovery is not None else recovery_from_env()
    resolution_limits = (
        local_resolution_limits
        if local_resolution_limits is not None
        else LocalResolutionLimits(
            max_read_bytes=recovery_policy.max_bytes,
            deadline_seconds=subprocess_deadline_seconds,
        )
    )
    return default_registry(
        local_path_policy=local_path_policy,
        local_file_reader=local_file_reader,
        local_recovery_enumerator=local_recovery_enumerator,
        local_resolution_limits=resolution_limits,
        recovery=recovery_policy,
        http_policy=(http_policy if http_policy is not None else outbound_http_policy_from_env()),
        http_client=http_client,
        http_deadline_seconds=http_deadline_seconds,
        subprocess_deadline_seconds=subprocess_deadline_seconds,
        rclone_remote_policy=rclone_remote_policy,
        git_remote_policy=git_remote_policy,
        process_executor=process_executor,
        git_cache_root=git_cache_root,
        git_max_cache_bytes=git_max_cache_bytes or None,
    )
