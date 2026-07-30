"""Consumer-owned collaborator roles for graph-draft scheduling."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.graph_drafting import GraphDraftClient
from lab_tracker.models import (
    GraphChangeSet,
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftBatchSettings,
    Note,
    Project,
)


class SchedulingRecords(Protocol):
    def get_graph_draft_batch_run(self, run_id: UUID) -> GraphDraftBatchRun: ...

    def list_graph_draft_batch_runs(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphDraftBatchRunStatus | None = None,
    ) -> list[GraphDraftBatchRun]: ...


class BatchDraftGenerator(Protocol):
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
    ) -> GraphChangeSet: ...


class SchedulingProjects(Protocol):
    def get_project(self, project_id: UUID) -> Project: ...


class SchedulingNotes(Protocol):
    def get_note(self, note_id: UUID) -> Note: ...

    def list_notes(self, *, project_id: UUID | None = None) -> list[Note]: ...


class SchedulingAuthorization(Protocol):
    def has_global_admin(self, actor: AuthContext | None) -> bool: ...

    def require_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...

    def require_contributor(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...

    def require_owner(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...


class SchedulingProvenanceLinks(Protocol):
    def propose_links_from_content_hash(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> int: ...


class BatchSettingsStore(Protocol):
    def save(self, settings: GraphDraftBatchSettings) -> None: ...


class BatchRunStore(Protocol):
    def get(self, run_id: UUID) -> GraphDraftBatchRun | None: ...

    def save(self, run: GraphDraftBatchRun) -> None: ...


class SchedulingRepository(Protocol):
    def user_exists(self, user_id: UUID) -> bool: ...

    @property
    def graph_draft_batch_settings(self) -> BatchSettingsStore: ...

    @property
    def graph_draft_batch_runs(self) -> BatchRunStore: ...

    def get_graph_draft_batch_settings_by_project(
        self,
        project_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> GraphDraftBatchSettings | None: ...

    def list_graph_draft_batch_settings_for_project(
        self,
        project_id: UUID,
    ) -> list[GraphDraftBatchSettings]: ...

    def list_due_graph_draft_batch_settings(
        self,
        now: datetime,
    ) -> list[GraphDraftBatchSettings]: ...

    def claim_due_graph_draft_batch_settings(
        self,
        settings_id: UUID,
        *,
        observed_next_run_at: datetime,
        next_run_at: datetime,
        updated_at: datetime,
        updated_by: str | None,
    ) -> GraphDraftBatchSettings | None: ...

    def get_graph_draft_batch_run_by_key(
        self,
        batch_key: str,
    ) -> GraphDraftBatchRun | None: ...

    def lock_graph_draft_batch_settings(self, project_id: UUID) -> None: ...

    def lock_graph_draft_batch_reviewer(
        self,
        project_id: UUID,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> None: ...

    def latest_graph_draft_batch_run(
        self,
        project_id: UUID,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> GraphDraftBatchRun | None: ...

    def active_graph_draft_batch_runs(
        self,
        project_id: UUID,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> list[GraphDraftBatchRun]: ...

    def latest_successful_graph_draft_batch_run(
        self,
        project_id: UUID,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> GraphDraftBatchRun | None: ...

    def successful_graph_draft_batch_source_note_ids_at_window_end(
        self,
        project_id: UUID,
        window_end: datetime,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> set[UUID]: ...

    def claim_next_pending_graph_draft_batch_run(
        self,
        *,
        claimed_at: datetime,
    ) -> GraphDraftBatchRun | None: ...
