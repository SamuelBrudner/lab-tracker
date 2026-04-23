"""Shared SQLAlchemy repository helpers."""

from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.repository import EntityRepository

EntityT = TypeVar("EntityT")
ModelT = TypeVar("ModelT")


def replace_child_rows(
    session: OrmSession,
    model_type: type[Any],
    column: Any,
    owner_id: str,
    replacement_rows: list[Any],
) -> None:
    session.execute(delete(model_type).where(column == owner_id))
    if replacement_rows:
        session.add_all(replacement_rows)


def count_from_statement(session: OrmSession, statement: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)


def apply_pagination(statement: Any, *, limit: int | None, offset: int) -> Any:
    if offset > 0:
        statement = statement.offset(offset)
    if limit is not None:
        statement = statement.limit(limit)
    return statement


class SQLAlchemyModelRepository(Generic[EntityT, ModelT], EntityRepository[EntityT]):
    def __init__(
        self,
        session: OrmSession,
        *,
        model_type: type[ModelT],
        id_column: Any,
        entity_id_getter: Callable[[EntityT], UUID],
        to_model: Callable[[EntityT], ModelT],
        from_model: Callable[[ModelT], EntityT],
        apply_to_model: Callable[[ModelT, EntityT], None],
    ) -> None:
        self._session = session
        self._model_type = model_type
        self._id_column = id_column
        self._entity_id_getter = entity_id_getter
        self._to_model = to_model
        self._from_model = from_model
        self._apply_to_model = apply_to_model

    def get(self, entity_id: UUID) -> EntityT | None:
        self._session.flush()
        row = self._session.get(self._model_type, str(entity_id))
        if row is None:
            return None
        return self._from_model(row)

    def list(self) -> list[EntityT]:
        self._session.flush()
        rows = list(self._session.scalars(select(self._model_type).order_by(self._id_column)))
        return [self._from_model(row) for row in rows]

    def save(self, entity: EntityT) -> None:
        entity_id = self._entity_id_getter(entity)
        row = self._session.get(self._model_type, str(entity_id))
        if row is None:
            self._session.add(self._to_model(entity))
            return
        self._apply_to_model(row, entity)

    def delete(self, entity_id: UUID) -> EntityT | None:
        row = self._session.get(self._model_type, str(entity_id))
        if row is None:
            return None
        entity = self._from_model(row)
        self._session.delete(row)
        return entity
