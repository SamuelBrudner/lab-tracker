"""Typed graph-draft API delegation.

The public façade deliberately mirrors :class:`GraphDraftService`.  Keeping
these signatures explicit makes the compatibility layer useful to callers and
lets static checks detect drift in parameter defaults and keyword-only
semantics.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.config import Settings
from lab_tracker.graph_drafting import GraphDraftClient, GraphDraftClientFactory
from lab_tracker.models import (
    AcceptanceMode,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftBatchSettings,
    GraphDraftBatchTrigger,
    GraphDraftMode,
    GraphDraftPurpose,
    Note,
    UsageEventResourceType,
    UsageEventVerb,
)
from lab_tracker.patching import NOT_PROVIDED, PatchValue
from lab_tracker.services.graph_draft_generation import DEFAULT_BATCH_RETRY_ATTEMPTS
from lab_tracker.services.graph_draft_review import RevisionInputs
from lab_tracker.services.graph_draft_service import GraphDraftService

ResultT = TypeVar("ResultT")


class GraphDraftsApiMixin:
    """Compatibility API with signatures kept in lockstep with its service."""

    if TYPE_CHECKING:
        graph_drafts: GraphDraftService

        def _with_usage_event(
            self,
            action: Callable[[], ResultT],
            *,
            verb: UsageEventVerb,
            resource_type: UsageEventResourceType,
            actor: AuthContext | None = None,
            resource_id: UUID | None = None,
            project_id: UUID | None = None,
            resource_id_attr: str | None = None,
            project_id_attr: str | None = "project_id",
        ) -> ResultT: ...

    def create_graph_draft_from_note(
        self,
        note_id: UUID,
        *,
        draft_client: GraphDraftClient,
        mode: GraphDraftMode = GraphDraftMode.GRAPH_CONTEXT,
        purpose: GraphDraftPurpose = GraphDraftPurpose.GENERAL,
        idempotency_key: str | None = None,
        external_provider_acknowledged: bool = False,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        return self._with_usage_event(
            lambda: self.graph_drafts.create_graph_draft_from_note(
                note_id,
                draft_client=draft_client,
                mode=mode,
                purpose=purpose,
                idempotency_key=idempotency_key,
                external_provider_acknowledged=external_provider_acknowledged,
                user_hint=user_hint,
                actor=actor,
            ),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=actor,
            resource_id_attr="change_set_id",
        )

    def create_analysis_graph_draft_from_note(
        self,
        note_id: UUID,
        *,
        draft_client: GraphDraftClient,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        return self._with_usage_event(
            lambda: self.graph_drafts.create_analysis_graph_draft_from_note(
                note_id,
                draft_client=draft_client,
                actor=actor,
            ),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=actor,
            resource_id_attr="change_set_id",
        )

    def create_batch_graph_draft(
        self,
        notes: list[Note],
        *,
        draft_client: GraphDraftClient,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
        window: tuple[datetime, datetime] | None = None,
        batch_key: str | None = None,
        review_assignee: str | None = None,
        review_assignee_user_id: UUID | None = None,
        max_attempts: int = DEFAULT_BATCH_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = 0.0,
    ) -> GraphChangeSet:
        return self._with_usage_event(
            lambda: self.graph_drafts.create_batch_graph_draft(
                notes,
                draft_client=draft_client,
                user_hint=user_hint,
                actor=actor,
                window=window,
                batch_key=batch_key,
                review_assignee=review_assignee,
                review_assignee_user_id=review_assignee_user_id,
                max_attempts=max_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
            ),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=actor,
            resource_id_attr="change_set_id",
        )

    def get_graph_change_set(self, change_set_id: UUID) -> GraphChangeSet:
        return self.graph_drafts.get_graph_change_set(change_set_id)

    def get_graph_change_set_for_read(
        self,
        change_set_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        return self.graph_drafts.get_graph_change_set_for_read(
            change_set_id,
            actor=actor,
        )

    def list_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
        draft_mode: GraphDraftMode | None = None,
        batch_key: str | None = None,
    ) -> list[GraphChangeSet]:
        return self.graph_drafts.list_graph_change_sets(
            project_id=project_id,
            status=status,
            source_note_id=source_note_id,
            draft_mode=draft_mode,
            batch_key=batch_key,
        )

    def query_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
        draft_mode: GraphDraftMode | None = None,
        batch_key: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_operations: bool = True,
    ) -> tuple[list[GraphChangeSet], int]:
        return self.graph_drafts.query_graph_change_sets(
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            source_note_id=source_note_id,
            draft_mode=draft_mode,
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
        return self.graph_drafts.list_batch_graph_drafts(
            project_id=project_id,
            status=status,
        )

    def update_graph_change_operation(
        self,
        change_set_id: UUID,
        operation_id: UUID,
        *,
        payload: PatchValue[dict[str, Any] | None] = NOT_PROVIDED,
        status: PatchValue[GraphChangeOperationStatus | None] = NOT_PROVIDED,
        review_note: PatchValue[str | None] = NOT_PROVIDED,
        acceptance_mode: AcceptanceMode = AcceptanceMode.HUMAN_SELECTED,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        return self._with_usage_event(
            lambda: self.graph_drafts.update_graph_change_operation(
                change_set_id,
                operation_id,
                payload=payload,
                status=status,
                review_note=review_note,
                acceptance_mode=acceptance_mode,
                actor=actor,
            ),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=actor,
            resource_id=change_set_id,
            project_id_attr=None,
        )

    def bulk_accept_graph_change_operations(
        self,
        change_set_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        return self._with_usage_event(
            lambda: self.graph_drafts.bulk_accept_graph_change_operations(
                change_set_id,
                actor=actor,
            ),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=actor,
            resource_id=change_set_id,
            resource_id_attr="change_set_id",
        )

    def submit_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        return self._with_usage_event(
            lambda: self.graph_drafts.submit_graph_change_set(
                change_set_id,
                actor=actor,
            ),
            verb=UsageEventVerb.SUBMIT,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=actor,
            resource_id=change_set_id,
            resource_id_attr="change_set_id",
        )

    def review_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        status: GraphChangeSetStatus,
        note: str | None = None,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        return self._with_usage_event(
            lambda: self.graph_drafts.review_graph_change_set(
                change_set_id,
                status=status,
                note=note,
                actor=actor,
            ),
            verb=UsageEventVerb.REVIEW,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=actor,
            resource_id=change_set_id,
            resource_id_attr="change_set_id",
        )

    def revise_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        feedback: str | None = None,
        inputs: RevisionInputs | None = None,
        draft_client: GraphDraftClient,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        return self.graph_drafts.revise_graph_change_set(
            change_set_id,
            feedback=feedback,
            inputs=inputs,
            draft_client=draft_client,
            actor=actor,
        )

    def commit_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        message: str,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        return self._with_usage_event(
            lambda: self.graph_drafts.commit_graph_change_set(
                change_set_id,
                message=message,
                actor=actor,
            ),
            verb=UsageEventVerb.COMMIT,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=actor,
            resource_id=change_set_id,
            resource_id_attr="change_set_id",
        )

    def build_graph_context_for_note(
        self,
        note_id: UUID,
        *,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
    ) -> dict[str, Any]:
        return self.graph_drafts.build_graph_context_for_note(
            note_id,
            user_hint=user_hint,
            actor=actor,
        )

    def build_batch_graph_context(
        self,
        notes: list[Note],
        *,
        window: tuple[datetime, datetime] | None = None,
        actor: AuthContext | None = None,
    ) -> dict[str, Any]:
        return self.graph_drafts.build_batch_graph_context(
            notes,
            window=window,
            actor=actor,
        )

    def get_graph_draft_batch_settings(
        self,
        project_id: UUID,
        *,
        user_id: UUID | None = None,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchSettings:
        return self.graph_drafts.get_graph_draft_batch_settings(
            project_id,
            user_id=user_id,
            actor=actor,
        )

    def update_graph_draft_batch_settings(
        self,
        project_id: UUID,
        *,
        enabled: PatchValue[bool | None] = NOT_PROVIDED,
        cadence_minutes: PatchValue[int | None] = NOT_PROVIDED,
        run_at_local_time: PatchValue[str | None] = NOT_PROVIDED,
        timezone_name: PatchValue[str | None] = NOT_PROVIDED,
        user_id: PatchValue[UUID | None] = NOT_PROVIDED,
        email_notifications_enabled: PatchValue[bool | None] = NOT_PROVIDED,
        notification_email: PatchValue[str | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchSettings:
        return self._with_usage_event(
            lambda: self.graph_drafts.update_graph_draft_batch_settings(
                project_id,
                enabled=enabled,
                cadence_minutes=cadence_minutes,
                run_at_local_time=run_at_local_time,
                timezone_name=timezone_name,
                user_id=user_id,
                email_notifications_enabled=email_notifications_enabled,
                notification_email=notification_email,
                actor=actor,
            ),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GRAPH_DRAFT_BATCH_SETTINGS,
            actor=actor,
            resource_id_attr="settings_id",
        )

    def run_graph_draft_batch_for_project(
        self,
        project_id: UUID,
        *,
        draft_client: GraphDraftClient,
        since: datetime | None = None,
        until: datetime | None = None,
        trigger: GraphDraftBatchTrigger = GraphDraftBatchTrigger.MANUAL,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
        review_assignee: str | None = None,
        review_assignee_user_id: UUID | None = None,
    ) -> GraphDraftBatchRun:
        return self._with_usage_event(
            lambda: self.graph_drafts.run_graph_draft_batch_for_project(
                project_id,
                draft_client=draft_client,
                since=since,
                until=until,
                trigger=trigger,
                user_hint=user_hint,
                actor=actor,
                review_assignee=review_assignee,
                review_assignee_user_id=review_assignee_user_id,
            ),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_DRAFT_BATCH_RUN,
            actor=actor,
            resource_id_attr="run_id",
        )

    def enqueue_graph_draft_batch_for_project(
        self,
        project_id: UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        trigger: GraphDraftBatchTrigger = GraphDraftBatchTrigger.MANUAL,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
        review_assignee: str | None = None,
        review_assignee_user_id: UUID | None = None,
    ) -> GraphDraftBatchRun:
        return self._with_usage_event(
            lambda: self.graph_drafts.enqueue_graph_draft_batch_for_project(
                project_id,
                since=since,
                until=until,
                trigger=trigger,
                user_hint=user_hint,
                actor=actor,
                review_assignee=review_assignee,
                review_assignee_user_id=review_assignee_user_id,
            ),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_DRAFT_BATCH_RUN,
            actor=actor,
            resource_id_attr="run_id",
        )

    def process_next_graph_draft_batch_run(
        self,
        *,
        draft_client_factory: GraphDraftClientFactory,
        app_settings: Settings,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchRun | None:
        return self.graph_drafts.process_next_graph_draft_batch_run(
            draft_client_factory=draft_client_factory,
            app_settings=app_settings,
            actor=actor,
        )

    def execute_graph_draft_batch_run(
        self,
        run_id: UUID,
        *,
        draft_client: GraphDraftClient,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchRun:
        return self.graph_drafts.execute_graph_draft_batch_run(
            run_id,
            draft_client=draft_client,
            actor=actor,
        )

    def get_graph_draft_batch_run(self, run_id: UUID) -> GraphDraftBatchRun:
        return self.graph_drafts.get_graph_draft_batch_run(run_id)

    def run_due_graph_draft_batches(
        self,
        *,
        draft_client_factory: GraphDraftClientFactory,
        app_settings: Settings,
        actor: AuthContext | None = None,
        now: datetime | None = None,
    ) -> list[GraphDraftBatchRun]:
        return self.graph_drafts.run_due_graph_draft_batches(
            draft_client_factory=draft_client_factory,
            app_settings=app_settings,
            actor=actor,
            now=now,
        )

    def enqueue_due_graph_draft_batches(
        self,
        *,
        actor: AuthContext | None = None,
        now: datetime | None = None,
    ) -> list[GraphDraftBatchRun]:
        return self.graph_drafts.enqueue_due_graph_draft_batches(
            actor=actor,
            now=now,
        )

    def list_graph_draft_batch_runs(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphDraftBatchRunStatus | None = None,
    ) -> list[GraphDraftBatchRun]:
        return self.graph_drafts.list_graph_draft_batch_runs(
            project_id=project_id,
            status=status,
        )
