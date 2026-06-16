"""Top-level SQLAlchemy repository composed from focused domain modules."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimModel,
    DatasetModel,
    NoteModel,
    QuestionModel,
    UserModel,
)
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    Dataset,
    DatasetFile,
    Goal,
    GoalLink,
    GraphChangeSet,
    GraphDraftBatchRun,
    GraphDraftBatchSettings,
    GroupMembership,
    Note,
    OwnershipReassignment,
    Project,
    ProjectGroup,
    Question,
    QuestionRefactor,
    RecordExportEvent,
    RecordExportRecords,
    Session,
    SupervisionEdge,
    Visualization,
)
from lab_tracker.repository import LabTrackerRepository

from .analyses import (
    SQLAlchemyAnalysisRepository,
    SQLAlchemyClaimRepository,
    SQLAlchemyVisualizationRepository,
)
from .core import (
    SQLAlchemyGroupMembershipRepository,
    SQLAlchemyProjectGroupRepository,
    SQLAlchemyProjectMembershipRepository,
    SQLAlchemyProjectRepository,
    SQLAlchemyQuestionRefactorRepository,
    SQLAlchemyQuestionRepository,
)
from .datasets import SQLAlchemyDatasetRepository
from .goals import SQLAlchemyGoalRepository
from .graph_batches import (
    SQLAlchemyGraphDraftBatchRunRepository,
    SQLAlchemyGraphDraftBatchSettingsRepository,
)
from .graph_drafts import SQLAlchemyGraphChangeSetRepository
from .notes import SQLAlchemyNoteRepository
from .ownership import (
    SQLAlchemyOwnershipReassignmentRepository,
    SQLAlchemyRecordExportEventRepository,
)
from .sessions import SQLAlchemyAcquisitionOutputRepository, SQLAlchemySessionRepository
from .supervision import SQLAlchemySupervisionEdgeRepository


class SQLAlchemyLabTrackerRepository(LabTrackerRepository):
    """Repository scaffold backed by a SQLAlchemy ORM session."""

    def __init__(self, session: OrmSession) -> None:
        self._session = session
        self.projects = SQLAlchemyProjectRepository(session)
        self.project_groups = SQLAlchemyProjectGroupRepository(session)
        self.project_memberships = SQLAlchemyProjectMembershipRepository(session)
        self.group_memberships = SQLAlchemyGroupMembershipRepository(session)
        self.supervision_edges = SQLAlchemySupervisionEdgeRepository(session)
        self.ownership_reassignments = SQLAlchemyOwnershipReassignmentRepository(session)
        self.record_export_events = SQLAlchemyRecordExportEventRepository(session)
        self.questions = SQLAlchemyQuestionRepository(session)
        self.question_refactors = SQLAlchemyQuestionRefactorRepository(session)
        self.datasets = SQLAlchemyDatasetRepository(session)
        self.notes = SQLAlchemyNoteRepository(session)
        self.sessions = SQLAlchemySessionRepository(session)
        self.acquisition_outputs = SQLAlchemyAcquisitionOutputRepository(session)
        self.analyses = SQLAlchemyAnalysisRepository(session)
        self.claims = SQLAlchemyClaimRepository(session)
        self.goals = SQLAlchemyGoalRepository(session)
        self.visualizations = SQLAlchemyVisualizationRepository(session)
        self.graph_change_sets = SQLAlchemyGraphChangeSetRepository(session)
        self.graph_draft_batch_settings = SQLAlchemyGraphDraftBatchSettingsRepository(session)
        self.graph_draft_batch_runs = SQLAlchemyGraphDraftBatchRunRepository(session)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def user_exists(self, user_id: UUID) -> bool:
        return self._session.get(UserModel, str(user_id)) is not None

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
        group_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Project], int]:
        return self.projects.query(
            group_id=group_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    def query_project_groups(
        self,
        *,
        kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ProjectGroup], int]:
        return self.project_groups.query(kind=kind, limit=limit, offset=offset)

    def query_project_memberships(
        self,
        *,
        project_id: UUID | None = None,
        user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ):
        return self.project_memberships.query(
            project_id=project_id,
            user_id=user_id,
            limit=limit,
            offset=offset,
        )

    def get_project_membership(
        self,
        *,
        project_id: UUID,
        user_id: UUID,
    ):
        return self.project_memberships.get_by_project_user(
            project_id=project_id,
            user_id=user_id,
        )

    def query_group_memberships(
        self,
        *,
        group_id: UUID | None = None,
        user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GroupMembership], int]:
        return self.group_memberships.query(
            group_id=group_id,
            user_id=user_id,
            limit=limit,
            offset=offset,
        )

    def get_group_membership(
        self,
        *,
        group_id: UUID,
        user_id: UUID,
    ) -> GroupMembership | None:
        return self.group_memberships.get_by_group_user(
            group_id=group_id,
            user_id=user_id,
        )

    def query_supervision_edges(
        self,
        *,
        supervisor_user_id: UUID | None = None,
        supervisee_user_id: UUID | None = None,
        active_only: bool = False,
        as_of: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[SupervisionEdge], int]:
        return self.supervision_edges.query(
            supervisor_user_id=supervisor_user_id,
            supervisee_user_id=supervisee_user_id,
            active_only=active_only,
            as_of=as_of,
            limit=limit,
            offset=offset,
        )

    def query_ownership_reassignments(
        self,
        *,
        from_user_id: UUID | None = None,
        to_user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[OwnershipReassignment], int]:
        return self.ownership_reassignments.query(
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            limit=limit,
            offset=offset,
        )

    def reassign_ownership(
        self,
        *,
        reassignment_id: UUID,
        from_user_id: UUID,
        to_user_id: UUID,
        reason: str,
        created_by: str | None,
        created_by_user_id: UUID | None,
        created_at: datetime,
    ) -> OwnershipReassignment:
        return self.ownership_reassignments.reassign(
            reassignment_id=reassignment_id,
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            reason=reason,
            created_by=created_by,
            created_by_user_id=created_by_user_id,
            created_at=created_at,
        )

    def query_record_export_events(
        self,
        *,
        user_id: UUID | None = None,
        group_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[RecordExportEvent], int]:
        return self.record_export_events.query(
            user_id=user_id,
            group_id=group_id,
            limit=limit,
            offset=offset,
        )

    def records_attributed_to_user(
        self,
        *,
        user_id: UUID,
        project_ids: set[UUID] | None = None,
    ) -> RecordExportRecords:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return RecordExportRecords()
        project_filter = [str(project_id) for project_id in project_ids or set()]
        question_rows = list(
            self._session.scalars(
                self._attributed_record_statement(
                    QuestionModel,
                    created_by_column=QuestionModel.created_by,
                    user_id_column=QuestionModel.created_by_user_id,
                    user_id=user_id,
                    project_filter=project_filter,
                ).order_by(QuestionModel.created_at, QuestionModel.question_id)
            )
        )
        dataset_rows = list(
            self._session.scalars(
                self._attributed_record_statement(
                    DatasetModel,
                    created_by_column=DatasetModel.created_by,
                    user_id_column=DatasetModel.created_by_user_id,
                    user_id=user_id,
                    project_filter=project_filter,
                ).order_by(DatasetModel.created_at, DatasetModel.dataset_id)
            )
        )
        note_rows = list(
            self._session.scalars(
                self._attributed_record_statement(
                    NoteModel,
                    created_by_column=NoteModel.created_by,
                    user_id_column=NoteModel.created_by_user_id,
                    user_id=user_id,
                    project_filter=project_filter,
                ).order_by(NoteModel.created_at, NoteModel.note_id)
            )
        )
        analysis_rows = list(
            self._session.scalars(
                self._attributed_record_statement(
                    AnalysisModel,
                    created_by_column=AnalysisModel.executed_by,
                    user_id_column=AnalysisModel.executed_by_user_id,
                    user_id=user_id,
                    project_filter=project_filter,
                ).order_by(AnalysisModel.created_at, AnalysisModel.analysis_id)
            )
        )
        claim_rows = list(
            self._session.scalars(
                self._attributed_claim_statement(
                    user_id=user_id,
                    project_filter=project_filter,
                )
            )
        )
        return RecordExportRecords(
            questions=self.questions.questions_from_rows(question_rows),
            datasets=self.datasets.datasets_from_rows(dataset_rows),
            notes=self.notes.notes_from_rows(note_rows),
            analyses=self.analyses.analyses_from_rows(analysis_rows),
            claims=self.claims.claims_from_rows(claim_rows),
        )

    def _attributed_record_statement(
        self,
        model_type,
        *,
        created_by_column,
        user_id_column,
        user_id: UUID,
        project_filter: list[str],
    ):
        stmt = select(model_type).where(
            self._attribution_predicate(
                created_by_column=created_by_column,
                user_id_column=user_id_column,
                user_id=user_id,
            )
        )
        if project_filter:
            stmt = stmt.where(model_type.project_id.in_(project_filter))
        return stmt

    def _attributed_claim_statement(
        self,
        *,
        user_id: UUID,
        project_filter: list[str],
    ):
        dataset_claim_ids = (
            select(ClaimDatasetModel.claim_id)
            .join(DatasetModel, DatasetModel.dataset_id == ClaimDatasetModel.dataset_id)
            .where(
                self._attribution_predicate(
                    created_by_column=DatasetModel.created_by,
                    user_id_column=DatasetModel.created_by_user_id,
                    user_id=user_id,
                )
            )
        )
        analysis_claim_ids = (
            select(ClaimAnalysisModel.claim_id)
            .join(AnalysisModel, AnalysisModel.analysis_id == ClaimAnalysisModel.analysis_id)
            .where(
                self._attribution_predicate(
                    created_by_column=AnalysisModel.executed_by,
                    user_id_column=AnalysisModel.executed_by_user_id,
                    user_id=user_id,
                )
            )
        )
        stmt = select(ClaimModel).where(
            or_(
                ClaimModel.claim_id.in_(dataset_claim_ids),
                ClaimModel.claim_id.in_(analysis_claim_ids),
            )
        )
        if project_filter:
            stmt = stmt.where(ClaimModel.project_id.in_(project_filter))
        return stmt.order_by(ClaimModel.created_at, ClaimModel.claim_id)

    @staticmethod
    def _attribution_predicate(
        *,
        created_by_column,
        user_id_column,
        user_id: UUID,
    ):
        user_id_text = str(user_id)
        return or_(
            user_id_column == user_id_text,
            user_id_column.is_(None) & (created_by_column == user_id_text),
        )

    def query_questions(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
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
            created_by=created_by,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
            limit=limit,
            offset=offset,
        )

    def query_question_refactors(
        self,
        *,
        question_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[QuestionRefactor], int]:
        return self.question_refactors.query(
            question_id=question_id,
            limit=limit,
            offset=offset,
        )

    def query_datasets(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Dataset], int]:
        return self.datasets.query(
            project_id=project_id,
            status=status,
            created_by=created_by,
            limit=limit,
            offset=offset,
        )

    def query_notes(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Note], int]:
        return self.notes.query(
            project_id=project_id,
            status=status,
            search=search,
            created_by=created_by,
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
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Analysis], int]:
        return self.analyses.query(
            project_id=project_id,
            dataset_id=dataset_id,
            question_id=question_id,
            status=status,
            created_by=created_by,
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
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Claim], int]:
        return self.claims.query(
            project_id=project_id,
            status=status,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            created_by=created_by,
            limit=limit,
            offset=offset,
        )

    def query_goals(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        goal_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Goal], int]:
        return self.goals.query(
            project_id=project_id,
            project_ids=project_ids,
            goal_type=goal_type,
            status=status,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            limit=limit,
            offset=offset,
        )

    def query_goal_links(
        self,
        *,
        goal_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        link_status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GoalLink], int]:
        return self.goals.query_links(
            goal_id=goal_id,
            entity_type=entity_type,
            entity_id=entity_id,
            link_status=link_status,
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
        draft_mode: str | None = None,
        batch_key: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GraphChangeSet], int]:
        return self.graph_change_sets.query(
            project_id=project_id,
            status=status,
            source_note_id=source_note_id,
            draft_mode=draft_mode,
            batch_key=batch_key,
            limit=limit,
            offset=offset,
        )

    def get_graph_draft_batch_settings_by_project(
        self,
        project_id: UUID,
    ) -> GraphDraftBatchSettings | None:
        return self.graph_draft_batch_settings.get_by_project(project_id)

    def list_due_graph_draft_batch_settings(
        self,
        now: datetime,
    ) -> list[GraphDraftBatchSettings]:
        return self.graph_draft_batch_settings.list_due(now)

    def get_graph_draft_batch_run_by_key(self, batch_key: str) -> GraphDraftBatchRun | None:
        return self.graph_draft_batch_runs.get_by_batch_key(batch_key)

    def latest_successful_graph_draft_batch_run(
        self,
        project_id: UUID,
    ) -> GraphDraftBatchRun | None:
        return self.graph_draft_batch_runs.latest_successful_for_project(project_id)

    def query_graph_draft_batch_runs(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GraphDraftBatchRun], int]:
        return self.graph_draft_batch_runs.query(
            project_id=project_id,
            status=status,
            limit=limit,
            offset=offset,
        )
