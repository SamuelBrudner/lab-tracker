from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from lab_tracker.app import create_app
from lab_tracker.auth import LOCAL_AUTH_USER_ID
from lab_tracker.db_models import ProjectModel


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def test_project_model_declares_capture_creator_check() -> None:
    check_names = {
        constraint.name
        for constraint in ProjectModel.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_projects_client_capture_creator" in check_names


def _database_url(tmp_path, name: str) -> str:
    return f"sqlite+pysqlite:///{tmp_path / name}"


def _insert_keyed_project(
    database_url: str,
    *,
    client_capture_id: str | None,
    created_by: str | None,
    name: str | None = None,
) -> str:
    project_id = str(uuid4())
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
                    "project_id": project_id,
                    "name": name or f"Migration project {uuid4().hex[:8]}",
                    "client_capture_id": client_capture_id,
                    "created_by": created_by,
                },
            )
    finally:
        engine.dispose()
    return project_id


def _insert_question_child(database_url: str, project_id: str) -> str:
    question_id = str(uuid4())
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO questions "
                    "(question_id, project_id, text, question_type, status, origin, "
                    "created_at, updated_at) VALUES "
                    "(:question_id, :project_id, :text, 'descriptive', 'staged', "
                    "'user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "question_id": question_id,
                    "project_id": project_id,
                    "text": "Preserved child question?",
                },
            )
    finally:
        engine.dispose()
    return question_id


def _project_constraints(database_url: str) -> tuple[dict[str, tuple[str, ...]], set[str]]:
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("projects")
        }
        check_constraints = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("projects")
        }
    finally:
        engine.dispose()
    return unique_constraints, check_constraints


def _assert_project_tree_exists(
    database_url: str,
    *,
    project_id: str,
    question_id: str,
) -> None:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            project_count = connection.scalar(
                text("SELECT COUNT(*) FROM projects WHERE project_id = :project_id"),
                {"project_id": project_id},
            )
            question_count = connection.scalar(
                text("SELECT COUNT(*) FROM questions WHERE question_id = :question_id"),
                {"question_id": question_id},
            )
    finally:
        engine.dispose()
    assert project_count == 1
    assert question_count == 1


def _assert_capture_creator_check(database_url: str) -> None:
    for created_by in (None, "   "):
        with pytest.raises(IntegrityError):
            _insert_keyed_project(
                database_url,
                client_capture_id=f"invalid-principal-{uuid4()}",
                created_by=created_by,
            )

    # The invariant is conditional: ordinary projects without a capture key
    # may retain legacy/null attribution.
    _insert_keyed_project(
        database_url,
        client_capture_id=None,
        created_by=None,
    )


def _exercise_valid_existing_data_cycle(database_url: str) -> None:
    config = _config()
    capture_key = f"preserved-key-{uuid4()}"
    project_name = "Preserved pre-0054 project"
    project_id = _insert_keyed_project(
        database_url,
        client_capture_id=capture_key,
        created_by=str(LOCAL_AUTH_USER_ID),
        name=project_name,
    )
    question_id = _insert_question_child(database_url, project_id)

    command.upgrade(config, "0054_project_capture_key_principal_scope")
    _assert_project_tree_exists(
        database_url,
        project_id=project_id,
        question_id=question_id,
    )
    unique_constraints, check_constraints = _project_constraints(database_url)
    assert unique_constraints["uq_projects_creator_client_capture"] == (
        "created_by",
        "client_capture_id",
    )
    assert "ck_projects_client_capture_creator" in check_constraints
    _assert_capture_creator_check(database_url)

    application = create_app()
    try:
        with TestClient(application) as client:
            replay = client.post(
                "/projects",
                json={
                    "name": project_name,
                    "client_capture_id": capture_key,
                },
            )
    finally:
        application.state.db_engine.dispose()
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["project_id"] == project_id

    command.downgrade(config, "0053_personal_access_token_scope")
    _assert_project_tree_exists(
        database_url,
        project_id=project_id,
        question_id=question_id,
    )
    unique_constraints, check_constraints = _project_constraints(database_url)
    assert unique_constraints["uq_projects_client_capture"] == ("client_capture_id",)
    assert "uq_projects_creator_client_capture" not in unique_constraints
    assert "ck_projects_client_capture_creator" not in check_constraints


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


def test_0054_preserves_valid_existing_project_tree_and_replay_across_cycle(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = _database_url(tmp_path, "preserved-tree.db")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "file-storage"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path / "note-storage"))
    config = _config()
    command.upgrade(config, "0053_personal_access_token_scope")

    _exercise_valid_existing_data_cycle(database_url)


def test_0054_enforces_creator_and_capture_key_pair_and_guards_downgrade(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = _database_url(tmp_path, "principal-scope.db")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _config()
    command.upgrade(config, "0054_project_capture_key_principal_scope")

    unique_constraints, check_constraints = _project_constraints(database_url)
    assert unique_constraints["uq_projects_creator_client_capture"] == (
        "created_by",
        "client_capture_id",
    )
    assert "ck_projects_client_capture_creator" in check_constraints
    _assert_capture_creator_check(database_url)

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

    unique_constraints, check_constraints = _project_constraints(
        migrated_postgres_database_url
    )
    assert unique_constraints["uq_projects_creator_client_capture"] == (
        "created_by",
        "client_capture_id",
    )
    assert "ck_projects_client_capture_creator" in check_constraints


@pytest.mark.postgres
def test_0054_postgres_preserves_valid_existing_project_tree_and_replay_across_cycle(
    migrated_postgres_database_url: str,
    monkeypatch,
) -> None:
    config = _config()
    command.downgrade(config, "0053_personal_access_token_scope")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")

    _exercise_valid_existing_data_cycle(migrated_postgres_database_url)


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
