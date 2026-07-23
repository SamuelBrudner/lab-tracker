"""Top-level SQLAlchemy repository composed from focused domain modules."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from hashlib import blake2b
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimEdgeModel,
    ClaimModel,
    DatasetModel,
    ExplorationNodeModel,
    NoteModel,
    QuestionModel,
    SessionModel,
    UserModel,
    VisualizationModel,
)
from lab_tracker.db_types import ensure_uuid
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    ClaimEdge,
    Dataset,
    DatasetFile,
    EntityVersion,
    ExplorationNode,
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
    ProjectMembership,
    ProvenanceLink,
    Question,
    QuestionRefactor,
    RecordExportEvent,
    RecordExportRecords,
    Session,
    SupervisionEdge,
    UsageEvent,
    Visualization,
)
from lab_tracker.sqlalchemy_mappers import session_from_model

from .analyses import (
    SQLAlchemyAnalysisRepository,
    SQLAlchemyClaimEdgeRepository,
    SQLAlchemyClaimRepository,
    SQLAlchemyVisualizationRepository,
)
from .common import apply_pagination, substring_pattern, uuid_values
from .core import (
    SQLAlchemyGroupMembershipRepository,
    SQLAlchemyProjectGroupRepository,
    SQLAlchemyProjectMembershipRepository,
    SQLAlchemyProjectRepository,
    SQLAlchemyQuestionRefactorRepository,
    SQLAlchemyQuestionRepository,
)
from .data_stores import SQLAlchemyDataStoreRepository
from .datasets import SQLAlchemyDatasetRepository
from .evidence_bundles import SQLAlchemyEvidenceBundleRepository
from .exploration import SQLAlchemyExplorationNodeRepository
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
from .provenance_links import SQLAlchemyProvenanceLinkRepository
from .sessions import SQLAlchemyAcquisitionOutputRepository, SQLAlchemySessionRepository
from .supervision import SQLAlchemySupervisionEdgeRepository
from .usage import (
    SQLAlchemyUsageEventRepository,
    SQLAlchemyUsageEventRollupRepository,
    rollup_usage_events_before,
    summarize_usage_events,
)
from .versions import SQLAlchemyEntityVersionRepository

_QUESTION_DAG_LOCK_DOMAIN = b"lab-tracker:question-dag:v1\0"


def _project_question_dag_lock_key(project_id: UUID) -> int:
    """Return a stable, domain-separated signed bigint lock key.

    PostgreSQL advisory locks accept a signed 64-bit integer. Hashing all 128
    UUID bits avoids coupling lock identity to a collision-prone UUID prefix,
    while the domain prefix keeps this lock namespace separate from future
    advisory-lock uses.
    """

    digest = blake2b(
        _QUESTION_DAG_LOCK_DOMAIN + project_id.bytes,
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


class SQLAlchemyLabTrackerRepository:
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
        self.usage_events = SQLAlchemyUsageEventRepository(session)
        self.usage_event_rollups = SQLAlchemyUsageEventRollupRepository(session)
        self.questions = SQLAlchemyQuestionRepository(session)
        self.question_refactors = SQLAlchemyQuestionRefactorRepository(session)
        self.datasets = SQLAlchemyDatasetRepository(session)
        self.notes = SQLAlchemyNoteRepository(session)
        self.sessions = SQLAlchemySessionRepository(session)
        self.acquisition_outputs = SQLAlchemyAcquisitionOutputRepository(session)
        self.analyses = SQLAlchemyAnalysisRepository(session)
        self.claims = SQLAlchemyClaimRepository(session)
        self.claim_edges = SQLAlchemyClaimEdgeRepository(session)
        self.exploration_nodes = SQLAlchemyExplorationNodeRepository(session)
        self.provenance_links = SQLAlchemyProvenanceLinkRepository(session)
        self.entity_versions = SQLAlchemyEntityVersionRepository(session)
        self.goals = SQLAlchemyGoalRepository(session)
        self.data_stores = SQLAlchemyDataStoreRepository(session)
        self.evidence_bundles = SQLAlchemyEvidenceBundleRepository(session)
        self.visualizations = SQLAlchemyVisualizationRepository(session)
        self.graph_change_sets = SQLAlchemyGraphChangeSetRepository(session)
        self.graph_draft_batch_settings = SQLAlchemyGraphDraftBatchSettingsRepository(session)
        self.graph_draft_batch_runs = SQLAlchemyGraphDraftBatchRunRepository(session)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """Use a nested SQL transaction for an expected recoverable failure."""

        bind = self._session.get_bind()
        if bind.dialect.name == "sqlite":
            connection = self._session.connection()
            driver_connection = connection.connection.driver_connection
            # Python's sqlite3 driver uses legacy transaction control: a
            # SAVEPOINT issued before a real BEGIN becomes its own transaction,
            # so RELEASE would make the isolated insert durable too early.
            # Establish the outer DBAPI transaction explicitly when needed.
            if not bool(getattr(driver_connection, "in_transaction", True)):
                connection.exec_driver_sql("BEGIN")
        with self._session.begin_nested():
            yield

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

    def lock_project_question_dag(self, project_id: UUID) -> None:
        """Hold the project's question-DAG lock until transaction completion.

        SQLite remains a supported single-process development backend, where
        this deliberately has no effect. PostgreSQL waits transactionally,
        then expires ORM state so reads after a wait observe the winning
        transaction before validating a proposed graph mutation.
        """

        if self._session.get_bind().dialect.name != "postgresql":
            return
        self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _project_question_dag_lock_key(project_id)},
        )
        self._session.expire_all()

    def list_dataset_files(self, dataset_id: UUID) -> list[DatasetFile]:
        return self.datasets.list_file_entities(dataset_id)

    def list_dataset_note_target_ids(self, dataset_id: UUID) -> list[UUID]:
        return self.datasets.list_note_target_ids(dataset_id)

    def query_projects(
        self,
        *,
        group_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        created_by: str | None = None,
        client_capture_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Project], int]:
        return self.projects.query(
            group_id=group_id,
            project_ids=project_ids,
            status=status,
            created_by=created_by,
            client_capture_id=client_capture_id,
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
    ) -> tuple[list[ProjectMembership], int]:
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
    ) -> ProjectMembership | None:
        return self.project_memberships.get_by_project_user(
            project_id=project_id,
            user_id=user_id,
        )

    def lock_project_owner_memberships(self, project_id: UUID) -> None:
        self.project_memberships.lock_project_owners(project_id)

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

    def query_usage_events(
        self,
        *,
        project_id: UUID | None = None,
        verb: str | None = None,
        resource_type: str | None = None,
        surface: str | None = None,
        outcome: str | None = None,
        occurred_before: datetime | None = None,
        occurred_on_or_after: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[UsageEvent], int]:
        return self.usage_events.query(
            project_id=project_id,
            verb=verb,
            resource_type=resource_type,
            surface=surface,
            outcome=outcome,
            occurred_before=occurred_before,
            occurred_on_or_after=occurred_on_or_after,
            limit=limit,
            offset=offset,
        )

    def usage_event_summary(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, object]]:
        self._session.flush()
        return summarize_usage_events(self._session, start=start, end=end)

    def rollup_usage_events_before(self, cutoff: datetime) -> int:
        self._session.flush()
        return rollup_usage_events_before(self._session, cutoff)

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
        session_rows = list(
            self._session.scalars(
                self._attributed_record_statement(
                    SessionModel,
                    created_by_column=SessionModel.created_by,
                    user_id_column=SessionModel.created_by_user_id,
                    user_id=user_id,
                    project_filter=project_filter,
                ).order_by(SessionModel.started_at, SessionModel.session_id)
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
        visualization_rows = list(
            self._session.scalars(
                self._attributed_visualization_statement(
                    user_id=user_id,
                    project_filter=project_filter,
                ).order_by(VisualizationModel.created_at, VisualizationModel.viz_id)
            )
        )
        exploration_node_rows = list(
            self._session.scalars(
                self._attributed_record_statement(
                    ExplorationNodeModel,
                    created_by_column=ExplorationNodeModel.created_by,
                    user_id_column=ExplorationNodeModel.created_by_user_id,
                    user_id=user_id,
                    project_filter=project_filter,
                ).order_by(ExplorationNodeModel.created_at, ExplorationNodeModel.node_id)
            )
        )
        claims = self.claims.claims_from_rows(claim_rows)
        claim_ids = {claim.claim_id for claim in claims}
        claim_edges = self._claim_edges_for_record_claims(
            claim_ids,
            project_filter=project_filter,
        )
        return RecordExportRecords(
            questions=self.questions.questions_from_rows(question_rows),
            datasets=self.datasets.datasets_from_rows(dataset_rows),
            sessions=[session_from_model(row) for row in session_rows],
            notes=self.notes.notes_from_rows(note_rows),
            analyses=self.analyses.analyses_from_rows(analysis_rows),
            claims=claims,
            claim_edges=claim_edges,
            exploration_nodes=self.exploration_nodes.nodes_from_rows(exploration_node_rows),
            visualizations=self.visualizations.visualizations_from_rows(visualization_rows),
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
                self._attribution_predicate(
                    created_by_column=ClaimModel.created_by,
                    user_id_column=ClaimModel.created_by_user_id,
                    user_id=user_id,
                ),
                ClaimModel.claim_id.in_(dataset_claim_ids),
                ClaimModel.claim_id.in_(analysis_claim_ids),
            )
        )
        if project_filter:
            stmt = stmt.where(ClaimModel.project_id.in_(project_filter))
        return stmt.order_by(ClaimModel.created_at, ClaimModel.claim_id)

    def _attributed_visualization_statement(
        self,
        *,
        user_id: UUID,
        project_filter: list[str],
    ):
        stmt = (
            select(VisualizationModel)
            .join(AnalysisModel, AnalysisModel.analysis_id == VisualizationModel.analysis_id)
            .where(
                self._attribution_predicate(
                    created_by_column=VisualizationModel.created_by,
                    user_id_column=VisualizationModel.created_by_user_id,
                    user_id=user_id,
                )
            )
        )
        if project_filter:
            stmt = stmt.where(AnalysisModel.project_id.in_(project_filter))
        return stmt

    def _claim_edges_for_record_claims(
        self,
        claim_ids: set[UUID],
        *,
        project_filter: list[str],
    ) -> list[ClaimEdge]:
        if not claim_ids:
            return []
        stmt = select(ClaimEdgeModel).where(
            or_(
                ClaimEdgeModel.claim_id.in_([str(claim_id) for claim_id in claim_ids]),
                ClaimEdgeModel.target_claim_id.in_([str(claim_id) for claim_id in claim_ids]),
            )
        )
        if project_filter:
            stmt = stmt.join(
                ClaimModel,
                ClaimModel.claim_id == ClaimEdgeModel.claim_id,
            ).where(ClaimModel.project_id.in_(project_filter))
        stmt = stmt.order_by(ClaimEdgeModel.created_at, ClaimEdgeModel.edge_id)
        rows = list(self._session.scalars(stmt))
        edges: list[ClaimEdge] = []
        for row in rows:
            edge = self.claim_edges.get(ensure_uuid(row.edge_id))
            if edge is not None:
                edges.append(edge)
        return edges

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
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        client_capture_id: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        superseded_by_question_ids: set[UUID] | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
        updated_first: bool = False,
    ) -> tuple[list[Question], int]:
        return self.questions.query(
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            question_type=question_type,
            search=search,
            created_by=created_by,
            client_capture_id=client_capture_id,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
            superseded_by_question_ids=superseded_by_question_ids,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
            updated_first=updated_first,
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
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Dataset], int]:
        return self.datasets.query(
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )

    def query_notes(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        client_capture_id: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Note], int]:
        return self.notes.query(
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            search=search,
            created_by=created_by,
            since=since,
            until=until,
            client_capture_id=client_capture_id,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )

    def project_ids_with_search_matches(
        self,
        *,
        search: str,
        project_ids: set[UUID] | None = None,
        limit: int | None = None,
    ) -> set[UUID]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return set()
        pattern = substring_pattern(search)
        if pattern is None:
            return set()

        project_values = uuid_values(project_ids) if project_ids is not None else None
        question_stmt = select(QuestionModel.project_id).where(
            or_(
                QuestionModel.text.ilike(pattern, escape="\\"),
                QuestionModel.hypothesis.ilike(pattern, escape="\\"),
            )
        )
        note_stmt = select(NoteModel.project_id).where(
            or_(
                NoteModel.raw_content.ilike(pattern, escape="\\"),
                NoteModel.transcribed_text.ilike(pattern, escape="\\"),
            )
        )
        if project_values is not None:
            question_stmt = question_stmt.where(QuestionModel.project_id.in_(project_values))
            note_stmt = note_stmt.where(NoteModel.project_id.in_(project_values))

        matching_projects = question_stmt.union(note_stmt).subquery()
        stmt = select(matching_projects.c.project_id).order_by(matching_projects.c.project_id)
        rows = self._session.scalars(apply_pagination(stmt, limit=limit, offset=0))
        return {ensure_uuid(project_id) for project_id in rows}

    def query_sessions(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        session_type: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Session], int]:
        return self.sessions.query(
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            session_type=session_type,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
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
        project_ids: set[UUID] | None = None,
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Analysis], int]:
        return self.analyses.query(
            project_id=project_id,
            project_ids=project_ids,
            dataset_id=dataset_id,
            question_id=question_id,
            status=status,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )

    def query_claims(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Claim], int]:
        return self.claims.query(
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )

    def query_claim_edges(
        self,
        *,
        project_id: UUID | None = None,
        claim_id: UUID | None = None,
        target_claim_id: UUID | None = None,
        relation: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ClaimEdge], int]:
        return self.claim_edges.query(
            project_id=project_id,
            claim_id=claim_id,
            target_claim_id=target_claim_id,
            relation=relation,
            limit=limit,
            offset=offset,
        )

    def query_exploration_nodes(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        node_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[ExplorationNode], int]:
        return self.exploration_nodes.query(
            project_id=project_id,
            project_ids=project_ids,
            node_type=node_type,
            status=status,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            created_by=created_by,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )

    def query_provenance_links(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[ProvenanceLink], int]:
        return self.provenance_links.query(
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )

    def query_entity_versions(
        self,
        *,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        change_set_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[EntityVersion], int]:
        return self.entity_versions.query(
            entity_type=entity_type,
            entity_id=entity_id,
            change_set_id=change_set_id,
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
        target_entity_keys: set[tuple[str, UUID]] | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Goal], int]:
        return self.goals.query(
            project_id=project_id,
            project_ids=project_ids,
            goal_type=goal_type,
            status=status,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            target_entity_keys=target_entity_keys,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
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
        project_ids: set[UUID] | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Visualization], int]:
        return self.visualizations.query(
            project_id=project_id,
            project_ids=project_ids,
            analysis_id=analysis_id,
            claim_id=claim_id,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )

    def query_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        source_note_id: UUID | None = None,
        draft_mode: str | None = None,
        batch_key: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_operations: bool = True,
    ) -> tuple[list[GraphChangeSet], int]:
        return self.graph_change_sets.query(
            project_id=project_id,
            project_ids=project_ids,
            status=status,
            source_note_id=source_note_id,
            draft_mode=draft_mode,
            batch_key=batch_key,
            limit=limit,
            offset=offset,
            include_operations=include_operations,
        )

    def claim_graph_change_set_for_commit(
        self,
        change_set_id: UUID,
    ) -> GraphChangeSet | None:
        return self.graph_change_sets.claim_for_commit(change_set_id)

    def get_graph_draft_batch_settings_by_project(
        self,
        project_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> GraphDraftBatchSettings | None:
        return self.graph_draft_batch_settings.get_by_project(
            project_id,
            user_id=user_id,
        )

    def list_graph_draft_batch_settings_for_project(
        self,
        project_id: UUID,
    ) -> list[GraphDraftBatchSettings]:
        return self.graph_draft_batch_settings.list_for_project(project_id)

    def list_due_graph_draft_batch_settings(
        self,
        now: datetime,
    ) -> list[GraphDraftBatchSettings]:
        return self.graph_draft_batch_settings.list_due(now)

    def claim_due_graph_draft_batch_settings(
        self,
        settings_id: UUID,
        *,
        observed_next_run_at: datetime,
        next_run_at: datetime,
        updated_at: datetime,
        updated_by: str | None,
    ) -> GraphDraftBatchSettings | None:
        return self.graph_draft_batch_settings.claim_due(
            settings_id,
            observed_next_run_at=observed_next_run_at,
            next_run_at=next_run_at,
            updated_at=updated_at,
            updated_by=updated_by,
        )

    def get_graph_draft_batch_run_by_key(self, batch_key: str) -> GraphDraftBatchRun | None:
        return self.graph_draft_batch_runs.get_by_batch_key(batch_key)

    def latest_successful_graph_draft_batch_run(
        self,
        project_id: UUID,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> GraphDraftBatchRun | None:
        return self.graph_draft_batch_runs.latest_successful_for_project(
            project_id,
            review_assignee_user_id=review_assignee_user_id,
            review_assignee=review_assignee,
        )

    def successful_graph_draft_batch_source_note_ids_at_window_end(
        self,
        project_id: UUID,
        window_end: datetime,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> set[UUID]:
        return self.graph_draft_batch_runs.successful_source_note_ids_at_window_end(
            project_id,
            window_end,
            review_assignee_user_id=review_assignee_user_id,
            review_assignee=review_assignee,
        )

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

    def claim_next_pending_graph_draft_batch_run(
        self,
        *,
        claimed_at: datetime,
    ) -> GraphDraftBatchRun | None:
        return self.graph_draft_batch_runs.claim_next_pending(claimed_at=claimed_at)
