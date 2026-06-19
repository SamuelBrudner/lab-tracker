"""Repository interfaces for persistence backends."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    ClaimEdge,
    Dataset,
    DatasetFile,
    EntityVersion,
    Goal,
    GoalLink,
    GraphChangeSet,
    GraphDraftBatchRun,
    GraphDraftBatchSettings,
    GroupMembership,
    Note,
    OwnershipReassignment,
    Project,
    ProjectGroup,
    ProjectMembership,
    Question,
    QuestionRefactor,
    RecordExportEvent,
    RecordExportRecords,
    Session,
    SupervisionEdge,
    UsageEvent,
    UsageEventRollup,
    Visualization,
)

EntityT = TypeVar("EntityT")


class EntityRepository(Protocol, Generic[EntityT]):
    """CRUD contract for a single entity type."""

    def get(self, entity_id: UUID) -> EntityT | None:
        """Return one entity by ID, or None when it does not exist."""

    def list(self) -> list[EntityT]:
        """Return every entity of this type."""

    def save(self, entity: EntityT) -> None:
        """Persist an entity create/update operation."""

    def delete(self, entity_id: UUID) -> EntityT | None:
        """Delete one entity by ID and return the removed value."""


class LabTrackerRepository(Protocol):
    """Repository surface expected by the Lab Tracker domain layer."""

    projects: EntityRepository[Project]
    project_groups: EntityRepository[ProjectGroup]
    project_memberships: EntityRepository[ProjectMembership]
    group_memberships: EntityRepository[GroupMembership]
    supervision_edges: EntityRepository[SupervisionEdge]
    ownership_reassignments: EntityRepository[OwnershipReassignment]
    record_export_events: EntityRepository[RecordExportEvent]
    usage_events: EntityRepository[UsageEvent]
    usage_event_rollups: EntityRepository[UsageEventRollup]
    questions: EntityRepository[Question]
    question_refactors: EntityRepository[QuestionRefactor]
    datasets: EntityRepository[Dataset]
    notes: EntityRepository[Note]
    sessions: EntityRepository[Session]
    acquisition_outputs: EntityRepository[AcquisitionOutput]
    analyses: EntityRepository[Analysis]
    claims: EntityRepository[Claim]
    claim_edges: EntityRepository[ClaimEdge]
    entity_versions: EntityRepository[EntityVersion]
    goals: EntityRepository[Goal]
    visualizations: EntityRepository[Visualization]
    graph_change_sets: EntityRepository[GraphChangeSet]
    graph_draft_batch_settings: EntityRepository[GraphDraftBatchSettings]
    graph_draft_batch_runs: EntityRepository[GraphDraftBatchRun]

    def user_exists(self, user_id: UUID) -> bool:
        """Return whether a user exists for FK-backed attribution."""

    def fetch_questions(self, question_ids: list[UUID]) -> list[Question]:
        """Fetch questions in the provided order."""

    def fetch_notes(self, note_ids: list[UUID]) -> list[Note]:
        """Fetch notes in the provided order."""

    def query_projects(
        self,
        *,
        group_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Project], int]:
        """Query projects with filters and pagination."""

    def query_project_groups(
        self,
        *,
        kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ProjectGroup], int]:
        """Query project groups with filters and pagination."""

    def query_project_memberships(
        self,
        *,
        project_id: UUID | None = None,
        user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ProjectMembership], int]:
        """Query project memberships with optional project/user filters."""

    def get_project_membership(
        self,
        *,
        project_id: UUID,
        user_id: UUID,
    ) -> ProjectMembership | None:
        """Return one project membership by project and user."""

    def lock_project_owner_memberships(self, project_id: UUID) -> None:
        """Lock direct owner membership rows for a project during invariant checks."""

    def query_group_memberships(
        self,
        *,
        group_id: UUID | None = None,
        user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GroupMembership], int]:
        """Query group memberships with optional group/user filters."""

    def get_group_membership(
        self,
        *,
        group_id: UUID,
        user_id: UUID,
    ) -> GroupMembership | None:
        """Return one group membership by group and user."""

    def query_supervision_edges(
        self,
        *,
        supervisor_user_id: UUID | None = None,
        supervisee_user_id: UUID | None = None,
        active_only: bool = False,
        as_of: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[SupervisionEdge], int]:
        """Query dated supervision edges."""

    def query_ownership_reassignments(
        self,
        *,
        from_user_id: UUID | None = None,
        to_user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[OwnershipReassignment], int]:
        """Query ownership reassignment audit records."""

    def reassign_ownership(
        self,
        *,
        reassignment_id: UUID,
        from_user_id: UUID,
        to_user_id: UUID,
        reason: str,
        created_by: str | None,
        created_by_user_id: UUID | None,
        created_at: datetime,
    ) -> OwnershipReassignment:
        """Move attribution values and record the reassignment audit row."""

    def query_record_export_events(
        self,
        *,
        user_id: UUID | None = None,
        group_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[RecordExportEvent], int]:
        """Query user record export audit events."""

    def query_usage_events(
        self,
        *,
        project_id: UUID | None = None,
        verb: str | None = None,
        resource_type: str | None = None,
        surface: str | None = None,
        outcome: str | None = None,
        occurred_before: datetime | None = None,
        occurred_on_or_after: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[UsageEvent], int]:
        """Query local usage telemetry events."""

    def usage_event_summary(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, object]]:
        """Return usage event counts by day, verb, and resource type."""

    def rollup_usage_events_before(self, cutoff: datetime) -> int:
        """Summarize and delete raw usage events older than cutoff."""

    def records_attributed_to_user(
        self,
        *,
        user_id: UUID,
        project_ids: set[UUID] | None = None,
    ) -> RecordExportRecords:
        """Return records attributed to a user for export/offboarding recovery."""

    def query_questions(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        superseded_by_question_ids: set[UUID] | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
        updated_first: bool = False,
    ) -> tuple[list[Question], int]:
        """Query questions with filters and pagination."""

    def query_question_refactors(
        self,
        *,
        question_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[QuestionRefactor], int]:
        """Query refactor history where the question is source or replacement."""

    def query_datasets(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Dataset], int]:
        """Query datasets with filters and pagination."""

    def query_notes(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Note], int]:
        """Query notes with filters and pagination."""

    def project_ids_with_search_matches(
        self,
        *,
        search: str,
        project_ids: set[UUID] | None = None,
        limit: int | None = None,
    ) -> set[UUID]:
        """Return distinct project IDs whose questions or notes match search."""

    def query_sessions(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Session], int]:
        """Query sessions with filters and pagination."""

    def query_acquisition_outputs(
        self,
        *,
        session_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[AcquisitionOutput], int]:
        """Query acquisition outputs with filters and pagination."""

    def query_dataset_files(
        self,
        *,
        dataset_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DatasetFile], int]:
        """Query dataset files with pagination."""

    def query_analyses(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
        status: str | None = None,
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Analysis], int]:
        """Query analyses with filters and pagination."""

    def query_claims(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Claim], int]:
        """Query claims with filters and pagination."""

    def query_claim_edges(
        self,
        *,
        project_id: UUID | None = None,
        claim_id: UUID | None = None,
        target_claim_id: UUID | None = None,
        relation: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ClaimEdge], int]:
        """Query typed claim-to-claim edges with filters and pagination."""

    def query_entity_versions(
        self,
        *,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        change_set_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[EntityVersion], int]:
        """Query recorded entity snapshots with filters and pagination."""

    def query_goals(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        goal_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        target_entity_keys: set[tuple[str, UUID]] | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Goal], int]:
        """Query goals with filters and optional reverse node lookup."""

    def query_goal_links(
        self,
        *,
        goal_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        link_status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GoalLink], int]:
        """Query goal links with filters and pagination."""

    def query_visualizations(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Visualization], int]:
        """Query visualizations with filters and pagination."""

    def query_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        source_note_id: UUID | None = None,
        draft_mode: str | None = None,
        batch_key: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_operations: bool = True,
    ) -> tuple[list[GraphChangeSet], int]:
        """Query graph draft change sets with filters and pagination."""

    def claim_graph_change_set_for_commit(
        self,
        change_set_id: UUID,
    ) -> GraphChangeSet | None:
        """Atomically move a ready/submitted graph draft into commit processing."""

    def get_graph_draft_batch_settings_by_project(
        self,
        project_id: UUID,
    ) -> GraphDraftBatchSettings | None:
        """Return graph draft batch settings for a project."""

    def list_due_graph_draft_batch_settings(
        self,
        now: datetime,
    ) -> list[GraphDraftBatchSettings]:
        """Return batch settings whose next_run_at is due."""

    def get_graph_draft_batch_run_by_key(self, batch_key: str) -> GraphDraftBatchRun | None:
        """Return one batch run by idempotency key."""

    def latest_successful_graph_draft_batch_run(
        self,
        project_id: UUID,
    ) -> GraphDraftBatchRun | None:
        """Return the latest successful/skipped batch run for a project."""

    def successful_graph_draft_batch_source_note_ids_at_window_end(
        self,
        project_id: UUID,
        window_end: datetime,
    ) -> set[UUID]:
        """Return source note IDs from successful batch runs ending at a window boundary."""

    def query_graph_draft_batch_runs(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GraphDraftBatchRun], int]:
        """Query graph draft batch run history."""

    def list_dataset_files(self, dataset_id: UUID) -> list[DatasetFile]:
        """Return all files attached to a dataset."""

    def list_dataset_note_target_ids(self, dataset_id: UUID) -> list[UUID]:
        """Return note IDs that target the dataset."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""
