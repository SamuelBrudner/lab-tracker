"""Entity version SQLAlchemy repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import EntityVersionModel
from lab_tracker.models import EntityVersion
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_entity_version_to_model,
    entity_version_from_model,
    entity_version_to_model,
)

from .common import apply_pagination, count_from_statement


class SQLAlchemyEntityVersionRepository(EntityRepository[EntityVersion]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> EntityVersion | None:
        self._session.flush()
        row = self._session.get(EntityVersionModel, str(entity_id))
        if row is None:
            return None
        return entity_version_from_model(row)

    def list(self) -> list[EntityVersion]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(EntityVersionModel).order_by(
                    EntityVersionModel.entity_type,
                    EntityVersionModel.entity_id,
                    EntityVersionModel.version_number,
                    EntityVersionModel.version_id,
                )
            )
        )
        return [entity_version_from_model(row) for row in rows]

    def save(self, entity: EntityVersion) -> None:
        entity_id = str(entity.version_id)
        row = self._session.get(EntityVersionModel, entity_id)
        if row is None:
            self._session.add(entity_version_to_model(entity))
            return
        apply_entity_version_to_model(row, entity)

    def delete(self, entity_id: UUID) -> EntityVersion | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(EntityVersionModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        change_set_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[EntityVersion], int]:
        self._session.flush()
        stmt = select(EntityVersionModel)
        count_stmt = select(EntityVersionModel.version_id)
        if entity_type is not None:
            stmt = stmt.where(EntityVersionModel.entity_type == entity_type)
            count_stmt = count_stmt.where(EntityVersionModel.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(EntityVersionModel.entity_id == str(entity_id))
            count_stmt = count_stmt.where(EntityVersionModel.entity_id == str(entity_id))
        if change_set_id is not None:
            stmt = stmt.where(EntityVersionModel.change_set_id == str(change_set_id))
            count_stmt = count_stmt.where(
                EntityVersionModel.change_set_id == str(change_set_id)
            )
        stmt = stmt.order_by(
            EntityVersionModel.entity_type,
            EntityVersionModel.entity_id,
            EntityVersionModel.version_number,
            EntityVersionModel.version_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [entity_version_from_model(row) for row in rows], total
