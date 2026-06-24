from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from lab_tracker.backup import (
    BackupError,
    _copy_sqlite_database,
    _lock_path_for,
    _sqlite_ro_uri,
    create_sqlite_backup,
    database_lock_path,
    restore_sqlite_backup,
    sqlite_database_path,
)
from lab_tracker.cli import main
from lab_tracker.process_lock import ProcessLock


def _sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _read_values(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection, connection:
        rows = connection.execute("SELECT value FROM example ORDER BY value").fetchall()
    return [row[0] for row in rows]


def _write_example_database(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE example (value TEXT NOT NULL)")
        connection.execute("INSERT INTO example (value) VALUES (?)", (value,))


def test_sqlite_database_path_resolves_file_backed_urls(tmp_path: Path) -> None:
    db_path = tmp_path / "lab-tracker.db"

    assert sqlite_database_path(_sqlite_url(db_path)) == db_path.resolve()
    assert sqlite_database_path("sqlite+pysqlite:///:memory:") is None
    assert sqlite_database_path("postgresql+psycopg://example") is None


def test_backup_uses_online_api_and_captures_wal_commits(tmp_path: Path) -> None:
    db_path = tmp_path / "live.db"
    backup_dir = tmp_path / "backups"
    live_connection = sqlite3.connect(db_path)
    try:
        live_connection.execute("PRAGMA journal_mode=WAL")
        live_connection.execute("CREATE TABLE example (value TEXT NOT NULL)")
        live_connection.execute("INSERT INTO example (value) VALUES ('committed')")
        live_connection.commit()

        result = create_sqlite_backup(_sqlite_url(db_path), backup_dir=backup_dir)
    finally:
        live_connection.close()

    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert _read_values(result.backup_path) == ["committed"]


def test_backup_prunes_old_snapshots(tmp_path: Path) -> None:
    db_path = tmp_path / "lab_tracker.db"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    _write_example_database(db_path, "current")

    old_paths = [
        backup_dir / f"lab_tracker.backup-2026010{index}T000000Z.sqlite3"
        for index in range(3)
    ]
    for index, path in enumerate(old_paths):
        _write_example_database(path, f"old-{index}")

    result = create_sqlite_backup(_sqlite_url(db_path), backup_dir=backup_dir, keep=2)

    assert result.backup_path is not None
    remaining = sorted(path.name for path in backup_dir.glob("lab_tracker.backup-*.sqlite3"))
    assert len(remaining) == 2
    assert result.backup_path.name in remaining


def test_restore_refuses_existing_database_without_force(tmp_path: Path) -> None:
    backup_path = tmp_path / "backup.sqlite3"
    target_path = tmp_path / "target.db"
    _write_example_database(backup_path, "backup")
    _write_example_database(target_path, "target")

    with pytest.raises(BackupError, match="Refusing to overwrite"):
        restore_sqlite_backup(backup_path, _sqlite_url(target_path))

    assert _read_values(target_path) == ["target"]


def test_restore_refuses_locked_target_database(tmp_path: Path) -> None:
    backup_path = tmp_path / "backup.sqlite3"
    target_path = tmp_path / "target.db"
    _write_example_database(backup_path, "backup")
    _write_example_database(target_path, "target")

    lock_connection = sqlite3.connect(target_path)
    lock_connection.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(BackupError, match="appears to be in use"):
            restore_sqlite_backup(backup_path, _sqlite_url(target_path), force=True)
    finally:
        lock_connection.rollback()
        lock_connection.close()

    assert _read_values(target_path) == ["target"]


def test_restore_refuses_when_database_lock_is_held(tmp_path: Path) -> None:
    # Simulates a *running* server: it holds the advisory lock but has no active
    # SQLite transaction (an idle WAL pool connection), which the old
    # BEGIN EXCLUSIVE probe could not detect.
    backup_path = tmp_path / "backup.sqlite3"
    target_path = tmp_path / "target.db"
    _write_example_database(backup_path, "backup")
    _write_example_database(target_path, "target")

    server_lock = ProcessLock(_lock_path_for(target_path.resolve()))
    assert server_lock.acquire()
    try:
        with pytest.raises(BackupError, match="appears to be running"):
            restore_sqlite_backup(backup_path, _sqlite_url(target_path), force=True)
    finally:
        server_lock.release()

    assert _read_values(target_path) == ["target"]

    # Once the "server" releases the lock, restore proceeds normally.
    result = restore_sqlite_backup(backup_path, _sqlite_url(target_path), force=True)
    assert result.restored_path == target_path.resolve()
    assert _read_values(target_path) == ["backup"]


def test_database_lock_path_only_for_file_backed_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "lab.db"
    assert database_lock_path(_sqlite_url(db_path)) == _lock_path_for(db_path.resolve())
    assert database_lock_path("sqlite+pysqlite:///:memory:") is None
    assert database_lock_path("postgresql+psycopg://example") is None


def test_copy_sqlite_database_round_trips_path_with_special_characters(
    tmp_path: Path,
) -> None:
    # Spaces and '#' need URI escaping; the old f"file:{as_posix()}?mode=ro"
    # form left them bare and could address the wrong database.
    source_path = tmp_path / "weird name #1.db"
    destination_path = tmp_path / "copy.db"
    _write_example_database(source_path, "special")

    _copy_sqlite_database(source_path, destination_path)

    assert _read_values(destination_path) == ["special"]


def test_sqlite_ro_uri_is_absolute_and_escaped() -> None:
    posix_uri = _sqlite_ro_uri(PurePosixPath("/var/lib/lab tracker.db"))
    assert posix_uri.startswith("file:///")
    assert posix_uri.endswith("?mode=ro")
    assert "%20" in posix_uri  # space is percent-encoded, not left bare

    # A Windows drive-letter path must yield file:///C:/..., not a relative
    # file:C:/... that SQLite resolves against the current directory.
    windows_uri = _sqlite_ro_uri(PureWindowsPath("C:/Users/sam/lab.db"))
    assert windows_uri == "file:///C:/Users/sam/lab.db?mode=ro"


def test_restore_replaces_target_database_with_backup(tmp_path: Path) -> None:
    backup_path = tmp_path / "backup.sqlite3"
    target_path = tmp_path / "target.db"
    _write_example_database(backup_path, "backup")
    _write_example_database(target_path, "target")

    result = restore_sqlite_backup(backup_path, _sqlite_url(target_path), force=True)

    assert result.restored_path == target_path.resolve()
    assert _read_values(target_path) == ["backup"]


def test_backup_and_restore_cli_round_trip(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "source.db"
    restored_path = tmp_path / "restored.db"
    backup_dir = tmp_path / "backups"
    _write_example_database(db_path, "from-cli")

    main(
        [
            "backup",
            "--database-url",
            _sqlite_url(db_path),
            "--to",
            str(backup_dir),
            "--keep",
            "1",
        ]
    )
    backup_output = json.loads(capsys.readouterr().out)

    main(
        [
            "restore",
            backup_output["backup_path"],
            "--database-url",
            _sqlite_url(restored_path),
        ]
    )
    restore_output = json.loads(capsys.readouterr().out)

    assert Path(backup_output["backup_path"]).exists()
    assert restore_output["restored_path"] == str(restored_path.resolve())
    assert _read_values(restored_path) == ["from-cli"]
