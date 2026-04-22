"""Core API facade and store wiring for lab tracker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Callable
from uuid import UUID

from lab_tracker.errors import NotFoundError
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
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.request_context import LabTrackerRequestContext
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.services import (
    AnalysisServiceMixin,
    ClaimServiceMixin,
    DatasetServiceMixin,
    DatasetReviewServiceMixin,
    NoteServiceMixin,
    ProjectServiceMixin,
    QuestionServiceMixin,
    SessionServiceMixin,
    VisualizationServiceMixin,
)
from lab_tracker.services.extraction_backends import QuestionExtractionBackend
from lab_tracker.services.ocr_backends import OCRBackend
from lab_tracker.services.search_backends import (
    InMemorySubstringSearchBackend,
    SearchBackend,
    SearchQuery,
    note_matches_substring,
    question_matches_substring,
)

_logger = logging.getLogger(__name__)


class InMemoryStore:
    def __init__(self) -> None:
        self.projects: dict[UUID, Project] = {}
        self.questions: dict[UUID, Question] = {}
        self.datasets: dict[UUID, Dataset] = {}
        self.dataset_reviews: dict[UUID, DatasetReview] = {}
        self.notes: dict[UUID, Note] = {}
        self.sessions: dict[UUID, Session] = {}
        self.acquisition_outputs: dict[UUID, AcquisitionOutput] = {}
        self.analyses: dict[UUID, Analysis] = {}
        self.claims: dict[UUID, Claim] = {}
        self.visualizations: dict[UUID, Visualization] = {}


@dataclass(frozen=True)
class SearchHealthSnapshot:
    backend_name: str
    degraded: bool
    failure_count: int
    last_failure_at: str | None
    last_failure_message: str | None
    last_failure_operation: str | None


@dataclass
class _SearchHealthState:
    failure_count: int = 0
    last_failure_at: datetime | None = None
    last_failure_message: str | None = None
    last_failure_operation: str | None = None


class LabTrackerAPI(
    ProjectServiceMixin,
    QuestionServiceMixin,
    DatasetServiceMixin,
    DatasetReviewServiceMixin,
    NoteServiceMixin,
    SessionServiceMixin,
    AnalysisServiceMixin,
    ClaimServiceMixin,
    VisualizationServiceMixin,
):
    @property
    def _store(self) -> InMemoryStore:
        return self._base_store

    @_store.setter
    def _store(self, value: InMemoryStore) -> None:
        self._base_store = value

    def __init__(
        self,
        store: InMemoryStore | None = None,
        *,
        raw_storage: LocalNoteStorage | None = None,
        ocr_backend: OCRBackend | None = None,
        question_extraction_backend: QuestionExtractionBackend | None = None,
        search_backend: SearchBackend | None = None,
        repository: LabTrackerRepository | None = None,
        allow_in_memory: bool = False,
    ) -> None:
        self._store = store or InMemoryStore()
        self._raw_storage = raw_storage
        self._repository = repository
        self._request_context: LabTrackerRequestContext | None = None
        self._ocr_backend = ocr_backend
        self._question_extraction_backend = question_extraction_backend
        self._search_backend = search_backend or InMemorySubstringSearchBackend()
        self._allow_in_memory = allow_in_memory or store is not None
        self._search_health_state = _SearchHealthState()
        if repository is not None:
            if self._allow_in_memory:
                self.hydrate_from_repository(
                    repository,
                    hydrate_search_backend=self._should_sync_search_backend(),
                )
            elif self._should_sync_search_backend():
                self._hydrate_search_backend(self._build_store_from_repository(repository))
        elif self._should_sync_search_backend():
            self._hydrate_search_backend()

    @classmethod
    def in_memory(
        cls,
        *,
        raw_storage: LocalNoteStorage | None = None,
        store: InMemoryStore | None = None,
        ocr_backend: OCRBackend | None = None,
        question_extraction_backend: QuestionExtractionBackend | None = None,
        search_backend: SearchBackend | None = None,
    ) -> "LabTrackerAPI":
        return cls(
            store=store,
            raw_storage=raw_storage,
            ocr_backend=ocr_backend,
            question_extraction_backend=question_extraction_backend,
            search_backend=search_backend,
            allow_in_memory=True,
        )

    def build_request_context(
        self,
        repository: LabTrackerRepository,
    ) -> LabTrackerRequestContext:
        return LabTrackerRequestContext(repository=repository)

    def bind_request_context(self, request_context: LabTrackerRequestContext) -> "LabTrackerAPI":
        bound = object.__new__(self.__class__)
        bound.__dict__ = {**self.__dict__, "_request_context": request_context}
        return bound

    def _active_repository(self) -> LabTrackerRepository | None:
        if self._request_context is not None:
            return self._request_context.repository
        return self._repository

    def _is_repository_backed(self) -> bool:
        return self._active_repository() is not None and not self._allow_in_memory

    def _uses_in_memory_entity_cache(self) -> bool:
        return not self._is_repository_backed()

    def _uses_live_repository_substring_search(self) -> bool:
        return (
            self._is_repository_backed()
            and self._search_backend.backend_name == InMemorySubstringSearchBackend.backend_name
        )

    def _should_sync_search_backend(self) -> bool:
        return not self._uses_live_repository_substring_search()

    def _cache_map(self, attribute_name: str) -> dict[UUID, object]:
        return getattr(self._store, attribute_name)

    def _cache_entity(self, attribute_name: str, entity_id: UUID, entity: object):
        if not self._uses_in_memory_entity_cache():
            return entity
        self._cache_map(attribute_name)[entity_id] = entity
        return entity

    def _remember_entity(self, attribute_name: str, entity_id: UUID, entity: object):
        return self._cache_entity(attribute_name, entity_id, entity)

    def _cache_entities(
        self,
        attribute_name: str,
        entities: list[object],
        entity_id_getter: Callable[[object], UUID],
    ) -> list[object]:
        if not self._uses_in_memory_entity_cache():
            return entities
        cache = self._cache_map(attribute_name)
        for entity in entities:
            cache[entity_id_getter(entity)] = entity
        return entities

    def _get_cached_entity(self, attribute_name: str, entity_id: UUID):
        if not self._uses_in_memory_entity_cache():
            return None
        return self._cache_map(attribute_name).get(entity_id)

    def _forget_entity(self, attribute_name: str, entity_id: UUID) -> None:
        if not self._uses_in_memory_entity_cache():
            return
        self._cache_map(attribute_name).pop(entity_id, None)

    def _get_from_repository_or_store(
        self,
        *,
        attribute_name: str,
        entity_id: UUID,
        label: str,
        loader: Callable[[LabTrackerRepository], object | None],
    ):
        cached = self._get_cached_entity(attribute_name, entity_id)
        if cached is not None:
            return cached
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            entity = loader(repository)
            if entity is None:
                raise NotFoundError(f"{label} does not exist.")
            return self._cache_entity(attribute_name, entity_id, entity)
        raise NotFoundError(f"{label} does not exist.")

    def _list_from_repository_or_store(
        self,
        *,
        attribute_name: str,
        loader: Callable[[LabTrackerRepository], list[object]],
        entity_id_getter: Callable[[object], UUID],
    ) -> list[object]:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            entities = loader(repository)
            return self._cache_entities(attribute_name, entities, entity_id_getter)
        return list(self._cache_map(attribute_name).values())

    def _record_search_failure(self, operation: str, exc: Exception) -> None:
        state = self._search_health_state
        state.failure_count += 1
        state.last_failure_at = datetime.now(timezone.utc)
        state.last_failure_message = f"{exc.__class__.__name__}: {exc}"
        state.last_failure_operation = operation
        _logger.warning(
            "Search backend operation %s failed; search is now degraded.",
            operation,
            exc_info=True,
        )

    def _clear_search_failure_state(self) -> None:
        state = self._search_health_state
        state.failure_count = 0
        state.last_failure_at = None
        state.last_failure_message = None
        state.last_failure_operation = None

    def search_health(self) -> SearchHealthSnapshot:
        state = self._search_health_state
        return SearchHealthSnapshot(
            backend_name=self._search_backend.backend_name,
            degraded=state.failure_count > 0,
            failure_count=state.failure_count,
            last_failure_at=(
                state.last_failure_at.isoformat()
                if state.last_failure_at is not None
                else None
            ),
            last_failure_message=state.last_failure_message,
            last_failure_operation=state.last_failure_operation,
        )

    def _is_request_managed(self) -> bool:
        return self._request_context is not None

    def _build_store_from_repository(
        self,
        repository: LabTrackerRepository,
    ) -> InMemoryStore:
        store = InMemoryStore()
        self._hydrate_store_from_repository(store, repository)
        return store

    def _hydrate_store_from_repository(
        self,
        store: InMemoryStore,
        repository: LabTrackerRepository,
    ) -> None:
        store.projects = {project.project_id: project for project in repository.projects.list()}
        store.questions = {
            question.question_id: question for question in repository.questions.list()
        }
        store.datasets = {dataset.dataset_id: dataset for dataset in repository.datasets.list()}
        store.dataset_reviews = {
            review.review_id: review for review in repository.dataset_reviews.list()
        }
        store.notes = {note.note_id: note for note in repository.notes.list()}
        store.sessions = {session.session_id: session for session in repository.sessions.list()}
        store.acquisition_outputs = {
            output.output_id: output for output in repository.acquisition_outputs.list()
        }
        store.analyses = {
            analysis.analysis_id: analysis for analysis in repository.analyses.list()
        }
        store.claims = {claim.claim_id: claim for claim in repository.claims.list()}
        store.visualizations = {
            visualization.viz_id: visualization
            for visualization in repository.visualizations.list()
        }

    def hydrate_from_repository(
        self,
        repository: LabTrackerRepository | None = None,
        *,
        store: InMemoryStore | None = None,
        hydrate_search_backend: bool = True,
    ) -> None:
        resolved_repository = repository or self._active_repository()
        if resolved_repository is None:
            return
        target_store = store or self._store
        self._hydrate_store_from_repository(target_store, resolved_repository)
        if hydrate_search_backend and self._should_sync_search_backend():
            self._hydrate_search_backend(target_store)

    def _hydrate_search_backend(self, store: InMemoryStore | None = None) -> None:
        target_store = store or self._store
        self._search_backend.upsert_questions(target_store.questions.values())
        self._search_backend.upsert_notes(target_store.notes.values())

    def _queue_search_op(self, operation: str, *args: object) -> None:
        if not self._should_sync_search_backend():
            return
        action = lambda operation=operation, args=args: self._apply_search_op_safely(
            operation,
            *args,
        )
        if self._request_context is not None:
            self._request_context.after_commit_actions.append(action)
            return
        action()

    @staticmethod
    def _slice_entities(items: list[object], *, limit: int | None, offset: int) -> list[object]:
        resolved_offset = max(offset, 0)
        if limit is None:
            return items[resolved_offset:]
        if limit <= 0:
            return []
        return items[resolved_offset : resolved_offset + limit]

    def _apply_search_op(self, operation: str, *args: object) -> None:
        if operation == "upsert_questions":
            self._search_backend.upsert_questions(args[0])
            return
        if operation == "delete_questions":
            self._search_backend.delete_questions(args[0])
            return
        if operation == "upsert_notes":
            self._search_backend.upsert_notes(args[0])
            return
        if operation == "delete_notes":
            self._search_backend.delete_notes(args[0])
            return
        raise ValueError(f"Unknown search operation: {operation}")

    def _apply_search_op_safely(self, operation: str, *args: object) -> None:
        try:
            self._apply_search_op(operation, *args)
            self._clear_search_failure_state()
        except Exception as exc:
            self._record_search_failure(operation, exc)

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
        if resolved_repository is None:
            if not self._allow_in_memory:
                raise RuntimeError(
                    "In-memory runtime persistence is deprecated. "
                    "Configure a SQLAlchemy repository context, or use "
                    "LabTrackerAPI.in_memory() for explicit non-persistent mode."
                )
            return
        try:
            operation(resolved_repository)
            if not self._is_request_managed():
                resolved_repository.commit()
        except Exception:
            resolved_repository.rollback()
            if resolved_repository is self._repository:
                self.hydrate_from_repository(
                    resolved_repository,
                    store=self._base_store,
                    hydrate_search_backend=False,
                )
            raise

    def search_questions(
        self,
        query: str,
        *,
        project_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Question]:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            if self._uses_live_repository_substring_search():
                questions, _ = repository.query_questions(
                    project_id=project_id,
                    limit=None,
                    offset=0,
                )
                matches = [
                    question
                    for question in questions
                    if question_matches_substring(question, query)
                ]
                paged = self._slice_entities(matches, limit=limit, offset=offset)
                return self._cache_entities(
                    "questions",
                    paged,
                    lambda question: question.question_id,
                )
            try:
                ids = self._search_backend.search_question_ids(
                    SearchQuery(query=query, project_id=project_id, limit=limit, offset=offset)
                )
                self._clear_search_failure_state()
            except Exception as exc:
                self._record_search_failure("search_questions", exc)
                raise
            questions = repository.fetch_questions(ids)
            return self._cache_entities(
                "questions",
                questions,
                lambda question: question.question_id,
            )
        try:
            ids = self._search_backend.search_question_ids(
                SearchQuery(query=query, project_id=project_id, limit=limit, offset=offset)
            )
            self._clear_search_failure_state()
        except Exception as exc:
            self._record_search_failure("search_questions", exc)
            raise
        results: list[Question] = []
        for question_id in ids:
            question = self._store.questions.get(question_id)
            if question is not None:
                results.append(question)
        return results

    def search_notes(
        self,
        query: str,
        *,
        project_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Note]:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            if self._uses_live_repository_substring_search():
                notes, _ = repository.query_notes(
                    project_id=project_id,
                    limit=None,
                    offset=0,
                )
                matches = [note for note in notes if note_matches_substring(note, query)]
                paged = self._slice_entities(matches, limit=limit, offset=offset)
                return self._cache_entities(
                    "notes",
                    paged,
                    lambda note: note.note_id,
                )
            try:
                ids = self._search_backend.search_note_ids(
                    SearchQuery(query=query, project_id=project_id, limit=limit, offset=offset)
                )
                self._clear_search_failure_state()
            except Exception as exc:
                self._record_search_failure("search_notes", exc)
                raise
            notes = repository.fetch_notes(ids)
            return self._cache_entities(
                "notes",
                notes,
                lambda note: note.note_id,
            )
        try:
            ids = self._search_backend.search_note_ids(
                SearchQuery(query=query, project_id=project_id, limit=limit, offset=offset)
            )
            self._clear_search_failure_state()
        except Exception as exc:
            self._record_search_failure("search_notes", exc)
            raise
        results: list[Note] = []
        for note_id in ids:
            note = self._store.notes.get(note_id)
            if note is not None:
                results.append(note)
        return results
