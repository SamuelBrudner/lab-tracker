"""Capture context SQLAlchemy repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import CaptureContextModel
from lab_tracker.models import CaptureContext
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_capture_context_to_model,
    capture_context_from_model,
    capture_context_to_model,
)

from .common import apply_pagination, count_from_statement


class SQLAlchemyCaptureContextRepository(EntityRepository[CaptureContext]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> CaptureContext | None:
        self._session.flush()
        row = self._session.get(CaptureContextModel, str(entity_id))
        return capture_context_from_model(row) if row is not None else None

    def list(self) -> list[CaptureContext]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(CaptureContextModel).order_by(
                    CaptureContextModel.created_at,
                    CaptureContextModel.capture_context_id,
                )
            )
        )
        return [capture_context_from_model(row) for row in rows]

    def save(self, entity: CaptureContext) -> None:
        entity_id = str(entity.capture_context_id)
        row = self._session.get(CaptureContextModel, entity_id)
        if row is None:
            self._session.add(capture_context_to_model(entity))
        else:
            apply_capture_context_to_model(row, entity)
        self._session.flush()

    def delete(self, entity_id: UUID) -> CaptureContext | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(CaptureContextModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        include_revoked: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[CaptureContext], int]:
        self._session.flush()
        stmt = select(CaptureContextModel)
        count_stmt = select(CaptureContextModel.capture_context_id)
        if project_id is not None:
            clause = CaptureContextModel.project_id == str(project_id)
            stmt = stmt.where(clause)
            count_stmt = count_stmt.where(clause)
        elif project_ids is not None:
            if not project_ids:
                return [], 0
            values = [str(project_id) for project_id in project_ids]
            stmt = stmt.where(CaptureContextModel.project_id.in_(values))
            count_stmt = count_stmt.where(CaptureContextModel.project_id.in_(values))
        if not include_revoked:
            stmt = stmt.where(CaptureContextModel.revoked_at.is_(None))
            count_stmt = count_stmt.where(CaptureContextModel.revoked_at.is_(None))
        stmt = stmt.order_by(
            CaptureContextModel.created_at,
            CaptureContextModel.capture_context_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [capture_context_from_model(row) for row in rows], total
