"""Supervision edge SQLAlchemy repository."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import SupervisionEdgeModel
from lab_tracker.models import SupervisionEdge
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc, uuid_from_db, uuid_to_db

from .common import apply_pagination, count_from_statement


def supervision_edge_to_model(edge: SupervisionEdge) -> SupervisionEdgeModel:
    return SupervisionEdgeModel(
        edge_id=uuid_to_db(edge.edge_id),
        supervisor_user_id=uuid_to_db(edge.supervisor_user_id),
        supervisee_user_id=uuid_to_db(edge.supervisee_user_id),
        started_at=edge.started_at,
        ended_at=edge.ended_at,
        created_at=edge.created_at,
        updated_at=edge.updated_at,
    )


def supervision_edge_from_model(row: SupervisionEdgeModel) -> SupervisionEdge:
    return SupervisionEdge(
        edge_id=uuid_from_db(row.edge_id),
        supervisor_user_id=uuid_from_db(row.supervisor_user_id),
        supervisee_user_id=uuid_from_db(row.supervisee_user_id),
        started_at=as_utc(row.started_at),
        ended_at=as_utc(row.ended_at) if row.ended_at is not None else None,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


def apply_supervision_edge_to_model(
    row: SupervisionEdgeModel,
    edge: SupervisionEdge,
) -> None:
    row.supervisor_user_id = uuid_to_db(edge.supervisor_user_id)
    row.supervisee_user_id = uuid_to_db(edge.supervisee_user_id)
    row.started_at = edge.started_at
    row.ended_at = edge.ended_at
    row.created_at = edge.created_at
    row.updated_at = edge.updated_at


class SQLAlchemySupervisionEdgeRepository(EntityRepository[SupervisionEdge]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> SupervisionEdge | None:
        self._session.flush()
        row = self._session.get(SupervisionEdgeModel, str(entity_id))
        if row is None:
            return None
        return supervision_edge_from_model(row)

    def list(self) -> list[SupervisionEdge]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(SupervisionEdgeModel).order_by(
                    SupervisionEdgeModel.started_at,
                    SupervisionEdgeModel.edge_id,
                )
            )
        )
        return [supervision_edge_from_model(row) for row in rows]

    def save(self, entity: SupervisionEdge) -> None:
        entity_id = str(entity.edge_id)
        row = self._session.get(SupervisionEdgeModel, entity_id)
        if row is None:
            self._session.add(supervision_edge_to_model(entity))
            return
        apply_supervision_edge_to_model(row, entity)

    def delete(self, entity_id: UUID) -> SupervisionEdge | None:
        row = self._session.get(SupervisionEdgeModel, str(entity_id))
        if row is None:
            return None
        edge = supervision_edge_from_model(row)
        self._session.delete(row)
        return edge

    def query(
        self,
        *,
        supervisor_user_id: UUID | None = None,
        supervisee_user_id: UUID | None = None,
        active_only: bool = False,
        as_of: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[SupervisionEdge], int]:
        self._session.flush()
        stmt = select(SupervisionEdgeModel)
        count_stmt = select(SupervisionEdgeModel.edge_id)
        if supervisor_user_id is not None:
            supervisor = str(supervisor_user_id)
            stmt = stmt.where(SupervisionEdgeModel.supervisor_user_id == supervisor)
            count_stmt = count_stmt.where(SupervisionEdgeModel.supervisor_user_id == supervisor)
        if supervisee_user_id is not None:
            supervisee = str(supervisee_user_id)
            stmt = stmt.where(SupervisionEdgeModel.supervisee_user_id == supervisee)
            count_stmt = count_stmt.where(SupervisionEdgeModel.supervisee_user_id == supervisee)
        if active_only:
            stmt = stmt.where(SupervisionEdgeModel.ended_at.is_(None))
            count_stmt = count_stmt.where(SupervisionEdgeModel.ended_at.is_(None))
        if as_of is not None:
            as_of_clause = (
                SupervisionEdgeModel.started_at <= as_of,
                or_(
                    SupervisionEdgeModel.ended_at.is_(None),
                    SupervisionEdgeModel.ended_at > as_of,
                ),
            )
            stmt = stmt.where(*as_of_clause)
            count_stmt = count_stmt.where(*as_of_clause)
        stmt = stmt.order_by(
            SupervisionEdgeModel.started_at,
            SupervisionEdgeModel.edge_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [supervision_edge_from_model(row) for row in rows], total
