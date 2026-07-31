from __future__ import annotations

from datetime import datetime, timezone
from typing import cast
from uuid import UUID, uuid4

from api_helpers import repository_backed_api
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import TextClause

from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphDraftSemanticType,
    QuestionStatus,
    QuestionType,
    SessionType,
)
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

    first_key = repository_module._session_acquisition_lock_key(first)
    second_key = repository_module._session_acquisition_lock_key(second)

    assert first_key == repository_module._session_acquisition_lock_key(first)
    assert first_key != second_key
    assert first_key != repository_module._project_question_dag_lock_key(first)
    assert first_key != repository_module._dataset_file_dataset_lock_key(first)
    assert -(2**63) <= first_key < 2**63
    assert -(2**63) <= second_key < 2**63


def test_postgres_session_acquisition_lock_is_transaction_scoped_and_expires() -> None:
    recording_session = _RecordingSession()
    repository = SQLAlchemyLabTrackerRepository(cast(Session, recording_session))
    session_id = UUID(int=7)

    repository.lock_session_acquisition_state(session_id)

    assert recording_session.calls == [
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {"lock_key": repository_module._session_acquisition_lock_key(session_id)},
        )
    ]
    assert recording_session.expire_count == 1


def test_postgres_experiment_update_locks_are_canonical_and_expire_once() -> None:
    recording_session = _RecordingSession()
    repository = SQLAlchemyLabTrackerRepository(cast(Session, recording_session))
    first = UUID(int=7)
    second = UUID(int=8)

    repository.lock_experiment_updates((second, first, second))

    assert recording_session.calls == [
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {"lock_key": repository_module._experiment_update_lock_key(first)},
        ),
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {"lock_key": repository_module._experiment_update_lock_key(second)},
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


def test_acquisition_commands_keep_lock_order_inside_one_transaction(
    monkeypatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=1), role=Role.ADMIN)
    project = api.create_project("Acquisition lock boundaries", actor=actor)
    question = api.create_question(
        project.project_id,
        "Which Session state is captured?",
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
        name="Locked membership",
        primary_question_id=question.question_id,
        actor=actor,
    )
    dataset = api.create_dataset(
        project.project_id,
        question.question_id,
        actor=actor,
    )
    context = api.sessions._context
    repository = api.sessions.repository
    original_session_lock = repository.lock_session_acquisition_state
    original_experiment_lock = repository.lock_experiment_updates
    original_dataset_lock = repository.lock_dataset_updates
    original_commit = repository.commit
    lock_order: list[tuple[str, tuple[UUID, ...]]] = []
    commit_count = 0

    def observed_session_lock(session_id: UUID) -> None:
        assert context.transaction.active
        lock_order.append(("session", (session_id,)))
        original_session_lock(session_id)

    def observed_experiment_lock(experiment_ids) -> None:
        assert context.transaction.active
        resolved_ids = tuple(experiment_ids)
        lock_order.append(("experiment", resolved_ids))
        original_experiment_lock(resolved_ids)

    def observed_dataset_lock(project_id: UUID, dataset_ids) -> None:
        assert context.transaction.active
        resolved_ids = tuple(dataset_ids)
        lock_order.append(("dataset", resolved_ids))
        original_dataset_lock(project_id, resolved_ids)

    def counted_commit() -> None:
        nonlocal commit_count
        commit_count += 1
        original_commit()

    monkeypatch.setattr(
        repository,
        "lock_session_acquisition_state",
        observed_session_lock,
    )
    monkeypatch.setattr(
        repository,
        "lock_experiment_updates",
        observed_experiment_lock,
    )
    monkeypatch.setattr(
        repository,
        "lock_dataset_updates",
        observed_dataset_lock,
    )
    monkeypatch.setattr(repository, "commit", counted_commit)

    api.acquisition_collections.capture_snapshot(
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
    api.sessions.promote_operational_session(
        session.session_id,
        question.question_id,
        actor=actor,
    )
    api.experiments.update_experiment(
        experiment.experiment_id,
        name="Locked membership renamed",
        actor=actor,
    )
    api.experiments.add_dataset(
        experiment.experiment_id,
        dataset.dataset_id,
        actor=actor,
    )
    api.experiments.remove_dataset(
        experiment.experiment_id,
        dataset.dataset_id,
        actor=actor,
    )
    api.sessions.delete_session(session.session_id, actor=actor)

    assert lock_order == [
        ("session", (session.session_id,)),
        ("session", (session.session_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("session", (session.session_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("session", (session.session_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("dataset", (dataset.dataset_id,)),
        ("experiment", (experiment.experiment_id,)),
        ("session", (session.session_id,)),
    ]
    assert commit_count == 8


def test_graph_dataset_update_prelocks_parent_experiment_before_dataset(
    monkeypatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=1), role=Role.ADMIN)
    project = api.create_project("Graph Dataset lock order", actor=actor)
    question = api.create_question(
        project.project_id,
        "Which Experiment question must the Dataset retain?",
        QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    experiment = api.create_experiment(
        project_id=project.project_id,
        name="Graph parent",
        primary_question_id=question.question_id,
        actor=actor,
    )
    dataset = api.create_dataset(
        project.project_id,
        question.question_id,
        actor=actor,
    )
    api.experiments.add_dataset(
        experiment.experiment_id,
        dataset.dataset_id,
        actor=actor,
    )

    coordinator = api.graph_drafts.commit
    repository = coordinator.commit_repository
    original_experiment_lock = repository.lock_experiment_updates
    original_dataset_lock = repository.lock_dataset_updates
    lock_order: list[tuple[str, tuple[UUID, ...]]] = []

    def observed_experiment_lock(experiment_ids) -> None:
        resolved_ids = tuple(experiment_ids)
        lock_order.append(("experiment", resolved_ids))
        original_experiment_lock(resolved_ids)

    def observed_dataset_lock(project_id: UUID, dataset_ids) -> None:
        resolved_ids = tuple(dataset_ids)
        lock_order.append(("dataset", resolved_ids))
        original_dataset_lock(project_id, resolved_ids)

    monkeypatch.setattr(
        repository,
        "lock_experiment_updates",
        observed_experiment_lock,
    )
    monkeypatch.setattr(
        repository,
        "lock_dataset_updates",
        observed_dataset_lock,
    )
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=1,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.DATASET,
        semantic_type=GraphDraftSemanticType.UPDATE_ENTITY,
        target_entity_id=dataset.dataset_id,
        payload={"status": "staged"},
    )

    with coordinator.application_transaction():
        coordinator._lock_dataset_update_projects(
            [operation],
            project_id=project.project_id,
            actor=actor,
        )

    assert lock_order == [
        ("experiment", (experiment.experiment_id,)),
        ("dataset", (dataset.dataset_id,)),
    ]
