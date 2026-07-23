"""Base infrastructure for composed domain services."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Generic, Literal, TypeVar
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

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdempotentCreateResult(Generic[EntityT]):
    """Typed outcome for a capture-key-aware create command.

    Domain-facing ``create_*`` methods continue to return the entity directly;
    HTTP adapters use this richer result to distinguish a new ``201`` from an
    idempotent ``200`` replay without inferring from a second query.
    """

    action: Literal["created", "reused"]
    entity: EntityT

    @property
    def created(self) -> bool:
        return self.action == "created"

    @property
    def reused(self) -> bool:
        return self.action == "reused"

    def __getattr__(self, name: str):
        """Delegate entity identifiers for usage-event instrumentation."""

        return getattr(self.entity, name)


class ApplicationTransaction:
    """Mutable transaction boundary shared by all services in one context.

    Direct and background APIs do not have request middleware to own a wider
    transaction. A top-level composite command opens this boundary so nested
    units of work share one commit/rollback decision and deferred actions run
    only after that decision. Request-scoped APIs continue to be owned by
    ``LabTrackerRequestScope`` instead.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.after_commit_actions: list[Callable[[], None]] = []
        self.after_rollback_actions: list[Callable[[], None]] = []

    @property
    def active(self) -> bool:
        return self.depth > 0

    def reset(self) -> None:
        self.depth = 0
        self.after_commit_actions.clear()
        self.after_rollback_actions.clear()


def _run_boundary_actions(actions: list[Callable[[], None]], label: str) -> None:
    """Run deferred actions without changing an already-decided outcome."""

    for action in actions:
        try:
            action()
        except Exception as exc:
            _logger.warning(
                "Deferred %s action failed: %s",
                label,
                exc,
                exc_info=True,
            )


def _safe_rollback(repository: LabTrackerRepository) -> None:
    """Best-effort rollback after a failed application transaction."""

    try:
        repository.rollback()
    except Exception:
        _logger.exception("Application transaction rollback failed")


@dataclass(frozen=True)
class ServiceContext:
    raw_storage: LocalNoteStorage | None = None
    repository: LabTrackerRepository | None = None
    request_context: LabTrackerRequestContext | None = None
    settings: Settings | None = None
    surface: str | None = None
    transaction: ApplicationTransaction = field(default_factory=ApplicationTransaction)

    def active_repository(self) -> LabTrackerRepository:
        if self.request_context is not None:
            return self.request_context.repository
        if self.repository is None:
            raise RuntimeError("Lab Tracker repository is not available.")
        return self.repository

    def is_request_managed(self) -> bool:
        return self.request_context is not None

    def owns_active_boundary(self) -> bool:
        """Whether request middleware or a direct command owns the transaction."""

        return self.is_request_managed() or self.transaction.active

    @contextmanager
    def application_transaction(self) -> Iterator[None]:
        """Open or join one atomic boundary for a top-level application command.

        Request middleware already owns request-scoped transactions, so this is
        deliberately a no-op there. For direct/background contexts the outer
        boundary commits once on success, rolls back on body or commit failure,
        and always resets its mutable state so the same API remains reusable.
        """

        if self.is_request_managed():
            yield
            return

        transaction = self.transaction
        if transaction.active:
            transaction.depth += 1
            try:
                yield
            finally:
                transaction.depth -= 1
            return

        repository = self.active_repository()
        transaction.depth = 1
        try:
            yield
        except BaseException:
            self._abort_transaction(transaction, repository)
            raise

        try:
            repository.commit()
        except BaseException:
            self._abort_transaction(transaction, repository)
            raise

        commit_actions = list(transaction.after_commit_actions)
        transaction.reset()
        _run_boundary_actions(commit_actions, "after_commit")

    @staticmethod
    def _abort_transaction(
        transaction: ApplicationTransaction,
        repository: LabTrackerRepository,
    ) -> None:
        rollback_actions = list(transaction.after_rollback_actions)
        transaction.reset()
        _safe_rollback(repository)
        _run_boundary_actions(rollback_actions, "after_rollback")

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
            if not self._context.owns_active_boundary():
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

    @contextmanager
    def recoverable_unit_of_work(self) -> Iterator[LabTrackerRepository]:
        """Isolate a write whose exception may be handled by its caller.

        A caught uniqueness error must not roll back unrelated writes already
        staged by an outer request/application transaction. Use a repository
        savepoint when such a boundary exists; standalone calls retain the
        ordinary unit-of-work commit/rollback behavior.
        """

        if not self._context.owns_active_boundary():
            with self.unit_of_work() as repository:
                yield repository
            return

        repository = self._context.active_repository()
        with repository.savepoint():
            yield repository

    def application_transaction(self) -> AbstractContextManager[None]:
        """Open or join this service context's application transaction."""

        return self._context.application_transaction()

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
        if self._context.transaction.active:
            self._context.transaction.after_commit_actions.append(action)
            return
        action()

    def run_after_rollback(self, action: Callable[[], None]) -> None:
        if self._context.request_context is not None:
            self._context.request_context.after_rollback_actions.append(action)
            return
        if self._context.transaction.active:
            self._context.transaction.after_rollback_actions.append(action)

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
            try:
                repository.usage_events.save(event)
                repository.commit()
            except Exception:
                _safe_rollback(repository)
                raise

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
