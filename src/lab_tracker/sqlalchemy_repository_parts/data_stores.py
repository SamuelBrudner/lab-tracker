"""DataStore SQLAlchemy repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import DataStoreModel
from lab_tracker.models import DataStore
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mappers import (
    apply_data_store_to_model,
    data_store_from_model,
    data_store_to_model,
)

from .common import apply_pagination, count_from_statement


class SQLAlchemyDataStoreRepository(EntityRepository[DataStore]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> DataStore | None:
        self._session.flush()
        row = self._session.get(DataStoreModel, str(entity_id))
        return data_store_from_model(row) if row is not None else None

    def list(self) -> list[DataStore]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(DataStoreModel).order_by(
                    DataStoreModel.created_at, DataStoreModel.store_id
                )
            )
        )
        return [data_store_from_model(row) for row in rows]

    def save(self, entity: DataStore) -> None:
        entity_id = str(entity.store_id)
        row = self._session.get(DataStoreModel, entity_id)
        if row is None:
            self._session.add(data_store_to_model(entity))
        else:
            apply_data_store_to_model(row, entity)
        self._session.flush()

    def delete(self, entity_id: UUID) -> DataStore | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(DataStoreModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DataStore], int]:
        self._session.flush()
        stmt = select(DataStoreModel)
        count_stmt = select(DataStoreModel.store_id)
        if project_id is not None:
            stmt = stmt.where(DataStoreModel.project_id == str(project_id))
            count_stmt = count_stmt.where(DataStoreModel.project_id == str(project_id))
        elif project_ids is not None:
            if not project_ids:
                return [], 0
            values = [str(value) for value in project_ids]
            stmt = stmt.where(DataStoreModel.project_id.in_(values))
            count_stmt = count_stmt.where(DataStoreModel.project_id.in_(values))
        if kind is not None:
            stmt = stmt.where(DataStoreModel.kind == kind)
            count_stmt = count_stmt.where(DataStoreModel.kind == kind)
        stmt = stmt.order_by(DataStoreModel.created_at, DataStoreModel.store_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [data_store_from_model(row) for row in rows], total

    def get_by_name(self, project_id: UUID, name: str) -> DataStore | None:
        self._session.flush()
        row = self._session.scalars(
            select(DataStoreModel).where(
                DataStoreModel.project_id == str(project_id),
                DataStoreModel.name == name,
            )
        ).first()
        return data_store_from_model(row) if row is not None else None

    def get_default(self, project_id: UUID) -> DataStore | None:
        self._session.flush()
        row = self._session.scalars(
            select(DataStoreModel)
            .where(
                DataStoreModel.project_id == str(project_id),
                DataStoreModel.is_default.is_(True),
            )
            .order_by(DataStoreModel.created_at, DataStoreModel.store_id)
        ).first()
        return data_store_from_model(row) if row is not None else None

    def clear_default(self, project_id: UUID, *, except_store_id: UUID | None = None) -> None:
        """Unset is_default on a project's stores, keeping at most one default."""

        self._session.flush()
        rows = self._session.scalars(
            select(DataStoreModel).where(
                DataStoreModel.project_id == str(project_id),
                DataStoreModel.is_default.is_(True),
            )
        )
        keep = str(except_store_id) if except_store_id is not None else None
        for row in rows:
            if row.store_id != keep:
                row.is_default = False
        self._session.flush()
