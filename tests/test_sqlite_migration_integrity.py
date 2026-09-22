"""SQLite migration integrity: batch rebuilds, atomicity and FK policing.

SQLite implements most ``batch_alter_table`` operations as
``CREATE _alembic_tmp_x -> INSERT ... SELECT -> DROP TABLE x -> RENAME``.  If
foreign keys are enforced while that runs, ``DROP TABLE x`` performs an implicit
``DELETE FROM x`` that fires ``ON DELETE CASCADE`` / ``SET NULL`` on every child
table.  These tests seed databases stamped at revisions whose next rebuild used
to destroy child rows, then assert every seeded row and backlink survives an
upgrade to head.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERSIONS_DIR = _REPO_ROOT / "src" / "lab_tracker" / "alembic" / "versions"

Statement = tuple[str, dict[str, Any]]


def _alembic_config() -> Config:
    return Config(str(_REPO_ROOT / "alembic.ini"))


def _database_url(tmp_path: Path, name: str) -> str:
    return f"sqlite+pysqlite:///{tmp_path / f'{name}.db'}"


@contextmanager
def _engine(database_url: str) -> Iterator[Engine]:
    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    try:
        yield engine
    finally:
        engine.dispose()


def _seed(database_url: str, statements: Sequence[Statement]) -> None:
    with _engine(database_url) as engine, engine.begin() as connection:
        for statement, parameters in statements:
            connection.execute(text(statement), parameters)


def _seed_without_foreign_keys(database_url: str, statements: Sequence[Statement]) -> None:
    with _engine(database_url) as engine, engine.connect() as connection:
        # Legacy pysqlite mode: the PRAGMA runs before any physical transaction.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
        for statement, parameters in statements:
            connection.execute(text(statement), parameters)
        connection.commit()


def _snapshot(database_url: str, watched: dict[str, tuple[str, ...]]) -> dict[str, list[tuple]]:
    with _engine(database_url) as engine, engine.connect() as connection:
        return {
            table: sorted(
                tuple(row)
                for row in connection.execute(
                    text(f"SELECT {', '.join(columns)} FROM {table}")  # noqa: S608
                )
            )
            for table, columns in watched.items()
        }


def _current_revision(database_url: str) -> str | None:
    with _engine(database_url) as engine:
        if "alembic_version" not in inspect(engine).get_table_names():
            return None
        with engine.connect() as connection:
            return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _stale_batch_tables(database_url: str) -> list[str]:
    with _engine(database_url) as engine, engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name LIKE '\\_alembic\\_tmp\\_%' ESCAPE '\\'"
                )
            )
        ]


def _foreign_key_violations(database_url: str) -> list[tuple]:
    with _engine(database_url) as engine, engine.connect() as connection:
        return [tuple(row) for row in connection.exec_driver_sql("PRAGMA foreign_key_check")]


def _column_names(database_url: str, table: str) -> set[str]:
    with _engine(database_url) as engine:
        return {column["name"] for column in inspect(engine).get_columns(table)}


_NOW = "CURRENT_TIMESTAMP"


def _project(project_id: str) -> Statement:
    return (
        "INSERT INTO projects (project_id, name, description, status, created_at, updated_at) "
        f"VALUES (:project_id, 'P', '', 'active', {_NOW}, {_NOW})",
        {"project_id": project_id},
    )


def _graph_change_set(change_set_id: str, project_id: str, note_id: str) -> Statement:
    return (
        "INSERT INTO graph_change_sets (change_set_id, project_id, source_note_id, "
        "source_note_ids, provider, model, prompt_version, draft_mode, context_packet, "
        "summary, uncertain_fields, clarification_requests, status, error_metadata, "
        "created_at, updated_at) VALUES (:change_set_id, :project_id, :note_id, '[]', "
        "'openai', 'm', 'v1', 'graph_context', '{}', '', '[]', '[]', 'committed', '{}', "
        f"{_NOW}, {_NOW})",
        {"change_set_id": change_set_id, "project_id": project_id, "note_id": note_id},
    )


def _graph_draft_scenario() -> tuple[list[Statement], dict[str, tuple[str, ...]]]:
    """Accepted AI-draft provenance as it looks at 0056..0060."""

    project_id, note_id, change_set_id = str(uuid4()), str(uuid4()), str(uuid4())
    question_id, operation_id, version_id = str(uuid4()), str(uuid4()), str(uuid4())
    statements: list[Statement] = [
        _project(project_id),
        (
            "INSERT INTO notes (note_id, project_id, raw_content, status, origin, "
            f"created_at, updated_at) VALUES (:note_id, :project_id, 'r', 'committed', "
            f"'user', {_NOW}, {_NOW})",
            {"note_id": note_id, "project_id": project_id},
        ),
        _graph_change_set(change_set_id, project_id, note_id),
        (
            "INSERT INTO graph_change_operations (operation_id, change_set_id, sequence, op, "
            "entity_type, payload, rationale, source_refs, status, error_metadata, "
            "created_at, updated_at, acceptance_mode, accepted_by) VALUES (:operation_id, "
            ":change_set_id, 1, 'create', 'question', '{}', '', '[]', 'accepted', "
            f"'{{}}', {_NOW}, {_NOW}, 'human_selected', 'alice')",
            {"operation_id": operation_id, "change_set_id": change_set_id},
        ),
        (
            "INSERT INTO questions (question_id, project_id, text, question_type, status, "
            "origin, change_set_id, origin_provider, created_at, updated_at) VALUES "
            "(:question_id, :project_id, 'Q?', 'descriptive', 'committed', 'ai_drafted', "
            f":change_set_id, 'openai', {_NOW}, {_NOW})",
            {
                "question_id": question_id,
                "project_id": project_id,
                "change_set_id": change_set_id,
            },
        ),
        (
            "INSERT INTO entity_versions (version_id, entity_type, entity_id, version_number, "
            "snapshot, change_set_id, created_at) VALUES (:version_id, 'question', "
            f":question_id, 1, '{{}}', :change_set_id, {_NOW})",
            {
                "version_id": version_id,
                "question_id": question_id,
                "change_set_id": change_set_id,
            },
        ),
    ]
    watched = {
        "graph_change_sets": ("change_set_id", "project_id", "source_note_id"),
        "graph_change_operations": (
            "operation_id",
            "change_set_id",
            "acceptance_mode",
            "accepted_by",
        ),
        "questions": ("question_id", "change_set_id", "origin"),
        "notes": ("note_id", "project_id"),
        "entity_versions": ("version_id", "change_set_id"),
    }
    return statements, watched


def _goal_scenario() -> tuple[list[Statement], dict[str, tuple[str, ...]]]:
    project_id, goal_id, link_id, question_id = (str(uuid4()) for _ in range(4))
    statements: list[Statement] = [
        _project(project_id),
        (
            "INSERT INTO goals (goal_id, project_id, goal_type, title, created_at, updated_at) "
            f"VALUES (:goal_id, :project_id, 'answer_question', 'G', {_NOW}, {_NOW})",
            {"goal_id": goal_id, "project_id": project_id},
        ),
        (
            "INSERT INTO goal_links (link_id, goal_id, entity_type, entity_id, relation, slot, "
            f"created_at) VALUES (:link_id, :goal_id, 'question', :question_id, 'milestone', "
            f"'', {_NOW})",
            {"link_id": link_id, "goal_id": goal_id, "question_id": question_id},
        ),
    ]
    watched = {
        "goals": ("goal_id", "project_id"),
        "goal_links": ("link_id", "goal_id", "entity_id"),
    }
    return statements, watched


def _origin_backlink_scenario(
    *, with_dataset: bool
) -> tuple[list[Statement], dict[str, tuple[str, ...]]]:
    """Graph as it looks at 0034, before 0035 rebuilds every origin table."""

    project_id, question_id, analysis_id, claim_id = (str(uuid4()) for _ in range(4))
    viz_id, note_id, change_set_id, dataset_id = (str(uuid4()) for _ in range(4))
    statements: list[Statement] = [
        _project(project_id),
        (
            "INSERT INTO questions (question_id, project_id, text, question_type, status, "
            f"created_at, updated_at) VALUES (:question_id, :project_id, 'Q?', 'descriptive', "
            f"'staged', {_NOW}, {_NOW})",
            {"question_id": question_id, "project_id": project_id},
        ),
        (
            "INSERT INTO question_parents (question_id, parent_question_id) "
            "VALUES (:question_id, :question_id)",
            {"question_id": question_id},
        ),
        (
            "INSERT INTO analyses (analysis_id, project_id, method_hash, code_version, "
            "executed_at, status, created_at, updated_at) VALUES (:analysis_id, :project_id, "
            f"'m', 'c', {_NOW}, 'staged', {_NOW}, {_NOW})",
            {"analysis_id": analysis_id, "project_id": project_id},
        ),
        (
            "INSERT INTO claims (claim_id, project_id, statement, confidence, status, "
            f"created_at, updated_at) VALUES (:claim_id, :project_id, 'S', 50, 'proposed', "
            f"{_NOW}, {_NOW})",
            {"claim_id": claim_id, "project_id": project_id},
        ),
        (
            "INSERT INTO claim_analyses (claim_id, analysis_id) VALUES (:claim_id, :analysis_id)",
            {"claim_id": claim_id, "analysis_id": analysis_id},
        ),
        (
            "INSERT INTO claim_questions (claim_id, question_id) VALUES (:claim_id, :question_id)",
            {"claim_id": claim_id, "question_id": question_id},
        ),
        (
            "INSERT INTO visualizations (viz_id, analysis_id, viz_type, file_path, created_at, "
            f"updated_at) VALUES (:viz_id, :analysis_id, 't', 'f', {_NOW}, {_NOW})",
            {"viz_id": viz_id, "analysis_id": analysis_id},
        ),
        (
            "INSERT INTO visualization_claims (viz_id, claim_id) VALUES (:viz_id, :claim_id)",
            {"viz_id": viz_id, "claim_id": claim_id},
        ),
        (
            "INSERT INTO notes (note_id, project_id, raw_content, status, created_at, "
            f"updated_at) VALUES (:note_id, :project_id, 'r', 'staged', {_NOW}, {_NOW})",
            {"note_id": note_id, "project_id": project_id},
        ),
        (
            "INSERT INTO note_targets (note_id, entity_type, entity_id) "
            "VALUES (:note_id, 'question', :question_id)",
            {"note_id": note_id, "question_id": question_id},
        ),
        _graph_change_set(change_set_id, project_id, note_id),
    ]
    watched: dict[str, tuple[str, ...]] = {
        "questions": ("question_id", "project_id"),
        "question_parents": ("question_id", "parent_question_id"),
        "analyses": ("analysis_id", "project_id"),
        "claims": ("claim_id", "project_id"),
        "claim_analyses": ("claim_id", "analysis_id"),
        "claim_questions": ("claim_id", "question_id"),
        "visualizations": ("viz_id", "analysis_id"),
        "visualization_claims": ("viz_id", "claim_id"),
        "notes": ("note_id", "project_id"),
        "note_targets": ("note_id", "entity_id"),
        "graph_change_sets": ("change_set_id", "source_note_id"),
    }
    if with_dataset:
        # datasets.primary_question_id is a NO ACTION child of questions, so an
        # FK-enforced DROP TABLE questions aborted 0035 half-applied.
        statements.append(
            (
                "INSERT INTO datasets (dataset_id, project_id, commit_hash, "
                "primary_question_id, status, created_at, updated_at) VALUES (:dataset_id, "
                f":project_id, 'h', :question_id, 'staged', {_NOW}, {_NOW})",
                {
                    "dataset_id": dataset_id,
                    "project_id": project_id,
                    "question_id": question_id,
                },
            )
        )
        watched["datasets"] = ("dataset_id", "primary_question_id")
    return statements, watched


def _review_policy_scenario() -> tuple[list[Statement], dict[str, tuple[str, ...]]]:
    project_id, question_id, note_id = (str(uuid4()) for _ in range(3))
    statements: list[Statement] = [
        _project(project_id),
        (
            "INSERT INTO questions (question_id, project_id, text, question_type, status, "
            f"created_at, updated_at) VALUES (:question_id, :project_id, 'Q?', 'descriptive', "
            f"'staged', {_NOW}, {_NOW})",
            {"question_id": question_id, "project_id": project_id},
        ),
        (
            "INSERT INTO notes (note_id, project_id, raw_content, status, created_at, "
            f"updated_at) VALUES (:note_id, :project_id, 'r', 'staged', {_NOW}, {_NOW})",
            {"note_id": note_id, "project_id": project_id},
        ),
    ]
    watched = {
        "projects": ("project_id",),
        "questions": ("question_id", "project_id"),
        "notes": ("note_id", "project_id"),
    }
    return statements, watched


_SCENARIOS: dict[
    str,
    tuple[str, Callable[[], tuple[list[Statement], dict[str, tuple[str, ...]]]]],
] = {
    # 0061 rebuilds graph_change_sets: CASCADE graph_change_operations and
    # SET NULL every change_set_id backlink.
    "0060_graph_drafts": ("0060_acquisition_collections", _graph_draft_scenario),
    "0056_graph_drafts": ("0056_claim_confidence_bounds", _graph_draft_scenario),
    # 0030 rebuilds goals: CASCADE goal_links.
    "0029_goal_links": ("0029_supervision_edges", _goal_scenario),
    # 0035 rebuilds claims, visualizations and every origin table.
    "0034_origin_tables": (
        "0034_terminal_transition_reasons",
        lambda: _origin_backlink_scenario(with_dataset=False),
    ),
    "0034_origin_tables_with_dataset": (
        "0034_terminal_transition_reasons",
        lambda: _origin_backlink_scenario(with_dataset=True),
    ),
    # 0008 rebuilds projects: CASCADE questions and notes.
    "0007_projects": ("0007_dataset_reviews", _review_policy_scenario),
}


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_sqlite_upgrade_to_head_preserves_children_of_rebuilt_tables(
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    start_revision, build = _SCENARIOS[scenario]
    database_url = _database_url(tmp_path, scenario)
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    command.upgrade(config, start_revision)
    statements, watched = build()
    _seed(database_url, statements)
    before = _snapshot(database_url, watched)
    assert all(before[table] for table in watched), before

    command.upgrade(config, "head")

    assert _snapshot(database_url, watched) == before
    assert _foreign_key_violations(database_url) == []
    assert _stale_batch_tables(database_url) == []


def test_sqlite_upgrade_refuses_to_commit_foreign_key_violations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path, "fk-violation")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    start_revision = "0060_acquisition_collections"
    command.upgrade(config, start_revision)
    statements, _watched = _graph_draft_scenario()
    _seed(database_url, statements)
    orphan_change_set_id = str(uuid4())
    _seed_without_foreign_keys(
        database_url,
        [
            (
                "INSERT INTO graph_change_operations (operation_id, change_set_id, sequence, "
                "op, entity_type, payload, rationale, source_refs, status, error_metadata, "
                "created_at, updated_at) VALUES (:operation_id, :change_set_id, 1, 'create', "
                f"'question', '{{}}', '', '[]', 'accepted', '{{}}', {_NOW}, {_NOW})",
                {"operation_id": str(uuid4()), "change_set_id": orphan_change_set_id},
            )
        ],
    )
    columns_before = _column_names(database_url, "graph_change_sets")

    with pytest.raises(RuntimeError) as error:
        command.upgrade(config, "head")

    message = str(error.value)
    assert "PRAGMA foreign_key_check" in message
    assert "graph_change_operations" in message
    assert "graph_change_sets" in message
    # The whole run rolled back: nothing from 0061+ was committed.
    assert _current_revision(database_url) == start_revision
    assert _column_names(database_url, "graph_change_sets") == columns_before
    assert _stale_batch_tables(database_url) == []


def test_sqlite_failed_migration_rolls_back_ddl_and_batch_residue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path, "atomic")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    start_revision = "0034_terminal_transition_reasons"
    command.upgrade(config, start_revision)
    statements, watched = _origin_backlink_scenario(with_dataset=True)
    _seed(database_url, statements)
    before = _snapshot(database_url, watched)
    claim_columns_before = _column_names(database_url, "claims")

    class InjectedDiskFailure(RuntimeError):
        pass

    def fail_claims_rename(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if " ".join(statement.split()).startswith("ALTER TABLE _alembic_tmp_claims RENAME"):
            raise InjectedDiskFailure("simulated failure while renaming _alembic_tmp_claims")

    event.listen(Engine, "before_cursor_execute", fail_claims_rename)
    try:
        with pytest.raises(InjectedDiskFailure):
            command.upgrade(config, "head")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_claims_rename)

    assert _current_revision(database_url) == start_revision
    assert _stale_batch_tables(database_url) == []
    # 0035 adds claims.created_by before rebuilding claims; it must roll back too.
    assert _column_names(database_url, "claims") == claim_columns_before
    assert _snapshot(database_url, watched) == before

    command.upgrade(config, "head")

    assert _snapshot(database_url, watched) == before
    assert _foreign_key_violations(database_url) == []


def test_sqlite_upgrade_refuses_to_run_over_stale_batch_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path, "stale-tmp")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    start_revision = "0061_graph_draft_generation_fencing"
    command.upgrade(config, start_revision)
    _seed(database_url, [("CREATE TABLE _alembic_tmp_claims (claim_id VARCHAR(36))", {})])

    with pytest.raises(RuntimeError) as error:
        command.upgrade(config, "head")

    message = str(error.value)
    assert "_alembic_tmp_claims" in message
    assert "backup" in message
    assert _current_revision(database_url) == start_revision


def test_no_revision_toggles_sqlite_foreign_keys() -> None:
    """env.py owns FK enforcement for the whole run; revisions must not toggle it.

    ``PRAGMA foreign_keys`` is a no-op inside a transaction, so a per-revision
    OFF/ON pair either does nothing or leaves enforcement off for later
    revisions depending on the starting revision.
    """

    offenders = sorted(
        path.name
        for path in _VERSIONS_DIR.glob("*.py")
        if "foreign_keys" in path.read_text(encoding="utf-8").lower()
    )
    assert offenders == []
