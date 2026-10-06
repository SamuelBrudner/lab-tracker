"""Registered-store scans: stage a pointer note for each new file under a prefix.

For each operator-declared scan ``{project_id, store, prefix, patterns}`` the
poller lists the registered store beneath the prefix and stages one pointer
note per new file: a ``store://<name>/<path>`` locator, size, modification
time, any provider hash the listing reports, and a SHA-256 only when the file
could be streamed through the existing bounded adapter under
``LAB_TRACKER_STORE_SCAN_HASH_MAX_BYTES`` (otherwise ``content_hash_pending``;
a SHA-256 is never guessed). Bytes are never copied into Lab Tracker.

Before any adapter exists, the dispatcher detaches the store's persisted
authority grant binding under the database scope, releases that scope, and
revalidates the binding against one operator registry snapshot
(:mod:`lab_tracker.store_authority_use`). Listing requires the grant's ``list``
capability and hashing its ``bytes_by_path`` capability; every adapter input
comes from the resulting use-time proof, never from the released store row.

* rclone-backed kinds: ``rclone lsjson`` and ``rclone cat`` through the bounded
  process executor and the exact ``LAB_TRACKER_RCLONE_ALLOWED_REMOTES`` policy,
  which stays a conjunctive outer ceiling on the revalidated grant.
* ``local_fs``: refused in this build with a static detail. The adapter below
  checks only the global ``LAB_TRACKER_RESOLVER_ALLOWED_ROOTS``, so it is not
  dispatched until the local-use slice retains the store's revalidated grant
  boundary inside enumeration and hashing.
* Other kinds (``http``, ``git``, ``object_table``, ``database``) cannot list
  and are reported as unsupported.

The first run of a scan against one registered store records what is already
there as a baseline and stages nothing (unless the scan sets
``include_existing``), so enabling a scan on a full store does not flood the
review inbox. Baselines are keyed by store ID, so a name that later resolves to
a different registration starts over rather than inheriting the old baseline; a
baseline recorded under the earlier name-only key is adopted once, by whichever
registration the name resolves to on the first authorized run, and removed.
Files still changing (modified in the last ``SETTLE_SECONDS``) wait for a later
poll. Notes are authored by the ``SYSTEM`` principal with
``capture_channel=store_scan``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.bounded_subprocess import (
    DEFAULT_PROCESS_STDERR_LIMIT_BYTES,
    ProcessDeadline,
    ProcessExecutionError,
    ProcessExecutor,
)
from lab_tracker.capture_channels.common import (
    CAPTURE_CHANNEL_KEY,
    CAPTURED_AT_KEY,
    EVIDENCE_ADAPTER_KEY,
    EVIDENCE_CAPTURE_KIND_KEY,
    EVIDENCE_CONTENT_HASH_KEY,
    EVIDENCE_SOURCE_EXTERNAL_ID_KEY,
    EVIDENCE_SOURCE_OBSERVED_AT_KEY,
    EVIDENCE_SOURCE_PROVIDER_KEY,
    EVIDENCE_SOURCE_URI_KEY,
    EVIDENCE_TITLE_KEY,
    bound_value,
    stable_key,
)
from lab_tracker.capture_channels.settings import StoreScan
from lab_tracker.data_store_definition import ValidatedDataStoreDefinition
from lab_tracker.local_filesystem_authority import LocalFilesystemAuthority
from lab_tracker.local_filesystem_operations import BoundedLocalFilesystemOperations
from lab_tracker.local_filesystem_ports import (
    LocalRegularFileReader,
    LocalRegularFileReadOutcome,
    RegisteredLocalRegularFileTarget,
)
from lab_tracker.local_resolution_budget import (
    MAX_LOCAL_RESOLUTION_MAX_READ_BYTES,
    LocalResolutionBudget,
    LocalResolutionLimits,
)
from lab_tracker.local_store_locator import (
    PortableStorePath,
    canonical_local_store_uri,
    canonical_store_uri,
)
from lab_tracker.models import (
    EntityOrigin,
    NoteMetadataScalar,
    NoteStatus,
    StoreCapability,
    StoreKind,
)
from lab_tracker.rclone_remote_policy import RcloneRemotePolicy
from lab_tracker.rclone_store_definition import (
    RegisteredRcloneStoreAddress,
    is_rclone_store_kind,
)
from lab_tracker.store_authority_use import StoreAuthorityUseProof

STORE_SCAN_CHANNEL: Final = "store_scan"
STORE_SCAN_ADAPTER: Final = "lab-tracker-store-scan"
MAX_LISTED_FILES: Final = 5_000
MAX_LISTED_DIRECTORIES: Final = 2_000
MAX_LIST_DEPTH: Final = 16
MAX_NEW_FILES_PER_SCAN: Final = 100
MAX_BASELINE_KEYS: Final = 20_000
SETTLE_SECONDS: Final = 120
HASH_BUDGET_SECONDS: Final = 300.0
RCLONE_LISTING_LIMIT_BYTES: Final = 8 * 1024 * 1024
# Backends whose listing reports a stored hash cheaply; asking an SFTP or a
# generic (possibly local) rclone remote for hashes would re-read every file.
_METADATA_HASH_KINDS: Final = frozenset(
    {
        StoreKind.S3,
        StoreKind.GCS,
        StoreKind.AZURE_BLOB,
        StoreKind.DROPBOX,
        StoreKind.GDRIVE,
        StoreKind.BOX,
        StoreKind.ONEDRIVE,
    }
)
_IGNORED_PREFIXES: Final = (".", "~$")
_IGNORED_SUFFIXES: Final = (".tmp", ".partial", ".part", ".crdownload")
_FRACTION_RE = re.compile(r"\.(\d+)")
_HASH_NAME_RE = re.compile(r"[a-z0-9_-]{1,32}\Z")


# One opaque detail for every authority denial (no binding, a revoked, changed,
# or scope-mismatched grant, or a grant without ``list``): it never says which
# check failed or discloses the store's target, grant, or credential handle.
STORE_SCAN_UNAUTHORIZED_MESSAGE: Final = (
    "The data store is not authorized for listing by a current operator grant."
)
LOCAL_STORE_SCAN_UNSUPPORTED_MESSAGE: Final = "Local store scans are not supported in this build."


class StoreScanError(RuntimeError):
    """A scan could not list its store; the message is static and path-free."""


@dataclass(frozen=True, slots=True)
class ListedFile:
    """One regular file beneath a registered store root."""

    locator: PortableStorePath
    size: int
    modified_at: datetime | None
    provider_hashes: tuple[tuple[str, str], ...] = ()

    def capture_key(self, store_name: str) -> str:
        modified = self.modified_at.isoformat() if self.modified_at else ""
        return stable_key("store", store_name, self.locator.path, str(self.size), modified)


@dataclass(frozen=True, slots=True)
class StoreListing:
    files: tuple[ListedFile, ...]
    truncated: bool


Include = Callable[[PortableStorePath], bool]


class StoreAdapter(Protocol):
    """List a store beneath a prefix and, when possible, hash one listed file.

    ``include`` is applied while listing, so only matching files count toward
    ``MAX_LISTED_FILES`` and non-matching clutter cannot hide a match.
    """

    def list(self, prefix: PortableStorePath | None, *, include: Include) -> StoreListing: ...

    def sha256(self, listed: ListedFile, *, max_bytes: int) -> str | None: ...


# --------------------------------------------------------------------------- local_fs


@dataclass(frozen=True, eq=False, repr=False)
class LocalStoreScanAccess:
    """The narrow local capability a scan holds: list and hash beneath a store root.

    It wraps the runtime's bounded local-filesystem broker so the broker itself
    is never published on the app state; a holder can only build a
    :class:`LocalStoreAdapter`, which re-checks the operator's local roots.
    Store scans do not dispatch it in this build: those roots are not the
    store's revalidated grant boundary.
    """

    _operations: BoundedLocalFilesystemOperations

    def adapter(self, root: str, *, deadline_seconds: float) -> LocalStoreAdapter:
        return LocalStoreAdapter(
            root=root,
            authority=self._operations.authority,
            reader=self._operations,
            deadline_seconds=deadline_seconds,
        )


@dataclass(frozen=True)
class LocalStoreAdapter:
    """List and hash a ``local_fs`` store confined to the operator's local roots."""

    root: str
    authority: LocalFilesystemAuthority
    reader: LocalRegularFileReader
    deadline_seconds: float

    def list(self, prefix: PortableStorePath | None, *, include: Include) -> StoreListing:
        if self.authority.select_directory(self.root) is None:
            raise StoreScanError("The store root is outside LAB_TRACKER_RESOLVER_ALLOWED_ROOTS.")
        real_root = os.path.realpath(self.root)
        if self.authority.select_directory(real_root) is None:
            raise StoreScanError(
                "The store root resolves outside LAB_TRACKER_RESOLVER_ALLOWED_ROOTS."
            )
        components = prefix.components if prefix is not None else ()
        start = os.path.realpath(os.path.join(self.root, *components))
        if start != real_root and not start.startswith(real_root.rstrip(os.sep) + os.sep):
            raise StoreScanError("The scan prefix resolves outside the store root.")
        if not os.path.isdir(start):
            return StoreListing((), False)
        if _FD_WALK_SUPPORTED:
            return _walk_local_fds(real_root, components, include)
        return _walk_local_paths(real_root, components, include)

    def sha256(self, listed: ListedFile, *, max_bytes: int) -> str | None:
        if listed.size > max_bytes or listed.size > MAX_LOCAL_RESOLUTION_MAX_READ_BYTES:
            return None
        digest = hashlib.sha256()
        try:
            result = self.reader.read_regular_file(
                RegisteredLocalRegularFileTarget(self.root, listed.locator.components),
                budget=LocalResolutionBudget(
                    LocalResolutionLimits(
                        max_read_bytes=max(1, listed.size),
                        deadline_seconds=self.deadline_seconds,
                    )
                ),
                stdout_consumer=digest.update,
            )
        except Exception:
            return None
        if result.outcome is not LocalRegularFileReadOutcome.COMPLETE:
            return None
        if result.bytes_read != listed.size:
            return None  # changed while being read; hash on a later poll
        return digest.hexdigest()


_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
# Descend with directory descriptors and O_NOFOLLOW where the platform allows:
# a directory swapped for a symlink mid-walk then fails to open instead of
# leading the listing outside the store.
_FD_WALK_SUPPORTED: Final = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.scandir in os.supports_fd
)


class _Walk:
    """Shared bookkeeping for both local walk strategies."""

    def __init__(self, include: Include) -> None:
        self.include = include
        self.files: list[ListedFile] = []
        self.directories = 0
        self.truncated = False

    def visit(self) -> bool:
        self.directories += 1
        if self.directories > MAX_LISTED_DIRECTORIES:
            self.truncated = True
            return False
        return True

    def add(self, components: tuple[str, ...], stat_result: os.stat_result) -> bool:
        """Record one regular file; return False once the listing is full."""

        locator = _locator(components)
        if locator is None or not self.include(locator):
            return True
        self.files.append(
            ListedFile(
                locator=locator,
                size=int(stat_result.st_size),
                modified_at=datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).replace(
                    microsecond=0
                ),
            )
        )
        if len(self.files) >= MAX_LISTED_FILES:
            self.truncated = True
            return False
        return True

    def listing(self) -> StoreListing:
        return StoreListing(tuple(self.files), self.truncated)


def _scan_entries(
    entries: list[os.DirEntry[str]],
    *,
    components: tuple[str, ...],
    depth: int,
    walk: _Walk,
    stack: list[tuple[tuple[str, ...], int]],
) -> bool:
    for entry in entries:
        if _ignored_name(entry.name):
            continue
        try:
            if entry.is_symlink():
                continue  # never follow an alias out of (or around) the store
            if entry.is_dir(follow_symlinks=False):
                if depth + 1 < MAX_LIST_DEPTH:
                    stack.append(((*components, entry.name), depth + 1))
                else:
                    walk.truncated = True
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            stat_result = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not walk.add((*components, entry.name), stat_result):
            return False
    return True


def _walk_local_fds(
    real_root: str, prefix_components: tuple[str, ...], include: Include
) -> StoreListing:
    walk = _Walk(include)
    root_fd = os.open(real_root, _DIRECTORY_FLAGS)
    try:
        stack: list[tuple[tuple[str, ...], int]] = [(prefix_components, 0)]
        while stack:
            components, depth = stack.pop()
            if not walk.visit():
                break
            try:
                directory_fd = _open_below(root_fd, components)
            except OSError:
                continue  # vanished, not a directory, or now a symlink
            keep_going = True
            try:
                with os.scandir(directory_fd) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
                    keep_going = _scan_entries(
                        entries, components=components, depth=depth, walk=walk, stack=stack
                    )
            except OSError:
                keep_going = True
            finally:
                os.close(directory_fd)
            if not keep_going:
                break
    finally:
        os.close(root_fd)
    return walk.listing()


def _open_below(root_fd: int, components: tuple[str, ...]) -> int:
    """Open ``root/components`` one no-follow step at a time from the root descriptor."""

    descriptor = os.dup(root_fd)
    try:
        for name in components:
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _walk_local_paths(
    real_root: str, prefix_components: tuple[str, ...], include: Include
) -> StoreListing:
    """Fallback without descriptor walking: re-check each directory just before listing it."""

    walk = _Walk(include)
    stack: list[tuple[tuple[str, ...], int]] = [(prefix_components, 0)]
    while stack:
        components, depth = stack.pop()
        if not walk.visit():
            break
        directory = os.path.join(real_root, *components)
        if not _is_contained_directory(directory, real_root):
            continue
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            continue
        if not _scan_entries(entries, components=components, depth=depth, walk=walk, stack=stack):
            break
    return walk.listing()


def _is_contained_directory(path: str, real_root: str) -> bool:
    try:
        stat_result = os.lstat(path)
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if not stat.S_ISDIR(stat_result.st_mode) or (
        getattr(stat_result, "st_file_attributes", 0) & reparse
    ):
        return False
    resolved = os.path.realpath(path)
    return resolved == real_root or resolved.startswith(real_root.rstrip(os.sep) + os.sep)


# --------------------------------------------------------------------------- rclone


@dataclass(frozen=True)
class RcloneStoreAdapter:
    """List and hash an rclone-backed store through the exact remote policy.

    Every remote, root, and kind input comes from one use-time authority proof,
    so the adapter can only address what the operator's grant currently covers.
    """

    authority: StoreAuthorityUseProof
    policy: RcloneRemotePolicy
    executor: ProcessExecutor
    deadline_seconds: float
    binary: str = "rclone"
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if type(self.authority) is not StoreAuthorityUseProof:
            raise TypeError("A store scan adapter requires a use-time authority proof.")

    def _address(self) -> tuple[RegisteredRcloneStoreAddress, Any]:
        definition = self.authority.definition
        address = RegisteredRcloneStoreAddress.parse(
            kind=definition.kind,
            name=definition.name,
            root=definition.root,
            credential_ref=definition.credential_ref,
        )
        if address is None:
            raise StoreScanError("The store's rclone remote or root is invalid.")
        remote = self.policy.authorize_name(address.remote)
        if remote is None:
            raise StoreScanError("The store's remote is not in LAB_TRACKER_RCLONE_ALLOWED_REMOTES.")
        return address, remote

    def list(self, prefix: PortableStorePath | None, *, include: Include) -> StoreListing:
        address, remote = self._address()
        target = (
            address.root.compose(remote, prefix)
            if prefix is not None
            else address.root.compose_root(remote)
        )
        if target is None:
            raise StoreScanError("The scan prefix does not fit beneath the store root.")
        argv = [
            self.binary,
            "lsjson",
            "--recursive",
            "--files-only",
            "--no-mimetype",
            "--max-depth",
            str(MAX_LIST_DEPTH),
        ]
        if self.authority.definition.kind in _METADATA_HASH_KINDS:
            argv.append("--hash")
        argv.append(target)
        try:
            result = self.executor.run(
                argv,
                deadline=ProcessDeadline.after(self.deadline_seconds, clock=self.clock),
                stdout_limit_bytes=RCLONE_LISTING_LIMIT_BYTES,
                stderr_limit_bytes=DEFAULT_PROCESS_STDERR_LIMIT_BYTES,
            )
        except ProcessExecutionError as exc:
            raise StoreScanError(
                "rclone listing failed, timed out, or exceeded its output limit."
            ) from exc
        if result.returncode != 0:
            raise StoreScanError("rclone listing failed.")
        return parse_rclone_listing(result.stdout, prefix=prefix, include=include)

    def sha256(self, listed: ListedFile, *, max_bytes: int) -> str | None:
        # Streaming bytes is a separate operation from listing: a grant without
        # ``bytes_by_path`` still lists, and its files stay hash-pending.
        if (
            listed.size > max_bytes
            or StoreCapability.BYTES_BY_PATH not in self.authority.capabilities
        ):
            return None
        try:
            address, remote = self._address()
        except StoreScanError:
            return None
        target = address.root.compose(remote, listed.locator)
        if target is None:
            return None
        digest = hashlib.sha256()
        try:
            result = self.executor.run(
                [self.binary, "cat", target],
                deadline=ProcessDeadline.after(self.deadline_seconds, clock=self.clock),
                stdout_limit_bytes=max(1, listed.size),
                stderr_limit_bytes=DEFAULT_PROCESS_STDERR_LIMIT_BYTES,
                stdout_consumer=digest.update,
            )
        except (ProcessExecutionError, OSError, ValueError):
            return None
        if result.returncode != 0 or result.stdout_bytes != listed.size:
            return None
        return digest.hexdigest()


def parse_rclone_listing(
    stdout: bytes,
    *,
    prefix: PortableStorePath | None,
    include: Include = lambda _locator: True,
) -> StoreListing:
    """Parse ``rclone lsjson`` output into locators relative to the store root."""

    try:
        payload = json.loads(stdout.decode("utf-8") or "[]")
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise StoreScanError("rclone listing was not valid JSON.") from exc
    if not isinstance(payload, list):
        raise StoreScanError("rclone listing was not a JSON list.")
    base = prefix.components if prefix is not None else ()
    files: list[ListedFile] = []
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("IsDir"):
            continue
        path, size = entry.get("Path"), entry.get("Size")
        if not isinstance(path, str) or type(size) is not int or size < 0:
            continue
        parts = tuple(path.split("/"))
        if _ignored_name(parts[-1]):
            continue
        locator = _locator((*base, *parts))
        if locator is None or not include(locator):
            continue
        files.append(
            ListedFile(
                locator=locator,
                size=size,
                modified_at=_rclone_time(entry.get("ModTime")),
                provider_hashes=_provider_hashes(entry.get("Hashes")),
            )
        )
        if len(files) >= MAX_LISTED_FILES:
            return StoreListing(tuple(files), True)
    return StoreListing(tuple(files), False)


def _rclone_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = _FRACTION_RE.sub(lambda match: "." + match.group(1)[:6].ljust(6, "0"), value, 1)
    normalized = normalized.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _provider_hashes(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    hashes = [
        (name.lower(), digest.lower())
        for name, digest in value.items()
        if isinstance(name, str)
        and isinstance(digest, str)
        and _HASH_NAME_RE.fullmatch(name.lower())
        and digest
        and len(digest) <= 128
        and digest.isalnum()
    ]
    return tuple(sorted(hashes)[:4])


# --------------------------------------------------------------------------- staging


class ScanApi(Protocol):
    """The slice of ``LabTrackerAPI`` a scan uses."""

    def find_note_by_client_capture_id(self, *args: Any, **kwargs: Any) -> Any: ...

    def create_note_result(self, *args: Any, **kwargs: Any) -> Any: ...


class BaselineStore(Protocol):
    """Where a scan remembers the files that existed when it was first enabled."""

    def store_scan_baseline(self, scan_key: str) -> frozenset[str] | None: ...

    def set_store_scan_baseline(self, scan_key: str, keys: frozenset[str]) -> None: ...

    def adopt_store_scan_baseline(
        self, legacy_key: str, scan_key: str
    ) -> frozenset[str] | None: ...


@dataclass
class StoreScanResult:
    listed: int = 0
    matched: int = 0
    baseline_recorded: int = 0
    created: int = 0
    already_captured: int = 0
    settling: int = 0
    deferred: int = 0
    hashed: int = 0
    hash_pending: int = 0
    truncated_listing: bool = False


def scan_key(scan: StoreScan, *, store_id: UUID) -> str:
    """The identity a scan's baseline is remembered under.

    It includes the exact registered store, so a name that later resolves to a
    different registration records a fresh baseline.
    """

    prefix = scan.prefix.path if scan.prefix is not None else ""
    return stable_key(
        "scan",
        str(scan.project_id),
        str(store_id),
        scan.store,
        prefix,
        "\x1e".join(scan.patterns),
    )


def _legacy_scan_key(scan: StoreScan) -> str:
    """The name-only baseline identity used before baselines named their store."""

    prefix = scan.prefix.path if scan.prefix is not None else ""
    return stable_key("scan", str(scan.project_id), scan.store, prefix, "\x1e".join(scan.patterns))


def run_store_scan(
    scan: StoreScan,
    *,
    authority: StoreAuthorityUseProof,
    adapter: StoreAdapter,
    api: ScanApi,
    actor: AuthContext,
    baselines: BaselineStore,
    now: datetime,
    hash_max_bytes: int,
    monotonic: Callable[[], float] = time.monotonic,
    expired: Callable[[], bool] = lambda: False,
) -> StoreScanResult:
    """List, match, baseline, and stage new files for one revalidated store."""

    store = authority.definition
    listing = adapter.list(scan.prefix, include=lambda locator: _matches(locator, scan))
    result = StoreScanResult(listed=len(listing.files), truncated_listing=listing.truncated)
    matched = list(listing.files)
    result.matched = len(matched)
    keys = {listed: listed.capture_key(store.name) for listed in matched}
    identity = scan_key(scan, store_id=authority.store_id)
    baseline = baselines.store_scan_baseline(identity)
    if baseline is None:
        # Upgrade without a capture gap: the first authorized run adopts (and
        # removes) the baseline recorded under the old name-only key.
        baseline = baselines.adopt_store_scan_baseline(_legacy_scan_key(scan), identity)
    if baseline is None:
        if not scan.include_existing:
            recorded = frozenset(sorted(keys.values())[:MAX_BASELINE_KEYS])
            baselines.set_store_scan_baseline(identity, recorded)
            result.baseline_recorded = len(recorded)
            return result
        baselines.set_store_scan_baseline(identity, frozenset())
        baseline = frozenset()
    candidates = sorted(
        (listed for listed in matched if keys[listed] not in baseline),
        key=lambda listed: (listed.modified_at or now, listed.locator.path),
    )
    hash_deadline = monotonic() + HASH_BUDGET_SECONDS
    for listed in candidates:
        if listed.modified_at is not None and (now - listed.modified_at).total_seconds() < (
            SETTLE_SECONDS
        ):
            result.settling += 1
            continue
        key = keys[listed]
        if api.find_note_by_client_capture_id(scan.project_id, key, actor=actor) is not None:
            result.already_captured += 1
            continue
        if result.created >= MAX_NEW_FILES_PER_SCAN or expired():
            result.deferred += 1
            continue
        digest = (
            adapter.sha256(listed, max_bytes=hash_max_bytes)
            if hash_max_bytes > 0 and monotonic() < hash_deadline
            else None
        )
        if digest is None:
            result.hash_pending += 1
        else:
            result.hashed += 1
        api.create_note_result(
            project_id=scan.project_id,
            raw_content=_pointer_text(store, listed, digest=digest, hash_max_bytes=hash_max_bytes),
            metadata=_pointer_metadata(store, listed, digest=digest, now=now),
            client_capture_id=key,
            status=NoteStatus.STAGED,
            actor=actor,
            origin=EntityOrigin.USER,
            origin_provider=STORE_SCAN_CHANNEL,
        )
        result.created += 1
    return result


def store_uri(store: ValidatedDataStoreDefinition, locator: PortableStorePath) -> str:
    """The canonical ``store://<name>/<path>`` pointer for a listed file."""

    uri = (
        canonical_local_store_uri(store.name, locator)
        if store.kind is StoreKind.LOCAL_FS
        else canonical_store_uri(store.name, locator)
    )
    return uri or f"store://{store.name}/{locator.uri_path}"


def adapter_supports_listing(kind: StoreKind) -> bool:
    return kind is StoreKind.LOCAL_FS or is_rclone_store_kind(kind)


def _pointer_metadata(
    store: ValidatedDataStoreDefinition,
    listed: ListedFile,
    *,
    digest: str | None,
    now: datetime,
) -> dict[str, NoteMetadataScalar]:
    uri = store_uri(store, listed.locator)
    metadata: dict[str, NoteMetadataScalar] = {
        CAPTURE_CHANNEL_KEY: STORE_SCAN_CHANNEL,
        EVIDENCE_SOURCE_PROVIDER_KEY: "data_store",
        EVIDENCE_ADAPTER_KEY: STORE_SCAN_ADAPTER,
        EVIDENCE_CAPTURE_KIND_KEY: "file",
        EVIDENCE_SOURCE_URI_KEY: uri,
        EVIDENCE_SOURCE_EXTERNAL_ID_KEY: bound_value(listed.locator.path),
        EVIDENCE_SOURCE_OBSERVED_AT_KEY: now.astimezone(timezone.utc).isoformat(),
        EVIDENCE_TITLE_KEY: bound_value(listed.locator.components[-1], limit=200),
        "store_scan_store": store.name,
        "store_scan_store_kind": store.kind.value,
        "store_file_path": bound_value(listed.locator.path),
        "store_file_size_bytes": listed.size,
    }
    if listed.modified_at is not None:
        metadata["store_file_modified_at"] = listed.modified_at.isoformat()
        # The file's own clock places it in time (sessions, day windows), not the poll.
        metadata[CAPTURED_AT_KEY] = listed.modified_at.isoformat()
    for name, value in listed.provider_hashes:
        metadata[f"store_file_provider_hash_{name}"] = value
    if digest is not None:
        metadata[EVIDENCE_CONTENT_HASH_KEY] = digest
    else:
        metadata["content_hash_pending"] = True
    return metadata


def _pointer_text(
    store: ValidatedDataStoreDefinition,
    listed: ListedFile,
    *,
    digest: str | None,
    hash_max_bytes: int,
) -> str:
    modified = listed.modified_at.isoformat() if listed.modified_at else "unknown"
    lines = [
        f"New file in data store {store.name}: {listed.locator.path}",
        f"{listed.size} bytes, modified {modified}",
        f"Pointer: {store_uri(store, listed.locator)}",
    ]
    if digest is not None:
        lines.append(f"sha256: {digest}")
    else:
        lines.append(
            "Content hash pending: the scan did not stream this file "
            f"(larger than {hash_max_bytes} bytes, unreadable, or out of hashing time)."
        )
    lines.append("Staged by the Lab Tracker store scan; the bytes stay in the store.")
    return "\n".join(lines)


def _matches(locator: PortableStorePath, scan: StoreScan) -> bool:
    prefix_length = len(scan.prefix.components) if scan.prefix is not None else 0
    relative = "/".join(locator.components[prefix_length:]).lower()
    name = locator.components[-1].lower()
    return any(
        fnmatch.fnmatchcase(name, pattern.lower()) or fnmatch.fnmatchcase(relative, pattern.lower())
        for pattern in scan.patterns
    )


def _ignored_name(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith(_IGNORED_PREFIXES) or lowered.endswith(_IGNORED_SUFFIXES)


def _locator(components: tuple[str, ...]) -> PortableStorePath | None:
    try:
        return PortableStorePath(components)
    except (TypeError, ValueError):
        return None
