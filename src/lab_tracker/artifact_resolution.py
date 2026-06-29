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
import mimetypes
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lab_tracker.models import DataStore, ExternalArtifactReference, StoreKind

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

_STREAM_CHUNK_SIZE = 1024 * 1024


class ResolutionStatus(str, Enum):
    """Outcome of an attempt to dereference an external artifact pointer."""

    VERIFIED = "verified"
    DRIFTED = "drifted"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ResolvedArtifact:
    """The result of resolving an :class:`ExternalArtifactReference`.

    ``content`` holds the bounded, returned bytes (``None`` when unresolved).
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
            "uri": self.uri,
            "expected_hash": self.expected_hash,
            "observed_hash": self.observed_hash,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "returned_bytes": self.returned_bytes,
            "truncated": self.truncated,
            "fetched_at": self.fetched_at.isoformat(),
            "detail": self.detail,
        }


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
    the deferred snapshot/query adapters). Credentials are never embedded — the
    rclone remote name comes from the store's ``credential_ref``.
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
    return None


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
    """

    def __init__(self, allowed_roots: Sequence[str | Path] | None = None) -> None:
        self._allowed_roots: list[str] | None
        if allowed_roots is None:
            self._allowed_roots = None
        else:
            self._allowed_roots = [
                os.path.realpath(Path(root).expanduser()) for root in allowed_roots
            ]

    def can_resolve(self, ref: ExternalArtifactReference) -> bool:
        if ref.source_system.strip().lower() in _LOCAL_SOURCE_SYSTEMS:
            return True
        return urlsplit(ref.uri).scheme.lower() == "file"

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

        path = self._local_path(ref.uri)
        if path is None:
            return _unresolved(ref, detail="Reference URI is not a local filesystem path.")

        real_path = os.path.realpath(path)
        if not self._within_allowed_roots(real_path):
            return _unresolved(ref, detail="Path is outside the allowed resolver roots.")
        if not os.path.isfile(real_path):
            return _unresolved(ref, detail="Local artifact not found.")

        algorithm, _ = parse_content_hash(ref.content_hash)
        try:
            content, total, truncated, observed = _hash_and_collect(
                _read_file_chunks(real_path),
                algorithm=algorithm,
                max_bytes=max_bytes,
                window=window,
            )
        except OSError as exc:
            return _unresolved(ref, detail=f"Failed to read local artifact: {exc}")

        content_type, _ = mimetypes.guess_type(real_path)
        return _build_resolved(
            ref,
            observed=observed,
            content=content,
            total=total,
            truncated=truncated,
            content_type=content_type or "application/octet-stream",
        )

    def _local_path(self, uri: str) -> str | None:
        parsed = urlsplit(uri)
        scheme = parsed.scheme.lower()
        if scheme == "file":
            # file:///abs/path -> /abs/path ; tolerate a localhost netloc.
            return unquote(parsed.path) or None
        if scheme == "":
            return uri or None
        return None

    def _within_allowed_roots(self, real_path: str) -> bool:
        if self._allowed_roots is None:
            return True
        return any(
            real_path == root or real_path.startswith(root + os.sep)
            for root in self._allowed_roots
        )


class HttpResolver(ArtifactResolver):
    """Resolves artifacts addressed by ``http(s)`` URLs.

    Streams the full body through the hasher to verify it, capping the fetch at
    ``max_fetch_bytes``; an artifact larger than that is refused as UNRESOLVED
    rather than returned uncertified. A custom ``client`` may be injected (e.g.
    for tests); otherwise a short-lived client is created per call.
    """

    def __init__(
        self,
        *,
        client: object | None = None,
        timeout: float = 30.0,
        max_fetch_bytes: int = DEFAULT_MAX_FETCH_BYTES,
    ) -> None:
        self._client = client
        self._timeout = timeout
        self._max_fetch_bytes = max_fetch_bytes

    def can_resolve(self, ref: ExternalArtifactReference) -> bool:
        return urlsplit(ref.uri).scheme.lower() in _HTTP_SCHEMES

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

        import httpx

        algorithm, _ = parse_content_hash(ref.content_hash)
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            with client.stream("GET", ref.uri) as response:
                if response.status_code >= 400:
                    return _unresolved(
                        ref, detail=f"HTTP {response.status_code} fetching artifact."
                    )
                content, total, truncated, observed = _hash_and_collect(
                    response.iter_bytes(),
                    algorithm=algorithm,
                    max_bytes=max_bytes,
                    window=window,
                    max_total=self._max_fetch_bytes,
                )
                content_type = response.headers.get("content-type")
        except _FetchTooLarge as exc:
            return _unresolved(ref, detail=str(exc))
        except httpx.HTTPError as exc:
            return _unresolved(ref, detail=f"Failed to fetch artifact: {exc}")
        finally:
            if owns_client:
                client.close()

        if content_type:
            content_type = content_type.split(";", 1)[0].strip()
        return _build_resolved(
            ref,
            observed=observed,
            content=content,
            total=total,
            truncated=truncated,
            content_type=content_type or "application/octet-stream",
        )


@dataclass(frozen=True)
class RcloneCompleted:
    """Result of one rclone invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes


# A runner takes rclone argv (without the binary name) and returns its result.
RcloneRunner = Callable[[list[str]], RcloneCompleted]


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
    """

    def __init__(
        self,
        *,
        runner: RcloneRunner | None = None,
        binary: str = "rclone",
        max_fetch_bytes: int = DEFAULT_MAX_FETCH_BYTES,
    ) -> None:
        self._runner = runner or _subprocess_rclone_runner(binary)
        self._max_fetch_bytes = max_fetch_bytes

    def can_resolve(self, ref: ExternalArtifactReference) -> bool:
        return urlsplit(ref.uri).scheme.lower() == "rclone"

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

        try:
            size = self._object_size(target)
            if size is None:
                return _unresolved(ref, detail="rclone could not stat the artifact.")
            if size > self._max_fetch_bytes:
                return _unresolved(
                    ref,
                    detail=(
                        f"Artifact ({size} bytes) exceeds the "
                        f"{self._max_fetch_bytes}-byte fetch limit."
                    ),
                )
            cat = self._runner(["cat", target])
        except OSError as exc:
            return _unresolved(ref, detail=f"rclone is unavailable: {exc}")

        if cat.returncode != 0:
            return _unresolved(ref, detail=_rclone_error_detail(cat))

        algorithm, _ = parse_content_hash(ref.content_hash)
        content, total, truncated, observed = _hash_and_collect(
            [cat.stdout],
            algorithm=algorithm,
            max_bytes=max_bytes,
            window=window,
        )
        content_type, _ = mimetypes.guess_type(target)
        return _build_resolved(
            ref,
            observed=observed,
            content=content,
            total=total,
            truncated=truncated,
            content_type=content_type or "application/octet-stream",
        )

    def _object_size(self, target: str) -> int | None:
        completed = self._runner(["size", "--json", target])
        if completed.returncode != 0:
            return None
        try:
            payload = json.loads(completed.stdout.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        size = payload.get("bytes")
        return size if isinstance(size, int) and size >= 0 else None

    def _rclone_target(self, uri: str) -> str | None:
        parsed = urlsplit(uri)
        if parsed.scheme.lower() != "rclone" or not parsed.netloc:
            return None
        path = unquote(parsed.path).lstrip("/")
        return f"{parsed.netloc}:{path}"


def _rclone_error_detail(completed: RcloneCompleted) -> str:
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    if stderr:
        return f"rclone failed: {stderr.splitlines()[-1]}"
    return f"rclone exited with status {completed.returncode}."


class _FetchTooLarge(Exception):
    """Raised when a streamed artifact exceeds the verification fetch cap."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"Artifact exceeds the {limit}-byte fetch limit.")
        self.limit = limit


def _hash_and_collect(
    chunks: Iterable[bytes],
    *,
    algorithm: str,
    max_bytes: int,
    window: tuple[int, int] | None,
    max_total: int | None = None,
) -> tuple[bytes, int, bool, str]:
    """Stream ``chunks`` through a hasher, collecting the bounded returned bytes.

    Returns ``(content, total_size, truncated, observed_hash)``. The whole stream
    is hashed; ``content`` is either the first ``max_bytes`` (no window) or the
    requested ``[start, end)`` slice. Raises :class:`_FetchTooLarge` if the total
    exceeds ``max_total`` (so an oversized artifact is refused rather than
    returned uncertified).
    """

    hasher = hashlib.new(algorithm)
    collected = bytearray()
    total = 0
    for chunk in chunks:
        if not chunk:
            continue
        hasher.update(chunk)
        chunk_start = total
        total += len(chunk)
        if max_total is not None and total > max_total:
            raise _FetchTooLarge(max_total)
        if window is None:
            if len(collected) < max_bytes:
                collected.extend(chunk[: max_bytes - len(collected)])
        else:
            start, end = window
            lo = max(start, chunk_start)
            hi = min(end, total)
            if hi > lo:
                collected.extend(chunk[lo - chunk_start : hi - chunk_start])

    observed = f"{algorithm}:{hasher.hexdigest()}"
    content = bytes(collected)
    truncated = len(content) < total
    return content, total, truncated, observed


def _read_file_chunks(path: str) -> Iterable[bytes]:
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_STREAM_CHUNK_SIZE)
            if not chunk:
                break
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
        content=content,
        truncated=truncated,
        fetched_at=_now(),
        detail=None if verified else "Recomputed hash does not match content_hash.",
    )


def default_registry(
    *,
    allowed_roots: Sequence[str | Path] | None = None,
) -> ResolverRegistry:
    """Build a registry with the adapters available in this slice.

    Local-filesystem, HTTP(S), and rclone resolvers. The rclone adapter degrades
    to UNRESOLVED when the binary is absent, so including it is safe by default.
    Native store-backed adapters (s3, ssh, database) register here as they land.
    """

    return ResolverRegistry(
        [
            LocalFilesystemResolver(allowed_roots=allowed_roots),
            HttpResolver(),
            RcloneResolver(),
        ]
    )


# os.pathsep-separated list of directories the local resolver may read. When
# unset, the local resolver is restricted to *no* roots, so filesystem artifacts
# resolve as UNRESOLVED until an operator opts specific roots in. HTTP(S)
# resolution is unaffected.
LAB_TRACKER_RESOLVER_ALLOWED_ROOTS_ENV = "LAB_TRACKER_RESOLVER_ALLOWED_ROOTS"


def registry_from_env() -> ResolverRegistry:
    """Build the default registry, reading allowed local roots from the env."""

    raw = os.environ.get(LAB_TRACKER_RESOLVER_ALLOWED_ROOTS_ENV)
    allowed_roots = (
        [part for part in raw.split(os.pathsep) if part.strip()] if raw else []
    )
    return default_registry(allowed_roots=allowed_roots)
