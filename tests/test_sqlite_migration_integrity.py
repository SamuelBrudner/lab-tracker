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

import logging
import re
import shlex
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine

from lab_tracker.backup import create_sqlite_backup

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_DIR = _REPO_ROOT / "src" / "lab_tracker" / "alembic"
_VERSIONS_DIR = _ALEMBIC_DIR / "versions"
_ADVISORY = "docs/advisories/2026-09-sqlite-migration-cascade.md"

Statement = tuple[str, dict[str, Any]]


def _alembic_config() -> Config:
    return Config(str(_REPO_ROOT / "alembic.ini"))


def _serve_alembic_config() -> Config:
    """Config as ``lab-tracker serve`` builds it: no alembic.ini.

    Without an ini file env.py skips ``logging.config.fileConfig``, which
    would otherwise strip pytest's capture handler from the root logger.
    """

    config = Config()
    config.set_main_option("script_location", str(_ALEMBIC_DIR))
    return config


def _head_revision() -> str:
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert head is not None
    return head


@contextmanager
def _recorded_statements() -> Iterator[list[str]]:
    statements: list[str] = []

    def record(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", record)


@contextmanager
def _lossy_rebuild_of(table: str) -> Iterator[None]:
    """Simulate a faulty batch rebuild that loses every copied parent row.

    Emptying ``_alembic_tmp_<table>`` right after Alembic copies into it
    orphans every child of ``table`` once the copy is renamed into place:
    violations this migration run introduces, not ones it found.
    """

    copy_prefix = f"INSERT INTO _alembic_tmp_{table} "

    def drop_copied_rows(
        _connection: Connection,
        cursor: sqlite3.Cursor,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if " ".join(statement.split()).startswith(copy_prefix):
            cursor.connection.execute(f"DELETE FROM _alembic_tmp_{table}")  # noqa: S608

    event.listen(Engine, "after_cursor_execute", drop_copied_rows)
    try:
        yield
    finally:
        event.remove(Engine, "after_cursor_execute", drop_copied_rows)


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


def _orphan_operation() -> Statement:
    """A graph_change_operations row whose change set does not exist."""

    return (
        "INSERT INTO graph_change_operations (operation_id, change_set_id, sequence, op, "
        "entity_type, payload, rationale, source_refs, status, error_metadata, created_at, "
        "updated_at) VALUES (:operation_id, :change_set_id, 1, 'create', 'question', '{}', "
        f"'', '[]', 'accepted', '{{}}', {_NOW}, {_NOW})",
        {"operation_id": str(uuid4()), "change_set_id": str(uuid4())},
    )


def _orphan_goal_link() -> Statement:
    """A goal_links row whose goal does not exist."""

    return (
        "INSERT INTO goal_links (link_id, goal_id, entity_type, entity_id, relation, slot, "
        "link_status, created_at) VALUES (:link_id, :goal_id, 'question', :entity_id, "
        f"'milestone', '', 'candidate', {_NOW})",
        {"link_id": str(uuid4()), "goal_id": str(uuid4()), "entity_id": str(uuid4())},
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


def _violation_rowids(violations: Sequence[tuple], table: str) -> list[int]:
    return sorted(int(row[1]) for row in violations if row[0] == table)


def _foreign_key_scans(statements: Sequence[str]) -> int:
    return sum("foreign_key_check" in statement.lower() for statement in statements)


def _assert_preexisting_violations_warned(
    caplog: pytest.LogCaptureFixture,
    expected: Sequence[str],
) -> None:
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name.startswith(("alembic", "lab_tracker"))
    ]
    assert len(warnings) == 1, [record.getMessage() for record in caplog.records]
    message = warnings[0].getMessage()
    for reference in expected:
        assert reference in message, message
    assert _ADVISORY in message


def test_sqlite_migration_tolerates_foreign_key_violations_it_did_not_introduce(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Orphans that predate a run are reported loudly but do not block it.

    SQLite databases that ran without runtime FK enforcement can hold orphan
    rows no migration created. ``alembic upgrade head`` runs at server start,
    so refusing to migrate over them would stop the server on every release
    that ships a revision.
    """

    database_url = _database_url(tmp_path, "fk-preexisting")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _serve_alembic_config()
    start_revision = "0060_acquisition_collections"
    command.upgrade(config, start_revision)
    statements, watched = _graph_draft_scenario()
    _seed(database_url, statements)
    filler_id, orphan_change_set_id = str(uuid4()), str(uuid4())
    _seed_without_foreign_keys(
        database_url,
        [
            _graph_change_set(filler_id, str(uuid4()), str(uuid4())),
            # Missing project and missing source note: two violations.
            _graph_change_set(orphan_change_set_id, str(uuid4()), str(uuid4())),
            # Leave a rowid gap below the orphan change set.
            (
                "DELETE FROM graph_change_sets WHERE change_set_id = :change_set_id",
                {"change_set_id": filler_id},
            ),
            _orphan_operation(),
        ],
    )
    before = _snapshot(database_url, watched)
    violations_before = _foreign_key_violations(database_url)
    assert len(violations_before) == 3, violations_before
    expected = (
        "graph_change_operations -> graph_change_sets: 1 row(s)",
        "graph_change_sets -> notes: 1 row(s)",
        "graph_change_sets -> projects: 1 row(s)",
    )
    caplog.set_level(logging.WARNING)

    command.upgrade(config, "head")

    assert _current_revision(database_url) == _head_revision()
    assert _snapshot(database_url, watched) == before
    violations_after = _foreign_key_violations(database_url)
    assert len(violations_after) == len(violations_before)
    # 0061 rebuilds graph_change_sets without copying the implicit rowid, so
    # the orphan change set was renumbered past the deleted filler row. A
    # baseline keyed on rowid would have mistaken it for a new violation.
    assert _violation_rowids(violations_after, "graph_change_sets") != _violation_rowids(
        violations_before, "graph_change_sets"
    )
    _assert_preexisting_violations_warned(caplog, expected)

    caplog.clear()
    command.downgrade(config, start_revision)

    assert _current_revision(database_url) == start_revision
    assert _snapshot(database_url, watched) == before
    assert len(_foreign_key_violations(database_url)) == len(violations_before)
    _assert_preexisting_violations_warned(caplog, expected)


def test_sqlite_preexisting_violation_warning_reaches_stderr_at_serve_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``lab-tracker serve`` migrates before anything configures logging.

    Alembic attaches a NullHandler to its "alembic" logger, so a warning
    logged under it would be swallowed there instead of reaching stderr
    through the root logger's last-resort handler.
    """

    database_url = _database_url(tmp_path, "fk-serve-stderr")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    command.upgrade(_alembic_config(), "0060_acquisition_collections")
    _seed_without_foreign_keys(database_url, [_orphan_goal_link()])

    # The same call serve_app makes, in a process with pristine logging.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from alembic import command\n"
            "from lab_tracker.cli import _alembic_config\n"
            "command.upgrade(_alembic_config(), 'head')\n",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr
    assert "goal_links -> goals: 1 row(s)" in result.stderr
    assert _ADVISORY in result.stderr
    assert _current_revision(database_url) == _head_revision()


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_sqlite_migration_refuses_to_commit_foreign_key_violations_it_introduces(
    direction: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path, f"fk-introduced-{direction}")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    lower_revision = "0060_acquisition_collections"
    command.upgrade(config, lower_revision)
    statements, watched = _graph_draft_scenario()
    _seed(database_url, statements)
    # Pre-existing and unrelated to the rebuilt table: it must not be blamed.
    _seed_without_foreign_keys(database_url, [_orphan_goal_link()])
    if direction == "downgrade":
        command.upgrade(config, "head")
    start_revision = _current_revision(database_url)
    columns_before = _column_names(database_url, "graph_change_sets")
    before = _snapshot(database_url, watched)

    with _lossy_rebuild_of("graph_change_sets"), pytest.raises(RuntimeError) as error:
        if direction == "upgrade":
            command.upgrade(config, "head")
        else:
            command.downgrade(config, lower_revision)

    message = str(error.value)
    assert "PRAGMA foreign_key_check" in message
    assert "graph_change_operations -> graph_change_sets: 1 row(s)" in message
    assert "goal_links" not in message
    # The whole run rolled back: no revision step was committed.
    assert _current_revision(database_url) == start_revision
    assert _column_names(database_url, "graph_change_sets") == columns_before
    assert _stale_batch_tables(database_url) == []
    assert _snapshot(database_url, watched) == before


@pytest.mark.parametrize(
    ("direction", "target"),
    [
        ("upgrade", "head"),
        ("upgrade", "heads"),
        ("upgrade", "<head id>"),
        ("downgrade", "<head id>"),
    ],
)
def test_sqlite_noop_migration_run_skips_foreign_key_scans(
    direction: str,
    target: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path, "fk-noop")
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    head = _head_revision()
    with _recorded_statements() as pending_run:
        command.upgrade(config, "head")
    # With revisions pending, the scan runs as a baseline and again before commit.
    assert _foreign_key_scans(pending_run) == 2
    _seed_without_foreign_keys(database_url, [_orphan_goal_link()])

    with _recorded_statements() as noop_run:
        getattr(command, direction)(config, head if target == "<head id>" else target)

    assert noop_run, "the listener saw no statements from the no-op run"
    assert _foreign_key_scans(noop_run) == 0, noop_run
    assert _current_revision(database_url) == head


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
    backup_path = create_sqlite_backup(database_url, backup_dir=tmp_path / "backups").backup_path
    assert backup_path is not None
    _seed(database_url, [("CREATE TABLE _alembic_tmp_claims (claim_id VARCHAR(36))", {})])

    with pytest.raises(RuntimeError) as error:
        command.upgrade(config, "head")

    message = str(error.value)
    assert "_alembic_tmp_claims" in message
    assert "backup" in message
    assert _current_revision(database_url) == start_revision

    # Follow the recovery the message prescribes, through the installed
    # console script and its real argument parser.
    cli_commands = re.findall(r"`(lab-tracker [^`]+)`", message)
    assert cli_commands, message
    for cli_command in cli_commands:
        program, *arguments = shlex.split(
            cli_command.replace("<backup>", shlex.quote(str(backup_path)))
        )
        (console_script,) = entry_points(group="console_scripts", name=program)
        console_script.load()(arguments)

    assert _stale_batch_tables(database_url) == []
    command.upgrade(config, "head")
    assert _current_revision(database_url) == _head_revision()


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
