"""Top-level SQLAlchemy repository composed from focused domain modules."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    AcquisitionOutputModel,
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimModel,
    DatasetFileModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    NoteModel,
    NoteTargetModel,
    QuestionModel,
    SessionModel,
    VisualizationClaimModel,
    VisualizationModel,
)
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
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.sqlalchemy_mappers import (
    acquisition_output_from_model,
    analysis_from_model,
    claim_from_model,
    dataset_from_model,
    dataset_question_link_from_model,
    session_from_model,
    visualization_from_model,
)

from .analyses import (
    SQLAlchemyAnalysisRepository,
    SQLAlchemyClaimRepository,
    SQLAlchemyVisualizationRepository,
)
from .common import apply_pagination, count_from_statement
from .core import SQLAlchemyProjectRepository, SQLAlchemyQuestionRepository
from .datasets import SQLAlchemyDatasetRepository
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

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def dataset_entities_from_rows(self, rows: list[DatasetModel]) -> list[Dataset]:
        dataset_ids = [row.dataset_id for row in rows]
        link_map = self.datasets.link_map(dataset_ids)
        return [
            dataset_from_model(
                row,
                question_links=[
                    dataset_question_link_from_model(link)
                    for link in link_map.get(row.dataset_id, [])
                ],
            )
            for row in rows
        ]

    def analysis_entities_from_rows(self, rows: list[AnalysisModel]) -> list[Analysis]:
        analysis_ids = [row.analysis_id for row in rows]
        dataset_map = self.analyses.dataset_map(analysis_ids)
        return [
            analysis_from_model(row, dataset_ids=dataset_map.get(row.analysis_id, []))
            for row in rows
        ]

    def claim_entities_from_rows(self, rows: list[ClaimModel]) -> list[Claim]:
        claim_ids = [row.claim_id for row in rows]
        dataset_map = self.claims.dataset_map(claim_ids)
        analysis_map = self.claims.analysis_map(claim_ids)
        return [
            claim_from_model(
                row,
                supported_by_dataset_ids=dataset_map.get(row.claim_id, []),
                supported_by_analysis_ids=analysis_map.get(row.claim_id, []),
            )
            for row in rows
        ]

    def visualization_entities_from_rows(
        self,
        rows: list[VisualizationModel],
    ) -> list[Visualization]:
        viz_ids = [row.viz_id for row in rows]
        claim_map = self.visualizations.claim_map(viz_ids)
        return [
            visualization_from_model(
                row,
                related_claim_ids=claim_map.get(row.viz_id, []),
            )
            for row in rows
        ]

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
        files, _ = self.query_dataset_files(dataset_id=dataset_id, limit=None, offset=0)
        return files

    def list_dataset_note_target_ids(self, dataset_id: UUID) -> list[UUID]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(NoteTargetModel.note_id).where(
                    NoteTargetModel.entity_type == "dataset",
                    NoteTargetModel.entity_id == str(dataset_id),
                )
            )
        )
        return [UUID(note_id) for note_id in rows]

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
        self._session.flush()
        stmt = select(DatasetModel)
        count_stmt = select(DatasetModel.dataset_id)
        if project_id is not None:
            stmt = stmt.where(DatasetModel.project_id == str(project_id))
            count_stmt = count_stmt.where(DatasetModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(DatasetModel.status == status)
            count_stmt = count_stmt.where(DatasetModel.status == status)
        stmt = stmt.order_by(DatasetModel.created_at, DatasetModel.dataset_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.dataset_entities_from_rows(rows), total

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
        self._session.flush()
        stmt = select(SessionModel)
        count_stmt = select(SessionModel.session_id)
        if project_id is not None:
            stmt = stmt.where(SessionModel.project_id == str(project_id))
            count_stmt = count_stmt.where(SessionModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(SessionModel.status == status)
            count_stmt = count_stmt.where(SessionModel.status == status)
        if session_type is not None:
            stmt = stmt.where(SessionModel.session_type == session_type)
            count_stmt = count_stmt.where(SessionModel.session_type == session_type)
        stmt = stmt.order_by(SessionModel.started_at, SessionModel.session_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [session_from_model(row) for row in rows], total

    def query_acquisition_outputs(
        self,
        *,
        session_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[AcquisitionOutput], int]:
        self._session.flush()
        stmt = select(AcquisitionOutputModel)
        count_stmt = select(AcquisitionOutputModel.output_id)
        if session_id is not None:
            stmt = stmt.where(AcquisitionOutputModel.session_id == str(session_id))
            count_stmt = count_stmt.where(AcquisitionOutputModel.session_id == str(session_id))
        stmt = stmt.order_by(AcquisitionOutputModel.created_at, AcquisitionOutputModel.output_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [acquisition_output_from_model(row) for row in rows], total

    def query_dataset_files(
        self,
        *,
        dataset_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DatasetFile], int]:
        self._session.flush()
        stmt = select(DatasetFileModel).where(DatasetFileModel.dataset_id == str(dataset_id))
        count_stmt = select(DatasetFileModel.file_id).where(
            DatasetFileModel.dataset_id == str(dataset_id)
        )
        stmt = stmt.order_by(DatasetFileModel.created_at, DatasetFileModel.file_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return (
            [
                DatasetFile(
                    file_id=UUID(row.file_id),
                    path=row.path,
                    checksum=row.checksum,
                    size_bytes=row.size_bytes,
                )
                for row in rows
            ],
            total,
        )

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
        self._session.flush()
        stmt = select(AnalysisModel)
        count_stmt = select(AnalysisModel.analysis_id)
        distinct_required = False
        if project_id is not None:
            stmt = stmt.where(AnalysisModel.project_id == str(project_id))
            count_stmt = count_stmt.where(AnalysisModel.project_id == str(project_id))
        if dataset_id is not None:
            stmt = stmt.join(
                AnalysisDatasetModel,
                AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
            ).where(AnalysisDatasetModel.dataset_id == str(dataset_id))
            count_stmt = count_stmt.join(
                AnalysisDatasetModel,
                AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
            ).where(AnalysisDatasetModel.dataset_id == str(dataset_id))
        if question_id is not None:
            distinct_required = True
            stmt = stmt.join(
                AnalysisDatasetModel,
                AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
            ).join(
                DatasetQuestionLinkModel,
                DatasetQuestionLinkModel.dataset_id == AnalysisDatasetModel.dataset_id,
            ).where(DatasetQuestionLinkModel.question_id == str(question_id))
            count_stmt = count_stmt.join(
                AnalysisDatasetModel,
                AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
            ).join(
                DatasetQuestionLinkModel,
                DatasetQuestionLinkModel.dataset_id == AnalysisDatasetModel.dataset_id,
            ).where(DatasetQuestionLinkModel.question_id == str(question_id))
        if status is not None:
            stmt = stmt.where(AnalysisModel.status == status)
            count_stmt = count_stmt.where(AnalysisModel.status == status)
        if distinct_required:
            stmt = stmt.distinct()
            count_stmt = count_stmt.distinct()
        stmt = stmt.order_by(AnalysisModel.created_at, AnalysisModel.analysis_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.analysis_entities_from_rows(rows), total

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
        self._session.flush()
        stmt = select(ClaimModel)
        count_stmt = select(ClaimModel.claim_id)
        distinct_required = False
        if project_id is not None:
            stmt = stmt.where(ClaimModel.project_id == str(project_id))
            count_stmt = count_stmt.where(ClaimModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(ClaimModel.status == status)
            count_stmt = count_stmt.where(ClaimModel.status == status)
        if dataset_id is not None:
            distinct_required = True
            stmt = stmt.join(
                ClaimDatasetModel,
                ClaimDatasetModel.claim_id == ClaimModel.claim_id,
            ).where(ClaimDatasetModel.dataset_id == str(dataset_id))
            count_stmt = count_stmt.join(
                ClaimDatasetModel,
                ClaimDatasetModel.claim_id == ClaimModel.claim_id,
            ).where(ClaimDatasetModel.dataset_id == str(dataset_id))
        if analysis_id is not None:
            distinct_required = True
            stmt = stmt.join(
                ClaimAnalysisModel,
                ClaimAnalysisModel.claim_id == ClaimModel.claim_id,
            ).where(ClaimAnalysisModel.analysis_id == str(analysis_id))
            count_stmt = count_stmt.join(
                ClaimAnalysisModel,
                ClaimAnalysisModel.claim_id == ClaimModel.claim_id,
            ).where(ClaimAnalysisModel.analysis_id == str(analysis_id))
        if distinct_required:
            stmt = stmt.distinct()
            count_stmt = count_stmt.distinct()
        stmt = stmt.order_by(ClaimModel.created_at, ClaimModel.claim_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.claim_entities_from_rows(rows), total

    def query_visualizations(
        self,
        *,
        project_id: UUID | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Visualization], int]:
        self._session.flush()
        stmt = select(VisualizationModel)
        count_stmt = select(VisualizationModel.viz_id)
        distinct_required = False
        if project_id is not None:
            stmt = stmt.join(
                AnalysisModel,
                AnalysisModel.analysis_id == VisualizationModel.analysis_id,
            ).where(AnalysisModel.project_id == str(project_id))
            count_stmt = count_stmt.join(
                AnalysisModel,
                AnalysisModel.analysis_id == VisualizationModel.analysis_id,
            ).where(AnalysisModel.project_id == str(project_id))
        if analysis_id is not None:
            stmt = stmt.where(VisualizationModel.analysis_id == str(analysis_id))
            count_stmt = count_stmt.where(VisualizationModel.analysis_id == str(analysis_id))
        if claim_id is not None:
            distinct_required = True
            stmt = stmt.join(
                VisualizationClaimModel,
                VisualizationClaimModel.viz_id == VisualizationModel.viz_id,
            ).where(VisualizationClaimModel.claim_id == str(claim_id))
            count_stmt = count_stmt.join(
                VisualizationClaimModel,
                VisualizationClaimModel.viz_id == VisualizationModel.viz_id,
            ).where(VisualizationClaimModel.claim_id == str(claim_id))
        if distinct_required:
            stmt = stmt.distinct()
            count_stmt = count_stmt.distinct()
        stmt = stmt.order_by(VisualizationModel.created_at, VisualizationModel.viz_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self.visualization_entities_from_rows(rows), total
