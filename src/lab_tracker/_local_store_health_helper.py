"""Bounded, pre-follow POSIX operations for local store health and reads.

The application executes this file directly with ``python -I -S -B``.  Keep
the module self-contained and standard-library-only. Directory inspection is
output-free. Successful file reads emit only the exact raw artifact bytes.
Successful recovery enumeration emits one bounded canonical ASCII JSON value
after cleanup; every operation also returns one fixed process status.

Configured roots are trusted, operator-owned bootstrap paths.  The helper may
follow a configured root while opening its retained anchor, but it never opens
a candidate pathname.  Candidate components and symbolic links are resolved
relative to retained directory descriptors.  An absolute link must name the
same selected lexical grant before the helper performs target-side work.
Mounts visible below the configured root are intentionally eligible; device
identity is not an authorization boundary.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

LOCAL_FILESYSTEM_REQUEST_ENV = "LAB_TRACKER_INTERNAL_LOCAL_FILESYSTEM_REQUEST"
LOCAL_FILESYSTEM_PROTOCOL_VERSION = 1
INSPECT_DIRECTORY_OPERATION = "inspect-directory"
READ_FILE_OPERATION = "read-file"
READ_REGISTERED_FILE_OPERATION = "read-registered-file"
ENUMERATE_FILES_OPERATION = "enumerate-files"
ENUMERATE_REGISTERED_FILES_OPERATION = "enumerate-registered-files"

COMPLETE_EXIT = 0
ACCESSIBLE_EXIT = COMPLETE_EXIT
DENIED_EXIT = 2
FAILED_EXIT = 3
MISSING_EXIT = 4

MAX_LOCAL_FILESYSTEM_REQUEST_BYTES = 24 * 1024
MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS = 64
MAX_LOCAL_FILESYSTEM_READ_BYTES = 512 * 1024 * 1024
MAX_LOCAL_FILESYSTEM_ENUMERATION_ITEMS = 4_096
MAX_LOCAL_FILESYSTEM_ENUMERATION_METADATA_BYTES = 8 * 1024 * 1024

_MAX_PATH_BYTES = 16 * 1024
_MAX_COMPONENT_BYTES = 255
_MAX_COMPONENTS = 4_096
_MAX_RESOLUTION_STEPS = 8_192
_MAX_SYMLINKS = 40
_MAX_SYMLINK_TARGET_BYTES = 16 * 1024
_READ_CHUNK_BYTES = 64 * 1024
# Darwin's public sys/fcntl.h defines O_EXEC as this stable ABI bit. Some
# setup-python builds omit both O_EXEC and O_SEARCH from the os module even
# though the running kernel supports them.
_DARWIN_O_EXEC = 0x40000000

_DENIED_ERRNOS = frozenset(
    error
    for error in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EPERM", None),
        getattr(errno, "ENOENT", None),
        getattr(errno, "ENOTDIR", None),
        getattr(errno, "ELOOP", None),
        getattr(errno, "ENAMETOOLONG", None),
        getattr(errno, "ESTALE", None),
        getattr(errno, "ETIMEDOUT", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENODEV", None),
    )
    if isinstance(error, int)
)


class _Denied(Exception):
    """The request cannot be satisfied within its selected grant."""


class _Missing(Exception):
    """The requested leaf was cleanly absent before any artifact bytes were read."""


class _Failed(Exception):
    """The helper could not safely complete a valid request."""


@dataclass(frozen=True, slots=True)
class _Request:
    candidate: str
    roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DirectReadRequest:
    candidate: str
    roots: tuple[str, ...]
    max_bytes: int


@dataclass(frozen=True, slots=True)
class _RegisteredReadRequest:
    store_root: str
    locator: tuple[str, ...]
    roots: tuple[str, ...]
    max_bytes: int


@dataclass(frozen=True, slots=True)
class _EnumerationRequest:
    roots: tuple[str, ...]
    target_name: str | None
    max_files: int
    max_directories: int


@dataclass(frozen=True, slots=True)
class _RegisteredEnumerationRequest:
    store_root: str
    roots: tuple[str, ...]
    target_name: str | None
    max_files: int
    max_directories: int


@dataclass(frozen=True, slots=True)
class _SelectedPath:
    root: str
    root_components: tuple[str, ...]
    candidate_components: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    file_type: int
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class _EnumerationBoundary:
    fd: int
    components: tuple[str, ...]
    spellings: frozenset[tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _DirectoryWork:
    root_index: int
    locator: tuple[str, ...]
    expected_identity: tuple[int, int] | None
    encoded_bytes: int


@dataclass(frozen=True, slots=True)
class _EnumerationCandidate:
    root_index: int
    locator: tuple[str, ...]
    identity: tuple[int, int]
    encoded_bytes: int


@dataclass(slots=True)
class _EnumerationState:
    target_name: str | None
    max_files: int
    max_directories: int
    directories: int
    visited_directories: set[tuple[int, int]]
    preferred: list[_EnumerationCandidate]
    fallback: list[_EnumerationCandidate]
    retained_file_identities: dict[tuple[int, int], _EnumerationCandidate]
    candidate_encoded_bytes: int
    queued_directory_encoded_bytes: int
    candidate_limited: bool
    directory_limited: bool


class _StaticEnumerationSkip(Exception):
    """A directory entry is statically outside scope, cyclic, or ineligible."""


class _ScandirIterator(Protocol):
    def __iter__(self) -> Iterator[os.DirEntry[str]]: ...

    def close(self) -> None: ...


def _directory_open_flags(*, follow_symlinks: bool = False) -> int | None:
    directory_mode = getattr(os, "O_DIRECTORY", None)
    cloexec_mode = getattr(os, "O_CLOEXEC", None)
    nofollow_mode = getattr(os, "O_NOFOLLOW", None)
    if (
        not isinstance(directory_mode, int)
        or not isinstance(cloexec_mode, int)
        or (not follow_symlinks and not isinstance(nofollow_mode, int))
    ):
        return None

    search_mode = getattr(os, "O_SEARCH", None)
    path_mode = getattr(os, "O_PATH", None)
    execute_mode = getattr(os, "O_EXEC", None)
    if isinstance(search_mode, int):
        access_mode = search_mode
    elif isinstance(path_mode, int):
        access_mode = path_mode
    elif isinstance(execute_mode, int):
        access_mode = execute_mode
    elif sys.platform == "darwin":
        access_mode = _DARWIN_O_EXEC
    else:
        return None

    flags = int(access_mode) | directory_mode | cloexec_mode
    if not follow_symlinks:
        assert isinstance(nofollow_mode, int)
        flags |= nofollow_mode
    return flags


def _enumerable_directory_open_flags(*, follow_symlinks: bool = False) -> int | None:
    directory_mode = getattr(os, "O_DIRECTORY", None)
    cloexec_mode = getattr(os, "O_CLOEXEC", None)
    nofollow_mode = getattr(os, "O_NOFOLLOW", None)
    readonly_mode = getattr(os, "O_RDONLY", None)
    if (
        not isinstance(directory_mode, int)
        or not isinstance(cloexec_mode, int)
        or not isinstance(readonly_mode, int)
        or (not follow_symlinks and not isinstance(nofollow_mode, int))
    ):
        return None

    flags = readonly_mode | directory_mode | cloexec_mode
    if not follow_symlinks:
        assert isinstance(nofollow_mode, int)
        flags |= nofollow_mode
    return flags


def _regular_file_open_flags() -> int | None:
    cloexec_mode = getattr(os, "O_CLOEXEC", None)
    nofollow_mode = getattr(os, "O_NOFOLLOW", None)
    nonblocking_mode = getattr(os, "O_NONBLOCK", None)
    readonly_mode = getattr(os, "O_RDONLY", None)
    if (
        not isinstance(cloexec_mode, int)
        or not isinstance(nofollow_mode, int)
        or not isinstance(nonblocking_mode, int)
        or not isinstance(readonly_mode, int)
    ):
        return None
    return readonly_mode | cloexec_mode | nofollow_mode | nonblocking_mode


def _canonical_request_payload(request: object) -> str:
    return json.dumps(
        request,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_request(
    environment: Mapping[str, str],
) -> (
    _Request
    | _DirectReadRequest
    | _RegisteredReadRequest
    | _EnumerationRequest
    | _RegisteredEnumerationRequest
):
    raw = environment.get(LOCAL_FILESYSTEM_REQUEST_ENV)
    if not isinstance(raw, str):
        raise _Failed
    try:
        encoded = raw.encode("ascii")
    except (UnicodeEncodeError, MemoryError) as exc:
        raise _Failed from exc
    if not encoded or len(encoded) > MAX_LOCAL_FILESYSTEM_REQUEST_BYTES:
        raise _Failed

    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError, MemoryError) as exc:
        raise _Failed from exc
    if not isinstance(value, dict):
        raise _Failed
    try:
        canonical = _canonical_request_payload(value)
    except (TypeError, ValueError, RecursionError, MemoryError) as exc:
        raise _Failed from exc
    if canonical != raw:
        raise _Failed

    version = value.get("v")
    operation = value.get("op")
    if (
        type(version) is not int
        or version != LOCAL_FILESYSTEM_PROTOCOL_VERSION
        or type(operation) is not str
    ):
        raise _Failed

    if operation == INSPECT_DIRECTORY_OPERATION:
        if set(value) != {"candidate", "op", "roots", "v"}:
            raise _Failed
        candidate = value["candidate"]
        roots = value["roots"]
        if type(candidate) is not str:
            raise _Failed
        normalized_roots = _validate_roots(roots, exact_one=False)
        _validate_candidate(candidate)
        return _Request(candidate=candidate, roots=normalized_roots)

    if operation == READ_FILE_OPERATION:
        if set(value) != {"candidate", "max_bytes", "op", "roots", "v"}:
            raise _Failed
        candidate = value["candidate"]
        roots = value["roots"]
        max_bytes = value["max_bytes"]
        if type(candidate) is not str:
            raise _Failed
        _validate_candidate(candidate)
        normalized_roots = _validate_roots(roots, exact_one=True)
        return _DirectReadRequest(
            candidate=candidate,
            roots=normalized_roots,
            max_bytes=_validate_max_bytes(max_bytes),
        )

    if operation == READ_REGISTERED_FILE_OPERATION:
        if set(value) != {
            "locator",
            "max_bytes",
            "op",
            "roots",
            "store_root",
            "v",
        }:
            raise _Failed
        store_root = value["store_root"]
        locator = value["locator"]
        roots = value["roots"]
        max_bytes = value["max_bytes"]
        if type(store_root) is not str:
            raise _Failed
        _validate_candidate(store_root)
        return _RegisteredReadRequest(
            store_root=store_root,
            locator=_validate_locator(locator),
            roots=_validate_roots(roots, exact_one=True),
            max_bytes=_validate_max_bytes(max_bytes),
        )

    if operation == ENUMERATE_FILES_OPERATION:
        if set(value) != {
            "max_directories",
            "max_files",
            "op",
            "roots",
            "target_name",
            "v",
        }:
            raise _Failed
        roots = _validate_roots(value["roots"], exact_one=False)
        if not roots:
            raise _Failed
        return _EnumerationRequest(
            roots=roots,
            target_name=_validate_target_name(value["target_name"]),
            max_files=_validate_enumeration_limit(value["max_files"]),
            max_directories=_validate_enumeration_limit(value["max_directories"]),
        )

    if operation == ENUMERATE_REGISTERED_FILES_OPERATION:
        if set(value) != {
            "max_directories",
            "max_files",
            "op",
            "roots",
            "store_root",
            "target_name",
            "v",
        }:
            raise _Failed
        store_root = value["store_root"]
        if type(store_root) is not str:
            raise _Failed
        _validate_candidate(store_root)
        return _RegisteredEnumerationRequest(
            store_root=store_root,
            roots=_validate_roots(value["roots"], exact_one=True),
            target_name=_validate_target_name(value["target_name"]),
            max_files=_validate_enumeration_limit(value["max_files"]),
            max_directories=_validate_enumeration_limit(value["max_directories"]),
        )

    raise _Failed


def _validate_roots(value: object, *, exact_one: bool) -> tuple[str, ...]:
    if (
        type(value) is not list
        or len(value) > MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS
        or (exact_one and len(value) != 1)
        or any(type(root) is not str for root in value)
    ):
        raise _Failed
    normalized_roots: list[str] = []
    for root in value:
        assert isinstance(root, str)
        _normalized_root_components(root)
        if root in normalized_roots:
            raise _Failed
        normalized_roots.append(root)
    return tuple(normalized_roots)


def _validate_max_bytes(value: object) -> int:
    if (
        type(value) is not int
        or value < 1
        or value > MAX_LOCAL_FILESYSTEM_READ_BYTES
    ):
        raise _Failed
    return value


def _validate_enumeration_limit(value: object) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > MAX_LOCAL_FILESYSTEM_ENUMERATION_ITEMS
    ):
        raise _Failed
    return value


def _validate_target_name(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or value in ("", ".", "..")
        or "/" in value
        or "\0" in value
        or _path_bytes(value) > _MAX_COMPONENT_BYTES
    ):
        raise _Failed
    return value


def _validate_locator(value: object) -> tuple[str, ...]:
    if (
        type(value) is not list
        or not value
        or len(value) > _MAX_COMPONENTS
        or any(
            type(component) is not str
            or component in ("", ".", "..")
            or "/" in component
            or "\0" in component
            or _path_bytes(component) > _MAX_COMPONENT_BYTES
            for component in value
        )
    ):
        raise _Failed
    return tuple(value)


def _path_bytes(path: str) -> int:
    try:
        return len(os.fsencode(path))
    except (UnicodeEncodeError, LookupError, MemoryError) as exc:
        raise _Failed from exc


def _has_forbidden_characters(path: str) -> bool:
    return any(
        character == "\0"
        or ord(character) < 32
        or ord(character) == 127
        or (
            0xD800 <= ord(character) <= 0xDFFF
            and not 0xDC80 <= ord(character) <= 0xDCFF
        )
        for character in path
    )


def _validate_absolute_path(path: str) -> None:
    if (
        not path
        or not path.startswith("/")
        or path.startswith("//")
        or _has_forbidden_characters(path)
        or _path_bytes(path) > _MAX_PATH_BYTES
    ):
        raise _Failed


def _normalized_root_components(path: str) -> tuple[str, ...]:
    _validate_absolute_path(path)
    if path == "/":
        return ()
    components = tuple(path[1:].split("/"))
    if (
        len(components) > _MAX_COMPONENTS
        or any(component in ("", ".", "..") for component in components)
        or any(_path_bytes(component) > _MAX_COMPONENT_BYTES for component in components)
    ):
        raise _Failed
    return components


def _validate_candidate(path: str) -> None:
    _validate_absolute_path(path)
    components = path[1:].split("/")
    if (
        len(components) > _MAX_COMPONENTS
        or any(
            component not in ("", ".", "..")
            and _path_bytes(component) > _MAX_COMPONENT_BYTES
            for component in components
        )
    ):
        raise _Failed


def _resolution_components(path: str, *, absolute: bool) -> tuple[str, ...]:
    if _has_forbidden_characters(path) or _path_bytes(path) > _MAX_SYMLINK_TARGET_BYTES:
        raise _Denied
    if absolute:
        if not path.startswith("/") or path.startswith("//"):
            raise _Denied
        raw_components = path[1:].split("/")
    else:
        if path.startswith("/"):
            raise _Denied
        raw_components = path.split("/")
    components = tuple(
        component for component in raw_components if component not in ("", ".")
    )
    if len(components) > _MAX_COMPONENTS or any(
        component != ".." and _path_bytes(component) > _MAX_COMPONENT_BYTES
        for component in components
    ):
        raise _Denied
    return components


def _select_path(request: _Request) -> _SelectedPath:
    candidate_components = _resolution_components(
        request.candidate,
        absolute=True,
    )
    selected: tuple[str, tuple[str, ...]] | None = None
    for root in request.roots:
        root_components = _normalized_root_components(root)
        prefix_length = len(root_components)
        if candidate_components[:prefix_length] != root_components:
            continue
        if selected is None or prefix_length > len(selected[1]):
            selected = (root, root_components)
    if selected is None:
        raise _Denied
    root, root_components = selected
    candidate_suffix = candidate_components[len(root_components) :]
    if candidate_suffix[:1] == ("..",):
        raise _Denied
    return _SelectedPath(
        root=root,
        root_components=root_components,
        candidate_components=candidate_suffix,
    )


def _as_operation_error(
    exc: OSError,
    *,
    race_safe_invalid: bool = False,
    missing: bool = False,
) -> Exception:
    if missing and exc.errno == getattr(errno, "ENOENT", object()):
        return _Missing()
    if exc.errno in _DENIED_ERRNOS or (
        race_safe_invalid and exc.errno == getattr(errno, "EINVAL", object())
    ):
        return _Denied()
    return _Failed()


def _open_directory(
    path: str,
    flags: int,
    *,
    dir_fd: int | None = None,
    missing: bool = False,
) -> int:
    try:
        fd = (
            os.open(path, flags)
            if dir_fd is None
            else os.open(path, flags, dir_fd=dir_fd)
        )
    except OSError as exc:
        raise _as_operation_error(exc, missing=missing) from None
    except BaseException as exc:
        raise _Failed from exc
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
        raise _Failed
    return fd


def _open_regular_file(parent: int, component: str, flags: int) -> int:
    try:
        fd = os.open(component, flags, dir_fd=parent)
    except OSError as exc:
        raise _as_operation_error(exc, missing=True) from None
    except BaseException as exc:
        raise _Failed from exc
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
        raise _Failed
    return fd


def _track_descriptor(owned: list[int], fd: int) -> None:
    try:
        owned.append(fd)
    except BaseException as exc:
        with suppress(BaseException):
            os.close(fd)
        raise _Failed from exc


def _close_descriptor(owned: list[int], fd: int) -> None:
    try:
        owned.remove(fd)
    except (ValueError, MemoryError) as exc:
        raise _Failed from exc
    try:
        os.close(fd)
    except BaseException as exc:
        raise _Failed from exc


def _close_all(owned: list[int]) -> bool:
    clean = True
    while owned:
        fd = owned.pop()
        try:
            os.close(fd)
        except BaseException:
            clean = False
    return clean


def _is_searchable_directory_descriptor(fd: int) -> None:
    try:
        metadata = os.fstat(fd)
        searchable = os.access(
            ".",
            os.X_OK,
            dir_fd=fd,
            effective_ids=True,
        )
    except OSError as exc:
        raise _as_operation_error(exc) from None
    except BaseException as exc:
        raise _Failed from exc
    if not stat.S_ISDIR(metadata.st_mode) or not searchable:
        raise _Denied


def _lstat_component(
    parent: int,
    component: str,
    *,
    missing: bool = False,
) -> os.stat_result:
    try:
        return os.stat(component, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise _as_operation_error(exc, missing=missing) from None
    except BaseException as exc:
        raise _Failed from exc


def _readlink_component(
    parent: int,
    component: str,
    *,
    missing: bool = False,
) -> str:
    try:
        target = os.readlink(component, dir_fd=parent)
    except OSError as exc:
        raise _as_operation_error(
            exc,
            race_safe_invalid=True,
            missing=missing,
        ) from None
    except BaseException as exc:
        raise _Failed from exc
    if type(target) is not str:
        raise _Failed
    return target


def _bound_resolved_root_components(
    configured_root: str,
    root_fd: int,
) -> tuple[str, ...]:
    """Bind the trusted root's resolved spelling to its retained descriptor."""

    try:
        resolved_root = os.path.realpath(configured_root)
        retained_metadata = os.fstat(root_fd)
        resolved_metadata = os.stat(resolved_root)
    except OSError as exc:
        raise _as_operation_error(exc) from None
    except BaseException as exc:
        raise _Failed from exc
    resolved_components = _normalized_root_components(resolved_root)
    # Device/inode equality binds two spellings to this one retained root
    # object. It is not applied to descendants and is not a mount boundary.
    if (
        not stat.S_ISDIR(retained_metadata.st_mode)
        or not stat.S_ISDIR(resolved_metadata.st_mode)
        or retained_metadata.st_dev != resolved_metadata.st_dev
        or retained_metadata.st_ino != resolved_metadata.st_ino
    ):
        raise _Denied
    return resolved_components


def _reset_to_boundary(
    owned: list[int],
    stack: list[int],
    stack_components: list[str],
    *,
    boundary_depth: int,
    boundary_components: tuple[str, ...],
) -> None:
    while len(stack) > boundary_depth:
        child = stack.pop()
        _close_descriptor(owned, child)
        stack_components.pop()
    try:
        stack_components[:] = boundary_components
    except BaseException as exc:
        raise _Failed from exc


def _walk_components(
    *,
    owned: list[int],
    stack: list[int],
    stack_components: list[str],
    components: tuple[str, ...],
    boundary_depth: int,
    boundary_components: tuple[str, ...],
    root_spellings: set[tuple[str, ...]],
    directory_flags: int,
    regular_file_flags: int | None,
    missing: bool,
) -> int | None:
    """Resolve components beneath a retained descriptor boundary.

    ``regular_file_flags`` selects a regular-file leaf.  Otherwise every
    component, including the final one, must be a searchable directory.
    """

    try:
        pending = deque(components)
    except BaseException as exc:
        raise _Failed from exc
    steps = 0
    symlinks = 0
    while pending:
        steps += 1
        if steps > _MAX_RESOLUTION_STEPS or len(pending) > _MAX_COMPONENTS:
            raise _Denied
        component = pending.popleft()
        if component in ("", "."):
            continue
        if component == "..":
            if len(stack) == boundary_depth:
                raise _Denied
            child = stack.pop()
            _close_descriptor(owned, child)
            try:
                stack_components.pop()
            except BaseException as exc:
                raise _Failed from exc
            continue

        metadata = _lstat_component(stack[-1], component, missing=missing)
        if stat.S_ISLNK(metadata.st_mode):
            symlinks += 1
            if symlinks > _MAX_SYMLINKS:
                raise _Denied
            target = _readlink_component(stack[-1], component, missing=missing)
            absolute = target.startswith("/")
            target_components = _resolution_components(
                target,
                absolute=absolute,
            )
            if absolute:
                matching_prefixes = tuple(
                    root_components
                    for root_components in root_spellings
                    if target_components[: len(root_components)] == root_components
                )
                if not matching_prefixes:
                    raise _Denied
                prefix_length = max(map(len, matching_prefixes))
                _reset_to_boundary(
                    owned,
                    stack,
                    stack_components,
                    boundary_depth=boundary_depth,
                    boundary_components=boundary_components,
                )
                target_components = target_components[prefix_length:]
            if len(target_components) + len(pending) > _MAX_COMPONENTS:
                raise _Denied
            try:
                pending.extendleft(reversed(target_components))
            except BaseException as exc:
                raise _Failed from exc
            continue

        final_component = not pending
        if final_component and regular_file_flags is not None:
            if not stat.S_ISREG(metadata.st_mode):
                raise _Denied
            leaf = _open_regular_file(stack[-1], component, regular_file_flags)
            _track_descriptor(owned, leaf)
            return leaf

        if not stat.S_ISDIR(metadata.st_mode):
            raise _Denied
        child = _open_directory(
            component,
            directory_flags,
            dir_fd=stack[-1],
            missing=missing,
        )
        _track_descriptor(owned, child)
        stack.append(child)
        try:
            stack_components.append(component)
        except BaseException as exc:
            raise _Failed from exc
        _is_searchable_directory_descriptor(child)
    return None


def _lexically_normalized_absolute_components(path: str) -> tuple[str, ...]:
    components = _resolution_components(path, absolute=True)
    normalized: list[str] = []
    for component in components:
        if component == "..":
            if not normalized:
                raise _Denied
            normalized.pop()
        else:
            normalized.append(component)
    return tuple(normalized)


def _directory_identity_from_metadata(
    metadata: os.stat_result,
) -> tuple[int, int]:
    try:
        mode = metadata.st_mode
        device = metadata.st_dev
        inode = metadata.st_ino
    except BaseException as exc:
        raise _Failed from exc
    if (
        type(mode) is not int
        or type(device) is not int
        or type(inode) is not int
        or device < 0
        or inode < 0
        or not stat.S_ISDIR(mode)
    ):
        raise _Failed
    return device, inode


def _directory_descriptor_identity(fd: int) -> tuple[int, int]:
    try:
        metadata = os.fstat(fd)
    except BaseException as exc:
        raise _Failed from exc
    return _directory_identity_from_metadata(metadata)


def _symlink_identity(metadata: os.stat_result) -> tuple[int, int]:
    try:
        mode = metadata.st_mode
        device = metadata.st_dev
        inode = metadata.st_ino
    except BaseException as exc:
        raise _Failed from exc
    if (
        type(mode) is not int
        or type(device) is not int
        or type(inode) is not int
        or device < 0
        or inode < 0
        or not stat.S_ISLNK(mode)
    ):
        raise _Failed
    return device, inode


def _regular_identity_from_metadata(
    metadata: os.stat_result,
) -> tuple[int, int]:
    try:
        mode = metadata.st_mode
        device = metadata.st_dev
        inode = metadata.st_ino
    except BaseException as exc:
        raise _Failed from exc
    if (
        type(mode) is not int
        or type(device) is not int
        or type(inode) is not int
        or device < 0
        or inode < 0
        or not stat.S_ISREG(mode)
    ):
        raise _Failed
    return device, inode


def _open_enumeration_boundary(
    root: str,
    configured_components: tuple[str, ...],
    owned: list[int],
) -> _EnumerationBoundary:
    root_flags = _enumerable_directory_open_flags(follow_symlinks=True)
    if root_flags is None:
        raise _Failed
    root_fd = _open_directory(root, root_flags)
    _track_descriptor(owned, root_fd)
    _directory_descriptor_identity(root_fd)
    resolved_components = _bound_resolved_root_components(root, root_fd)
    try:
        spellings = frozenset((configured_components, resolved_components))
    except BaseException as exc:
        raise _Failed from exc
    return _EnumerationBoundary(
        fd=root_fd,
        components=resolved_components,
        spellings=spellings,
    )


def _replace_enumeration_descriptor(
    *,
    owned: list[int],
    current: int,
    replacement: int,
) -> int:
    _close_descriptor(owned, current)
    return replacement


def _resolve_enumeration_components(
    *,
    boundary: _EnumerationBoundary,
    components: tuple[str, ...],
    owned: list[int],
    directory_flags: int,
) -> tuple[str, int | None, tuple[str, ...], tuple[int, int]]:
    """Resolve a locator beneath ``boundary`` without opening a file leaf.

    The returned kind is ``"directory"`` with an owned enumerable descriptor,
    or ``"regular"`` with no descriptor. Statically proven escapes, symlink
    loops, and non-file/non-directory targets raise ``_StaticEnumerationSkip``.
    """

    try:
        pending = deque(components)
    except BaseException as exc:
        raise _Failed from exc
    current = _open_directory(".", directory_flags, dir_fd=boundary.fd, missing=True)
    _track_descriptor(owned, current)
    current_components = list(boundary.components)
    boundary_depth = len(current_components)
    seen_symlinks: set[tuple[int, int]] = set()
    steps = 0
    transferred = False
    try:
        while pending:
            steps += 1
            if steps > _MAX_RESOLUTION_STEPS or len(pending) > _MAX_COMPONENTS:
                raise _StaticEnumerationSkip
            component = pending.popleft()
            if component in ("", "."):
                continue
            if component == "..":
                if len(current_components) == boundary_depth:
                    raise _StaticEnumerationSkip
                parent = _open_directory(
                    "..",
                    directory_flags,
                    dir_fd=current,
                    missing=True,
                )
                _track_descriptor(owned, parent)
                _directory_descriptor_identity(parent)
                current = _replace_enumeration_descriptor(
                    owned=owned,
                    current=current,
                    replacement=parent,
                )
                try:
                    current_components.pop()
                except BaseException as exc:
                    raise _Failed from exc
                continue

            metadata = _lstat_component(current, component, missing=True)
            try:
                mode = metadata.st_mode
            except BaseException as exc:
                raise _Failed from exc
            if type(mode) is not int:
                raise _Failed

            if stat.S_ISLNK(mode):
                identity = _symlink_identity(metadata)
                if identity in seen_symlinks:
                    raise _StaticEnumerationSkip
                try:
                    seen_symlinks.add(identity)
                except BaseException as exc:
                    raise _Failed from exc
                if len(seen_symlinks) > _MAX_SYMLINKS:
                    raise _StaticEnumerationSkip
                target = _readlink_component(current, component, missing=True)
                absolute = target.startswith("/")
                try:
                    target_components = _resolution_components(
                        target,
                        absolute=absolute,
                    )
                except _Denied as exc:
                    raise _StaticEnumerationSkip from exc
                if absolute:
                    matching_prefixes = tuple(
                        spelling
                        for spelling in boundary.spellings
                        if target_components[: len(spelling)] == spelling
                    )
                    if not matching_prefixes:
                        raise _StaticEnumerationSkip
                    prefix_length = max(map(len, matching_prefixes))
                    reset = _open_directory(
                        ".",
                        directory_flags,
                        dir_fd=boundary.fd,
                        missing=True,
                    )
                    _track_descriptor(owned, reset)
                    _directory_descriptor_identity(reset)
                    current = _replace_enumeration_descriptor(
                        owned=owned,
                        current=current,
                        replacement=reset,
                    )
                    try:
                        current_components[:] = boundary.components
                    except BaseException as exc:
                        raise _Failed from exc
                    target_components = target_components[prefix_length:]
                if len(target_components) + len(pending) > _MAX_COMPONENTS:
                    raise _StaticEnumerationSkip
                try:
                    pending.extendleft(reversed(target_components))
                except BaseException as exc:
                    raise _Failed from exc
                continue

            final_component = not pending
            if stat.S_ISREG(mode):
                if not final_component:
                    raise _StaticEnumerationSkip
                identity = _regular_identity_from_metadata(metadata)
                _close_descriptor(owned, current)
                current = -1
                return "regular", None, tuple(current_components), identity
            if not stat.S_ISDIR(mode):
                raise _StaticEnumerationSkip

            expected_identity = _directory_identity_from_metadata(metadata)
            child = _open_directory(
                component,
                directory_flags,
                dir_fd=current,
                missing=True,
            )
            _track_descriptor(owned, child)
            if _directory_descriptor_identity(child) != expected_identity:
                raise _Failed
            current = _replace_enumeration_descriptor(
                owned=owned,
                current=current,
                replacement=child,
            )
            try:
                current_components.append(component)
            except BaseException as exc:
                raise _Failed from exc

        identity = _directory_descriptor_identity(current)
        transferred = True
        return "directory", current, tuple(current_components), identity
    finally:
        if not transferred and current >= 0 and current in owned:
            _close_descriptor(owned, current)


def _canonical_ascii_json(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded = rendered.encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, MemoryError) as exc:
        raise _Failed from exc
    return encoded


def _locator_payload(root_index: int, locator: tuple[str, ...]) -> dict[str, object]:
    return {
        "root_index": root_index,
        "locator": list(locator),
    }


def _encoded_locator_bytes(root_index: int, locator: tuple[str, ...]) -> int:
    return len(_canonical_ascii_json(_locator_payload(root_index, locator)))


def _locator_path_bytes(locator: tuple[str, ...]) -> int:
    total = max(0, len(locator) - 1)
    for component in locator:
        total += _path_bytes(component)
        if total > _MAX_PATH_BYTES:
            return total
    return total


_MAX_ENUMERATION_RESPONSE_OVERHEAD = len(
    _canonical_ascii_json(
        {
            "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
            "status": "complete",
            "directories": MAX_LOCAL_FILESYSTEM_ENUMERATION_ITEMS,
            "candidates": [],
        }
    )
)
_MAX_ENUMERATION_CANDIDATE_BYTES = (
    MAX_LOCAL_FILESYSTEM_ENUMERATION_METADATA_BYTES
    - _MAX_ENUMERATION_RESPONSE_OVERHEAD
)
_QUEUED_DIRECTORY_IDENTITY_BYTES = 2 * 8


def _candidate_payload_bytes(
    encoded_sum: int,
    count: int,
) -> int:
    return encoded_sum + max(0, count - 1)


def _enumeration_metadata_fits(
    state: _EnumerationState,
    *,
    candidate_encoded_bytes: int | None = None,
    candidate_count: int | None = None,
    queued_directory_encoded_bytes: int | None = None,
) -> bool:
    retained_encoded_bytes = (
        state.candidate_encoded_bytes
        if candidate_encoded_bytes is None
        else candidate_encoded_bytes
    )
    retained_count = (
        len(state.preferred) + len(state.fallback)
        if candidate_count is None
        else candidate_count
    )
    queued_encoded_bytes = (
        state.queued_directory_encoded_bytes
        if queued_directory_encoded_bytes is None
        else queued_directory_encoded_bytes
    )
    return (
        retained_encoded_bytes >= 0
        and retained_count >= 0
        and queued_encoded_bytes >= 0
        and _candidate_payload_bytes(retained_encoded_bytes, retained_count)
        + queued_encoded_bytes
        <= _MAX_ENUMERATION_CANDIDATE_BYTES
    )


def _retain_enumeration_candidate(
    state: _EnumerationState,
    *,
    root_index: int,
    locator: tuple[str, ...],
    identity: tuple[int, int],
) -> None:
    if _locator_path_bytes(locator) > _MAX_PATH_BYTES:
        state.candidate_limited = True
        return
    encoded_bytes = _encoded_locator_bytes(root_index, locator)
    candidate = _EnumerationCandidate(
        root_index=root_index,
        locator=locator,
        identity=identity,
        encoded_bytes=encoded_bytes,
    )
    preferred = state.target_name is not None and locator[-1] == state.target_name
    destination = state.preferred if preferred else state.fallback

    existing = state.retained_file_identities.get(identity)
    if existing is not None:
        existing_is_preferred = (
            state.target_name is not None
            and existing.locator[-1] == state.target_name
        )
        if not preferred or existing_is_preferred:
            return
        retained_count = len(state.preferred) + len(state.fallback)
        if (
            retained_count > state.max_files
            or not _enumeration_metadata_fits(
                state,
                candidate_encoded_bytes=(
                    state.candidate_encoded_bytes
                    - existing.encoded_bytes
                    + encoded_bytes
                ),
                candidate_count=retained_count,
            )
        ):
            state.candidate_limited = True
            return
        try:
            state.fallback.remove(existing)
            del state.retained_file_identities[identity]
        except BaseException as exc:
            raise _Failed from exc
        state.candidate_encoded_bytes -= existing.encoded_bytes

    def fits(additional_bytes: int) -> bool:
        count = len(state.preferred) + len(state.fallback) + 1
        return (
            count <= state.max_files
            and _enumeration_metadata_fits(
                state,
                candidate_encoded_bytes=(
                    state.candidate_encoded_bytes + additional_bytes
                ),
                candidate_count=count,
            )
        )

    if not preferred:
        if not fits(encoded_bytes):
            state.candidate_limited = True
            return
        try:
            destination.append(candidate)
            state.retained_file_identities[identity] = candidate
        except BaseException as exc:
            raise _Failed from exc
        state.candidate_encoded_bytes += encoded_bytes
        return

    while not fits(encoded_bytes) and state.fallback:
        try:
            removed = state.fallback.pop()
            del state.retained_file_identities[removed.identity]
        except BaseException as exc:
            raise _Failed from exc
        state.candidate_encoded_bytes -= removed.encoded_bytes
        state.candidate_limited = True
    if not fits(encoded_bytes):
        state.candidate_limited = True
        return
    try:
        destination.append(candidate)
        state.retained_file_identities[identity] = candidate
    except BaseException as exc:
        raise _Failed from exc
    state.candidate_encoded_bytes += encoded_bytes


def _validated_scandir_name(entry: os.DirEntry[str]) -> str:
    try:
        name = entry.name
    except BaseException as exc:
        raise _Failed from exc
    if (
        type(name) is not str
        or name in ("", ".", "..")
        or "/" in name
        or "\0" in name
        or _path_bytes(name) > _MAX_COMPONENT_BYTES
    ):
        raise _Failed
    return name


def _scandir_iterator(fd: int) -> _ScandirIterator:
    try:
        iterator = os.scandir(fd)
    except BaseException as exc:
        raise _Failed from exc
    try:
        close = iterator.close
        iter(iterator)
    except BaseException as exc:
        with suppress(BaseException):
            close = iterator.close
            close()
        raise _Failed from exc
    if not callable(close):
        with suppress(BaseException):
            iterator.close()
        raise _Failed
    return iterator


def _close_scandir_iterator(iterator: _ScandirIterator) -> None:
    try:
        close = iterator.close
        if not callable(close):
            raise TypeError
        close()
    except BaseException as exc:
        raise _Failed from exc


def _open_selected_scope(
    selected: _SelectedPath,
    owned: list[int],
) -> tuple[
    list[int],
    list[str],
    tuple[str, ...],
    set[tuple[str, ...]],
    int,
]:
    root_flags = _directory_open_flags(follow_symlinks=True)
    child_flags = _directory_open_flags()
    if root_flags is None or child_flags is None:
        raise _Failed

    stack: list[int] = []
    root = _open_directory(selected.root, root_flags)
    _track_descriptor(owned, root)
    stack.append(root)
    _is_searchable_directory_descriptor(root)
    resolved_root_components = _bound_resolved_root_components(
        selected.root,
        root,
    )
    stack_components = list(resolved_root_components)
    root_spellings = {
        selected.root_components,
        resolved_root_components,
    }
    return (
        stack,
        stack_components,
        resolved_root_components,
        root_spellings,
        child_flags,
    )


def _inspect_selected_directory(selected: _SelectedPath) -> None:
    owned: list[int] = []
    outcome: BaseException | None = None
    try:
        (
            stack,
            stack_components,
            resolved_root_components,
            root_spellings,
            child_flags,
        ) = _open_selected_scope(selected, owned)
        leaf = _walk_components(
            owned=owned,
            stack=stack,
            stack_components=stack_components,
            components=selected.candidate_components,
            boundary_depth=1,
            boundary_components=resolved_root_components,
            root_spellings=root_spellings,
            directory_flags=child_flags,
            regular_file_flags=None,
            missing=False,
        )
        if leaf is not None:
            raise _Failed
    except BaseException as exc:
        outcome = exc
    finally:
        if not _close_all(owned):
            outcome = _Failed()

    if outcome is None:
        return
    if isinstance(outcome, _Denied):
        raise outcome
    raise _Failed from outcome


def _snapshot_regular_file(fd: int) -> _FileSnapshot:
    try:
        metadata = os.fstat(fd)
        values = (
            metadata.st_mode,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
    except OSError as exc:
        raise _Failed from exc
    except BaseException as exc:
        raise _Failed from exc
    if any(type(value) is not int for value in values):
        raise _Failed
    mode, device, inode, size, modified_ns, changed_ns = values
    return _FileSnapshot(
        file_type=stat.S_IFMT(mode),
        device=device,
        inode=inode,
        size=size,
        modified_ns=modified_ns,
        changed_ns=changed_ns,
    )


def _read_from_descriptor(fd: int, count: int) -> bytes:
    try:
        data = os.read(fd, count)
    except BaseException as exc:
        raise _Failed from exc
    if type(data) is not bytes or len(data) > count:
        raise _Failed
    return data


def _write_stdout(data: bytes) -> None:
    offset = 0
    while offset < len(data):
        try:
            written = os.write(1, data[offset:])
        except BaseException as exc:
            raise _Failed from exc
        if type(written) is not int or written < 1 or written > len(data) - offset:
            raise _Failed
        offset += written


def _stream_stable_regular_file(fd: int, max_bytes: int) -> None:
    before = _snapshot_regular_file(fd)
    if not stat.S_ISREG(before.file_type):
        raise _Denied
    if before.size < 0:
        raise _Failed
    if before.size > max_bytes:
        raise _Failed

    remaining = before.size
    while remaining:
        requested = min(remaining, _READ_CHUNK_BYTES)
        chunk = _read_from_descriptor(fd, requested)
        if len(chunk) != requested:
            raise _Failed
        _write_stdout(chunk)
        remaining -= len(chunk)

    proof = _read_from_descriptor(fd, 1)
    if proof:
        _write_stdout(proof)
        raise _Failed

    after = _snapshot_regular_file(fd)
    if not stat.S_ISREG(after.file_type) or after.size < 0 or after != before:
        raise _Failed


def _read_selected_file(selected: _SelectedPath, max_bytes: int) -> None:
    owned: list[int] = []
    outcome: BaseException | None = None
    try:
        regular_file_flags = _regular_file_open_flags()
        if regular_file_flags is None:
            raise _Failed
        (
            stack,
            stack_components,
            resolved_root_components,
            root_spellings,
            child_flags,
        ) = _open_selected_scope(selected, owned)
        if not selected.candidate_components:
            raise _Denied
        leaf = _walk_components(
            owned=owned,
            stack=stack,
            stack_components=stack_components,
            components=selected.candidate_components,
            boundary_depth=1,
            boundary_components=resolved_root_components,
            root_spellings=root_spellings,
            directory_flags=child_flags,
            regular_file_flags=regular_file_flags,
            missing=True,
        )
        if leaf is None:
            raise _Denied
        _stream_stable_regular_file(leaf, max_bytes)
    except BaseException as exc:
        outcome = exc
    finally:
        if not _close_all(owned):
            outcome = _Failed()

    if outcome is None:
        return
    if isinstance(outcome, (_Denied, _Missing, _Failed)):
        raise outcome
    raise _Failed from outcome


def _read_registered_file(request: _RegisteredReadRequest) -> None:
    selected = _select_candidate(request.store_root, request.roots)
    owned: list[int] = []
    outcome: BaseException | None = None
    try:
        regular_file_flags = _regular_file_open_flags()
        if regular_file_flags is None:
            raise _Failed
        (
            stack,
            stack_components,
            operator_components,
            operator_spellings,
            child_flags,
        ) = _open_selected_scope(selected, owned)
        store_leaf = _walk_components(
            owned=owned,
            stack=stack,
            stack_components=stack_components,
            components=selected.candidate_components,
            boundary_depth=1,
            boundary_components=operator_components,
            root_spellings=operator_spellings,
            directory_flags=child_flags,
            regular_file_flags=None,
            missing=True,
        )
        if store_leaf is not None:
            raise _Failed

        store_boundary_depth = len(stack)
        store_boundary_components = tuple(stack_components)
        store_spellings = {
            store_boundary_components,
            _lexically_normalized_absolute_components(request.store_root),
        }
        leaf = _walk_components(
            owned=owned,
            stack=stack,
            stack_components=stack_components,
            components=request.locator,
            boundary_depth=store_boundary_depth,
            boundary_components=store_boundary_components,
            root_spellings=store_spellings,
            directory_flags=child_flags,
            regular_file_flags=regular_file_flags,
            missing=True,
        )
        if leaf is None:
            raise _Denied
        _stream_stable_regular_file(leaf, request.max_bytes)
    except BaseException as exc:
        outcome = exc
    finally:
        if not _close_all(owned):
            outcome = _Failed()

    if outcome is None:
        return
    if isinstance(outcome, (_Denied, _Missing, _Failed)):
        raise outcome
    raise _Failed from outcome


def _new_enumeration_state(
    request: _EnumerationRequest | _RegisteredEnumerationRequest,
) -> _EnumerationState:
    try:
        return _EnumerationState(
            target_name=request.target_name,
            max_files=request.max_files,
            max_directories=request.max_directories,
            directories=0,
            visited_directories=set(),
            preferred=[],
            fallback=[],
            retained_file_identities={},
            candidate_encoded_bytes=0,
            queued_directory_encoded_bytes=0,
            candidate_limited=False,
            directory_limited=False,
        )
    except BaseException as exc:
        raise _Failed from exc


def _append_directory_work(
    queue: deque[_DirectoryWork],
    *,
    queued_bytes: int,
    state: _EnumerationState,
    root_index: int,
    locator: tuple[str, ...],
    expected_identity: tuple[int, int] | None,
) -> int:
    if queued_bytes != state.queued_directory_encoded_bytes:
        raise _Failed
    if (
        len(locator) > _MAX_COMPONENTS
        or _locator_path_bytes(locator) > _MAX_PATH_BYTES
    ):
        state.directory_limited = True
        return queued_bytes
    if state.directories + len(queue) >= state.max_directories:
        state.directory_limited = True
        return queued_bytes
    encoded_bytes = (
        _encoded_locator_bytes(root_index, locator)
        + _QUEUED_DIRECTORY_IDENTITY_BYTES
    )
    next_queued_bytes = queued_bytes + encoded_bytes
    if not _enumeration_metadata_fits(
        state,
        queued_directory_encoded_bytes=next_queued_bytes,
    ):
        state.directory_limited = True
        return queued_bytes
    work = _DirectoryWork(
        root_index=root_index,
        locator=locator,
        expected_identity=expected_identity,
        encoded_bytes=encoded_bytes,
    )
    try:
        queue.append(work)
    except BaseException as exc:
        raise _Failed from exc
    state.queued_directory_encoded_bytes = next_queued_bytes
    return next_queued_bytes


def _classify_enumeration_entry(
    *,
    boundary: _EnumerationBoundary,
    directory_fd: int,
    directory_locator: tuple[str, ...],
    name: str,
    root_index: int,
    owned: list[int],
    directory_flags: int,
    queue: deque[_DirectoryWork],
    queued_bytes: int,
    state: _EnumerationState,
) -> int:
    metadata = _lstat_component(directory_fd, name, missing=True)
    try:
        mode = metadata.st_mode
    except BaseException as exc:
        raise _Failed from exc
    if type(mode) is not int:
        raise _Failed
    try:
        locator = (*directory_locator, name)
    except BaseException as exc:
        raise _Failed from exc
    if (
        len(locator) > _MAX_COMPONENTS
        or _locator_path_bytes(locator) > _MAX_PATH_BYTES
    ):
        if stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            if stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                state.directory_limited = True
            else:
                state.candidate_limited = True
        return queued_bytes

    if stat.S_ISREG(mode):
        _retain_enumeration_candidate(
            state,
            root_index=root_index,
            locator=locator,
            identity=_regular_identity_from_metadata(metadata),
        )
        return queued_bytes

    if stat.S_ISDIR(mode):
        identity = _directory_identity_from_metadata(metadata)
        return _append_directory_work(
            queue,
            queued_bytes=queued_bytes,
            state=state,
            root_index=root_index,
            locator=locator,
            expected_identity=identity,
        )

    if not stat.S_ISLNK(mode):
        return queued_bytes

    if state.directories + len(queue) >= state.max_directories:
        state.directory_limited = True
        return queued_bytes

    try:
        (
            kind,
            resolved_fd,
            _resolved_components,
            resolved_identity,
        ) = _resolve_enumeration_components(
            boundary=boundary,
            components=locator,
            owned=owned,
            directory_flags=directory_flags,
        )
    except _StaticEnumerationSkip:
        return queued_bytes
    if kind == "regular":
        if resolved_fd is not None:
            raise _Failed
        _retain_enumeration_candidate(
            state,
            root_index=root_index,
            locator=locator,
            identity=resolved_identity,
        )
        return queued_bytes
    if kind != "directory" or resolved_fd is None:
        raise _Failed
    try:
        identity = _directory_descriptor_identity(resolved_fd)
    finally:
        _close_descriptor(owned, resolved_fd)
    return _append_directory_work(
        queue,
        queued_bytes=queued_bytes,
        state=state,
        root_index=root_index,
        locator=locator,
        expected_identity=identity,
    )


def _scan_enumeration_directory(
    *,
    boundary: _EnumerationBoundary,
    work: _DirectoryWork,
    directory_fd: int,
    owned: list[int],
    directory_flags: int,
    queue: deque[_DirectoryWork],
    queued_bytes: int,
    state: _EnumerationState,
) -> int:
    iterator = _scandir_iterator(directory_fd)
    outcome: BaseException | None = None
    try:
        for entry in iterator:
            name = _validated_scandir_name(entry)
            queued_bytes = _classify_enumeration_entry(
                boundary=boundary,
                directory_fd=directory_fd,
                directory_locator=work.locator,
                name=name,
                root_index=work.root_index,
                owned=owned,
                directory_flags=directory_flags,
                queue=queue,
                queued_bytes=queued_bytes,
                state=state,
            )
    except BaseException as exc:
        outcome = exc
    finally:
        try:
            _close_scandir_iterator(iterator)
        except BaseException as exc:
            outcome = exc
    if outcome is None:
        return queued_bytes
    if isinstance(outcome, (_Denied, _Missing, _Failed)):
        raise outcome
    raise _Failed from outcome


def _traverse_enumeration_boundary(
    *,
    boundary: _EnumerationBoundary,
    root_index: int,
    owned: list[int],
    state: _EnumerationState,
) -> None:
    directory_flags = _enumerable_directory_open_flags()
    if directory_flags is None:
        raise _Failed
    try:
        queue: deque[_DirectoryWork] = deque()
    except BaseException as exc:
        raise _Failed from exc
    queued_bytes = _append_directory_work(
        queue,
        queued_bytes=0,
        state=state,
        root_index=root_index,
        locator=(),
        expected_identity=_directory_descriptor_identity(boundary.fd),
    )
    while queue:
        try:
            work = queue.popleft()
        except BaseException as exc:
            raise _Failed from exc
        if (
            queued_bytes != state.queued_directory_encoded_bytes
            or work.encoded_bytes > queued_bytes
        ):
            raise _Failed
        if state.directories >= state.max_directories:
            state.directory_limited = True
            try:
                queue.clear()
            except BaseException as exc:
                raise _Failed from exc
            queued_bytes = 0
            state.queued_directory_encoded_bytes = 0
            break

        try:
            try:
                (
                    kind,
                    directory_fd,
                    _resolved_components,
                    _resolved_identity,
                ) = _resolve_enumeration_components(
                    boundary=boundary,
                    components=work.locator,
                    owned=owned,
                    directory_flags=directory_flags,
                )
            except _StaticEnumerationSkip as exc:
                raise _Failed from exc
            if kind != "directory" or directory_fd is None:
                raise _Failed
            try:
                identity = _directory_descriptor_identity(directory_fd)
                if (
                    work.expected_identity is not None
                    and identity != work.expected_identity
                ):
                    raise _Failed
                state.directories += 1
                if identity in state.visited_directories:
                    continue
                try:
                    state.visited_directories.add(identity)
                except BaseException as exc:
                    raise _Failed from exc
                queued_bytes = _scan_enumeration_directory(
                    boundary=boundary,
                    work=work,
                    directory_fd=directory_fd,
                    owned=owned,
                    directory_flags=directory_flags,
                    queue=queue,
                    queued_bytes=queued_bytes,
                    state=state,
                )
            finally:
                _close_descriptor(owned, directory_fd)
        finally:
            queued_bytes -= work.encoded_bytes
            state.queued_directory_encoded_bytes -= work.encoded_bytes
            if (
                queued_bytes < 0
                or state.queued_directory_encoded_bytes != queued_bytes
            ):
                raise _Failed
    if queued_bytes != 0 or state.queued_directory_encoded_bytes != 0:
        raise _Failed


def _registered_enumeration_boundary(
    request: _RegisteredEnumerationRequest,
    owned: list[int],
) -> _EnumerationBoundary:
    selected = _select_candidate(request.store_root, request.roots)
    operator_boundary = _open_enumeration_boundary(
        selected.root,
        selected.root_components,
        owned,
    )
    directory_flags = _enumerable_directory_open_flags()
    if directory_flags is None:
        raise _Failed
    try:
        (
            kind,
            store_fd,
            store_components,
            _store_identity,
        ) = _resolve_enumeration_components(
            boundary=operator_boundary,
            components=selected.candidate_components,
            owned=owned,
            directory_flags=directory_flags,
        )
    except _StaticEnumerationSkip as exc:
        raise _Denied from exc
    if kind != "directory" or store_fd is None:
        raise _Denied
    try:
        configured_store_components = _lexically_normalized_absolute_components(
            request.store_root
        )
        spellings = frozenset((store_components, configured_store_components))
    except BaseException:
        _close_descriptor(owned, store_fd)
        raise
    _close_descriptor(owned, operator_boundary.fd)
    return _EnumerationBoundary(
        fd=store_fd,
        components=store_components,
        spellings=spellings,
    )


def _enumeration_response(
    state: _EnumerationState,
) -> bytes:
    if state.queued_directory_encoded_bytes != 0:
        raise _Failed
    try:
        candidates = [
            _locator_payload(candidate.root_index, candidate.locator)
            for candidate in (*state.preferred, *state.fallback)
        ]
    except BaseException as exc:
        raise _Failed from exc
    status = (
        "limit"
        if state.candidate_limited or state.directory_limited
        else "complete"
    )
    encoded = _canonical_ascii_json(
        {
            "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
            "status": status,
            "directories": state.directories,
            "candidates": candidates,
        }
    )
    if len(encoded) > MAX_LOCAL_FILESYSTEM_ENUMERATION_METADATA_BYTES:
        raise _Failed
    return encoded


def _enumerate_files(
    request: _EnumerationRequest | _RegisteredEnumerationRequest,
) -> bytes:
    state = _new_enumeration_state(request)
    if request.max_files == 0:
        return _enumeration_response(state)
    if request.max_directories == 0:
        state.directory_limited = True
        return _enumeration_response(state)

    owned: list[int] = []
    outcome: BaseException | None = None
    response: bytes | None = None
    try:
        if isinstance(request, _RegisteredEnumerationRequest):
            boundary = _registered_enumeration_boundary(request, owned)
            _traverse_enumeration_boundary(
                boundary=boundary,
                root_index=0,
                owned=owned,
                state=state,
            )
            _close_descriptor(owned, boundary.fd)
        else:
            for root_index, root in enumerate(request.roots):
                if state.directories >= state.max_directories:
                    state.directory_limited = True
                    break
                boundary = _open_enumeration_boundary(
                    root,
                    _normalized_root_components(root),
                    owned,
                )
                _traverse_enumeration_boundary(
                    boundary=boundary,
                    root_index=root_index,
                    owned=owned,
                    state=state,
                )
                _close_descriptor(owned, boundary.fd)
                if state.directory_limited:
                    break
        response = _enumeration_response(state)
    except BaseException as exc:
        outcome = exc
    finally:
        if not _close_all(owned):
            outcome = _Failed()

    if outcome is None and response is not None:
        return response
    if isinstance(outcome, (_Denied, _Missing, _Failed)):
        raise outcome
    raise _Failed from outcome


def _select_candidate(candidate: str, roots: tuple[str, ...]) -> _SelectedPath:
    return _select_path(_Request(candidate=candidate, roots=roots))


def _inspect_request(environment: Mapping[str, str]) -> None:
    if os.name != "posix":
        raise _Failed
    request = _parse_request(environment)
    if not isinstance(request, _Request):
        raise _Failed
    selected = _select_path(request)
    _inspect_selected_directory(selected)


def _execute_request(environment: Mapping[str, str]) -> None:
    if os.name != "posix":
        raise _Failed
    request = _parse_request(environment)
    if isinstance(request, _Request):
        _inspect_selected_directory(_select_path(request))
        return
    if isinstance(request, _DirectReadRequest):
        _read_selected_file(
            _select_candidate(request.candidate, request.roots),
            request.max_bytes,
        )
        return
    if isinstance(request, _RegisteredReadRequest):
        _read_registered_file(request)
        return
    if isinstance(request, (_EnumerationRequest, _RegisteredEnumerationRequest)):
        _write_stdout(_enumerate_files(request))
        return
    raise _Failed


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Return a finite status without emitting path-bearing diagnostics."""

    try:
        arguments = sys.argv if argv is None else argv
        environment = os.environ if environ is None else environ
        if len(arguments) != 1:
            return FAILED_EXIT
        _execute_request(environment)
        return ACCESSIBLE_EXIT
    except _Denied:
        return DENIED_EXIT
    except _Missing:
        return MISSING_EXIT
    except BaseException:
        return FAILED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
