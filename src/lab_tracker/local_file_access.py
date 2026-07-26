"""Handle-bound access to authorized host-local artifact files.

Path parsing and canonical root selection live in :mod:`local_path_policy`.
This module owns the narrower capability boundary: once a path has been
prepared, open it exactly once, validate the opened object, and keep that same
descriptor alive while callers inspect its bytes.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from enum import Enum
from typing import BinaryIO, Protocol, TypeAlias, cast

from lab_tracker._windows_local_file import (
    CtypesWindowsFinalPathApi,
    WindowsFinalPathApi,
    WindowsFinalPathError,
)
from lab_tracker.local_path_policy import LocalPathPolicy


class LocalOpenFailureReason(str, Enum):
    """Stable internal reasons why no readable regular file was produced."""

    MISSING = "missing"
    DENIED = "denied"
    IO_ERROR = "io_error"


@dataclass(frozen=True)
class LocalOpenFailure:
    """Opaque handle-open failure suitable for resolver control flow."""

    reason: LocalOpenFailureReason


@dataclass(frozen=True)
class OpenedLocalFile:
    """One live, validated local file owned by the surrounding context.

    ``display_path`` is for MIME inference and diagnostics only.  The stream is
    the authority and the path must never be reopened.
    """

    stream: BinaryIO
    display_path: str
    size_hint_bytes: int


LocalOpenResult: TypeAlias = OpenedLocalFile | LocalOpenFailure


class LocalFileReader(Protocol):
    """Narrow port consumed by local artifact resolution."""

    def open_regular_file(
        self,
        path: str | os.PathLike[str],
    ) -> AbstractContextManager[LocalOpenResult]:
        """Open and validate one file, retaining ownership through the context."""


_MISSING_ERRNOS = frozenset({errno.ENOENT})
_DENIED_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.EISDIR,
        errno.ELOOP,
        errno.ENOTDIR,
        errno.EPERM,
    }
)


def _failure_from_os_error(exc: OSError) -> LocalOpenFailure:
    if exc.errno in _MISSING_ERRNOS:
        reason = LocalOpenFailureReason.MISSING
    elif exc.errno in _DENIED_ERRNOS:
        reason = LocalOpenFailureReason.DENIED
    else:
        reason = LocalOpenFailureReason.IO_ERROR
    return LocalOpenFailure(reason)


def _close_descriptor(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)


def _posix_directory_flags() -> int | None:
    directory = getattr(os, "O_DIRECTORY", None)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if directory is None or no_follow is None:
        return None
    access = getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY))
    return (
        int(access)
        | int(directory)
        | int(no_follow)
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def _posix_leaf_flags() -> int | None:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        return None
    return (
        os.O_RDONLY
        | int(no_follow)
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NONBLOCK", 0))
        | int(getattr(os, "O_NOCTTY", 0))
    )


def _open_posix_descriptor(path: str) -> int | LocalOpenFailure:
    directory_flags = _posix_directory_flags()
    leaf_flags = _posix_leaf_flags()
    if directory_flags is None or leaf_flags is None:
        return LocalOpenFailure(LocalOpenFailureReason.IO_ERROR)

    relative = path.removeprefix(os.path.sep)
    components = relative.split(os.path.sep) if relative else []
    if not components or any(part in {"", ".", ".."} for part in components):
        return LocalOpenFailure(LocalOpenFailureReason.DENIED)

    parent_descriptor: int | None = None
    try:
        parent_descriptor = os.open(os.path.sep, directory_flags)
        for component in components[:-1]:
            previous_descriptor = parent_descriptor
            next_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=previous_descriptor,
            )
            # Ownership has split across two descriptors.  Stop the outer
            # cleanup from retrying a close whose outcome becomes ambiguous if
            # an asynchronous BaseException arrives, and clean the new child
            # explicitly before propagating.
            parent_descriptor = None
            try:
                _close_descriptor(previous_descriptor)
            except BaseException:
                _close_descriptor(next_descriptor)
                raise
            parent_descriptor = next_descriptor

        # Non-authoritative safety preflight: avoid opening an already-known
        # FIFO/device/socket.  The no-follow open plus same-fd fstat below
        # remains the race-safe final type check.
        leaf_metadata = os.stat(
            components[-1],
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(leaf_metadata.st_mode):
            return LocalOpenFailure(LocalOpenFailureReason.DENIED)
        leaf_descriptor = os.open(
            components[-1],
            leaf_flags,
            dir_fd=parent_descriptor,
        )
        previous_descriptor = parent_descriptor
        parent_descriptor = None
        try:
            _close_descriptor(previous_descriptor)
        except BaseException:
            _close_descriptor(leaf_descriptor)
            raise
        return leaf_descriptor
    except OSError as exc:
        return _failure_from_os_error(exc)
    except (NotImplementedError, ValueError):
        return LocalOpenFailure(LocalOpenFailureReason.IO_ERROR)
    finally:
        if parent_descriptor is not None:
            _close_descriptor(parent_descriptor)


def _open_windows_descriptor(path: str) -> int | LocalOpenFailure:
    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_BINARY", 0))
        | int(getattr(os, "O_NOINHERIT", 0))
    )
    try:
        return os.open(path, flags)
    except OSError as exc:
        return _failure_from_os_error(exc)
    except (NotImplementedError, ValueError):
        return LocalOpenFailure(LocalOpenFailureReason.IO_ERROR)


class HandleBoundLocalFileAccess:
    """Production local-file reader that never reopens an authorized pathname."""

    def __init__(
        self,
        path_policy: LocalPathPolicy,
        *,
        windows_final_path_api: WindowsFinalPathApi | None = None,
    ) -> None:
        self._path_policy = path_policy
        self._windows_final_path_api: WindowsFinalPathApi | None = None
        if os.name == "nt":
            if windows_final_path_api is not None:
                self._windows_final_path_api = windows_final_path_api
            else:
                try:
                    self._windows_final_path_api = CtypesWindowsFinalPathApi()
                except WindowsFinalPathError:
                    # A missing platform primitive denies resolution without
                    # preventing the application from starting.
                    self._windows_final_path_api = None

    @contextmanager
    def open_regular_file(
        self,
        path: str | os.PathLike[str],
    ) -> Iterator[LocalOpenResult]:
        """Yield one same-descriptor regular file or an opaque typed failure."""

        canonical = self._path_policy.authorize_path(path)
        if canonical is None:
            yield LocalOpenFailure(LocalOpenFailureReason.DENIED)
            return

        opened = (
            _open_windows_descriptor(canonical)
            if os.name == "nt"
            else _open_posix_descriptor(canonical)
        )
        if isinstance(opened, LocalOpenFailure):
            yield opened
            return
        descriptor = opened

        try:
            canonical = self._validate_open_descriptor(descriptor, canonical)
            metadata = os.fstat(descriptor)
        except WindowsFinalPathError:
            _close_descriptor(descriptor)
            yield LocalOpenFailure(LocalOpenFailureReason.DENIED)
            return
        except OSError:
            _close_descriptor(descriptor)
            yield LocalOpenFailure(LocalOpenFailureReason.IO_ERROR)
            return
        except BaseException:
            _close_descriptor(descriptor)
            raise

        if not stat.S_ISREG(metadata.st_mode):
            _close_descriptor(descriptor)
            yield LocalOpenFailure(LocalOpenFailureReason.DENIED)
            return

        try:
            stream = cast(BinaryIO, os.fdopen(descriptor, "rb", closefd=True))
        except OSError:
            _close_descriptor(descriptor)
            yield LocalOpenFailure(LocalOpenFailureReason.IO_ERROR)
            return
        except BaseException:
            _close_descriptor(descriptor)
            raise

        try:
            yield OpenedLocalFile(
                stream=stream,
                display_path=canonical,
                size_hint_bytes=max(0, int(metadata.st_size)),
            )
        finally:
            with suppress(OSError):
                stream.close()

    def _validate_open_descriptor(self, descriptor: int, planned_path: str) -> str:
        if os.name != "nt":
            # Descriptor-relative, no-follow component traversal is the POSIX
            # final-target proof; no second pathname lookup is needed.
            return planned_path

        if self._windows_final_path_api is None:
            raise WindowsFinalPathError("Windows final-path validation failed.")
        final_path = self._windows_final_path_api.normalized_dos_path(descriptor)
        if not self._path_policy.contains_canonical_path(final_path):
            raise WindowsFinalPathError("Windows final-path validation failed.")
        return final_path
