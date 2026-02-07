from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db import get_session_factory
from lab_tracker.models import QuestionType
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


def _upgrade_head(database_url: str, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    config = Config(str(repo_root / "alembic.ini"))
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    command.upgrade(config, "head")


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
        "notes",
        "sessions",
        "analyses",
        "claims",
        "visualizations",
        "dataset_question_links",
        "question_parents",
        "analysis_datasets",
        "claim_datasets",
        "claim_analyses",
        "visualization_claims",
    }
    assert expected.issubset(table_names)
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
