"""Top-level SQLAlchemy repository composed from focused domain modules."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import NoteModel, QuestionModel
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    Dataset,
    DatasetFile,
    GraphChangeSet,
    Note,
    Project,
    Question,
    Session,
    Visualization,
)
from lab_tracker.repository import LabTrackerRepository

from .analyses import (
    SQLAlchemyAnalysisRepository,
    SQLAlchemyClaimRepository,
    SQLAlchemyVisualizationRepository,
)
from .core import SQLAlchemyProjectRepository, SQLAlchemyQuestionRepository
from .datasets import SQLAlchemyDatasetRepository
from .graph_drafts import SQLAlchemyGraphChangeSetRepository
from .notes import SQLAlchemyNoteRepository
from .sessions import SQLAlchemyAcquisitionOutputRepository, SQLAlchemySessionRepository


class SQLAlchemyLabTrackerRepository(LabTrackerRepository):
    """Repository scaffold backed by a SQLAlchemy ORM session."""

    def __init__(self, session: OrmSession) -> None:
        self._session = session
        self.projects = SQLAlchemyProjectRepository(session)
        self.questions = SQLAlchemyQuestionRepository(session)
        self.datasets = SQLAlchemyDatasetRepository(session)
        self.notes = SQLAlchemyNoteRepository(session)
        self.sessions = SQLAlchemySessionRepository(session)
        self.acquisition_outputs = SQLAlchemyAcquisitionOutputRepository(session)
        self.analyses = SQLAlchemyAnalysisRepository(session)
        self.claims = SQLAlchemyClaimRepository(session)
        self.visualizations = SQLAlchemyVisualizationRepository(session)
        self.graph_change_sets = SQLAlchemyGraphChangeSetRepository(session)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def fetch_questions(self, question_ids: list[UUID]) -> list[Question]:
        self._session.flush()
        if not question_ids:
            return []
        rows = list(
            self._session.scalars(
                select(QuestionModel).where(
                    QuestionModel.question_id.in_(
                        [str(question_id) for question_id in question_ids]
                    )
                )
            )
        )
        by_id = {
            question.question_id: question for question in self.questions.questions_from_rows(rows)
        }
        return [by_id[question_id] for question_id in question_ids if question_id in by_id]

    def fetch_notes(self, note_ids: list[UUID]) -> list[Note]:
        self._session.flush()
        if not note_ids:
            return []
        rows = list(
            self._session.scalars(
                select(NoteModel).where(
                    NoteModel.note_id.in_([str(note_id) for note_id in note_ids])
                )
            )
        )
        by_id = {note.note_id: note for note in self.notes.notes_from_rows(rows)}
        return [by_id[note_id] for note_id in note_ids if note_id in by_id]

    def list_dataset_files(self, dataset_id: UUID) -> list[DatasetFile]:
        return self.datasets.list_file_entities(dataset_id)

    def list_dataset_note_target_ids(self, dataset_id: UUID) -> list[UUID]:
        return self.datasets.list_note_target_ids(dataset_id)

    def query_projects(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Project], int]:
        return self.projects.query(status=status, limit=limit, offset=offset)

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
        return self.questions.query(
            project_id=project_id,
            status=status,
            question_type=question_type,
            search=search,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
            limit=limit,
            offset=offset,
        )

    def query_datasets(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Dataset], int]:
        return self.datasets.query(
            project_id=project_id,
            status=status,
            limit=limit,
            offset=offset,
        )

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
        return self.notes.query(
            project_id=project_id,
            status=status,
            search=search,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            limit=limit,
            offset=offset,
        )

    def query_sessions(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Session], int]:
        return self.sessions.query(
            project_id=project_id,
            status=status,
            session_type=session_type,
            limit=limit,
            offset=offset,
        )

    def query_acquisition_outputs(
        self,
        *,
        session_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[AcquisitionOutput], int]:
        return self.acquisition_outputs.query(
            session_id=session_id,
            limit=limit,
            offset=offset,
        )

    def query_dataset_files(
        self,
        *,
        dataset_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DatasetFile], int]:
        return self.datasets.query_files(dataset_id=dataset_id, limit=limit, offset=offset)

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
        return self.analyses.query(
            project_id=project_id,
            dataset_id=dataset_id,
            question_id=question_id,
            status=status,
            limit=limit,
            offset=offset,
        )

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
        return self.claims.query(
            project_id=project_id,
            status=status,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            limit=limit,
            offset=offset,
        )

    def query_visualizations(
        self,
        *,
        project_id: UUID | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Visualization], int]:
        return self.visualizations.query(
            project_id=project_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            limit=limit,
            offset=offset,
        )

    def query_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        source_note_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GraphChangeSet], int]:
        return self.graph_change_sets.query(
            project_id=project_id,
            status=status,
            source_note_id=source_note_id,
            limit=limit,
            offset=offset,
        )
