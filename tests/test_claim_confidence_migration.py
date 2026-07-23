"""Database and migration coverage for the claim confidence invariant."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from lab_tracker.db_models import ClaimModel

_REVISION = "0056_claim_confidence_bounds"
_PREVIOUS_REVISION = "0055_evidence_bundles"
_CONSTRAINT_NAME = "ck_claims_confidence_range"


def _alembic_config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def _current_revision(database_url: str) -> str:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()
    assert isinstance(revision, str)
    return revision


def _check_constraint_names(database_url: str) -> set[str | None]:
    engine = create_engine(database_url, future=True)
    try:
        return {
            constraint["name"]
            for constraint in inspect(engine).get_check_constraints("claims")
        }
    finally:
        engine.dispose()


def _claims_schema_snapshot(database_url: str) -> dict[str, object]:
    """Capture schema details that a SQLite batch rebuild must preserve."""

    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        return {
            "columns": tuple(
                (column["name"], column["nullable"])
                for column in inspector.get_columns("claims")
            ),
            "primary_key": tuple(
                inspector.get_pk_constraint("claims")["constrained_columns"]
            ),
            "foreign_keys": {
                (
                    tuple(foreign_key["constrained_columns"]),
                    foreign_key["referred_table"],
                    tuple(foreign_key["referred_columns"]),
                    foreign_key.get("options", {}).get("ondelete"),
                )
                for foreign_key in inspector.get_foreign_keys("claims")
            },
            "indexes": {
                (
                    index["name"],
                    tuple(index["column_names"]),
                    bool(index["unique"]),
                )
                for index in inspector.get_indexes("claims")
            },
        }
    finally:
        engine.dispose()


def _seed_claims(
    database_url: str,
    confidences: list[float],
    *,
    with_edge: bool,
) -> tuple[str, list[str], str | None]:
    project_id = str(uuid4())
    claim_ids = [str(uuid4()) for _ in confidences]
    edge_id = str(uuid4()) if with_edge else None
    assert not with_edge or len(claim_ids) >= 2

    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(project_id, name, description, status, created_at, updated_at) "
                    "VALUES (:project_id, 'Claim confidence migration', '', 'active', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"project_id": project_id},
            )
            for index, (claim_id, confidence) in enumerate(
                zip(claim_ids, confidences, strict=True)
            ):
                connection.execute(
                    text(
                        "INSERT INTO claims "
                        "(claim_id, project_id, statement, confidence, status, origin, "
                        "created_at, updated_at) VALUES "
                        "(:claim_id, :project_id, :statement, :confidence, 'proposed', "
                        "'user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "claim_id": claim_id,
                        "project_id": project_id,
                        "statement": f"Existing claim {index}",
                        "confidence": confidence,
                    },
                )
            if edge_id is not None:
                connection.execute(
                    text(
                        "INSERT INTO claim_edges "
                        "(edge_id, claim_id, target_claim_id, relation, created_at) "
                        "VALUES (:edge_id, :claim_id, :target_claim_id, 'extends', "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {
                        "edge_id": edge_id,
                        "claim_id": claim_ids[0],
                        "target_claim_id": claim_ids[1],
                    },
                )
    finally:
        engine.dispose()
    return project_id, claim_ids, edge_id


def _claim_confidences(database_url: str) -> dict[str, float]:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT claim_id, confidence FROM claims ORDER BY claim_id")
            ).all()
    finally:
        engine.dispose()
    return {str(row.claim_id): float(row.confidence) for row in rows}


def _edge_rows(database_url: str) -> set[tuple[str, str, str, str]]:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT edge_id, claim_id, target_claim_id, relation "
                    "FROM claim_edges"
                )
            ).all()
    finally:
        engine.dispose()
    return {
        (
            str(row.edge_id),
            str(row.claim_id),
            str(row.target_claim_id),
            str(row.relation),
        )
        for row in rows
    }


def _sqlite_foreign_key_check(database_url: str) -> list[tuple[object, ...]]:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text("PRAGMA foreign_key_check"))]
    finally:
        engine.dispose()


def _table_names(database_url: str) -> set[str]:
    engine = create_engine(database_url, future=True)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _update_confidence(database_url: str, claim_id: str, confidence: float) -> None:
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE claims SET confidence = :confidence "
                    "WHERE claim_id = :claim_id"
                ),
                {"claim_id": claim_id, "confidence": confidence},
            )
    finally:
        engine.dispose()


def _assert_confidences_unchanged(
    actual: dict[str, float],
    expected: dict[str, float],
) -> None:
    assert actual.keys() == expected.keys()
    for claim_id, expected_value in expected.items():
        actual_value = actual[claim_id]
        if math.isnan(expected_value):
            assert math.isnan(actual_value)
        else:
            assert actual_value == expected_value


def test_claim_model_declares_named_confidence_check() -> None:
    check_constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in ClaimModel.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert check_constraints == {
        _CONSTRAINT_NAME: "confidence >= 0 AND confidence <= 100"
    }


def test_0056_is_the_single_alembic_head() -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_heads() == [_REVISION]


def test_0056_sqlite_cycle_preserves_claim_schema_rows_and_child_links(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'claim-confidence-cycle.db'}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _project_id, claim_ids, edge_id = _seed_claims(
        database_url,
        [0.0, 0.8, 100.0],
        with_edge=True,
    )
    assert edge_id is not None
    expected_confidences = _claim_confidences(database_url)
    expected_edges = _edge_rows(database_url)
    baseline_schema = _claims_schema_snapshot(database_url)

    command.upgrade(config, _REVISION)

    assert _current_revision(database_url) == _REVISION
    assert _check_constraint_names(database_url) == {_CONSTRAINT_NAME}
    assert _claims_schema_snapshot(database_url) == baseline_schema
    _assert_confidences_unchanged(
        _claim_confidences(database_url), expected_confidences
    )
    assert _edge_rows(database_url) == expected_edges
    assert _sqlite_foreign_key_check(database_url) == []

    for invalid_confidence in (-0.01, 100.01, float("-inf"), float("inf")):
        with pytest.raises(IntegrityError) as error:
            _update_confidence(database_url, claim_ids[0], invalid_confidence)
        assert _CONSTRAINT_NAME in str(error.value.orig)
        assert _claim_confidences(database_url)[claim_ids[0]] == 0.0

    command.downgrade(config, _PREVIOUS_REVISION)

    assert _current_revision(database_url) == _PREVIOUS_REVISION
    assert _CONSTRAINT_NAME not in _check_constraint_names(database_url)
    assert _claims_schema_snapshot(database_url) == baseline_schema
    _assert_confidences_unchanged(
        _claim_confidences(database_url), expected_confidences
    )
    assert _edge_rows(database_url) == expected_edges
    assert _sqlite_foreign_key_check(database_url) == []

    command.upgrade(config, _REVISION)
    assert _current_revision(database_url) == _REVISION
    assert _check_constraint_names(database_url) == {_CONSTRAINT_NAME}


def test_0056_sqlite_preflight_aborts_without_clamping_or_schema_changes(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'claim-confidence-invalid.db'}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _project_id, claim_ids, _edge_id = _seed_claims(
        database_url,
        [-0.25, 101.25, float("inf")],
        with_edge=True,
    )
    invalid_confidences = _claim_confidences(database_url)
    baseline_schema = _claims_schema_snapshot(database_url)

    with pytest.raises(RuntimeError) as error:
        command.upgrade(config, _REVISION)

    diagnostic = str(error.value)
    assert _REVISION in diagnostic
    assert _CONSTRAINT_NAME in diagnostic
    assert "found 3 existing claim row(s)" in diagnostic
    assert "No confidence values were changed" in diagnostic
    assert "never clamps scientific values" in diagnostic
    assert all(claim_id in diagnostic for claim_id in claim_ids)
    assert _current_revision(database_url) == _PREVIOUS_REVISION
    assert _CONSTRAINT_NAME not in _check_constraint_names(database_url)
    assert _claims_schema_snapshot(database_url) == baseline_schema
    _assert_confidences_unchanged(
        _claim_confidences(database_url), invalid_confidences
    )
    assert not any(
        name.startswith("_alembic_tmp_claims") for name in _table_names(database_url)
    )
    assert _sqlite_foreign_key_check(database_url) == []

    for claim_id, corrected_confidence in zip(
        claim_ids, [0.0, 100.0, 80.0], strict=True
    ):
        _update_confidence(database_url, claim_id, corrected_confidence)

    command.upgrade(config, _REVISION)
    assert _current_revision(database_url) == _REVISION
    assert _check_constraint_names(database_url) == {_CONSTRAINT_NAME}
    assert _claim_confidences(database_url) == dict(
        zip(claim_ids, [0.0, 100.0, 80.0], strict=True)
    )


def test_0056_sqlite_holds_writer_lock_from_preflight_through_constraint_install(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'claim-confidence-race.db'}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    _project_id, claim_ids, _edge_id = _seed_claims(
        database_url,
        [50.0],
        with_edge=False,
    )

    preflight_read = Event()
    writer_update_started = Event()

    def pause_preflight_for_writer(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany: bool,
    ) -> None:
        normalized_statement = " ".join(statement.split())
        if normalized_statement.startswith(
            "SELECT claim_id, confidence FROM claims WHERE confidence IS NULL"
        ):
            preflight_read.set()
            assert writer_update_started.wait(timeout=5)

    def observe_writer_update(
        _connection,
        _cursor,
        statement: str,
        parameters,
        _context,
        _executemany: bool,
    ) -> None:
        if statement.startswith("UPDATE claims SET confidence") and parameters:
            writer_update_started.set()

    event.listen(Engine, "after_cursor_execute", pause_preflight_for_writer)
    event.listen(Engine, "before_cursor_execute", observe_writer_update)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            migration = executor.submit(command.upgrade, config, _REVISION)
            assert preflight_read.wait(timeout=5)
            writer = executor.submit(
                _update_confidence,
                database_url,
                claim_ids[0],
                101.0,
            )

            migration.result(timeout=10)
            with pytest.raises(IntegrityError) as error:
                writer.result(timeout=10)
    finally:
        event.remove(Engine, "after_cursor_execute", pause_preflight_for_writer)
        event.remove(Engine, "before_cursor_execute", observe_writer_update)

    assert _CONSTRAINT_NAME in str(error.value.orig)
    assert _current_revision(database_url) == _REVISION
    assert _claim_confidences(database_url)[claim_ids[0]] == 50.0
    assert not any(
        name.startswith("_alembic_tmp_claims") for name in _table_names(database_url)
    )


@pytest.mark.postgres
def test_0056_postgres_cycle_and_named_constraint_preserve_existing_claims(
    migrated_postgres_database_url: str,
) -> None:
    database_url = migrated_postgres_database_url
    config = _alembic_config()
    command.downgrade(config, _PREVIOUS_REVISION)
    _project_id, claim_ids, edge_id = _seed_claims(
        database_url,
        [0.0, 0.8, 100.0],
        with_edge=True,
    )
    assert edge_id is not None
    expected_confidences = _claim_confidences(database_url)
    expected_edges = _edge_rows(database_url)
    baseline_schema = _claims_schema_snapshot(database_url)

    command.upgrade(config, _REVISION)

    assert _current_revision(database_url) == _REVISION
    assert _check_constraint_names(database_url) == {_CONSTRAINT_NAME}
    assert _claims_schema_snapshot(database_url) == baseline_schema
    _assert_confidences_unchanged(
        _claim_confidences(database_url), expected_confidences
    )
    assert _edge_rows(database_url) == expected_edges

    for invalid_confidence in (
        -0.01,
        100.01,
        float("-inf"),
        float("inf"),
        float("nan"),
    ):
        with pytest.raises(IntegrityError) as error:
            _update_confidence(database_url, claim_ids[0], invalid_confidence)
        assert error.value.orig.diag.constraint_name == _CONSTRAINT_NAME
        assert _claim_confidences(database_url)[claim_ids[0]] == 0.0

    command.downgrade(config, _PREVIOUS_REVISION)

    assert _current_revision(database_url) == _PREVIOUS_REVISION
    assert _CONSTRAINT_NAME not in _check_constraint_names(database_url)
    assert _claims_schema_snapshot(database_url) == baseline_schema
    _assert_confidences_unchanged(
        _claim_confidences(database_url), expected_confidences
    )
    assert _edge_rows(database_url) == expected_edges

    command.upgrade(config, _REVISION)
    assert _current_revision(database_url) == _REVISION
    assert _check_constraint_names(database_url) == {_CONSTRAINT_NAME}


@pytest.mark.postgres
def test_0056_postgres_preflight_reports_every_invalid_scientific_value_unchanged(
    migrated_postgres_database_url: str,
) -> None:
    database_url = migrated_postgres_database_url
    config = _alembic_config()
    command.downgrade(config, _PREVIOUS_REVISION)
    _project_id, claim_ids, _edge_id = _seed_claims(
        database_url,
        [-0.25, 101.25, float("-inf"), float("inf"), float("nan")],
        with_edge=True,
    )
    invalid_confidences = _claim_confidences(database_url)
    baseline_schema = _claims_schema_snapshot(database_url)

    with pytest.raises(RuntimeError) as error:
        command.upgrade(config, _REVISION)

    diagnostic = str(error.value)
    assert _REVISION in diagnostic
    assert _CONSTRAINT_NAME in diagnostic
    assert "found 5 existing claim row(s)" in diagnostic
    assert "No confidence values were changed" in diagnostic
    assert "never clamps scientific values" in diagnostic
    assert all(claim_id in diagnostic for claim_id in claim_ids)
    assert _current_revision(database_url) == _PREVIOUS_REVISION
    assert _CONSTRAINT_NAME not in _check_constraint_names(database_url)
    assert _claims_schema_snapshot(database_url) == baseline_schema
    _assert_confidences_unchanged(
        _claim_confidences(database_url), invalid_confidences
    )
    assert not any(
        name.startswith("_alembic_tmp_claims") for name in _table_names(database_url)
    )

    for claim_id, corrected_confidence in zip(
        claim_ids, [0.0, 100.0, 25.0, 75.0, 80.0], strict=True
    ):
        _update_confidence(database_url, claim_id, corrected_confidence)

    command.upgrade(config, _REVISION)
    assert _current_revision(database_url) == _REVISION
    assert _check_constraint_names(database_url) == {_CONSTRAINT_NAME}
