"""DataStore SQLAlchemy repository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import DataStoreModel, ProjectModel
from lab_tracker.models import DataStore
from lab_tracker.repository import (
    DataStoreForeignKeyRaceError,
    DataStoreInsertError,
    DataStoreNameRaceError,
    DataStoreRepository,
)
from lab_tracker.sqlalchemy_mappers import (
    data_store_from_model,
    data_store_to_model,
)

from .common import apply_pagination, count_from_statement

DATA_STORE_PROJECT_NAME_CONSTRAINT = "uq_data_stores_project_name"
DATA_STORE_GROUP_NAME_CONSTRAINT = "uq_data_stores_group_name"
DATA_STORE_PRIMARY_KEY_CONSTRAINT = "data_stores_pkey"
DATA_STORE_FOREIGN_KEY_CONSTRAINTS = frozenset(
    {
        "data_stores_project_id_fkey",
        "fk_data_stores_group_id",
        "data_stores_group_id_fkey",
        "data_stores_created_by_user_id_fkey",
    }
)
_DATA_STORE_NAME_CONSTRAINTS = frozenset(
    {
        DATA_STORE_PROJECT_NAME_CONSTRAINT,
        DATA_STORE_GROUP_NAME_CONSTRAINT,
    }
)
_SQLITE_DATA_STORE_NAME_COLUMNS = frozenset(
    {
        ("data_stores.project_id", "data_stores.name"),
        ("data_stores.group_id", "data_stores.name"),
    }
)
_SQLITE_DATA_STORE_PRIMARY_KEY_COLUMNS = ("data_stores.store_id",)


def _is_data_store_name_race(exc: IntegrityError) -> bool:
    """Identify only the two exact-scope data-store name constraints."""

    diagnostic = getattr(exc.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name is not None:
        return constraint_name in _DATA_STORE_NAME_CONSTRAINTS

    if not isinstance(exc.orig, sqlite3.IntegrityError):
        return False
    prefix = "unique constraint failed:"
    message = str(exc.orig).strip().lower()
    if not message.startswith(prefix):
        return False
    columns = tuple(column.strip() for column in message.removeprefix(prefix).strip().split(","))
    return columns in _SQLITE_DATA_STORE_NAME_COLUMNS


def _is_data_store_primary_key_conflict(exc: IntegrityError) -> bool:
    """Identify the exact data-store primary-key constraint."""

    diagnostic = getattr(exc.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name is not None:
        return constraint_name == DATA_STORE_PRIMARY_KEY_CONSTRAINT

    if not isinstance(exc.orig, sqlite3.IntegrityError):
        return False
    prefix = "unique constraint failed:"
    message = str(exc.orig).strip().lower()
    if not message.startswith(prefix):
        return False
    columns = tuple(column.strip() for column in message.removeprefix(prefix).strip().split(","))
    return columns == _SQLITE_DATA_STORE_PRIMARY_KEY_COLUMNS


def _is_data_store_foreign_key_race(exc: IntegrityError) -> bool:
    """Identify only foreign keys owned by the data-store insert."""

    diagnostic = getattr(exc.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name is not None:
        return constraint_name in DATA_STORE_FOREIGN_KEY_CONSTRAINTS

    return (
        isinstance(exc.orig, sqlite3.IntegrityError)
        and str(exc.orig).strip().lower() == "foreign key constraint failed"
    )


def _reserve_sqlite_registration_write(session: OrmSession) -> None:
    """Reserve SQLite's writer after admission and before any savepoint."""

    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return
    connection = session.connection()
    driver_connection = connection.connection.driver_connection
    if not bool(getattr(driver_connection, "in_transaction", True)):
        connection.exec_driver_sql("BEGIN IMMEDIATE")


@contextmanager
def _insert_savepoint(session: OrmSession) -> Iterator[None]:
    """Keep a failed insert recoverable before inspecting its exact cause."""

    # Service registration reserves the writer before its outer recoverable
    # savepoint. Retain this idempotent fallback for direct repository inserts.
    _reserve_sqlite_registration_write(session)
    with session.begin_nested():
        yield


class SQLAlchemyDataStoreRepository(DataStoreRepository):
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
                select(DataStoreModel).order_by(DataStoreModel.created_at, DataStoreModel.store_id)
            )
        )
        return [data_store_from_model(row) for row in rows]

    def save(self, entity: DataStore) -> None:
        """Accept only an exact no-op re-save of an existing registration."""

        entity_id = str(entity.store_id)
        row = self._session.get(DataStoreModel, entity_id)
        if row is None:
            raise ValueError("Data-store registration must already exist.")
        persisted = data_store_from_model(row)
        if persisted != entity:
            raise ValueError("Data-store registrations are immutable.")

    def reserve_registration_write(self) -> None:
        """Reserve SQLite's writer; other SQL backends rely on constraints."""

        try:
            _reserve_sqlite_registration_write(self._session)
        except SQLAlchemyError:
            raise DataStoreInsertError from None

    def insert(self, entity: DataStore) -> None:
        """Append one store and expose only classified, parameter-free failures."""

        append_only_conflict = False
        name_race = False
        foreign_key_race = False
        insert_failure = False
        try:
            with _insert_savepoint(self._session):
                self._session.add(data_store_to_model(entity))
                self._session.flush()
        except IntegrityError as exc:
            if _is_data_store_primary_key_conflict(exc):
                append_only_conflict = True
            elif _is_data_store_name_race(exc):
                # SQLite may report the scoped-name constraint when one row
                # violates both that key and the primary key. The failed write
                # has already rolled back to the insertion savepoint, so this
                # exact post-write lookup is safe and preserves the distinction.
                try:
                    append_only_conflict = (
                        self._session.get(
                            DataStoreModel,
                            str(entity.store_id),
                        )
                        is not None
                    )
                except SQLAlchemyError:
                    insert_failure = True
                else:
                    name_race = not append_only_conflict
            elif _is_data_store_foreign_key_race(exc):
                foreign_key_race = True
            else:
                insert_failure = True
        except SQLAlchemyError:
            insert_failure = True
        if append_only_conflict:
            raise ValueError("Data-store registrations are append-only.")
        if name_race:
            raise DataStoreNameRaceError(
                "A data-store name was inserted concurrently in this scope."
            )
        if foreign_key_race:
            raise DataStoreForeignKeyRaceError
        if insert_failure:
            raise DataStoreInsertError

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

        update_failure = False
        try:
            self._session.flush()
            clause = self._scope_clause(project_id=project_id, group_id=group_id)
            rows = self._session.scalars(
                select(DataStoreModel).where(
                    clause,
                    DataStoreModel.is_default.is_(True),
                )
            )
            keep = str(except_store_id) if except_store_id is not None else None
            for row in rows:
                if str(row.store_id) != keep:
                    row.is_default = False
            self._session.flush()
        except SQLAlchemyError:
            update_failure = True
        if update_failure:
            raise DataStoreInsertError

    @staticmethod
    def _scope_clause(*, project_id: UUID | None, group_id: UUID | None):
        if project_id is not None:
            return DataStoreModel.project_id == str(project_id)
        if group_id is not None:
            return DataStoreModel.group_id == str(group_id)
        raise ValueError("A project_id or group_id scope is required.")
