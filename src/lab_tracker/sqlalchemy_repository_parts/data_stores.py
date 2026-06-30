"""DataStore SQLAlchemy repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import DataStoreModel, ProjectModel
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
        group_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DataStore], int]:
        self._session.flush()
        stmt = select(DataStoreModel)
        count_stmt = select(DataStoreModel.store_id)
        if project_id is not None:
            clause = DataStoreModel.project_id == str(project_id)
        elif group_id is not None:
            clause = DataStoreModel.group_id == str(group_id)
        elif project_ids is not None:
            if not project_ids:
                return [], 0
            clause = DataStoreModel.project_id.in_([str(value) for value in project_ids])
        else:
            clause = None
        if clause is not None:
            stmt = stmt.where(clause)
            count_stmt = count_stmt.where(clause)
        if kind is not None:
            stmt = stmt.where(DataStoreModel.kind == kind)
            count_stmt = count_stmt.where(DataStoreModel.kind == kind)
        stmt = stmt.order_by(DataStoreModel.created_at, DataStoreModel.store_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [data_store_from_model(row) for row in rows], total

    def _project_group_id(self, project_id: UUID) -> str | None:
        return self._session.scalar(
            select(ProjectModel.group_id).where(ProjectModel.project_id == str(project_id))
        )

    def get_by_name(self, project_id: UUID, name: str) -> DataStore | None:
        """Resolve a store name for a project: own store first, then its group's."""

        self._session.flush()
        own = self.scoped_store_by_name(project_id=project_id, name=name)
        if own is not None:
            return own
        group_id = self._project_group_id(project_id)
        if group_id is not None:
            row = self._session.scalars(
                select(DataStoreModel).where(
                    DataStoreModel.group_id == group_id,
                    DataStoreModel.name == name,
                )
            ).first()
            if row is not None:
                return data_store_from_model(row)
        return None

    def scoped_store_by_name(
        self,
        *,
        project_id: UUID | None = None,
        group_id: UUID | None = None,
        name: str,
    ) -> DataStore | None:
        """Look up a store by name within exactly one scope (no inheritance)."""

        self._session.flush()
        clause = self._scope_clause(project_id=project_id, group_id=group_id)
        row = self._session.scalars(
            select(DataStoreModel).where(clause, DataStoreModel.name == name)
        ).first()
        return data_store_from_model(row) if row is not None else None

    def list_effective_for_project(self, project_id: UUID) -> list[DataStore]:
        """A project's own stores plus the stores it inherits from its group."""

        self._session.flush()
        clauses = [DataStoreModel.project_id == str(project_id)]
        group_id = self._project_group_id(project_id)
        if group_id is not None:
            clauses.append(DataStoreModel.group_id == group_id)
        rows = list(
            self._session.scalars(
                select(DataStoreModel)
                .where(or_(*clauses))
                .order_by(DataStoreModel.created_at, DataStoreModel.store_id)
            )
        )
        return [data_store_from_model(row) for row in rows]

    def get_default(
        self, project_id: UUID | None = None, *, group_id: UUID | None = None
    ) -> DataStore | None:
        self._session.flush()
        clause = self._scope_clause(project_id=project_id, group_id=group_id)
        row = self._session.scalars(
            select(DataStoreModel)
            .where(clause, DataStoreModel.is_default.is_(True))
            .order_by(DataStoreModel.created_at, DataStoreModel.store_id)
        ).first()
        return data_store_from_model(row) if row is not None else None

    def clear_default(
        self,
        project_id: UUID | None = None,
        *,
        group_id: UUID | None = None,
        except_store_id: UUID | None = None,
    ) -> None:
        """Unset is_default within a scope, keeping at most one default."""

        self._session.flush()
        clause = self._scope_clause(project_id=project_id, group_id=group_id)
        rows = self._session.scalars(
            select(DataStoreModel).where(clause, DataStoreModel.is_default.is_(True))
        )
        keep = str(except_store_id) if except_store_id is not None else None
        for row in rows:
            if row.store_id != keep:
                row.is_default = False
        self._session.flush()

    @staticmethod
    def _scope_clause(*, project_id: UUID | None, group_id: UUID | None):
        if project_id is not None:
            return DataStoreModel.project_id == str(project_id)
        if group_id is not None:
            return DataStoreModel.group_id == str(group_id)
        raise ValueError("A project_id or group_id scope is required.")
