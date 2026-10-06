"""Shared file discovery and stable fingerprint helpers."""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

# What ``open`` reports when ``O_NOFOLLOW`` meets a symlink (Linux and macOS
# say ELOOP, FreeBSD says EMLINK).
_SYMLINK_REFUSED_ERRNOS = {errno.ELOOP, errno.EMLINK}
# Whether a path can be walked one directory at a time (``openat``), so no
# component below a root is ever followed as a symlink. Not on Windows.
# Windows reparse tags that redirect to another path: symlinks and junctions
# (``stat`` defines these names only on Windows builds).
_LINK_REPARSE_TAGS = frozenset(
    {
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
    }
)
_WALKS_WITH_DIR_FD = (
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)


@dataclass(frozen=True)
class FileFingerprint:
    """Stable file fingerprint observed by a watch scan."""

    size_bytes: int
    mtime: float
    checksum: str


class NotRegularFileError(OSError):
    """A path that must name a regular file names a device, FIFO, socket, or directory."""


def open_regular_file(
    path: str | Path, *, follow_symlinks: bool = True, dir_fd: int | None = None
) -> BinaryIO:
    """Open ``path`` for binary reading only if it is a regular file.

    The type is checked before the open and again on the opened descriptor,
    so a device, FIFO, socket, or directory is refused without blocking (a
    FIFO swapped in after the first check is opened ``O_NONBLOCK``), and the
    file that passed the check is the one read. With ``follow_symlinks=False``
    a symlink as the final component is refused too (``O_NOFOLLOW``).
    """

    target = os.fspath(path)
    info = os.stat(target, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
    if not stat.S_ISREG(info.st_mode):
        raise NotRegularFileError(f"not a regular file: {target}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    if not follow_symlinks:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, dir_fd=dir_fd)
    except OSError as exc:
        if not follow_symlinks and exc.errno in _SYMLINK_REFUSED_ERRNOS:
            raise NotRegularFileError(f"not a regular file: {target}") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NotRegularFileError(f"not a regular file: {target}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def open_regular_file_within(root: str | Path, path: str | Path) -> BinaryIO:
    """Open ``path``, a file below ``root``, without following any symlink below it.

    Each directory under ``root`` is opened from its parent (``dir_fd``) with
    ``O_NOFOLLOW``, so a component swapped for a symlink after ``path`` was
    found fails the open instead of leading out of ``root``. Without
    ``dir_fd`` (Windows) each component is checked with ``lstat`` first,
    which narrows that race rather than closing it.
    """

    base = Path(root)
    try:
        parts = Path(path).relative_to(base).parts
    except ValueError:
        parts = ()
    if not parts or ".." in parts:
        raise NotRegularFileError(f"not a file below {base}: {path}")
    if not _WALKS_WITH_DIR_FD:
        return _open_regular_file_within_by_path(base, parts, path)
    try:
        # ``root`` itself is not followed either: a root swapped for a symlink
        # after the caller checked it would move the whole confinement.
        descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in _SYMLINK_REFUSED_ERRNOS or exc.errno == errno.ENOTDIR:
            raise NotRegularFileError(f"not a file below {base}: {path}") from exc
        raise
    try:
        for part in parts[:-1]:
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            except OSError as exc:
                if exc.errno in _SYMLINK_REFUSED_ERRNOS or exc.errno == errno.ENOTDIR:
                    raise NotRegularFileError(f"not a file below {base}: {path}") from exc
                raise
            os.close(descriptor)
            descriptor = child
        return open_regular_file(parts[-1], follow_symlinks=False, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def _open_regular_file_within_by_path(
    base: Path, parts: tuple[str, ...], path: str | Path
) -> BinaryIO:
    """The ``open_regular_file_within`` fallback where ``dir_fd`` is missing (Windows).

    ``root``, every component, and the file are refused when they are a
    symlink or a directory junction (see :func:`is_link`), and the opened
    path must still resolve below ``root``. Checks and open are separate
    calls, so this narrows a swap race rather than closing it.
    """

    for depth in range(len(parts) + 1):
        if is_link(base.joinpath(*parts[:depth])):
            raise NotRegularFileError(f"not a file below {base}: {path}")
    full_path = base.joinpath(*parts)
    handle = open_regular_file(full_path, follow_symlinks=False)
    try:
        real_root = Path(os.path.realpath(base))
        if not Path(os.path.realpath(full_path)).is_relative_to(real_root):
            raise NotRegularFileError(f"not a file below {base}: {path}")
    except BaseException:
        handle.close()
        raise
    return handle


def is_link(path: str | Path) -> bool:
    """Whether ``path`` is a symlink or, on Windows, a junction.

    Other Windows reparse points (cloud-sync placeholders such as OneDrive
    Files On-Demand, deduplicated or compressed files) are the files
    themselves, not links elsewhere, so they are not counted.
    """

    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return getattr(info, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS


class WatchRootSymlinkError(ValueError):
    """A watch root that is itself a symlink (or a Windows junction)."""


def absolute_path_keeping_name(path: str | Path) -> Path:
    """``path`` made absolute with its parent resolved but its own name kept.

    Unlike ``Path.resolve``, a symlink as the final component is not followed,
    so the caller can still see (and refuse) it.
    """

    expanded = Path(path).expanduser().absolute()
    if expanded.name in {"", ".."}:
        return expanded.resolve()
    return expanded.parent.resolve() / expanded.name


def resolve_watch_root(root: str | Path) -> Path:
    """The absolute path a watch root names, refusing a root that is a symlink.

    The root's parents are resolved, so a root below a symlinked folder still
    works, but the root's own name is never followed: a watched file or folder
    replaced by a symlink would otherwise move the watch, and with it the
    confinement of every read, to wherever the link points (``~/.ssh``).
    """

    resolved = absolute_path_keeping_name(root)
    if is_link(resolved):
        try:
            target = str(resolved.resolve())
        except (OSError, RuntimeError):  # a symlink loop (RuntimeError before 3.13)
            target = "a path that does not resolve"
        raise WatchRootSymlinkError(
            f"watch root is a symlink; point the watch at its target: {target}"
        )
    return resolved


def file_sha256(
    path: str | Path, *, chunk_size: int = 1024 * 1024, follow_symlinks: bool = True
) -> str:
    with open_regular_file(path, follow_symlinks=follow_symlinks) as handle:
        return _handle_sha256(handle, chunk_size=chunk_size)


def _handle_sha256(handle: BinaryIO, *, chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    for chunk in iter(lambda: handle.read(chunk_size), b""):
        hasher.update(chunk)
    return hasher.hexdigest()


def stable_file_fingerprint(
    path: str | Path, *, root: str | Path | None = None
) -> FileFingerprint | None:
    """Fingerprint a regular file that did not change while it was hashed.

    ``None`` when the file is missing, unreadable, not a regular file (a
    device or FIFO would never finish hashing), or changed during the read.
    With ``root`` (a watch root), the file is opened as
    :func:`fingerprint_within_root` opens it, so it is hashed from a path with
    no symlink at or below ``root``.
    """

    if root is not None:
        try:
            return fingerprint_within_root(root, path)
        except (PermissionError, WatchRootSymlinkError):
            return None
    resolved = Path(path).expanduser().resolve()
    try:
        before = resolved.stat()
        checksum = file_sha256(resolved)
        after = resolved.stat()
    except (FileNotFoundError, PermissionError, NotRegularFileError):
        return None
    if before.st_size != after.st_size or before.st_mtime != after.st_mtime:
        return None
    return FileFingerprint(
        size_bytes=after.st_size,
        mtime=after.st_mtime,
        checksum=checksum,
    )


class OversizeFileError(ValueError):
    """A file larger than the caller's byte limit, refused before it is hashed."""

    def __init__(self, path: str | Path, size_bytes: int, limit: int) -> None:
        super().__init__(f"file is {size_bytes} bytes, over the {limit}-byte limit: {path}")
        self.size_bytes = size_bytes
        self.limit = limit


def fingerprint_within_root(
    root: str | Path, path: str | Path, *, max_bytes: int | None = None
) -> FileFingerprint | None:
    """Fingerprint ``path``, a watch root or a file below one, following no symlink.

    ``root`` goes through :func:`resolve_watch_root` (a root that is a symlink
    raises :class:`WatchRootSymlinkError`). A single-file root is opened by
    its own path with ``O_NOFOLLOW`` (its folder is never opened, so a
    search-only folder works); a file below a folder root is opened through
    :func:`open_regular_file_within`. ``None`` when the file is missing, is
    not a regular file below the root, or changed while hashed; a permission
    error is raised, as is :class:`OversizeFileError` for a file over
    ``max_bytes`` (checked on the open descriptor, before hashing).
    """

    resolved_root = resolve_watch_root(root)
    target = absolute_path_keeping_name(path)
    try:
        handle = (
            open_regular_file(target, follow_symlinks=False)
            if target == resolved_root
            else open_regular_file_within(resolved_root, target)
        )
        with handle:
            before = os.fstat(handle.fileno())
            if max_bytes is not None and before.st_size > max_bytes:
                raise OversizeFileError(target, before.st_size, max_bytes)
            checksum = _handle_sha256(handle)
            after = os.fstat(handle.fileno())
    except (FileNotFoundError, NotRegularFileError):
        return None
    if before.st_size != after.st_size or before.st_mtime != after.st_mtime:
        return None
    return FileFingerprint(size_bytes=after.st_size, mtime=after.st_mtime, checksum=checksum)


def discover_files(
    root: str | Path,
    *,
    include_patterns: Sequence[str] | None = None,
    exclude_patterns: Sequence[str] | None = None,
    ignore_hidden: bool = True,
    refuse_symlinked_root: bool = False,
) -> list[Path]:
    """Regular, non-symlinked files below ``root`` (or ``root`` itself, a file).

    ``refuse_symlinked_root`` is for roots a client uploads from (``lt watch``,
    ``lt import-folder``): the root's own name is not followed (see
    :func:`resolve_watch_root`). Otherwise the root is resolved, as the
    server's acquisition watcher does for its operator-configured paths.
    """

    resolved_root = (
        resolve_watch_root(root)
        if refuse_symlinked_root
        else Path(root).expanduser().resolve()
    )
    if not resolved_root.exists():
        raise ValueError(f"watch root is not an existing path: {resolved_root}")
    include = list(include_patterns or ["*"])
    exclude = list(exclude_patterns or [])
    candidates = [resolved_root] if resolved_root.is_file() else sorted(resolved_root.rglob("*"))
    files: list[Path] = []
    base = resolved_root.parent if resolved_root.is_file() else resolved_root
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if ignore_hidden and is_hidden_relative(resolved, relative_to=base):
            continue
        if not matches_any(resolved, root=base, patterns=include):
            continue
        if matches_any(resolved, root=base, patterns=exclude):
            continue
        files.append(resolved)
    return files


def is_hidden_relative(path: Path, *, relative_to: Path) -> bool:
    try:
        candidate = path.relative_to(relative_to)
    except ValueError:
        candidate = path
    return any(part.startswith(".") for part in candidate.parts)


def matches_any(path: Path, *, root: Path, patterns: Sequence[str]) -> bool:
    if not patterns:
        return False
    try:
        relative_path = path.relative_to(root).as_posix()
    except ValueError:
        relative_path = path.name
    return any(
        fnmatch.fnmatch(relative_path, pattern) or fnmatch.fnmatch(path.name, pattern)
        for pattern in patterns
    )


def local_folder_external_id(root: Path, relative_path: str) -> str:
    return f"{root.as_uri()}::{relative_path}"
