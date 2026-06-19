"""Base infrastructure for composed domain services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.config import Settings, get_settings
from lab_tracker.errors import NotFoundError
from lab_tracker.models import (
    UsageEvent,
    UsageEventOutcome,
    UsageEventResourceType,
    UsageEventSurface,
    UsageEventVerb,
)
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.request_context import LabTrackerRequestContext

EntityT = TypeVar("EntityT")


@dataclass(frozen=True)
class ServiceContext:
    raw_storage: LocalNoteStorage | None = None
    repository: LabTrackerRepository | None = None
    request_context: LabTrackerRequestContext | None = None
    settings: Settings | None = None
    surface: str | None = None

    def active_repository(self) -> LabTrackerRepository:
        if self.request_context is not None:
            return self.request_context.repository
        if self.repository is None:
            raise RuntimeError("Lab Tracker repository is not available.")
        return self.repository

    def is_request_managed(self) -> bool:
        return self.request_context is not None

    def active_settings(self) -> Settings:
        return self.settings or get_settings()

    def active_surface(self) -> UsageEventSurface | None:
        surface = self.request_context.surface if self.request_context is not None else self.surface
        if surface is None:
            return None
        try:
            return UsageEventSurface(surface)
        except ValueError:
            return None


class RepositoryUnitOfWork:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._repository: LabTrackerRepository | None = None

    def __enter__(self) -> LabTrackerRepository:
        self._repository = self._context.active_repository()
        return self._repository

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        if self._repository is None:
            return
        if exc_type is not None:
            self._repository.rollback()
            return
        try:
            if not self._context.is_request_managed():
                self._repository.commit()
        except Exception:
            self._repository.rollback()
            raise


class BaseService:
    def __init__(self, context: ServiceContext) -> None:
        self._context = context

    @property
    def raw_storage(self) -> LocalNoteStorage | None:
        return self._context.raw_storage

    @property
    def repository(self) -> LabTrackerRepository:
        return self._context.active_repository()

    def unit_of_work(self) -> RepositoryUnitOfWork:
        return RepositoryUnitOfWork(self._context)

    def get_from_repository(
        self,
        *,
        entity_id: UUID,
        label: str,
        loader: Callable[[LabTrackerRepository], object | None],
    ):
        entity = loader(self.repository)
        if entity is None:
            raise NotFoundError(f"{label} does not exist.")
        return entity

    def list_from_repository(
        self,
        *,
        loader: Callable[[LabTrackerRepository], list[object]],
    ) -> list[object]:
        return loader(self.repository)

    def query_from_repository(
        self,
        *,
        loader: Callable[[LabTrackerRepository], tuple[list[EntityT], int]],
    ) -> list[EntityT]:
        entities, _ = loader(self.repository)
        return entities

    def run_after_commit(self, action: Callable[[], None]) -> None:
        if self._context.request_context is not None:
            self._context.request_context.after_commit_actions.append(action)
            return
        action()

    def run_after_rollback(self, action: Callable[[], None]) -> None:
        if self._context.request_context is None:
            return
        self._context.request_context.after_rollback_actions.append(action)

    def record_usage_event(
        self,
        *,
        verb: UsageEventVerb | str,
        resource_type: UsageEventResourceType | str,
        resource_id: UUID | None = None,
        project_id: UUID | None = None,
        actor: AuthContext | None = None,
        outcome: UsageEventOutcome | str = UsageEventOutcome.OK,
        duration_ms: int | None = None,
        result_count: int | None = None,
        surface: UsageEventSurface | str | None = None,
    ) -> None:
        if not self._context.active_settings().is_usage_events_enabled():
            return
        resolved_surface = _coerce_surface(surface) or self._context.active_surface()
        event = UsageEvent(
            event_id=uuid4(),
            verb=UsageEventVerb(verb),
            resource_type=UsageEventResourceType(resource_type),
            resource_id=resource_id,
            project_id=project_id,
            actor_user_id=actor.user_id if actor is not None else None,
            actor_role=actor.role.value if actor is not None else None,
            principal_type=actor.principal_type.value if actor is not None else None,
            surface=resolved_surface,
            outcome=UsageEventOutcome(outcome),
            duration_ms=duration_ms,
            result_count=result_count,
        )

        def persist_event() -> None:
            repository = self._context.active_repository()
            repository.usage_events.save(event)
            repository.commit()

        if event.outcome == UsageEventOutcome.ERROR:
            self.run_after_rollback(persist_event)
        else:
            self.run_after_commit(persist_event)


def _coerce_surface(value: UsageEventSurface | str | None) -> UsageEventSurface | None:
    if value is None:
        return None
    try:
        return UsageEventSurface(value)
    except ValueError:
        return None
