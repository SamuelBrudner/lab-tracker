from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def _database_url(tmp_path, name: str) -> str:
    return f"sqlite+pysqlite:///{tmp_path / name}"


def _insert_keyed_project(
    database_url: str,
    *,
    client_capture_id: str,
    created_by: str | None,
) -> None:
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(project_id, name, description, status, client_capture_id, created_by, "
                    "created_at, updated_at) VALUES "
                    "(:project_id, :name, '', 'active', :client_capture_id, :created_by, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "project_id": str(uuid4()),
                    "name": f"Migration project {uuid4().hex[:8]}",
                    "client_capture_id": client_capture_id,
                    "created_by": created_by,
                },
            )
    finally:
        engine.dispose()


def test_0054_preflight_rejects_keyed_project_without_creator(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = _database_url(tmp_path, "missing-creator.db")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _config()
    command.upgrade(config, "0053_personal_access_token_scope")
    _insert_keyed_project(
        database_url,
        client_capture_id="legacy-unowned-key",
        created_by=None,
    )

    with pytest.raises(RuntimeError, match="has no created_by principal"):
        command.upgrade(config, "0054_project_capture_key_principal_scope")


def test_0054_enforces_creator_and_capture_key_pair_and_guards_downgrade(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = _database_url(tmp_path, "principal-scope.db")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _config()
    command.upgrade(config, "0054_project_capture_key_principal_scope")

    engine = create_engine(database_url, future=True)
    try:
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspect(engine).get_unique_constraints("projects")
        }
    finally:
        engine.dispose()
    assert unique_constraints["uq_projects_creator_client_capture"] == (
        "created_by",
        "client_capture_id",
    )

    shared_key = "same-key-different-principals"
    _insert_keyed_project(database_url, client_capture_id=shared_key, created_by=str(uuid4()))
    _insert_keyed_project(database_url, client_capture_id=shared_key, created_by=str(uuid4()))

    with pytest.raises(RuntimeError, match="Cannot restore globally unique"):
        command.downgrade(config, "0053_personal_access_token_scope")


@pytest.mark.postgres
def test_0054_postgres_upgrade_downgrade_upgrade_cycle(
    migrated_postgres_database_url: str,
) -> None:
    config = _config()

    command.downgrade(config, "0053_personal_access_token_scope")
    command.upgrade(config, "0054_project_capture_key_principal_scope")

    engine = create_engine(migrated_postgres_database_url, future=True)
    try:
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspect(engine).get_unique_constraints("projects")
        }
    finally:
        engine.dispose()
    assert unique_constraints["uq_projects_creator_client_capture"] == (
        "created_by",
        "client_capture_id",
    )


@pytest.mark.postgres
def test_0054_postgres_preflight_rejects_keyed_project_without_creator(
    migrated_postgres_database_url: str,
) -> None:
    config = _config()
    command.downgrade(config, "0053_personal_access_token_scope")
    _insert_keyed_project(
        migrated_postgres_database_url,
        client_capture_id="legacy-unowned-postgres-key",
        created_by=None,
    )

    with pytest.raises(RuntimeError, match="has no created_by principal"):
        command.upgrade(config, "0054_project_capture_key_principal_scope")
