"""Cross-platform best-effort advisory file locks.

Used to coordinate the running Lab Tracker server with the offline
backup/restore tooling. The running server holds an exclusive lock on a
``<database>.lock`` sidecar for its whole lifetime; the offline ``restore``
command tries to acquire the same lock and refuses if it cannot, which means a
live server is using the database. The operating system releases the lock
automatically when the holding process exits, so a crash never leaves a stale
lock behind.

The lock is *advisory* and lives on a dedicated sidecar file, so it never
interferes with SQLite's own byte-range locks on the database file itself.
"""

from __future__ import annotations

import sys
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import IO

if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
    import msvcrt

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        with suppress(OSError):
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        with suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProcessLock:
    """A non-blocking, cross-process exclusive lock on a sidecar file.

    ``acquire()`` returns ``True`` when the lock was taken and ``False`` when it
    is already held by another open file description (typically a different
    process). Acquisition never blocks. The lock is released by ``release()``,
    by leaving the context manager, or by the process exiting.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._handle: IO[bytes] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+b")  # noqa: SIM115 - released in release()
        if not _try_lock(handle):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
