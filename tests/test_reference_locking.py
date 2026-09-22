"""Reference guards and reference-adding writes serialize on one project lock.

The interleaving tests mimic a concurrent transaction that wins the project
reference lock first: the patched lock commits the competing write before the
command under test re-reads, exactly as PostgreSQL would after the advisory
lock wait (see ``test_postgres_reference_locking.py`` for the real race).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread
from uuid import UUID, uuid4

import pytest
from api_helpers import repository_backed_api
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db import Base
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.member_onboarding import (
    CHECKPOINT_ROLE,
    CHECKPOINT_ROLE_KEY,
    FIRST_CAPTURE_NOTE_ID_KEY,
)
from lab_tracker.models import (
    AcceptanceMode,
    Claim,
    ClaimEdge,
    ClaimRelation,
    ClaimStatus,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    EntityRef,
    EntityType,
    ExplorationNodeType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    QuestionStatus,
    QuestionType,
    SessionType,
)
from lab_tracker.services.evidence_bundle_service import (
    CreateAnalysisIntent,
    CreateDatasetIntent,
    RecordEvidenceBundleCommand,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _actor() -> AuthContext:
    return AuthContext(user_id=UUID(int=1), role=Role.ADMIN)


def _project_with_question(api):
    actor = _actor()
    project = api.create_project("Reference locking", actor=actor)
    question = api.create_question(
        project.project_id,
        "Which references must stay consistent?",
        QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    return actor, project, question


def _record_lock_calls(
    monkeypatch: pytest.MonkeyPatch,
    repository: SQLAlchemyLabTrackerRepository,
) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []

    def recorder(name: str) -> Callable[..., None]:
        original = getattr(repository, name)

        def record(*args, **kwargs) -> None:
            first = args[0] if args else None
            calls.append((name, tuple(first) if name == "lock_experiment_updates" else first))
            original(*args, **kwargs)

        return record

    for name in (
        "lock_project_references",
        "lock_session_acquisition_state",
        "lock_experiment_updates",
        "lock_dataset_updates",
    ):
        monkeypatch.setattr(repository, name, recorder(name))
    return calls


def test_reference_writers_take_the_project_reference_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor, project, question = _project_with_question(api)
    dataset = api.create_dataset(
        project.project_id,
        question.question_id,
        status=DatasetStatus.COMMITTED,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")]
        ),
        actor=actor,
    )
    claim = api.create_claim(project.project_id, "Existing claim.", 0.5, actor=actor)
    staged_analysis = api.create_analysis(
        project.project_id,
        [dataset.dataset_id],
        "method-0",
        "v0",
        actor=actor,
    )
    other = api.create_claim(project.project_id, "Other claim.", 0.5, actor=actor)
    calls = _record_lock_calls(monkeypatch, api.claims.repository)

    for write in (
        lambda: api.create_claim(
            project.project_id,
            "New claim.",
            0.5,
            supported_by_dataset_ids=[dataset.dataset_id],
            actor=actor,
        ),
        lambda: api.update_claim(
            claim.claim_id,
            supported_by_dataset_ids=[dataset.dataset_id],
            actor=actor,
        ),
        lambda: api.create_analysis(
            project.project_id,
            [dataset.dataset_id],
            "method-1",
            "v1",
            actor=actor,
        ),
        lambda: api.commit_analysis(staged_analysis.analysis_id, actor=actor),
        lambda: api.create_claim_edge(
            claim.claim_id,
            target_claim_id=other.claim_id,
            relation=ClaimRelation.DEPENDS_ON,
            actor=actor,
        ),
    ):
        calls.clear()
        write()
        assert calls == [("lock_project_references", project.project_id)]


def test_every_guarded_delete_takes_the_reference_lock_before_entity_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor, project, question = _project_with_question(api)
    dataset = api.create_dataset(project.project_id, question.question_id, actor=actor)
    session = api.create_session(project.project_id, SessionType.OPERATIONAL, actor=actor)
    note = api.create_note(project.project_id, "Loose note.", actor=actor)
    claim = api.create_claim(project.project_id, "Loose claim.", 0.5, actor=actor)
    analysis_dataset = api.create_dataset(project.project_id, question.question_id, actor=actor)
    analysis = api.create_analysis(
        project.project_id,
        [analysis_dataset.dataset_id],
        "method",
        "v1",
        actor=actor,
    )
    visualization = api.create_visualization(
        analysis_id=analysis.analysis_id,
        viz_type="line",
        file_path="/tmp/figure.png",
        actor=actor,
    )
    node = api.create_exploration_node(
        project_id=project.project_id,
        node_type=ExplorationNodeType.DECISION,
        title="Decision",
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
        choice="Use the mixed model",
        alternatives_considered=["Bootstrap"],
        rationale="Structure.",
        actor=actor,
    )
    loose_question = api.create_question(
        project.project_id,
        "Loose question",
        QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    calls = _record_lock_calls(monkeypatch, api.claims.repository)
    reference_lock = ("lock_project_references", project.project_id)

    api.delete_dataset(dataset.dataset_id, actor=actor)
    assert calls == [
        reference_lock,
        ("lock_experiment_updates", ()),
        ("lock_dataset_updates", project.project_id),
    ]
    calls.clear()
    api.delete_session(session.session_id, actor=actor)
    assert calls == [reference_lock, ("lock_session_acquisition_state", session.session_id)]

    for delete in (
        lambda: api.delete_note(note.note_id, actor=actor),
        lambda: api.delete_claim(claim.claim_id, actor=actor),
        lambda: api.delete_visualization(visualization.viz_id, actor=actor),
        lambda: api.delete_analysis(analysis.analysis_id, actor=actor),
        lambda: api.delete_exploration_node(node.node_id, actor=actor),
        lambda: api.delete_question(loose_question.question_id, actor=actor),
    ):
        calls.clear()
        delete()
        assert calls == [reference_lock]


def test_dataset_delete_rechecks_claims_after_the_reference_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M56: a claim committed while the delete waited must block the delete."""

    api = repository_backed_api()
    actor, project, question = _project_with_question(api)
    dataset = api.create_dataset(project.project_id, question.question_id, actor=actor)
    repository = api.datasets.repository
    original_lock = repository.lock_project_references

    def claim_create_wins(project_id: UUID) -> None:
        original_lock(project_id)
        repository.claims.save(
            Claim(
                claim_id=uuid4(),
                project_id=project_id,
                statement="Concurrent supported claim.",
                confidence=0.9,
                status=ClaimStatus.SUPPORTED,
                supported_by_dataset_ids=[dataset.dataset_id],
            )
        )

    monkeypatch.setattr(repository, "lock_project_references", claim_create_wins)

    with pytest.raises(
        ValidationError,
        match="^Dataset cannot be deleted while claims reference it",
    ):
        api.delete_dataset(dataset.dataset_id, actor=actor)
    assert api.get_dataset(dataset.dataset_id).dataset_id == dataset.dataset_id


@pytest.mark.parametrize("create", ["claim", "analysis"])
def test_claim_and_analysis_create_revalidate_datasets_after_the_reference_lock(
    monkeypatch: pytest.MonkeyPatch,
    create: str,
) -> None:
    """M56: a dataset deleted while the create waited yields a 404, not a dangling FK."""

    api = repository_backed_api()
    actor, project, question = _project_with_question(api)
    dataset = api.create_dataset(project.project_id, question.question_id, actor=actor)
    repository = api.datasets.repository
    original_lock = repository.lock_project_references

    def dataset_delete_wins(project_id: UUID) -> None:
        original_lock(project_id)
        if repository.datasets.get(dataset.dataset_id) is not None:
            repository.datasets.delete(dataset.dataset_id)

    monkeypatch.setattr(repository, "lock_project_references", dataset_delete_wins)

    with pytest.raises(NotFoundError, match="Dataset does not exist"):
        if create == "claim":
            api.create_claim(
                project.project_id,
                "Supported by a vanishing dataset.",
                0.5,
                supported_by_dataset_ids=[dataset.dataset_id],
                actor=actor,
            )
        else:
            api.create_analysis(
                project.project_id,
                [dataset.dataset_id],
                "method",
                "v1",
                actor=actor,
            )


def test_claim_edge_create_rechecks_cycles_after_the_reference_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M52: the reverse edge committed while this create waited closes a cycle."""

    api = repository_backed_api()
    actor, project, _question = _project_with_question(api)
    first = api.create_claim(project.project_id, "First claim.", 0.5, actor=actor)
    second = api.create_claim(project.project_id, "Second claim.", 0.5, actor=actor)
    repository = api.claims.repository
    original_lock = repository.lock_project_references

    def reverse_edge_wins(project_id: UUID) -> None:
        original_lock(project_id)
        repository.claim_edges.save(
            ClaimEdge(
                edge_id=uuid4(),
                claim_id=second.claim_id,
                target_claim_id=first.claim_id,
                relation=ClaimRelation.DEPENDS_ON,
            )
        )

    monkeypatch.setattr(repository, "lock_project_references", reverse_edge_wins)

    with pytest.raises(ValidationError, match="Claim edge would create a cycle"):
        api.create_claim_edge(
            first.claim_id,
            target_claim_id=second.claim_id,
            relation=ClaimRelation.DEPENDS_ON,
            actor=actor,
        )


def test_sqlite_reference_lock_is_a_write_fence_until_commit(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'fence.db'}", future=True)
    try:
        Base.metadata.create_all(bind=engine)
        project_id = UUID(int=7)
        second_locked = Event()
        errors: list[BaseException] = []

        with OrmSession(engine) as first:
            SQLAlchemyLabTrackerRepository(first).lock_project_references(project_id)
            assert first.in_transaction()

            def second_writer() -> None:
                try:
                    with OrmSession(engine) as second:
                        SQLAlchemyLabTrackerRepository(second).lock_project_references(
                            project_id
                        )
                        second_locked.set()
                        second.rollback()
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            thread = Thread(target=second_writer)
            thread.start()
            assert not second_locked.wait(timeout=0.5)
            first.commit()
            thread.join(timeout=10)

        assert not thread.is_alive()
        assert errors == []
        assert second_locked.is_set()
    finally:
        engine.dispose()


def _ready_graph_draft(api, actor, project, operations) -> UUID:  # noqa: ANN001
    source_note = api.create_note(project.project_id, "Graph draft source", actor=actor)
    change_set_id = uuid4()
    api.graph_drafts.records.save_graph_change_set(
        GraphChangeSet(
            change_set_id=change_set_id,
            project_id=project.project_id,
            source_note_id=source_note.note_id,
            model="test-model",
            prompt_version="test-prompt",
            status=GraphChangeSetStatus.READY,
            operations=[
                GraphChangeOperation(
                    operation_id=uuid4(),
                    change_set_id=change_set_id,
                    sequence=sequence,
                    op=op,
                    entity_type=entity_type,
                    target_entity_id=target_entity_id,
                    payload=payload,
                    status=GraphChangeOperationStatus.ACCEPTED,
                    acceptance_mode=AcceptanceMode.BULK_ACCEPTED,
                )
                for sequence, (op, entity_type, target_entity_id, payload) in enumerate(
                    operations,
                    start=1,
                )
            ],
        )
    )
    return change_set_id


@pytest.mark.parametrize("reference_writer", [None, "claim", "goal"])
def test_graph_commit_prelocks_project_only_for_reference_lock_writers(
    monkeypatch: pytest.MonkeyPatch,
    reference_writer: str | None,
) -> None:
    api = repository_backed_api()
    actor, project, question = _project_with_question(api)
    dataset = api.create_dataset(
        project.project_id,
        question.question_id,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")]
        ),
        actor=actor,
    )
    operations: list[tuple[GraphChangeOp, EntityType, UUID | None, dict[str, object]]] = [
        (
            GraphChangeOp.UPDATE,
            EntityType.DATASET,
            dataset.dataset_id,
            {"commit_manifest": {"metadata": {"lock": "plan"}}},
        )
    ]
    if reference_writer == "goal":
        operations.append(
            (
                GraphChangeOp.CREATE,
                EntityType.GOAL,
                None,
                {
                    "project_id": str(project.project_id),
                    "goal_type": "paper",
                    "title": "Graph goal.",
                    "links": [
                        {
                            "entity_type": "question",
                            "entity_id": str(question.question_id),
                            "relation": "addresses",
                        }
                    ],
                },
            )
        )
    if reference_writer == "claim":
        operations.append(
            (
                GraphChangeOp.CREATE,
                EntityType.CLAIM,
                None,
                {
                    "project_id": str(project.project_id),
                    "statement": "Graph claim.",
                    "confidence": 50,
                    "supported_by_dataset_ids": [str(dataset.dataset_id)],
                },
            )
        )
    change_set_id = _ready_graph_draft(api, actor, project, operations)
    repository = api.graph_drafts.commit.repository
    events: list[tuple[str, object]] = []
    for name in ("lock_project_question_dag", "lock_dataset_updates"):
        original = getattr(repository, name)

        def observed(*args, _name=name, _original=original, **kwargs) -> None:  # noqa: ANN002, ANN003
            events.append((_name, args[0]))
            _original(*args, **kwargs)

        monkeypatch.setattr(repository, name, observed)

    committed = api.commit_graph_change_set(change_set_id, message="Lock plan", actor=actor)

    assert committed.status == GraphChangeSetStatus.COMMITTED
    dataset_lock = ("lock_dataset_updates", project.project_id)
    if reference_writer is not None:
        # Claim and goal creates take the project reference lock (the
        # question-DAG key), so commit takes it first, ahead of any Dataset
        # row lock.
        assert events[:2] == [
            ("lock_project_question_dag", project.project_id),
            dataset_lock,
        ]
    else:
        # Pure dataset updates keep the narrower plan: no project-level lock,
        # so two such commits in one project do not serialize on it.
        assert events == [dataset_lock]


def test_evidence_bundle_takes_reference_lock_after_key_lookup_before_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor, project, question = _project_with_question(api)
    repository = api.evidence_bundles.repository
    events: list[str] = []
    original_get_by_key = repository.evidence_bundles.get_by_key
    original_lock = repository.lock_project_references
    original_create_dataset = api.evidence_bundles.datasets.create_dataset

    def observed_get_by_key(**kwargs):  # noqa: ANN003, ANN202
        events.append("get_by_key")
        return original_get_by_key(**kwargs)

    def observed_lock(project_id: UUID) -> None:
        events.append("lock_project_references")
        original_lock(project_id)

    def observed_create_dataset(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        events.append("create_dataset")
        return original_create_dataset(*args, **kwargs)

    monkeypatch.setattr(repository.evidence_bundles, "get_by_key", observed_get_by_key)
    monkeypatch.setattr(repository, "lock_project_references", observed_lock)
    monkeypatch.setattr(api.evidence_bundles.datasets, "create_dataset", observed_create_dataset)

    api.record_evidence_bundle(
        RecordEvidenceBundleCommand(
            project_id=project.project_id,
            primary_question_id=question.question_id,
            dataset=CreateDatasetIntent(),
            analysis=CreateAnalysisIntent(
                dataset_ids=(),
                method_hash="method-v1",
                code_version="code-v1",
            ),
            dry_run=False,
            idempotency_key="reference-lock-order",
        ),
        actor=actor,
    )

    # The idempotency lookup is a plain read and must not wait on the project
    # lock: racing identical keys both miss, then the loser replays the winner.
    assert events[:3] == ["get_by_key", "lock_project_references", "create_dataset"]


def test_note_delete_rechecks_first_capture_designation_after_the_reference_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capture designated while the delete waited must stay undeletable."""

    api = repository_backed_api()
    actor, project, _question = _project_with_question(api)
    capture = api.create_note(project.project_id, "Forward capture", actor=actor)
    checkpoint = api.create_note(
        project.project_id,
        "Checkpoint stand-in",
        targets=[EntityRef(entity_type=EntityType.PROJECT, entity_id=project.project_id)],
        actor=actor,
    )
    repository = api.notes.repository
    original_lock = repository.lock_project_references

    def capture_designation_wins(project_id: UUID) -> None:
        original_lock(project_id)
        repository.notes.save(
            checkpoint.model_copy(
                update={
                    "metadata": {
                        CHECKPOINT_ROLE_KEY: CHECKPOINT_ROLE,
                        FIRST_CAPTURE_NOTE_ID_KEY: str(capture.note_id),
                    }
                }
            )
        )

    monkeypatch.setattr(repository, "lock_project_references", capture_designation_wins)

    with pytest.raises(
        ValidationError,
        match="^The designated first member-onboarding capture cannot be deleted\\.$",
    ):
        api.delete_note(capture.note_id, actor=actor)
    assert api.get_note(capture.note_id).note_id == capture.note_id


def _decision_node(api, actor, project, question, title: str):  # noqa: ANN001, ANN202
    return api.create_exploration_node(
        project_id=project.project_id,
        node_type=ExplorationNodeType.DECISION,
        title=title,
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
        choice="Use the mixed model path",
        alternatives_considered=["Bootstrap"],
        rationale="The model represents the acquisition structure.",
        actor=actor,
    )


@pytest.mark.parametrize("write", ["create", "update"])
def test_exploration_writers_revalidate_invalidated_node_after_the_reference_lock(
    monkeypatch: pytest.MonkeyPatch,
    write: str,
) -> None:
    """M57: a pivot must not be written against a node deleted while it waited."""

    api = repository_backed_api()
    actor, project, question = _project_with_question(api)
    doomed = _decision_node(api, actor, project, question, "Doomed decision")
    target = EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id)
    staged_pivot = None
    if write == "update":
        kept = _decision_node(api, actor, project, question, "Kept decision")
        staged_pivot = api.create_exploration_node(
            project_id=project.project_id,
            node_type=ExplorationNodeType.PIVOT,
            title="Pivot",
            target=target,
            trigger="A later run changed the interpretation.",
            rationale="Recording why the path changed.",
            invalidates_node_id=kept.node_id,
            actor=actor,
        )
    repository = api.exploration.repository
    original_lock = repository.lock_project_references

    def node_delete_wins(project_id: UUID) -> None:
        original_lock(project_id)
        repository.exploration_nodes.delete(doomed.node_id)

    monkeypatch.setattr(repository, "lock_project_references", node_delete_wins)

    with pytest.raises(NotFoundError, match="^Invalidated exploration node does not exist\\.$"):
        if staged_pivot is None:
            api.create_exploration_node(
                project_id=project.project_id,
                node_type=ExplorationNodeType.PIVOT,
                title="Racing pivot",
                target=target,
                trigger="A later run changed the interpretation.",
                rationale="Recording why the path changed.",
                invalidates_node_id=doomed.node_id,
                actor=actor,
            )
        else:
            api.update_exploration_node(
                staged_pivot.node_id,
                invalidates_node_id=doomed.node_id,
                actor=actor,
            )
