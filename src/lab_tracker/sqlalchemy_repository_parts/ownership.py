"""Ownership reassignment SQLAlchemy repository."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm.attributes import InstrumentedAttribute

from lab_tracker.db_models import (
    AnalysisModel,
    DatasetModel,
    GoalLinkModel,
    GoalModel,
    GraphChangeSetModel,
    GraphDraftBatchRunModel,
    GroupMembershipModel,
    NoteModel,
    OwnershipReassignmentModel,
    ProjectGroupModel,
    ProjectMembershipModel,
    ProjectModel,
    QuestionModel,
    QuestionRefactorModel,
    SessionModel,
)
from lab_tracker.models import OwnershipReassignment
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc, uuid_from_db, uuid_to_db

from .common import apply_pagination, count_from_statement

_CREATED_BY_TARGETS: tuple[
    tuple[str, type[object], InstrumentedAttribute[str | None], InstrumentedAttribute[str | None]],
    ...,
] = (
    (
        "project_groups",
        ProjectGroupModel,
        ProjectGroupModel.created_by,
        ProjectGroupModel.created_by_user_id,
    ),
    ("projects", ProjectModel, ProjectModel.created_by, ProjectModel.created_by_user_id),
    ("questions", QuestionModel, QuestionModel.created_by, QuestionModel.created_by_user_id),
    (
        "question_refactors",
        QuestionRefactorModel,
        QuestionRefactorModel.created_by,
        QuestionRefactorModel.created_by_user_id,
    ),
    ("datasets", DatasetModel, DatasetModel.created_by, DatasetModel.created_by_user_id),
    ("notes", NoteModel, NoteModel.created_by, NoteModel.created_by_user_id),
    (
        "graph_change_sets",
        GraphChangeSetModel,
        GraphChangeSetModel.created_by,
        GraphChangeSetModel.created_by_user_id,
    ),
    (
        "graph_draft_batch_runs",
        GraphDraftBatchRunModel,
        GraphDraftBatchRunModel.created_by,
        GraphDraftBatchRunModel.created_by_user_id,
    ),
    ("sessions", SessionModel, SessionModel.created_by, SessionModel.created_by_user_id),
    ("goals", GoalModel, GoalModel.created_by, GoalModel.created_by_user_id),
    ("goal_links", GoalLinkModel, GoalLinkModel.created_by, GoalLinkModel.created_by_user_id),
    (
        "project_memberships",
        ProjectMembershipModel,
        ProjectMembershipModel.created_by,
        ProjectMembershipModel.created_by_user_id,
    ),
    (
        "group_memberships",
        GroupMembershipModel,
        GroupMembershipModel.created_by,
        GroupMembershipModel.created_by_user_id,
    ),
)

_EXECUTED_BY_TARGETS: tuple[
    tuple[str, type[object], InstrumentedAttribute[str | None], InstrumentedAttribute[str | None]],
    ...,
] = (
    ("analyses", AnalysisModel, AnalysisModel.executed_by, AnalysisModel.executed_by_user_id),
)


def ownership_reassignment_from_model(
    row: OwnershipReassignmentModel,
) -> OwnershipReassignment:
    return OwnershipReassignment(
        reassignment_id=uuid_from_db(row.reassignment_id),
        from_user_id=uuid_from_db(row.from_user_id),
        to_user_id=uuid_from_db(row.to_user_id),
        reason=row.reason or "",
        record_counts={key: int(value) for key, value in (row.record_counts or {}).items()},
        created_by=row.created_by,
        created_by_user_id=(
            uuid_from_db(row.created_by_user_id)
            if row.created_by_user_id is not None
            else None
        ),
        created_at=as_utc(row.created_at),
    )


class SQLAlchemyOwnershipReassignmentRepository(EntityRepository[OwnershipReassignment]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> OwnershipReassignment | None:
        self._session.flush()
        row = self._session.get(OwnershipReassignmentModel, str(entity_id))
        if row is None:
            return None
        return ownership_reassignment_from_model(row)

    def list(self) -> list[OwnershipReassignment]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(OwnershipReassignmentModel).order_by(
                    OwnershipReassignmentModel.created_at.desc(),
                    OwnershipReassignmentModel.reassignment_id,
                )
            )
        )
        return [ownership_reassignment_from_model(row) for row in rows]

    def save(self, entity: OwnershipReassignment) -> None:
        entity_id = uuid_to_db(entity.reassignment_id)
        row = self._session.get(OwnershipReassignmentModel, entity_id)
        if row is None:
            self._session.add(
                OwnershipReassignmentModel(
                    reassignment_id=entity_id,
                    from_user_id=uuid_to_db(entity.from_user_id),
                    to_user_id=uuid_to_db(entity.to_user_id),
                    reason=entity.reason,
                    record_counts=dict(entity.record_counts),
                    created_by=entity.created_by,
                    created_by_user_id=(
                        uuid_to_db(entity.created_by_user_id)
                        if entity.created_by_user_id is not None
                        else None
                    ),
                    created_at=entity.created_at,
                )
            )
            return
        row.reason = entity.reason
        row.record_counts = dict(entity.record_counts)
        row.created_by = entity.created_by
        row.created_by_user_id = (
            uuid_to_db(entity.created_by_user_id)
            if entity.created_by_user_id is not None
            else None
        )

    def delete(self, entity_id: UUID) -> OwnershipReassignment | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(OwnershipReassignmentModel, uuid_to_db(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        from_user_id: UUID | None = None,
        to_user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[OwnershipReassignment], int]:
        self._session.flush()
        stmt = select(OwnershipReassignmentModel)
        count_stmt = select(OwnershipReassignmentModel.reassignment_id)
        if from_user_id is not None:
            from_user = uuid_to_db(from_user_id)
            stmt = stmt.where(OwnershipReassignmentModel.from_user_id == from_user)
            count_stmt = count_stmt.where(OwnershipReassignmentModel.from_user_id == from_user)
        if to_user_id is not None:
            to_user = uuid_to_db(to_user_id)
            stmt = stmt.where(OwnershipReassignmentModel.to_user_id == to_user)
            count_stmt = count_stmt.where(OwnershipReassignmentModel.to_user_id == to_user)
        stmt = stmt.order_by(
            OwnershipReassignmentModel.created_at.desc(),
            OwnershipReassignmentModel.reassignment_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [ownership_reassignment_from_model(row) for row in rows], total

    def reassign(
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
        from_user = uuid_to_db(from_user_id)
        to_user = uuid_to_db(to_user_id)
        counts: dict[str, int] = {}
        for label, model, text_column, user_id_column in (
            *_CREATED_BY_TARGETS,
            *_EXECUTED_BY_TARGETS,
        ):
            result = self._session.execute(
                update(model)
                .where(
                    or_(
                        text_column == from_user,
                        user_id_column == from_user,
                    )
                )
                .values(
                    {
                        text_column.key: to_user,
                        user_id_column.key: to_user,
                    }
                )
            )
            counts[label] = int(result.rowcount or 0)

        row = OwnershipReassignmentModel(
            reassignment_id=uuid_to_db(reassignment_id),
            from_user_id=from_user,
            to_user_id=to_user,
            reason=reason,
            record_counts=counts,
            created_by=created_by,
            created_by_user_id=(
                uuid_to_db(created_by_user_id)
                if created_by_user_id is not None
                else None
            ),
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return ownership_reassignment_from_model(row)
