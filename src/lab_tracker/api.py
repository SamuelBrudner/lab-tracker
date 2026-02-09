"""Core API facade and store wiring for lab tracker."""

from __future__ import annotations

from typing import Callable
from uuid import UUID

from lab_tracker.dependencies import get_active_repository
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
from lab_tracker.services.extraction_backends import (
    QuestionExtractionBackend,
    RegexQuestionExtractionBackend,
)
from lab_tracker.services.search_backends import (
    InMemorySubstringSearchBackend,
    SearchBackend,
    SearchQuery,
)


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
        question_extraction_backend: QuestionExtractionBackend | None = None,
        search_backend: SearchBackend | None = None,
        repository: LabTrackerRepository | None = None,
        allow_in_memory: bool = False,
    ) -> None:
        self._store = store or InMemoryStore()
        self._raw_storage = raw_storage
        self._repository = repository
        self._question_extraction_backend = (
            question_extraction_backend or RegexQuestionExtractionBackend()
        )
        self._search_backend = search_backend or InMemorySubstringSearchBackend()
        self._allow_in_memory = allow_in_memory or store is not None
        if repository is not None:
            self.hydrate_from_repository(repository)
        else:
            self._hydrate_search_backend()

    @classmethod
    def in_memory(
        cls,
        *,
        raw_storage: LocalNoteStorage | None = None,
        store: InMemoryStore | None = None,
        question_extraction_backend: QuestionExtractionBackend | None = None,
        search_backend: SearchBackend | None = None,
    ) -> "LabTrackerAPI":
        return cls(
            store=store,
            raw_storage=raw_storage,
            question_extraction_backend=question_extraction_backend,
            search_backend=search_backend,
            allow_in_memory=True,
        )

    def _active_repository(self) -> LabTrackerRepository | None:
        return get_active_repository() or self._repository

    def hydrate_from_repository(
        self,
        repository: LabTrackerRepository | None = None,
    ) -> None:
        resolved_repository = repository or self._active_repository()
        if resolved_repository is None:
            return
        self._store.projects = {
            project.project_id: project for project in resolved_repository.projects.list()
        }
        self._store.questions = {
            question.question_id: question for question in resolved_repository.questions.list()
        }
        self._store.datasets = {
            dataset.dataset_id: dataset for dataset in resolved_repository.datasets.list()
        }
        self._store.notes = {note.note_id: note for note in resolved_repository.notes.list()}
        self._store.sessions = {
            session.session_id: session for session in resolved_repository.sessions.list()
        }
        self._store.analyses = {
            analysis.analysis_id: analysis for analysis in resolved_repository.analyses.list()
        }
        self._store.claims = {claim.claim_id: claim for claim in resolved_repository.claims.list()}
        self._store.visualizations = {
            visualization.viz_id: visualization
            for visualization in resolved_repository.visualizations.list()
        }
        try:
            acquisition_outputs = resolved_repository.acquisition_outputs.list()
        except NotImplementedError:
            acquisition_outputs = []
        self._store.acquisition_outputs = {
            output.output_id: output for output in acquisition_outputs
        }
        self._hydrate_search_backend()

    def _hydrate_search_backend(self) -> None:
        self._search_backend.upsert_questions(self._store.questions.values())
        self._search_backend.upsert_notes(self._store.notes.values())

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
        ids = self._search_backend.search_question_ids(
            SearchQuery(query=query, project_id=project_id, limit=limit, offset=offset)
        )
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
        ids = self._search_backend.search_note_ids(
            SearchQuery(query=query, project_id=project_id, limit=limit, offset=offset)
        )
        results: list[Note] = []
        for note_id in ids:
            note = self._store.notes.get(note_id)
            if note is not None:
                results.append(note)
        return results
