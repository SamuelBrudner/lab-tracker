"""Bounded, pre-follow POSIX operations for local store health and reads.

The application executes this file directly with ``python -I -S -B``.  Keep
the module self-contained and standard-library-only. Directory inspection is
output-free. Successful file reads emit only the exact raw artifact bytes;
every operation also returns one fixed process status.

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
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass

LOCAL_FILESYSTEM_REQUEST_ENV = "LAB_TRACKER_INTERNAL_LOCAL_FILESYSTEM_REQUEST"
LOCAL_FILESYSTEM_PROTOCOL_VERSION = 1
INSPECT_DIRECTORY_OPERATION = "inspect-directory"
READ_FILE_OPERATION = "read-file"
READ_REGISTERED_FILE_OPERATION = "read-registered-file"

COMPLETE_EXIT = 0
ACCESSIBLE_EXIT = COMPLETE_EXIT
DENIED_EXIT = 2
FAILED_EXIT = 3
MISSING_EXIT = 4

MAX_LOCAL_FILESYSTEM_REQUEST_BYTES = 24 * 1024
MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS = 64
MAX_LOCAL_FILESYSTEM_READ_BYTES = 512 * 1024 * 1024

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
) -> _Request | _DirectReadRequest | _RegisteredReadRequest:
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


def _validate_locator(value: object) -> tuple[str, ...]:
    if (
        type(value) is not list
        or not value
        or len(value) > _MAX_COMPONENTS
        or any(
            type(component) is not str
            or component in ("", ".", "..")
            or "/" in component
            or _has_forbidden_characters(component)
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
    _read_registered_file(request)


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
