from __future__ import annotations

import csv
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import create_engine, insert

from lab_tracker import dolt_mirror
from lab_tracker.db import Base
from lab_tracker.db_models import ProjectModel


def _bootstrap_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'mirror.db'}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "test-secret")
    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(ProjectModel),
            [
                {
                    "project_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    "name": "B",
                    "description": "",
                    "status": "active",
                },
                {
                    "project_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "name": "A",
                    "description": "",
                    "status": "active",
                },
            ],
        )
    engine.dispose()
    return database_url


def test_retained_table_exports_exclude_users() -> None:
    table_names = {table.name for table in dolt_mirror.retained_tables()}

    assert "users" not in table_names
    assert "projects" in table_names
    assert "questions" in table_names
    assert "notes" in table_names
    assert "graph_change_sets" in table_names
    assert "graph_change_operations" in table_names


def test_export_tables_writes_deterministic_primary_key_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _bootstrap_db(monkeypatch, tmp_path)

    exports = dolt_mirror.export_tables(tmp_path / "export")
    projects_export = next(export for export in exports if export.name == "projects")

    with projects_export.csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert [row["project_id"] for row in rows] == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    ]
    assert projects_export.primary_keys == ("project_id",)
    assert projects_export.row_count == 2


def test_export_to_dolt_uses_create_replace_add_and_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _bootstrap_db(monkeypatch, tmp_path)
    commands: list[tuple[str, ...]] = []

    class FakeRunner:
        def __init__(self, dolt_bin: str, cwd: Path) -> None:
            assert dolt_bin == "dolt-test"
            assert cwd == tmp_path / "mirror"

        def run(
            self,
            *args: str,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            if args == ("table", "ls"):
                return subprocess.CompletedProcess(args, 0, stdout="projects\n")
            if args == ("status", "--porcelain"):
                return subprocess.CompletedProcess(args, 0, stdout=" M projects\n")
            return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(dolt_mirror, "DoltRunner", FakeRunner)

    result = dolt_mirror.export_to_dolt(
        mirror_path=tmp_path / "mirror",
        message="mirror snapshot",
        dolt_bin="dolt-test",
    )

    assert result.commit_created is True
    assert ("init",) in commands
    assert ("table", "import", "-r", "projects", "_export\\projects.csv") in commands or (
        "table",
        "import",
        "-r",
        "projects",
        "_export/projects.csv",
    ) in commands
    assert ("add", ".") in commands
    assert ("commit", "-m", "mirror snapshot") in commands


def test_missing_dolt_binary_has_clear_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(dolt_mirror.subprocess, "run", fake_run)
    runner = dolt_mirror.DoltRunner("not-a-real-dolt", tmp_path)

    with pytest.raises(dolt_mirror.DoltMirrorError, match="LAB_TRACKER_DOLT_BIN"):
        runner.run("status")
