from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from api_helpers import repository_backed_api

from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.models import (
    AcceptanceMode,
    DatasetCommitManifestInput,
    EntityType,
    ExternalArtifactReference,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    QuestionStatus,
    QuestionType,
)


def _staged_dataset(api, actor, *, label: str):  # noqa: ANN001, ANN202
    slug = label.casefold().replace(" ", "-")
    project = api.create_project(f"{label} project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text=f"{label} question",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=DatasetCommitManifestInput(
            external_artifacts=[
                ExternalArtifactReference(
                    source_system="s3",
                    uri=f"s3://lab-tracker/{slug}",
                    content_hash=f"sha256:{slug}",
                )
            ]
        ),
        actor=actor,
    )
    return project, dataset


def test_every_dataset_update_uses_the_snapshot_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=1), role=Role.ADMIN)
    project, dataset = _staged_dataset(api, actor, label="Snapshot lock")
    lock_calls: list[tuple[UUID, list[UUID]]] = []

    def record_lock(project_id: UUID, dataset_ids) -> None:  # noqa: ANN001
        lock_calls.append((project_id, list(dataset_ids)))

    monkeypatch.setattr(
        api.datasets.repository,
        "lock_dataset_updates",
        record_lock,
    )

    api.update_dataset(
        dataset.dataset_id,
        commit_manifest=DatasetCommitManifestInput(
            external_artifacts=dataset.commit_manifest.external_artifacts,
            metadata={"phase": "updated"},
        ),
        actor=actor,
    )

    assert lock_calls == [(project.project_id, [dataset.dataset_id])]


def test_dataset_update_authorizes_before_locking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    owner = AuthContext(user_id=UUID(int=1), role=Role.ADMIN)
    _, dataset = _staged_dataset(api, owner, label="Authorization order")
    lock_calls: list[tuple[UUID, list[UUID]]] = []

    def record_lock(project_id: UUID, dataset_ids) -> None:  # noqa: ANN001
        lock_calls.append((project_id, list(dataset_ids)))

    monkeypatch.setattr(
        api.datasets.repository,
        "lock_dataset_updates",
        record_lock,
    )

    with pytest.raises(AuthError):
        api.update_dataset(
            dataset.dataset_id,
            commit_manifest=DatasetCommitManifestInput(
                external_artifacts=dataset.commit_manifest.external_artifacts,
                metadata={"must": "fail"},
            ),
            actor=AuthContext(user_id=UUID(int=2), role=Role.VIEWER),
        )

    assert lock_calls == []


def test_graph_draft_prelocks_dataset_updates_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=3), role=Role.ADMIN)
    project, first_dataset = _staged_dataset(api, actor, label="First graph dataset")
    question = api.create_question(
        project_id=project.project_id,
        text="Second graph dataset question",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    second_dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        actor=actor,
    )
    change_set_id = uuid4()
    operations = [
        GraphChangeOperation(
            operation_id=uuid4(),
            change_set_id=change_set_id,
            sequence=1,
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.DATASET,
            target_entity_id=second_dataset.dataset_id,
            payload={"commit_manifest": {"metadata": {"order": "second"}}},
        ),
        GraphChangeOperation(
            operation_id=uuid4(),
            change_set_id=change_set_id,
            sequence=2,
            op=GraphChangeOp.CREATE,
            entity_type=EntityType.DATASET,
            payload={
                "project_id": str(project.project_id),
                "primary_question_id": str(question.question_id),
            },
        ),
        GraphChangeOperation(
            operation_id=uuid4(),
            change_set_id=change_set_id,
            sequence=3,
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.DATASET,
            target_entity_id=first_dataset.dataset_id,
            payload={"commit_manifest": {"metadata": {"order": "first"}}},
        ),
    ]
    lock_calls: list[tuple[UUID, list[UUID]]] = []

    def record_lock(project_id: UUID, dataset_ids: list[UUID]) -> None:
        lock_calls.append((project_id, list(dataset_ids)))

    monkeypatch.setattr(
        api.graph_drafts.commit.repository,
        "lock_dataset_updates",
        record_lock,
    )

    api.graph_drafts.commit._lock_dataset_update_projects(  # noqa: SLF001
        operations,
        project_id=project.project_id,
        actor=actor,
    )

    assert lock_calls == [
        (
            project.project_id,
            sorted(
                [first_dataset.dataset_id, second_dataset.dataset_id],
                key=str,
            ),
        )
    ]


def test_graph_draft_rejects_cross_project_dataset_before_locking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=4), role=Role.ADMIN)
    project, _ = _staged_dataset(api, actor, label="Graph draft project")
    _, other_dataset = _staged_dataset(api, actor, label="Other project")
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=1,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.DATASET,
        target_entity_id=other_dataset.dataset_id,
        payload={"commit_manifest": {"metadata": {"must": "fail"}}},
    )
    lock_calls: list[tuple[UUID, list[UUID]]] = []

    def record_lock(project_id: UUID, dataset_ids: list[UUID]) -> None:
        lock_calls.append((project_id, list(dataset_ids)))

    monkeypatch.setattr(
        api.graph_drafts.commit.repository,
        "lock_dataset_updates",
        record_lock,
    )

    with pytest.raises(
        ValidationError,
        match="Dataset updates must belong to the graph draft project",
    ):
        api.graph_drafts.commit._lock_dataset_update_projects(  # noqa: SLF001
            [operation],
            project_id=project.project_id,
            actor=actor,
        )

    assert lock_calls == []


def test_graph_commit_guards_project_before_claiming_or_locking_datasets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=5), role=Role.ADMIN)
    project, dataset = _staged_dataset(api, actor, label="Graph commit lock order")
    source_note = api.create_note(
        project.project_id,
        "Graph commit lock-order source",
        actor=actor,
    )
    change_set_id = uuid4()
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=change_set_id,
        sequence=1,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.DATASET,
        target_entity_id=dataset.dataset_id,
        payload={"commit_manifest": {"metadata": {"order": "guarded"}}},
        status=GraphChangeOperationStatus.ACCEPTED,
        acceptance_mode=AcceptanceMode.BULK_ACCEPTED,
    )
    api.graph_drafts.records.save_graph_change_set(
        GraphChangeSet(
            change_set_id=change_set_id,
            project_id=project.project_id,
            source_note_id=source_note.note_id,
            model="test-model",
            prompt_version="test-prompt",
            status=GraphChangeSetStatus.READY,
            operations=[operation],
        )
    )
    repository = api.graph_drafts.commit.repository
    original_guard = repository.lock_project_deletion_guard
    original_claim = repository.claim_graph_change_set_for_commit
    original_dataset_lock = repository.lock_dataset_updates
    events: list[str] = []

    def observed_guard(project_id: UUID) -> None:
        events.append("project_guard")
        original_guard(project_id)

    def observed_claim(claimed_change_set_id: UUID):
        events.append("claim")
        return original_claim(claimed_change_set_id)

    def observed_dataset_lock(project_id: UUID, dataset_ids) -> None:  # noqa: ANN001
        events.append("dataset_lock")
        original_dataset_lock(project_id, dataset_ids)

    monkeypatch.setattr(repository, "lock_project_deletion_guard", observed_guard)
    monkeypatch.setattr(repository, "claim_graph_change_set_for_commit", observed_claim)
    monkeypatch.setattr(repository, "lock_dataset_updates", observed_dataset_lock)

    committed = api.commit_graph_change_set(
        change_set_id,
        message="Verify canonical lock order",
        actor=actor,
    )

    assert committed.status == GraphChangeSetStatus.COMMITTED
    assert events[:3] == ["project_guard", "claim", "dataset_lock"]
