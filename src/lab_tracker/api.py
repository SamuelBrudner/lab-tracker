"""Core API facade and repository wiring for lab tracker."""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Callable, TypeVar
from uuid import UUID

from lab_tracker.errors import NotFoundError
from lab_tracker.models import (
    Note,
    Question,
)
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.request_context import LabTrackerRequestContext
from lab_tracker.services import (
    AnalysisServiceMixin,
    ClaimServiceMixin,
    DatasetServiceMixin,
    GraphDraftServiceMixin,
    NoteServiceMixin,
    ProjectServiceMixin,
    QuestionServiceMixin,
    SessionServiceMixin,
    VisualizationServiceMixin,
)

_logger = logging.getLogger(__name__)
EntityT = TypeVar("EntityT")
ResponseT = TypeVar("ResponseT")


class LabTrackerAPI(
    ProjectServiceMixin,
    QuestionServiceMixin,
    DatasetServiceMixin,
    NoteServiceMixin,
    SessionServiceMixin,
    AnalysisServiceMixin,
    ClaimServiceMixin,
    GraphDraftServiceMixin,
    VisualizationServiceMixin,
):
    def __init__(
        self,
        *,
        raw_storage: LocalNoteStorage | None = None,
        repository: LabTrackerRepository | None = None,
        request_context: LabTrackerRequestContext | None = None,
    ) -> None:
        self._raw_storage = raw_storage
        self._repository = repository
        self._request_context = request_context

    def for_request(self, repository: LabTrackerRepository) -> "LabTrackerAPI":
        return self._for_request_context(LabTrackerRequestContext(repository=repository))

    def _for_request_context(
        self,
        request_context: LabTrackerRequestContext,
    ) -> "LabTrackerAPI":
        return self.__class__(
            raw_storage=self._raw_storage,
            repository=request_context.repository,
            request_context=request_context,
        )

    def request_scope(
        self,
        repository: LabTrackerRepository,
        *,
        close: Callable[[], None] | None = None,
    ) -> "LabTrackerRequestScope":
        return LabTrackerRequestScope(root_api=self, repository=repository, close=close)

    def _active_repository(self) -> LabTrackerRepository:
        if self._request_context is not None:
            return self._request_context.repository
        if self._repository is None:
            raise RuntimeError("Lab Tracker repository is not available.")
        return self._repository

    def _get_from_repository(
        self,
        *,
        entity_id: UUID,
        label: str,
        loader: Callable[[LabTrackerRepository], object | None],
    ):
        entity = loader(self._active_repository())
        if entity is None:
            raise NotFoundError(f"{label} does not exist.")
        return entity

    def _list_from_repository(
        self,
        *,
        loader: Callable[[LabTrackerRepository], list[object]],
    ) -> list[object]:
        return loader(self._active_repository())

    def _query_from_repository(
        self,
        *,
        loader: Callable[[LabTrackerRepository], tuple[list[EntityT], int]],
    ) -> list[EntityT]:
        repository = self._active_repository()
        entities, _ = loader(repository)
        return entities

    def _is_request_managed(self) -> bool:
        return self._request_context is not None

    def _run_deferred_actions(
        self,
        actions: list[Callable[[], None]] | None,
        *,
        label: str,
    ) -> None:
        for action in actions or []:
            try:
                action()
            except Exception as exc:
                _logger.warning("Deferred %s action failed: %s", label, exc, exc_info=True)

    def run_after_commit(self, action: Callable[[], None]) -> None:
        if self._request_context is not None:
            self._request_context.after_commit_actions.append(action)
            return
        action()

    def run_after_rollback(self, action: Callable[[], None]) -> None:
        if self._request_context is None:
            return
        self._request_context.after_rollback_actions.append(action)

    def _run_repository_write(
        self,
        operation: Callable[[LabTrackerRepository], None],
    ) -> None:
        resolved_repository = self._active_repository()
        try:
            operation(resolved_repository)
            if not self._is_request_managed():
                resolved_repository.commit()
        except Exception:
            resolved_repository.rollback()
            raise

    def search_questions(
        self,
        query: str,
        *,
        project_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Question]:
        return self._query_from_repository(
            loader=lambda repository: repository.query_questions(
                project_id=project_id,
                search=query,
                limit=limit,
                offset=offset,
            ),
        )

    def search_notes(
        self,
        query: str,
        *,
        project_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Note]:
        return self._query_from_repository(
            loader=lambda repository: repository.query_notes(
                project_id=project_id,
                search=query,
                limit=limit,
                offset=offset,
            ),
        )


class LabTrackerRequestScope:
    def __init__(
        self,
        *,
        root_api: LabTrackerAPI,
        repository: LabTrackerRepository,
        close: Callable[[], None] | None = None,
    ) -> None:
        self._root_api = root_api
        self._context = LabTrackerRequestContext(repository=repository)
        self._close = close
        self._completed = False
        self.api = root_api._for_request_context(self._context)

    def __enter__(self) -> "LabTrackerRequestScope":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if not self._completed:
                self.rollback()
        finally:
            if self._close is not None:
                self._close()

    def complete_response(self, response: ResponseT) -> ResponseT:
        if response.status_code >= 400:
            self.rollback()
        else:
            self.commit()
        return response

    def commit(self) -> None:
        if self._completed:
            return
        try:
            self._context.repository.commit()
        except Exception:
            self._context.repository.rollback()
            self._complete_rollback()
            raise
        self._complete_commit()

    def rollback(self) -> None:
        if self._completed:
            return
        try:
            self._context.repository.rollback()
        finally:
            self._complete_rollback()

    def _complete_commit(self) -> None:
        self._completed = True
        self._context.complete_commit(
            run_deferred_actions=lambda actions, label: self._root_api._run_deferred_actions(
                actions,
                label=label,
            ),
        )

    def _complete_rollback(self) -> None:
        self._completed = True
        self._context.complete_rollback(
            run_deferred_actions=lambda actions, label: self._root_api._run_deferred_actions(
                actions,
                label=label,
            ),
        )
