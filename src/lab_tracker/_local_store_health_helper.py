"""Isolated, output-free POSIX directory predicate for local store health.

The application executes this file directly with an isolated Python
interpreter. It imports only the standard library and communicates exclusively
through its exit status. Parent-side policy authorization is preliminary; this
helper binds its predicate to directory descriptors opened without following
symlinks and verifies search permission through those descriptors, but it does
not define mount-crossing authority. Explicit descriptor cleanup is best
effort; helper-process exit backstops failed closes and asynchronous
interruption windows.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping, Sequence

LOCAL_STORE_HEALTH_ROOT_ENV = "LAB_TRACKER_INTERNAL_LOCAL_STORE_HEALTH_ROOT"

_HEALTHY_EXIT = 0
_UNREACHABLE_EXIT = 1


def _directory_open_flags() -> int | None:
    required_names = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required_names):
        return None
    search_mode = getattr(os, "O_SEARCH", None)
    path_mode = getattr(os, "O_PATH", None)
    if isinstance(search_mode, int):
        access_mode = search_mode
    elif isinstance(path_mode, int):
        access_mode = path_mode
    else:
        return None
    return (
        int(access_mode)
        | int(os.O_DIRECTORY)
        | int(os.O_NOFOLLOW)
        | int(os.O_CLOEXEC)
    )


def _absolute_components(path: str) -> tuple[str, ...] | None:
    if (
        os.name != "posix"
        or not path.startswith("/")
        or path.startswith("//")
        or "\0" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        return None
    try:
        if os.path.normpath(path) != path:
            return None
    except (OSError, ValueError):
        return None
    if path == "/":
        return ()
    components = tuple(path[1:].split("/"))
    if any(component in ("", ".", "..") for component in components):
        return None
    return components


def _is_searchable_directory_descriptor(fd: int) -> bool:
    return stat.S_ISDIR(os.fstat(fd).st_mode) and os.access(
        ".",
        os.X_OK,
        dir_fd=fd,
        effective_ids=True,
    )


def _close_owned(owned: list[int], fd: int) -> bool:
    try:
        owned.remove(fd)
    except ValueError:
        return False
    try:
        os.close(fd)
    except BaseException:
        return False
    return True


def _close_all(owned: list[int]) -> bool:
    clean = True
    while owned:
        fd = owned.pop()
        try:
            os.close(fd)
        except BaseException:
            clean = False
    return clean


def _is_handle_bound_searchable_directory(path: str) -> bool:
    components = _absolute_components(path)
    flags = _directory_open_flags()
    if components is None or flags is None:
        return False

    owned: list[int] = []
    healthy = False
    try:
        current = os.open("/", flags)
        owned.append(current)
        if not _is_searchable_directory_descriptor(current):
            return False

        for component in components:
            child = os.open(component, flags, dir_fd=current)
            owned.append(child)
            if not _is_searchable_directory_descriptor(child):
                return False
            if not _close_owned(owned, current):
                return False
            current = child
        healthy = True
    except BaseException:
        healthy = False
    finally:
        if not _close_all(owned):
            healthy = False
    return healthy


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Return zero only for one valid, handle-bound searchable directory."""

    try:
        arguments = sys.argv if argv is None else argv
        environment = os.environ if environ is None else environ
        if len(arguments) != 1:
            return _UNREACHABLE_EXIT
        root = environment.get(LOCAL_STORE_HEALTH_ROOT_ENV)
        if not root or "\0" in root:
            return _UNREACHABLE_EXIT
        return (
            _HEALTHY_EXIT
            if _is_handle_bound_searchable_directory(root)
            else _UNREACHABLE_EXIT
        )
    except BaseException:
        return _UNREACHABLE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
