from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from lab_tracker.db import Base
from lab_tracker.models import EvidenceBundleRecord, Project, ProjectStatus
from lab_tracker.repository import EvidenceBundleKeyRaceError
from lab_tracker.sqlalchemy_mappers import (
    evidence_bundle_record_from_model,
    evidence_bundle_record_to_model,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts.evidence_bundles import (
    EVIDENCE_BUNDLE_KEY_CONSTRAINT,
    _is_evidence_bundle_key_race,
)


def _timestamp() -> datetime:
    return datetime(2026, 7, 22, 12, 30, tzinfo=timezone.utc)


def _project(project_id=None) -> Project:  # noqa: ANN001
    return Project(
        project_id=project_id or uuid4(),
        name="Atomic evidence bundles",
        description="Persistence test scope",
        status=ProjectStatus.ACTIVE,
        created_by="owner@example.test",
        created_at=_timestamp(),
        updated_at=_timestamp(),
    )


def _record(
    *,
    project_id,
    created_by: str = "scientist-a@example.test",
    idempotency_key: str = "bundle-key-1",
) -> EvidenceBundleRecord:
    dataset_id = str(uuid4())
    return EvidenceBundleRecord(
        bundle_id=uuid4(),
        project_id=project_id,
        created_by=created_by,
        idempotency_key=idempotency_key,
        request_fingerprint="a" * 64,
        result={
            "dataset": {"dataset_id": dataset_id},
            "created": ["dataset"],
        },
        created_at=_timestamp(),
    )


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_evidence_bundle_mapper_round_trip_preserves_uuid_strings() -> None:
    record = _record(project_id=uuid4())

    mapped = evidence_bundle_record_from_model(
        evidence_bundle_record_to_model(record)
    )

    assert mapped == record
    assert mapped.result["dataset"]["dataset_id"] == record.result["dataset"][
        "dataset_id"
    ]
    assert isinstance(mapped.result["dataset"]["dataset_id"], str)


def test_evidence_bundle_record_rejects_non_json_safe_results() -> None:
    with pytest.raises(PydanticValidationError, match="JSON-safe"):
        EvidenceBundleRecord(
            bundle_id=uuid4(),
            project_id=uuid4(),
            created_by="scientist@example.test",
            idempotency_key="unsafe-result",
            request_fingerprint="b" * 64,
            result={"dataset_id": uuid4()},
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("created_by", "   "),
        ("created_by", "x" * 256),
        ("idempotency_key", "   "),
        ("idempotency_key", "x" * 201),
        ("request_fingerprint", "A" * 64),
        ("request_fingerprint", "a" * 63),
        ("request_fingerprint", "g" * 64),
    ],
)
def test_evidence_bundle_record_enforces_storage_invariants(
    field: str,
    value: str,
) -> None:
    values = {
        "bundle_id": uuid4(),
        "project_id": uuid4(),
        "created_by": "scientist@example.test",
        "idempotency_key": "safe-key",
        "request_fingerprint": "a" * 64,
        "result": {},
    }
    values[field] = value

    with pytest.raises(PydanticValidationError):
        EvidenceBundleRecord(**values)


def test_evidence_bundle_repository_scopes_keys_and_flushes_save(db_session) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    project = _project()
    other_project = _project()
    repository.projects.save(project)
    repository.projects.save(other_project)
    db_session.flush()

    first = _record(project_id=project.project_id)
    other_principal = _record(
        project_id=project.project_id,
        created_by="scientist-b@example.test",
        idempotency_key=first.idempotency_key,
    )
    other_scope = _record(
        project_id=other_project.project_id,
        created_by=first.created_by,
        idempotency_key=first.idempotency_key,
    )
    repository.evidence_bundles.insert(first)
    repository.evidence_bundles.insert(other_principal)
    repository.evidence_bundles.insert(other_scope)

    assert db_session.scalar(text("SELECT COUNT(*) FROM evidence_bundles")) == 3
    assert repository.evidence_bundles.get(first.bundle_id) == first
    assert (
        repository.evidence_bundles.get_by_key(
            project_id=project.project_id,
            created_by=first.created_by,
            idempotency_key=first.idempotency_key,
        )
        == first
    )
    assert (
        repository.evidence_bundles.get_by_key(
            project_id=project.project_id,
            created_by="missing-principal@example.test",
            idempotency_key=first.idempotency_key,
        )
        is None
    )


def test_evidence_bundle_repository_translates_only_scoped_key_races(db_session) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    project = _project()
    repository.projects.save(project)
    db_session.flush()
    first = _record(project_id=project.project_id)
    repository.evidence_bundles.insert(first)
    db_session.commit()

    with pytest.raises(ValueError, match="append-only"):
        repository.evidence_bundles.insert(first)

    duplicate = _record(
        project_id=project.project_id,
        created_by=first.created_by,
        idempotency_key=first.idempotency_key,
    )
    with pytest.raises(EvidenceBundleKeyRaceError):
        repository.evidence_bundles.insert(duplicate)
    db_session.rollback()

    invalid_fk = _record(project_id=uuid4(), idempotency_key="unrelated-fk")
    with pytest.raises(IntegrityError):
        repository.evidence_bundles.insert(invalid_fk)
    db_session.rollback()

    assert (
        repository.evidence_bundles.get_by_key(
            project_id=project.project_id,
            created_by=first.created_by,
            idempotency_key=first.idempotency_key,
        )
        == first
    )


class _Diagnostic:
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


class _PsycopgLikeError(Exception):
    def __init__(self, constraint_name: str) -> None:
        super().__init__("duplicate key value violates unique constraint")
        self.diag = _Diagnostic(constraint_name)


def test_key_race_detection_uses_postgres_constraint_name() -> None:
    expected = IntegrityError(
        None,
        None,
        _PsycopgLikeError(EVIDENCE_BUNDLE_KEY_CONSTRAINT),
    )
    unrelated = IntegrityError(None, None, _PsycopgLikeError("uq_other_constraint"))

    assert _is_evidence_bundle_key_race(expected) is True
    assert _is_evidence_bundle_key_race(unrelated) is False


def _alembic_config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def test_0055_is_single_head_and_preserves_existing_project_across_cycle(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'evidence-bundles.db'}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0055_evidence_bundles"]

    command.upgrade(config, "0054_project_capture_key_principal_scope")
    project_id = str(uuid4())
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects "
                    "(project_id, name, description, status, created_by, created_at, "
                    "updated_at) VALUES "
                    "(:project_id, 'Existing project', '', 'active', :created_by, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "project_id": project_id,
                    "created_by": "owner@example.test",
                },
            )

        command.upgrade(config, "0055_evidence_bundles")
        inspector = inspect(engine)
        assert "evidence_bundles" in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns("evidence_bundles")} == {
            "bundle_id",
            "project_id",
            "created_by",
            "idempotency_key",
            "request_fingerprint",
            "result",
            "created_at",
        }
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("evidence_bundles")
        }
        assert unique_constraints[EVIDENCE_BUNDLE_KEY_CONSTRAINT] == (
            "project_id",
            "created_by",
            "idempotency_key",
        )

        with engine.begin() as connection:
            for created_by in ("scientist-a@example.test", "scientist-b@example.test"):
                connection.execute(
                    text(
                        "INSERT INTO evidence_bundles "
                        "(bundle_id, project_id, created_by, idempotency_key, "
                        "request_fingerprint, result, created_at) VALUES "
                        "(:bundle_id, :project_id, :created_by, 'shared-key', "
                        ":request_fingerprint, :result, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "bundle_id": str(uuid4()),
                        "project_id": project_id,
                        "created_by": created_by,
                        "request_fingerprint": "c" * 64,
                        "result": json.dumps({"created": []}),
                    },
                )

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO evidence_bundles "
                    "(bundle_id, project_id, created_by, idempotency_key, "
                    "request_fingerprint, result, created_at) VALUES "
                    "(:bundle_id, :project_id, 'scientist-a@example.test', "
                    "'shared-key', :request_fingerprint, :result, CURRENT_TIMESTAMP)"
                ),
                {
                    "bundle_id": str(uuid4()),
                    "project_id": project_id,
                    "request_fingerprint": "d" * 64,
                    "result": json.dumps({"created": []}),
                },
            )

        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT COUNT(*) FROM projects WHERE project_id = :project_id"),
                {"project_id": project_id},
            ) == 1
            assert connection.scalar(text("SELECT COUNT(*) FROM evidence_bundles")) == 2

        command.downgrade(config, "0054_project_capture_key_principal_scope")
        inspector = inspect(engine)
        assert "evidence_bundles" not in inspector.get_table_names()
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT COUNT(*) FROM projects WHERE project_id = :project_id"),
                {"project_id": project_id},
            ) == 1

        command.upgrade(config, "0055_evidence_bundles")
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT COUNT(*) FROM projects WHERE project_id = :project_id"),
                {"project_id": project_id},
            ) == 1
            assert connection.scalar(text("SELECT COUNT(*) FROM evidence_bundles")) == 0
    finally:
        engine.dispose()
