"""Bounded, pre-follow POSIX directory inspection for local store health.

The application executes this file directly with ``python -I -S -B``.  Keep
the module self-contained and standard-library-only: its only observable
response is the process exit status.

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

ACCESSIBLE_EXIT = 0
DENIED_EXIT = 2
FAILED_EXIT = 3

MAX_LOCAL_FILESYSTEM_REQUEST_BYTES = 24 * 1024
MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS = 64

_MAX_PATH_BYTES = 16 * 1024
_MAX_COMPONENT_BYTES = 255
_MAX_COMPONENTS = 4_096
_MAX_RESOLUTION_STEPS = 8_192
_MAX_SYMLINKS = 40
_MAX_SYMLINK_TARGET_BYTES = 16 * 1024
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


class _Failed(Exception):
    """The helper could not safely complete a valid request."""


@dataclass(frozen=True, slots=True)
class _Request:
    candidate: str
    roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SelectedPath:
    root: str
    root_components: tuple[str, ...]
    candidate_components: tuple[str, ...]


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


def _canonical_request_payload(request: object) -> str:
    return json.dumps(
        request,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_request(environment: Mapping[str, str]) -> _Request:
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
    if not isinstance(value, dict) or set(value) != {
        "candidate",
        "op",
        "roots",
        "v",
    }:
        raise _Failed
    try:
        canonical = _canonical_request_payload(value)
    except (TypeError, ValueError, RecursionError, MemoryError) as exc:
        raise _Failed from exc
    if canonical != raw:
        raise _Failed

    version = value["v"]
    operation = value["op"]
    candidate = value["candidate"]
    roots = value["roots"]
    if (
        type(version) is not int
        or version != LOCAL_FILESYSTEM_PROTOCOL_VERSION
        or type(operation) is not str
        or operation != INSPECT_DIRECTORY_OPERATION
        or type(candidate) is not str
        or type(roots) is not list
        or len(roots) > MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS
        or any(type(root) is not str for root in roots)
    ):
        raise _Failed

    _validate_candidate(candidate)
    normalized_roots: list[str] = []
    for root in roots:
        assert isinstance(root, str)
        _normalized_root_components(root)
        if root in normalized_roots:
            raise _Failed
        normalized_roots.append(root)
    return _Request(candidate=candidate, roots=tuple(normalized_roots))


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


def _as_operation_error(exc: OSError, *, race_safe_invalid: bool = False) -> Exception:
    if exc.errno in _DENIED_ERRNOS or (
        race_safe_invalid and exc.errno == getattr(errno, "EINVAL", object())
    ):
        return _Denied()
    return _Failed()


def _open_directory(path: str, flags: int, *, dir_fd: int | None = None) -> int:
    try:
        fd = (
            os.open(path, flags)
            if dir_fd is None
            else os.open(path, flags, dir_fd=dir_fd)
        )
    except OSError as exc:
        raise _as_operation_error(exc) from None
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


def _lstat_component(parent: int, component: str) -> os.stat_result:
    try:
        return os.stat(component, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise _as_operation_error(exc) from None
    except BaseException as exc:
        raise _Failed from exc


def _readlink_component(parent: int, component: str) -> str:
    try:
        target = os.readlink(component, dir_fd=parent)
    except OSError as exc:
        raise _as_operation_error(exc, race_safe_invalid=True) from None
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


def _reset_to_root(owned: list[int], stack: list[int]) -> None:
    while len(stack) > 1:
        child = stack.pop()
        _close_descriptor(owned, child)


def _inspect_selected_directory(selected: _SelectedPath) -> None:
    root_flags = _directory_open_flags(follow_symlinks=True)
    child_flags = _directory_open_flags()
    if root_flags is None or child_flags is None:
        raise _Failed

    owned: list[int] = []
    stack: list[int] = []
    outcome: BaseException | None = None
    try:
        root = _open_directory(selected.root, root_flags)
        _track_descriptor(owned, root)
        stack.append(root)
        _is_searchable_directory_descriptor(root)
        resolved_root_components = _bound_resolved_root_components(
            selected.root,
            root,
        )
        root_spellings = {
            selected.root_components,
            resolved_root_components,
        }

        pending = deque(selected.candidate_components)
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
                if len(stack) == 1:
                    raise _Denied
                child = stack.pop()
                _close_descriptor(owned, child)
                continue

            metadata = _lstat_component(stack[-1], component)
            if stat.S_ISLNK(metadata.st_mode):
                symlinks += 1
                if symlinks > _MAX_SYMLINKS:
                    raise _Denied
                target = _readlink_component(stack[-1], component)
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
                    _reset_to_root(owned, stack)
                    target_components = target_components[prefix_length:]
                if len(target_components) + len(pending) > _MAX_COMPONENTS:
                    raise _Denied
                pending.extendleft(reversed(target_components))
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise _Denied

            child = _open_directory(component, child_flags, dir_fd=stack[-1])
            _track_descriptor(owned, child)
            stack.append(child)
            _is_searchable_directory_descriptor(child)
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


def _inspect_request(environment: Mapping[str, str]) -> None:
    if os.name != "posix":
        raise _Failed
    request = _parse_request(environment)
    selected = _select_path(request)
    _inspect_selected_directory(selected)


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
        _inspect_request(environment)
        return ACCESSIBLE_EXIT
    except _Denied:
        return DENIED_EXIT
    except BaseException:
        return FAILED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
