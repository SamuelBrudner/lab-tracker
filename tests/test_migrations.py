from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy import text

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db import get_session_factory
from lab_tracker.models import QuestionType
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


def _alembic_config() -> Config:
    repo_root = Path(__file__).resolve().parent.parent
    return Config(str(repo_root / "alembic.ini"))


def _set_database_url(monkeypatch, database_url: str) -> None:
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)


def _ordered_revisions(config: Config) -> list[str]:
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions(base="base", head="heads"))
    revisions.reverse()
    return [revision.revision for revision in revisions]


def _current_revision(database_url: str) -> str | None:
    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    try:
        inspector = inspect(engine)
        if "alembic_version" not in inspector.get_table_names():
            return None
        with engine.connect() as connection:
            row = connection.execute(text("SELECT version_num FROM alembic_version")).one_or_none()
        return str(row[0]) if row else None
    finally:
        engine.dispose()


def _upgrade_head(database_url: str, monkeypatch) -> None:
    _set_database_url(monkeypatch, database_url)
    command.upgrade(_alembic_config(), "head")


def test_alembic_upgrade_chain_from_empty_to_head(monkeypatch, tmp_path):
    db_path = tmp_path / "migrations-chain.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    _set_database_url(monkeypatch, database_url)

    assert _current_revision(database_url) is None

    revisions = _ordered_revisions(config)
    assert revisions
    for revision in revisions:
        command.upgrade(config, revision)
        assert _current_revision(database_url) == revision


def test_alembic_upgrade_head_creates_expected_tables(monkeypatch, tmp_path):
    db_path = tmp_path / "migrations-smoke.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    _upgrade_head(database_url, monkeypatch)

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    expected = {
        "projects",
        "questions",
        "datasets",
        "dataset_files",
        "notes",
        "sessions",
        "acquisition_outputs",
        "analyses",
        "claims",
        "visualizations",
        "dataset_question_links",
        "question_parents",
        "analysis_datasets",
        "claim_datasets",
        "claim_analyses",
        "visualization_claims",
        "graph_change_sets",
        "graph_change_operations",
    }
    assert expected.issubset(table_names)
    assert "dataset_reviews" not in table_names
    assert "note_extracted_entities" not in table_names
    assert "note_tag_suggestions" not in table_names

    project_columns = {column["name"] for column in inspector.get_columns("projects")}
    question_columns = {column["name"] for column in inspector.get_columns("questions")}
    dataset_columns = {column["name"] for column in inspector.get_columns("datasets")}

    assert "review_policy" not in project_columns
    assert "created_from" not in question_columns
    assert "source_provenance" not in question_columns
    assert "manifest_extraction_provenance" not in dataset_columns
    engine.dispose()


def test_migrated_database_supports_api_round_trip(monkeypatch, tmp_path):
    db_path = tmp_path / "migrations-roundtrip.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    _upgrade_head(database_url, monkeypatch)
    actor = _actor()

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    session_factory = get_session_factory(engine=engine)

    with session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        project = api.create_project("Migrated DB", actor=actor)
        question = api.create_question(
            project_id=project.project_id,
            text="Is Alembic wiring valid?",
            question_type=QuestionType.DESCRIPTIVE,
            actor=actor,
        )
        dataset = api.create_dataset(
            project_id=project.project_id,
            primary_question_id=question.question_id,
            actor=actor,
        )

    with session_factory() as session:
        api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session))
        assert api.get_project(project.project_id).name == "Migrated DB"
        assert api.get_question(question.question_id).project_id == project.project_id
        assert api.get_dataset(dataset.dataset_id).primary_question_id == question.question_id

    engine.dispose()
