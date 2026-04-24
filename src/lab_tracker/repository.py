"""Repository interfaces for persistence backends."""

from __future__ import annotations

from typing import Generic, Protocol, TypeVar
from uuid import UUID

from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    Dataset,
    DatasetFile,
    Note,
    Project,
    Question,
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
    questions: EntityRepository[Question]
    datasets: EntityRepository[Dataset]
    notes: EntityRepository[Note]
    sessions: EntityRepository[Session]
    acquisition_outputs: EntityRepository[AcquisitionOutput]
    analyses: EntityRepository[Analysis]
    claims: EntityRepository[Claim]
    visualizations: EntityRepository[Visualization]

    def fetch_questions(self, question_ids: list[UUID]) -> list[Question]:
        """Fetch questions in the provided order."""

    def fetch_notes(self, note_ids: list[UUID]) -> list[Note]:
        """Fetch notes in the provided order."""

    def query_projects(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Project], int]:
        """Query projects with filters and pagination."""

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

    def list_dataset_files(self, dataset_id: UUID) -> list[DatasetFile]:
        """Return all files attached to a dataset."""

    def list_dataset_note_target_ids(self, dataset_id: UUID) -> list[UUID]:
        """Return note IDs that target the dataset."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""
