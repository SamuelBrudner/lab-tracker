"""Repository interfaces for persistence backends."""

from __future__ import annotations

from typing import Generic, Protocol, TypeVar
from uuid import UUID

from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    Dataset,
    DatasetReview,
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
    dataset_reviews: EntityRepository[DatasetReview]
    notes: EntityRepository[Note]
    sessions: EntityRepository[Session]
    acquisition_outputs: EntityRepository[AcquisitionOutput]
    analyses: EntityRepository[Analysis]
    claims: EntityRepository[Claim]
    visualizations: EntityRepository[Visualization]

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""
