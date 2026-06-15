"""Repository interfaces for persistence backends."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    Dataset,
    DatasetFile,
    Goal,
    GoalLink,
    GraphChangeSet,
    GraphDraftBatchRun,
    GraphDraftBatchSettings,
    GroupMembership,
    Note,
    Project,
    ProjectGroup,
    ProjectMembership,
    Question,
    QuestionRefactor,
    Session,
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
    questions: EntityRepository[Question]
    question_refactors: EntityRepository[QuestionRefactor]
    datasets: EntityRepository[Dataset]
    notes: EntityRepository[Note]
    sessions: EntityRepository[Session]
    acquisition_outputs: EntityRepository[AcquisitionOutput]
    analyses: EntityRepository[Analysis]
    claims: EntityRepository[Claim]
    goals: EntityRepository[Goal]
    visualizations: EntityRepository[Visualization]
    graph_change_sets: EntityRepository[GraphChangeSet]
    graph_draft_batch_settings: EntityRepository[GraphDraftBatchSettings]
    graph_draft_batch_runs: EntityRepository[GraphDraftBatchRun]

    def fetch_questions(self, question_ids: list[UUID]) -> list[Question]:
        """Fetch questions in the provided order."""

    def fetch_notes(self, note_ids: list[UUID]) -> list[Note]:
        """Fetch notes in the provided order."""

    def query_projects(
        self,
        *,
        group_id: UUID | None = None,
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

    def query_questions(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
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
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Dataset], int]:
        """Query datasets with filters and pagination."""

    def query_notes(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        search: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Note], int]:
        """Query notes with filters and pagination."""

    def query_sessions(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
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
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Analysis], int]:
        """Query analyses with filters and pagination."""

    def query_claims(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Claim], int]:
        """Query claims with filters and pagination."""

    def query_goals(
        self,
        *,
        project_id: UUID | None = None,
        goal_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
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
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Visualization], int]:
        """Query visualizations with filters and pagination."""

    def query_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        source_note_id: UUID | None = None,
        draft_mode: str | None = None,
        batch_key: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GraphChangeSet], int]:
        """Query graph draft change sets with filters and pagination."""

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
