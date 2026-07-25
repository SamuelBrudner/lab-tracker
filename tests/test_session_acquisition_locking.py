from __future__ import annotations

from datetime import datetime, timezone
from typing import cast
from uuid import UUID

import pytest
from api_helpers import repository_backed_api
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import TextClause

from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import QuestionStatus, QuestionType, SessionType
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts import repository as repository_module


class _PostgresDialect:
    name = "postgresql"


class _PostgresBind:
    dialect = _PostgresDialect()


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, int]]] = []
        self.expire_count = 0

    def get_bind(self) -> _PostgresBind:
        return _PostgresBind()

    def execute(
        self,
        statement: TextClause,
        parameters: dict[str, int],
    ) -> None:
        self.calls.append((str(statement), parameters))

    def expire_all(self) -> None:
        self.expire_count += 1


def test_session_acquisition_lock_key_is_stable_scoped_and_uses_full_uuid() -> None:
    first = UUID("12345678-0000-4000-8000-000000000001")
    second = UUID("12345678-0000-4000-8000-000000000002")

    first_key = repository_module._session_acquisition_lock_key(first)  # noqa: SLF001
    second_key = repository_module._session_acquisition_lock_key(second)  # noqa: SLF001

    assert first_key == repository_module._session_acquisition_lock_key(first)  # noqa: SLF001
    assert first_key != second_key
    assert first_key != repository_module._project_question_dag_lock_key(first)  # noqa: SLF001
    assert first_key != repository_module._dataset_file_dataset_lock_key(first)  # noqa: SLF001
    assert -(2**63) <= first_key < 2**63
    assert -(2**63) <= second_key < 2**63


def test_postgres_session_acquisition_lock_is_transaction_scoped_and_expires() -> None:
    recording_session = _RecordingSession()
    repository = SQLAlchemyLabTrackerRepository(
        cast(Session, recording_session)
    )
    session_id = UUID(int=7)

    repository.lock_session_acquisition_state(session_id)

    assert recording_session.calls == [
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {
                "lock_key": repository_module._session_acquisition_lock_key(  # noqa: SLF001
                    session_id
                )
            },
        )
    ]
    assert recording_session.expire_count == 1


def test_postgres_experiment_update_locks_are_canonical_and_expire_once() -> None:
    recording_session = _RecordingSession()
    repository = SQLAlchemyLabTrackerRepository(
        cast(Session, recording_session)
    )
    first = UUID(int=7)
    second = UUID(int=8)

    repository.lock_experiment_updates((second, first, second))

    assert recording_session.calls == [
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {
                "lock_key": repository_module._experiment_update_lock_key(  # noqa: SLF001
                    first
                )
            },
        ),
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {
                "lock_key": repository_module._experiment_update_lock_key(  # noqa: SLF001
                    second
                )
            },
        ),
    ]
    assert recording_session.expire_count == 1


def test_sqlite_session_and_experiment_locks_are_no_ops() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        with Session(engine) as session:
            repository = SQLAlchemyLabTrackerRepository(session)

            repository.lock_session_acquisition_state(UUID(int=1))
            repository.lock_experiment_updates((UUID(int=3), UUID(int=2)))

            assert not session.in_transaction()
    finally:
        engine.dispose()


def test_session_state_commands_lock_inside_one_application_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=1), role=Role.ADMIN)
    project = api.create_project("Acquisition lock boundaries", actor=actor)
    question = api.create_question(
        project.project_id,
        "Which Session state is promoted?",
        QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    session = api.create_session(
        project.project_id,
        SessionType.OPERATIONAL,
        actor=actor,
    )
    experiment = api.create_experiment(
        project_id=project.project_id,
        name="Locked inheritance",
        primary_question_id=question.question_id,
        actor=actor,
    )
    context = api.sessions._context  # noqa: SLF001
    repository = api.sessions.repository
    original_lock = repository.lock_session_acquisition_state
    original_experiment_lock = repository.lock_experiment_updates
    original_commit = repository.commit
    locked_sessions: list[UUID] = []
    lock_order: list[tuple[str, tuple[UUID, ...]]] = []
    commit_count = 0

    def observed_lock(locked_session_id: UUID) -> None:
        assert context.transaction.active
        locked_sessions.append(locked_session_id)
        lock_order.append(("session", (locked_session_id,)))
        original_lock(locked_session_id)

    def observed_experiment_lock(experiment_ids) -> None:  # noqa: ANN001
        assert context.transaction.active
        resolved_ids = tuple(experiment_ids)
        lock_order.append(("experiment", resolved_ids))
        original_experiment_lock(resolved_ids)

    def counted_commit() -> None:
        nonlocal commit_count
        commit_count += 1
        original_commit()

    monkeypatch.setattr(
        repository,
        "lock_session_acquisition_state",
        observed_lock,
    )
    monkeypatch.setattr(
        repository,
        "lock_experiment_updates",
        observed_experiment_lock,
    )
    monkeypatch.setattr(repository, "commit", counted_commit)

    capture = api.acquisition_collections.capture_snapshot(
        session_id=session.session_id,
        collection_key="trials",
        client_capture_id="capture-1",
        observed_at=datetime(2026, 7, 24, 12, tzinfo=timezone.utc),
        complete=True,
        schema_version=1,
        members=[
            {
                "path": "trial-0001/data.bin",
                "checksum": "a" * 64,
                "size_bytes": 1,
            }
        ],
        actor=actor,
    )
    api.experiments.add_session(
        experiment.experiment_id,
        session.session_id,
        actor=actor,
    )
    api.experiments.remove_session(
        experiment.experiment_id,
        session.session_id,
        actor=actor,
    )
    api.experiments.add_session(
        experiment.experiment_id,
        session.session_id,
        actor=actor,
    )
    dataset = api.sessions.promote_operational_session_to_dataset(
        session.session_id,
        question.question_id,
        actor=actor,
    )
    api.experiments.update_experiment(
        experiment.experiment_id,
        name="Locked inheritance renamed",
        actor=actor,
    )
    api.experiments.remove_dataset(
        experiment.experiment_id,
        dataset.dataset_id,
        actor=actor,
    )
    api.experiments.add_dataset(
        experiment.experiment_id,
        dataset.dataset_id,
        actor=actor,
    )

    assert locked_sessions == [session.session_id] * 5
    assert lock_order == [
        ("session", (session.session_id,)),
        ("session", (session.session_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("session", (session.session_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("session", (session.session_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("session", (session.session_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("experiment", (experiment.experiment_id,)),
    ]
    assert commit_count == 8
    assert [
        reference.snapshot_id
        for reference in dataset.commit_manifest.collection_snapshots
    ] == [capture.snapshot.snapshot_id]
    assert api.experiments.query_datasets(experiment.experiment_id)[0] == [dataset]


def test_promotion_rolls_back_dataset_if_inherited_membership_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=2), role=Role.ADMIN)
    project = api.create_project("Atomic promotion", actor=actor)
    question = api.create_question(
        project.project_id,
        "Does promotion remain atomic?",
        QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    session = api.create_session(
        project.project_id,
        SessionType.OPERATIONAL,
        actor=actor,
    )
    experiment = api.create_experiment(
        project_id=project.project_id,
        name="Atomic inheritance",
        primary_question_id=question.question_id,
        actor=actor,
    )
    api.experiments.add_session(
        experiment.experiment_id,
        session.session_id,
        actor=actor,
    )
    api.sessions.register_acquisition_output(
        session.session_id,
        file_path="run/output.bin",
        checksum="sha256:atomic-promotion",
        actor=actor,
    )

    def fail_membership(**_kwargs: object) -> bool:
        raise RuntimeError("inherited membership insert failed")

    monkeypatch.setattr(
        api.sessions.repository,
        "add_experiment_dataset",
        fail_membership,
    )

    with pytest.raises(RuntimeError, match="inherited membership insert failed"):
        api.sessions.promote_operational_session_to_dataset(
            session.session_id,
            question.question_id,
            actor=actor,
        )

    datasets, total = api.sessions.repository.query_datasets(
        project_id=project.project_id,
        limit=None,
        offset=0,
    )
    assert datasets == []
    assert total == 0
