from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from lab_tracker.db import Base
from lab_tracker.db_models import GraphChangeSetModel
from lab_tracker.models import (
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftBatchTrigger,
    GraphDraftMode,
    Note,
    NoteStatus,
    Project,
    ProjectStatus,
)
from lab_tracker.services.graph_draft_generation import (
    provider_generation_lease_seconds,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _now() -> datetime:
    return datetime(2026, 8, 13, 12, tzinfo=timezone.utc)


def _candidate(
    project_id: UUID,
    note_id: UUID,
    *,
    batch_key: str = "generation:test-stable-key",
) -> GraphChangeSet:
    return GraphChangeSet(
        change_set_id=uuid4(),
        project_id=project_id,
        source_note_id=note_id,
        source_note_ids=[note_id],
        batch_key=batch_key,
        provider="fake",
        model="fake-model",
        prompt_version="test-v1",
        draft_mode=GraphDraftMode.GRAPH_CONTEXT,
        context_packet={"source": "fencing-test"},
    )


def _seed_project_and_note(repository: SQLAlchemyLabTrackerRepository) -> tuple[UUID, UUID]:
    project_id = uuid4()
    note_id = uuid4()
    repository.projects.save(
        Project(
            project_id=project_id,
            name="Fenced generation",
            status=ProjectStatus.ACTIVE,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    repository.commit()
    repository.notes.save(
        Note(
            note_id=note_id,
            project_id=project_id,
            raw_content="A durable provider input.",
            status=NoteStatus.STAGED,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    repository.commit()
    return project_id, note_id


@pytest.fixture()
def fencing_repository():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, future=True)
    session = factory()
    try:
        yield SQLAlchemyLabTrackerRepository(session)
    finally:
        session.close()
        engine.dispose()


def test_sqlite_generation_reclaim_rejects_stale_terminal_result(
    fencing_repository: SQLAlchemyLabTrackerRepository,
) -> None:
    repository = fencing_repository
    project_id, note_id = _seed_project_and_note(repository)
    first_token = uuid4()
    first, acquired = repository.graph_change_sets.claim_for_generation(
        _candidate(project_id, note_id),
        claimed_at=_now(),
        lease_until=_now() + timedelta(seconds=60),
        claim_token=first_token,
    )
    repository.commit()

    assert acquired is True
    assert first.generation_attempt_count == 1
    assert first.generation_claim_token == first_token

    joined, acquired = repository.graph_change_sets.claim_for_generation(
        _candidate(project_id, note_id),
        claimed_at=_now() + timedelta(seconds=30),
        lease_until=_now() + timedelta(seconds=90),
        claim_token=uuid4(),
    )
    repository.commit()
    assert acquired is False
    assert joined.change_set_id == first.change_set_id
    assert joined.generation_claim_token == first_token

    second_token = uuid4()
    reclaimed, acquired = repository.graph_change_sets.claim_for_generation(
        _candidate(project_id, note_id),
        claimed_at=_now() + timedelta(seconds=61),
        lease_until=_now() + timedelta(seconds=121),
        claim_token=second_token,
    )
    repository.commit()
    assert acquired is True
    assert reclaimed.change_set_id == first.change_set_id
    assert reclaimed.generation_attempt_count == 2
    assert reclaimed.generation_claim_token == second_token

    stale = first.model_copy(deep=True)
    stale.status = GraphChangeSetStatus.READY
    stale.summary = "stale provider result"
    assert (
        repository.graph_change_sets.complete_generation_claim(
            stale,
            first_token,
            completed_at=_now() + timedelta(seconds=62),
        )
        is None
    )
    repository.commit()

    winner = reclaimed.model_copy(deep=True)
    winner.status = GraphChangeSetStatus.READY
    winner.summary = "current provider result"
    completed = repository.graph_change_sets.complete_generation_claim(
        winner,
        second_token,
        completed_at=_now() + timedelta(seconds=63),
    )
    repository.commit()

    assert completed is not None
    assert completed.status == GraphChangeSetStatus.READY
    assert completed.summary == "current provider result"
    assert completed.generation_claim_token is None
    assert completed.generation_attempt_count == 2


def test_sqlite_batch_run_reclaim_and_terminal_writes_are_fenced(
    fencing_repository: SQLAlchemyLabTrackerRepository,
) -> None:
    repository = fencing_repository
    project_id, note_id = _seed_project_and_note(repository)
    run = GraphDraftBatchRun(
        run_id=uuid4(),
        project_id=project_id,
        trigger=GraphDraftBatchTrigger.MANUAL,
        status=GraphDraftBatchRunStatus.PENDING,
        window_start=_now() - timedelta(days=1),
        window_end=_now(),
        note_count=1,
        source_note_ids=[note_id],
        batch_key="batch-run:fencing-test",
        created_at=_now(),
        updated_at=_now(),
        started_at=_now(),
    )
    repository.graph_draft_batch_runs.save(run)
    repository.commit()

    first_token = uuid4()
    first = repository.graph_draft_batch_runs.claim(
        run.run_id,
        claimed_at=_now(),
        lease_until=_now() + timedelta(seconds=60),
        claim_token=first_token,
    )
    repository.commit()
    assert first is not None
    assert first.status == GraphDraftBatchRunStatus.RUNNING
    assert first.attempt_count == 1

    assert (
        repository.graph_draft_batch_runs.claim(
            run.run_id,
            claimed_at=_now() + timedelta(seconds=30),
            lease_until=_now() + timedelta(seconds=90),
            claim_token=uuid4(),
        )
        is None
    )
    repository.commit()

    second_token = uuid4()
    reclaimed = repository.graph_draft_batch_runs.claim(
        run.run_id,
        claimed_at=_now() + timedelta(seconds=61),
        lease_until=_now() + timedelta(seconds=121),
        claim_token=second_token,
    )
    repository.commit()
    assert reclaimed is not None
    assert reclaimed.attempt_count == 2
    assert reclaimed.claim_token == second_token

    stale = first.model_copy(deep=True)
    stale.status = GraphDraftBatchRunStatus.READY
    assert (
        repository.graph_draft_batch_runs.finish_claim(
            stale,
            first_token,
            finished_at=_now() + timedelta(seconds=62),
        )
        is None
    )
    repository.commit()

    reclaimed.status = GraphDraftBatchRunStatus.READY
    reclaimed.summary = "current batch result"
    completed = repository.graph_draft_batch_runs.finish_claim(
        reclaimed,
        second_token,
        finished_at=_now() + timedelta(seconds=63),
    )
    repository.commit()
    assert completed is not None
    assert completed.status == GraphDraftBatchRunStatus.READY
    assert completed.summary == "current batch result"
    assert completed.claim_token is None
    assert completed.attempt_count == 2


def test_generation_lease_uses_wrapped_provider_timeout_plus_margin() -> None:
    class LongRunningClient:
        timeout_seconds = 5400.25

    assert provider_generation_lease_seconds(LongRunningClient()) == 5431


def test_sqlite_migration_exposes_fencing_columns_defaults_and_indexes(
    migrated_sqlite_database_url: str,
) -> None:
    engine = create_engine(migrated_sqlite_database_url, future=True)
    schema = inspect(engine)
    change_set_columns = {
        column["name"] for column in schema.get_columns("graph_change_sets")
    }
    batch_run_columns = {
        column["name"] for column in schema.get_columns("graph_draft_batch_runs")
    }
    assert {
        "generation_claim_token",
        "generation_claimed_at",
        "generation_lease_expires_at",
        "generation_attempt_count",
    }.issubset(change_set_columns)
    assert {
        "claim_token",
        "claimed_at",
        "lease_expires_at",
        "attempt_count",
    }.issubset(batch_run_columns)
    assert "ix_graph_change_sets_generation_lease" in {
        item["name"] for item in schema.get_indexes("graph_change_sets")
    }
    assert "ix_graph_draft_batch_runs_claimable" in {
        item["name"] for item in schema.get_indexes("graph_draft_batch_runs")
    }

    project_id = str(uuid4())
    note_id = str(uuid4())
    change_set_id = str(uuid4())
    run_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects "
                "(project_id, name, description, status, created_at, updated_at) "
                "VALUES (:project_id, 'Migration fencing', '', 'active', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"project_id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO notes "
                "(note_id, project_id, raw_content, status, origin, created_at, updated_at) "
                "VALUES (:note_id, :project_id, 'source', 'staged', 'user', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"note_id": note_id, "project_id": project_id},
        )
        connection.execute(
            text(
                "INSERT INTO graph_change_sets "
                "(change_set_id, project_id, source_note_id, source_note_ids, provider, "
                "model, prompt_version, draft_mode, purpose, context_packet, summary, "
                "uncertain_fields, clarification_requests, status, error_metadata, "
                "created_at, updated_at, batch_key) VALUES "
                "(:change_set_id, :project_id, :note_id, '[]', 'fake', 'fake-model', "
                "'v1', 'graph_context', 'general', '{}', '', '[]', '[]', 'drafting', "
                "'{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :batch_key)"
            ),
            {
                "change_set_id": change_set_id,
                "project_id": project_id,
                "note_id": note_id,
                "batch_key": f"migration-generation-{uuid4()}",
            },
        )
        connection.execute(
            text(
                "INSERT INTO graph_draft_batch_runs "
                "(run_id, project_id, trigger, status, window_start, window_end, "
                "note_count, source_note_ids, batch_key, summary, error_metadata, "
                "created_at, updated_at, started_at) VALUES "
                "(:run_id, :project_id, 'manual', 'pending', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, 1, '[]', :batch_key, '', '{}', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "run_id": run_id,
                "project_id": project_id,
                "batch_key": f"migration-batch-{uuid4()}",
            },
        )
        assert connection.execute(
            text(
                "SELECT generation_attempt_count FROM graph_change_sets "
                "WHERE change_set_id = :change_set_id"
            ),
            {"change_set_id": change_set_id},
        ).scalar_one() == 0
        assert connection.execute(
            text(
                "SELECT attempt_count FROM graph_draft_batch_runs "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        ).scalar_one() == 0
    engine.dispose()


def test_postgres_first_claim_race_reclaim_and_stale_completion_are_fenced(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_engine(migrated_postgres_database_url, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, future=True)
    with factory() as seed_session:
        seed_repository = SQLAlchemyLabTrackerRepository(seed_session)
        project_id, note_id = _seed_project_and_note(seed_repository)

    barrier = Barrier(2)

    def claim_once() -> tuple[UUID, GraphChangeSet, bool]:
        token = uuid4()
        with factory() as session:
            repository = SQLAlchemyLabTrackerRepository(session)
            barrier.wait(timeout=5)
            claimed, acquired = repository.graph_change_sets.claim_for_generation(
                _candidate(project_id, note_id, batch_key="generation:postgres-race"),
                claimed_at=_now(),
                lease_until=_now() + timedelta(seconds=60),
                claim_token=token,
            )
            repository.commit()
            return token, claimed, acquired

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: claim_once(), range(2)))

    assert sorted(acquired for _token, _claimed, acquired in results) == [False, True]
    acquired_token, first, _ = next(item for item in results if item[2])
    joined = next(item[1] for item in results if not item[2])
    assert joined.change_set_id == first.change_set_id
    assert joined.generation_claim_token == acquired_token
    with factory() as session:
        assert session.scalar(select(func.count(GraphChangeSetModel.change_set_id))) == 1

    second_token = uuid4()
    with factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        reclaimed, acquired = repository.graph_change_sets.claim_for_generation(
            _candidate(project_id, note_id, batch_key="generation:postgres-race"),
            claimed_at=_now() + timedelta(seconds=61),
            lease_until=_now() + timedelta(seconds=121),
            claim_token=second_token,
        )
        repository.commit()
    assert acquired is True
    assert reclaimed.change_set_id == first.change_set_id
    assert reclaimed.generation_attempt_count == 2

    stale = first.model_copy(deep=True)
    stale.status = GraphChangeSetStatus.READY
    stale.summary = "stale postgres result"
    with factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        assert (
            repository.graph_change_sets.complete_generation_claim(
                stale,
                acquired_token,
                completed_at=_now() + timedelta(seconds=62),
            )
            is None
        )
        repository.commit()

    reclaimed.status = GraphChangeSetStatus.READY
    reclaimed.summary = "current postgres result"
    with factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        completed = repository.graph_change_sets.complete_generation_claim(
            reclaimed,
            second_token,
            completed_at=_now() + timedelta(seconds=63),
        )
        repository.commit()
    assert completed is not None
    assert completed.status == GraphChangeSetStatus.READY
    assert completed.summary == "current postgres result"
    engine.dispose()
