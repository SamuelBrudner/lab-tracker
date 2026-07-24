"""Transactional graph-draft commit coordinator."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Dataset,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    Question,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.graph_draft_context import EntityResult
from lab_tracker.services.graph_draft_context import entity_id as graph_entity_id
from lab_tracker.services.shared import actor_user_id


class CommitRecords(Protocol):
    def get_graph_change_set(self, change_set_id: UUID) -> GraphChangeSet: ...

    def save_graph_change_set(self, change_set: GraphChangeSet) -> None: ...


class CommitPatchApplier(Protocol):
    def apply_graph_operation(
        self,
        operation: GraphChangeOperation,
        *,
        ref_map: dict[str, UUID],
        actor: AuthContext | None,
        change_set: GraphChangeSet,
    ) -> EntityResult: ...


class CommitVersions(Protocol):
    def mark_change_set_committed(
        self,
        change_set_id: UUID,
        committed_at: datetime | None,
    ) -> None: ...


class CommitQuestions(Protocol):
    def get_question(self, question_id: UUID) -> Question: ...


class CommitDatasets(Protocol):
    def get_dataset(self, dataset_id: UUID) -> Dataset: ...


class CommitAuthorization(Protocol):
    def require_interactive(
        self,
        actor: AuthContext | None,
        *,
        action: str,
    ) -> None: ...

    def require_owner(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...


class CommitRepository(Protocol):
    def lock_project_deletion_guard(self, project_id: UUID) -> None: ...

    def claim_graph_change_set_for_commit(
        self,
        change_set_id: UUID,
    ) -> GraphChangeSet | None: ...

    def lock_project_question_dag(self, project_id: UUID) -> None: ...

    def lock_dataset_updates(
        self,
        project_id: UUID,
        dataset_ids: list[UUID],
    ) -> None: ...


class TransactionalDraftCommitCoordinator(BaseService):
    """Own the atomic transition from accepted proposal to canonical graph."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        records: CommitRecords,
        patch_applier: CommitPatchApplier,
        versions: CommitVersions,
        questions: CommitQuestions,
        datasets: CommitDatasets,
        authorization: CommitAuthorization,
    ) -> None:
        super().__init__(context)
        self.records = records
        self.patch_applier = patch_applier
        self.versions = versions
        self.questions = questions
        self.datasets = datasets
        self.authorization = authorization

    @property
    def commit_repository(self) -> CommitRepository:
        return self._context.active_repository()

    def commit_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        message: str,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        if not message or not message.strip():
            raise ValidationError("message must not be empty.")
        change_set = self.records.get_graph_change_set(change_set_id)
        self.authorization.require_owner(change_set.project_id, actor=actor)
        self.authorization.require_interactive(actor, action="Committing graph changes")
        if change_set.status == GraphChangeSetStatus.COMMITTING:
            raise ValidationError("This graph draft is already being committed.")
        if change_set.status not in {
            GraphChangeSetStatus.READY,
            GraphChangeSetStatus.SUBMITTED,
        }:
            raise ValidationError("Only ready or submitted graph drafts can be committed.")
        ref_map: dict[str, UUID] = {}
        accepted = [
            operation
            for operation in sorted(change_set.operations, key=lambda item: item.sequence)
            if operation.status == GraphChangeOperationStatus.ACCEPTED
        ]
        if not accepted:
            raise ValidationError("At least one accepted operation is required to commit.")
        _ensure_accepted_operation_refs_available(accepted)
        with self.application_transaction():
            # Project deletion takes this scope exclusively before cascading
            # into graph rows. Take it shared before claiming the change-set
            # row so graph commit and project deletion cannot invert those two
            # locks and deadlock.
            self.commit_repository.lock_project_deletion_guard(change_set.project_id)
            claimed = self.commit_repository.claim_graph_change_set_for_commit(change_set_id)
            if claimed is None:
                latest = self.records.get_graph_change_set(change_set_id)
                if latest.status == GraphChangeSetStatus.COMMITTED:
                    raise ValidationError("This graph draft has already been committed.")
                if latest.status == GraphChangeSetStatus.COMMITTING:
                    raise ValidationError("This graph draft is already being committed.")
                raise ValidationError("Only ready or submitted graph drafts can be committed.")
            change_set = claimed
            accepted = [
                operation
                for operation in sorted(change_set.operations, key=lambda item: item.sequence)
                if operation.status == GraphChangeOperationStatus.ACCEPTED
            ]
            self._lock_question_update_projects(accepted)
            self._lock_dataset_update_projects(
                accepted,
                project_id=change_set.project_id,
                actor=actor,
            )
            for operation in accepted:
                entity = self.patch_applier.apply_graph_operation(
                    operation,
                    ref_map=ref_map,
                    actor=actor,
                    change_set=change_set,
                )
                resolved_entity_id = graph_entity_id(operation.entity_type, entity)
                if operation.client_ref:
                    ref_map[operation.client_ref] = resolved_entity_id
                operation.status = GraphChangeOperationStatus.APPLIED
                operation.result_entity_id = resolved_entity_id
                operation.error_metadata = {}
                operation.updated_at = utc_now()
            change_set.status = GraphChangeSetStatus.COMMITTED
            change_set.commit_message = message.strip()
            change_set.committed_at = utc_now()
            change_set.committed_by = actor_user_id(actor)
            change_set.updated_at = change_set.committed_at
            self.versions.mark_change_set_committed(
                change_set.change_set_id,
                change_set.committed_at,
            )
            self.records.save_graph_change_set(change_set)
        return change_set

    def _lock_question_update_projects(
        self,
        operations: list[GraphChangeOperation],
    ) -> None:
        """Pre-lock every question project in canonical UUID order."""

        project_ids: set[UUID] = set()
        for operation in operations:
            if operation.op != GraphChangeOp.UPDATE or operation.entity_type != EntityType.QUESTION:
                continue
            if operation.target_entity_id is None:
                raise ValidationError("Question updates require target_entity_id.")
            question = self.questions.get_question(operation.target_entity_id)
            project_ids.add(question.project_id)
        for project_id in sorted(project_ids, key=str):
            self.commit_repository.lock_project_question_dag(project_id)

    def _lock_dataset_update_projects(
        self,
        operations: list[GraphChangeOperation],
        *,
        project_id: UUID,
        actor: AuthContext | None,
    ) -> None:
        """Pre-lock existing dataset update targets in canonical order."""

        dataset_ids_by_project: dict[UUID, set[UUID]] = {}
        for operation in operations:
            if operation.op != GraphChangeOp.UPDATE or operation.entity_type != EntityType.DATASET:
                continue
            if operation.target_entity_id is None:
                raise ValidationError("Dataset updates require target_entity_id.")
            dataset = self.datasets.get_dataset(operation.target_entity_id)
            if dataset.project_id != project_id:
                raise ValidationError("Dataset updates must belong to the graph draft project.")
            self.authorization.require_owner(dataset.project_id, actor=actor)
            dataset_ids_by_project.setdefault(dataset.project_id, set()).add(dataset.dataset_id)
        for project_id in sorted(dataset_ids_by_project, key=str):
            self.commit_repository.lock_dataset_updates(
                project_id,
                sorted(dataset_ids_by_project[project_id], key=str),
            )


def _ensure_accepted_operation_refs_available(
    operations: list[GraphChangeOperation],
) -> None:
    available_refs: set[str] = set()
    for operation in operations:
        missing = sorted(_payload_ref_names(operation.payload) - available_refs)
        if missing:
            missing_refs = ", ".join(missing)
            raise ValidationError(
                "Accepted graph draft operation "
                f"{operation.sequence} references unavailable operation ref(s): {missing_refs}. "
                "Accept the referenced operation and make sure it appears earlier in the draft, "
                "or edit this operation before committing."
            )
        if operation.client_ref:
            available_refs.add(operation.client_ref)


def _payload_ref_names(value: Any) -> set[str]:
    if isinstance(value, list):
        refs: set[str] = set()
        for item in value:
            refs.update(_payload_ref_names(item))
        return refs
    if not isinstance(value, dict):
        return set()
    if set(value) == {"$ref"}:
        ref_name = value["$ref"]
        return {ref_name} if isinstance(ref_name, str) else set()
    nested_refs: set[str] = set()
    for item in value.values():
        nested_refs.update(_payload_ref_names(item))
    return nested_refs
