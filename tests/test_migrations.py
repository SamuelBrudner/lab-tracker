from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from alembic import command
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
    revisions = _current_revisions(database_url)
    if not revisions:
        return None
    assert len(revisions) == 1
    return next(iter(revisions))


def _current_revisions(database_url: str) -> set[str]:
    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    try:
        inspector = inspect(engine)
        if "alembic_version" not in inspector.get_table_names():
            return set()
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        return {str(row[0]) for row in rows}
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
        assert revision in _current_revisions(database_url)
    assert _current_revision(database_url) == "0021_claim_questions"


def test_alembic_has_single_head() -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, (
        f"Expected exactly one Alembic head, found {sorted(heads)}. "
        "Migration filename prefixes (NNNN_) are decorative; Alembic chains on the "
        "revision/down_revision strings, not the number. A new migration must set "
        "down_revision to the current head (run `uv run alembic heads`). If two "
        "branches each added a head, reconcile them with `uv run alembic merge`."
    )


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
        "claim_questions",
        "visualization_claims",
        "graph_change_sets",
        "graph_change_operations",
        "daily_graph_reviews",
        "daily_graph_review_change_sets",
        "project_memberships",
    }
    assert expected.issubset(table_names)
    assert "dataset_reviews" not in table_names
    assert "note_extracted_entities" not in table_names
    assert "note_tag_suggestions" not in table_names

    project_columns = {column["name"] for column in inspector.get_columns("projects")}
    question_columns = {column["name"] for column in inspector.get_columns("questions")}
    dataset_columns = {column["name"] for column in inspector.get_columns("datasets")}
    graph_change_columns = {
        column["name"] for column in inspector.get_columns("graph_change_sets")
    }
    membership_columns = {
        column["name"] for column in inspector.get_columns("project_memberships")
    }
    visualization_columns = {
        column["name"] for column in inspector.get_columns("visualizations")
    }

    assert "review_policy" not in project_columns
    assert "created_from" not in question_columns
    assert "source_provenance" not in question_columns
    assert "manifest_extraction_provenance" not in dataset_columns
    assert {
        "submitted_at",
        "submitted_by",
        "reviewed_at",
        "reviewed_by",
        "review_note",
    }.issubset(graph_change_columns)
    assert {"project_id", "user_id", "role"}.issubset(membership_columns)
    assert {
        "asset_storage_id",
        "asset_filename",
        "asset_content_type",
        "asset_size_bytes",
        "asset_checksum",
    }.issubset(visualization_columns)
    engine.dispose()


def test_database_at_daily_review_branch_upgrades_to_current_head(monkeypatch, tmp_path):
    db_path = tmp_path / "daily-review-branch.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    config = _alembic_config()
    _set_database_url(monkeypatch, database_url)

    command.upgrade(config, "0017_daily_graph_reviews")
    assert _current_revision(database_url) == "0017_daily_graph_reviews"

    command.upgrade(config, "head")
    assert _current_revision(database_url) == "0021_claim_questions"

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert {
        "daily_graph_reviews",
        "daily_graph_review_change_sets",
        "device_tokens",
        "device_enrollments",
        "project_memberships",
    }.issubset(table_names)
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
