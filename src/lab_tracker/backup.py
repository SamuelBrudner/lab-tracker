"""SQLite backup and restore helpers for the local Lab Tracker runtime."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import make_url

DEFAULT_BACKUP_KEEP = 10


class BackupError(RuntimeError):
    """Raised when a requested backup or restore cannot be completed safely."""


@dataclass(frozen=True)
class BackupResult:
    source_path: Path | None
    backup_path: Path | None
    pruned_paths: tuple[Path, ...] = ()
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "source_path": str(self.source_path) if self.source_path else None,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "pruned_paths": [str(path) for path in self.pruned_paths],
            "skipped_reason": self.skipped_reason,
        }


@dataclass(frozen=True)
class RestoreResult:
    backup_path: Path
    restored_path: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "backup_path": str(self.backup_path),
            "restored_path": str(self.restored_path),
        }


def sqlite_database_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser().resolve()


def create_sqlite_backup(
    database_url: str,
    *,
    backup_dir: str | Path,
    keep: int = DEFAULT_BACKUP_KEEP,
    skip_missing: bool = False,
    skip_unsupported: bool = False,
) -> BackupResult:
    if keep < 1:
        raise BackupError("Backup retention must keep at least one snapshot.")

    source_path = sqlite_database_path(database_url)
    if source_path is None:
        if skip_unsupported:
            return BackupResult(
                source_path=None,
                backup_path=None,
                skipped_reason="database is not file-backed SQLite",
            )
        raise BackupError("Backup supports only file-backed SQLite database URLs.")
    if not source_path.exists():
        if skip_missing:
            return BackupResult(
                source_path=source_path,
                backup_path=None,
                skipped_reason="database file does not exist yet",
            )
        raise BackupError(f"SQLite database does not exist: {source_path}")

    destination_dir = Path(backup_dir).expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    backup_path = destination_dir / _backup_filename(source_path)

    _copy_sqlite_database(source_path, backup_path)
    _assert_sqlite_integrity(backup_path)
    pruned_paths = _prune_snapshots(destination_dir, source_path.stem, keep=keep)
    return BackupResult(
        source_path=source_path,
        backup_path=backup_path,
        pruned_paths=tuple(pruned_paths),
    )


def restore_sqlite_backup(
    backup_path: str | Path,
    database_url: str,
    *,
    force: bool = False,
) -> RestoreResult:
    source_path = Path(backup_path).expanduser().resolve()
    if not source_path.exists():
        raise BackupError(f"Backup does not exist: {source_path}")
    _assert_sqlite_integrity(source_path)

    target_path = sqlite_database_path(database_url)
    if target_path is None:
        raise BackupError("Restore supports only file-backed SQLite database URLs.")
    if target_path.exists() and not force:
        raise BackupError(
            f"Refusing to overwrite {target_path}. Re-run with --force after stopping Lab Tracker."
        )
    if target_path.exists():
        _assert_database_not_locked(target_path)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.restore-{uuid4().hex}.tmp")
    try:
        _copy_sqlite_database(source_path, temp_path)
        _assert_sqlite_integrity(temp_path)
        _remove_sqlite_sidecars(target_path)
        temp_path.replace(target_path)
        _remove_sqlite_sidecars(target_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return RestoreResult(backup_path=source_path, restored_path=target_path)


def _backup_filename(source_path: Path) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{source_path.stem}.backup-{timestamp}.sqlite3"


def _copy_sqlite_database(source_path: Path, destination_path: Path) -> None:
    uri = source_path.as_posix()
    with (
        closing(sqlite3.connect(f"file:{uri}?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(destination_path)) as destination,
        source,
        destination,
    ):
        source.backup(destination)


def _assert_sqlite_integrity(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise BackupError(f"SQLite integrity check failed for {path}.")


def _prune_snapshots(backup_dir: Path, source_stem: str, *, keep: int) -> list[Path]:
    snapshots = sorted(
        backup_dir.glob(f"{source_stem}.backup-*.sqlite3"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    pruned: list[Path] = []
    for snapshot in snapshots[keep:]:
        snapshot.unlink()
        pruned.append(snapshot)
    return pruned


def _assert_database_not_locked(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path, timeout=0.1, isolation_level=None)) as connection:
            connection.execute("PRAGMA busy_timeout=100")
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise BackupError(
            f"Refusing to restore because the target database appears to be in use: {path}"
        ) from exc


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(f"{path.name}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
