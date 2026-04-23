"""Core API facade and store wiring for lab tracker."""

from __future__ import annotations

import logging
from typing import Callable
from uuid import UUID

from lab_tracker.errors import NotFoundError
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    Dataset,
    Note,
    Project,
    Question,
    Session,
    Visualization,
)
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.request_context import LabTrackerRequestContext
from lab_tracker.services import (
    AnalysisServiceMixin,
    ClaimServiceMixin,
    DatasetServiceMixin,
    NoteServiceMixin,
    ProjectServiceMixin,
    QuestionServiceMixin,
    SessionServiceMixin,
    VisualizationServiceMixin,
)
from lab_tracker.services.shared import note_matches_substring, question_matches_substring

_logger = logging.getLogger(__name__)


class InMemoryStore:
    def __init__(self) -> None:
        self.projects: dict[UUID, Project] = {}
        self.questions: dict[UUID, Question] = {}
        self.datasets: dict[UUID, Dataset] = {}
        self.notes: dict[UUID, Note] = {}
        self.sessions: dict[UUID, Session] = {}
        self.acquisition_outputs: dict[UUID, AcquisitionOutput] = {}
        self.analyses: dict[UUID, Analysis] = {}
        self.claims: dict[UUID, Claim] = {}
        self.visualizations: dict[UUID, Visualization] = {}


class LabTrackerAPI(
    ProjectServiceMixin,
    QuestionServiceMixin,
    DatasetServiceMixin,
    NoteServiceMixin,
    SessionServiceMixin,
    AnalysisServiceMixin,
    ClaimServiceMixin,
    VisualizationServiceMixin,
):
    def __init__(
        self,
        store: InMemoryStore | None = None,
        *,
        raw_storage: LocalNoteStorage | None = None,
        repository: LabTrackerRepository | None = None,
        request_context: LabTrackerRequestContext | None = None,
        allow_in_memory: bool = False,
    ) -> None:
        self._store = store or InMemoryStore()
        self._raw_storage = raw_storage
        self._repository = repository
        self._request_context = request_context
        self._allow_in_memory = allow_in_memory or store is not None
        if repository is not None and self._allow_in_memory:
            self.hydrate_from_repository(repository)

    @classmethod
    def in_memory(
        cls,
        *,
        raw_storage: LocalNoteStorage | None = None,
        store: InMemoryStore | None = None,
    ) -> "LabTrackerAPI":
        return cls(
            store=store,
            raw_storage=raw_storage,
            allow_in_memory=True,
        )

    def for_request(self, repository: LabTrackerRepository) -> "LabTrackerAPI":
        return self.__class__(
            store=self._store if self._allow_in_memory else None,
            raw_storage=self._raw_storage,
            repository=repository,
            request_context=LabTrackerRequestContext(repository=repository),
            allow_in_memory=self._allow_in_memory,
        )

    def _active_repository(self) -> LabTrackerRepository | None:
        if self._request_context is not None:
            return self._request_context.repository
        return self._repository

    def _uses_in_memory_entity_cache(self) -> bool:
        repository = self._active_repository()
        return repository is None or self._allow_in_memory

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

    def _is_request_managed(self) -> bool:
        return self._request_context is not None

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
    ) -> None:
        resolved_repository = repository or self._active_repository()
        if resolved_repository is None:
            return
        self._hydrate_store_from_repository(store or self._store, resolved_repository)

    @staticmethod
    def _slice_entities(items: list[object], *, limit: int | None, offset: int) -> list[object]:
        resolved_offset = max(offset, 0)
        if limit is None:
            return items[resolved_offset:]
        if limit <= 0:
            return []
        return items[resolved_offset : resolved_offset + limit]

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

    def finish_request(self, *, committed: bool) -> None:
        if self._request_context is None:
            return
        self._request_context.finish(
            committed=committed,
            run_deferred_actions=lambda actions, label: self._run_deferred_actions(
                actions,
                label=label,
            ),
        )

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
                    store=self._store,
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
            questions, _ = repository.query_questions(
                project_id=project_id,
                limit=None,
                offset=0,
            )
            matches = [
                question for question in questions if question_matches_substring(question, query)
            ]
            paged = self._slice_entities(matches, limit=limit, offset=offset)
            return self._cache_entities(
                "questions",
                paged,
                lambda question: question.question_id,
            )
        questions = list(self._store.questions.values())
        if project_id is not None:
            questions = [question for question in questions if question.project_id == project_id]
        matches = [
            question for question in questions if question_matches_substring(question, query)
        ]
        return self._slice_entities(matches, limit=limit, offset=offset)

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
        notes = list(self._store.notes.values())
        if project_id is not None:
            notes = [note for note in notes if note.project_id == project_id]
        matches = [note for note in notes if note_matches_substring(note, query)]
        return self._slice_entities(matches, limit=limit, offset=offset)
