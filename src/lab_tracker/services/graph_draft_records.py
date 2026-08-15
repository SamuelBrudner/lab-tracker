"""Persistence-facing graph-draft records shared by lifecycle coordinators."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, OpaqueTargetNotFoundError
from lab_tracker.member_onboarding import COMPLETED_AT_KEY, FIRST_CAPTURE_NOTE_ID_KEY
from lab_tracker.models import (
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftMode,
    GraphDraftPurpose,
    Note,
    Question,
)
from lab_tracker.services.base import BaseService, ServiceContext


class GraphDraftReadAuthorization(Protocol):
    """Narrow authorization role needed for opaque graph-draft reads."""

    def can_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> bool: ...


class GraphDraftRecords(BaseService):
    """Own graph-draft record reads and the shared change-set save invariant."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        authorization: GraphDraftReadAuthorization,
    ) -> None:
        super().__init__(context)
        self.authorization = authorization

    def save_graph_change_set(self, change_set: GraphChangeSet) -> None:
        change_set.operation_count = len(change_set.operations)
        with self.unit_of_work() as repository:
            repository.graph_change_sets.save(change_set)

    def get_graph_change_set_for_update(
        self,
        change_set_id: UUID,
    ) -> GraphChangeSet:
        """Row-lock a change set inside the caller's application transaction."""

        change_set = self.repository.graph_change_sets.get_for_update(change_set_id)
        if change_set is None:
            raise NotFoundError("Graph change set does not exist.")
        return change_set

    def get_member_onboarding_checkpoint(self, note_id: UUID) -> Note | None:
        return self.repository.notes.get(note_id)

    def get_member_onboarding_question(self, question_id: UUID) -> Question | None:
        return self.repository.questions.get(question_id)

    def resolve_member_onboarding_ai_alignment(
        self,
        note_id: UUID,
        *,
        change_set_id: UUID,
        resolved_at: datetime,
        resolution: str,
    ) -> Note | None:
        return self.repository.notes.try_resolve_member_onboarding_ai_alignment(
            note_id,
            change_set_id=change_set_id,
            resolved_at=resolved_at,
            resolution=resolution,
        )

    def mark_member_onboarding_completed(
        self,
        note_id: UUID,
        *,
        completed_at: datetime,
    ) -> Note | None:
        return self.repository.notes.try_mark_member_onboarding_completed(
            note_id,
            completed_at=completed_at,
        )

    def reconcile_member_onboarding_completion(
        self,
        note_id: UUID,
        *,
        completed_at: datetime,
    ) -> Note | None:
        """Durably reconcile capture-first onboarding after the main commit."""

        checkpoint = self.repository.notes.get(note_id)
        if (
            checkpoint is None
            or checkpoint.metadata.get(COMPLETED_AT_KEY)
            or not checkpoint.metadata.get(FIRST_CAPTURE_NOTE_ID_KEY)
        ):
            return None
        completed = self.repository.notes.try_mark_member_onboarding_completed(
            note_id,
            completed_at=completed_at,
        )
        if completed is not None:
            self.repository.commit()
        return completed

    def claim_graph_change_set_generation(
        self,
        candidate: GraphChangeSet,
        *,
        claimed_at: datetime,
        lease_until: datetime,
        claim_token: UUID,
    ) -> tuple[GraphChangeSet, bool]:
        """Reserve one keyed generation attempt without holding DB I/O open."""

        try:
            with self.recoverable_unit_of_work() as repository:
                result = repository.graph_change_sets.claim_for_generation(
                    candidate,
                    claimed_at=claimed_at,
                    lease_until=lease_until,
                    claim_token=claim_token,
                )
        except IntegrityError:
            # Two transactions may race to insert the same unique batch key.
            # The losing savepoint/transaction is clean here, so rejoin or
            # reclaim the now-persisted row through the same CAS contract.
            with self.unit_of_work() as repository:
                result = repository.graph_change_sets.claim_for_generation(
                    candidate,
                    claimed_at=claimed_at,
                    lease_until=lease_until,
                    claim_token=claim_token,
                )
        self._commit_request_generation_checkpoint()
        return result

    def renew_graph_change_set_generation(
        self,
        change_set_id: UUID,
        claim_token: UUID,
        *,
        renewed_at: datetime,
        lease_until: datetime,
    ) -> GraphChangeSet | None:
        with self.unit_of_work() as repository:
            renewed = repository.graph_change_sets.renew_generation_claim(
                change_set_id,
                claim_token,
                renewed_at=renewed_at,
                lease_until=lease_until,
            )
        if renewed is not None:
            self._commit_request_generation_checkpoint()
        return renewed

    def _commit_request_generation_checkpoint(self) -> None:
        """Make an ownership fence visible before slow provider work.

        Ordinary standalone unit-of-work calls have already committed on exit.
        HTTP requests intentionally own a wider transaction, so claims and
        renewals need this narrow durable checkpoint before the provider call;
        otherwise a concurrent request cannot observe the active owner.
        """

        if self._context.is_request_managed():
            self._context.active_repository().commit()

    def complete_graph_change_set_generation(
        self,
        change_set: GraphChangeSet,
        claim_token: UUID,
        *,
        completed_at: datetime,
    ) -> GraphChangeSet | None:
        change_set.operation_count = len(change_set.operations)
        with self.unit_of_work() as repository:
            return repository.graph_change_sets.complete_generation_claim(
                change_set,
                claim_token,
                completed_at=completed_at,
            )

    def fail_graph_change_set_generation(
        self,
        change_set: GraphChangeSet,
        claim_token: UUID,
        *,
        failed_at: datetime,
    ) -> GraphChangeSet | None:
        change_set.operation_count = len(change_set.operations)
        with self.unit_of_work() as repository:
            failed = repository.graph_change_sets.fail_generation_claim(
                change_set,
                claim_token,
                failed_at=failed_at,
            )
        if failed is not None:
            # Provider/runtime exceptions are re-raised after this CAS. Publish
            # the terminal result now so request middleware cannot roll it
            # back and strand the already-committed claim in DRAFTING.
            self._commit_request_generation_checkpoint()
        return failed

    def get_graph_draft_batch_run(self, run_id: UUID) -> GraphDraftBatchRun:
        run = self.repository.graph_draft_batch_runs.get(run_id)
        if run is None:
            raise NotFoundError("Graph draft batch run does not exist.")
        return run

    def list_graph_draft_batch_runs(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphDraftBatchRunStatus | None = None,
    ) -> list[GraphDraftBatchRun]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_graph_draft_batch_runs(
                project_id=project_id,
                status=status.value if status is not None else None,
                limit=None,
                offset=0,
            ),
        )

    def get_graph_change_set(self, change_set_id: UUID) -> GraphChangeSet:
        change_set = self.repository.graph_change_sets.get(change_set_id)
        if change_set is None:
            raise NotFoundError("Graph draft does not exist.")
        return change_set

    def get_graph_change_set_for_read(
        self,
        change_set_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        """Authorize from scalar scope before materializing a graph draft."""

        project_id = self.repository.graph_change_sets.project_id_for(change_set_id)
        if project_id is None or not self.authorization.can_read(project_id, actor=actor):
            raise OpaqueTargetNotFoundError("Graph draft does not exist.")
        try:
            return self.get_graph_change_set(change_set_id)
        except NotFoundError as exc:
            raise OpaqueTargetNotFoundError("Graph draft does not exist.") from exc

    def list_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
        draft_mode: GraphDraftMode | None = None,
        purpose: GraphDraftPurpose | None = None,
        batch_key: str | None = None,
    ) -> list[GraphChangeSet]:
        change_sets, _ = self.query_graph_change_sets(
            project_id=project_id,
            status=status,
            source_note_id=source_note_id,
            draft_mode=draft_mode,
            purpose=purpose,
            batch_key=batch_key,
            limit=None,
            offset=0,
        )
        return change_sets

    def query_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
        draft_mode: GraphDraftMode | None = None,
        purpose: GraphDraftPurpose | None = None,
        batch_key: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_operations: bool = True,
    ) -> tuple[list[GraphChangeSet], int]:
        return self.repository.query_graph_change_sets(
            project_id=project_id,
            project_ids=project_ids,
            status=status.value if status is not None else None,
            source_note_id=source_note_id,
            draft_mode=draft_mode.value if draft_mode is not None else None,
            purpose=purpose.value if purpose is not None else None,
            batch_key=batch_key,
            limit=limit,
            offset=offset,
            include_operations=include_operations,
        )

    def list_batch_graph_drafts(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
    ) -> list[GraphChangeSet]:
        return self.list_graph_change_sets(
            project_id=project_id,
            status=status,
            draft_mode=GraphDraftMode.GRAPH_BATCH,
        )
