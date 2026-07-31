"""Experiment SQLAlchemy repository and membership queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    DatasetModel,
    ExperimentDatasetModel,
    ExperimentModel,
    ExperimentSessionModel,
    SessionModel,
)
from lab_tracker.models import Dataset, Experiment, Session
from lab_tracker.sqlalchemy_mappers import (
    apply_experiment_to_model,
    experiment_from_model,
    experiment_to_model,
    session_from_model,
)

from .common import (
    SQLAlchemyModelRepository,
    apply_pagination,
    count_from_statement,
    substring_pattern,
    uuid_values,
)
from .datasets import SQLAlchemyDatasetRepository


def _insert_membership_ignoring_conflict(
    session: OrmSession,
    *,
    model: type[ExperimentSessionModel] | type[ExperimentDatasetModel],
    values: dict[str, object],
    index_elements: tuple[str, str],
) -> bool | None:
    """Atomically create a membership on supported production databases."""

    dialect_name = session.get_bind().dialect.name
    statement: Any
    if dialect_name == "sqlite":
        statement = sqlite_insert(model)
    elif dialect_name == "postgresql":
        statement = postgresql_insert(model)
    else:
        return None
    result = cast(
        CursorResult[Any],
        session.execute(
            statement.values(**values).on_conflict_do_nothing(
                index_elements=index_elements,
            )
        ),
    )
    session.flush()
    return int(result.rowcount or 0) > 0


class SQLAlchemyExperimentRepository(SQLAlchemyModelRepository[Experiment, ExperimentModel]):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=ExperimentModel,
            id_column=ExperimentModel.experiment_id,
            entity_id_getter=lambda entity: entity.experiment_id,
            to_model=experiment_to_model,
            from_model=experiment_from_model,
            apply_to_model=apply_experiment_to_model,
        )

    def query(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        primary_question_id: UUID | None = None,
        status: str | None = None,
        search: str | None = None,
        session_id: UUID | None = None,
        dataset_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Experiment], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        stmt = select(ExperimentModel)
        count_stmt = select(ExperimentModel.experiment_id)
        if session_id is not None:
            stmt = stmt.join(
                ExperimentSessionModel,
                ExperimentSessionModel.experiment_id == ExperimentModel.experiment_id,
            ).where(ExperimentSessionModel.session_id == str(session_id))
            count_stmt = count_stmt.join(
                ExperimentSessionModel,
                ExperimentSessionModel.experiment_id == ExperimentModel.experiment_id,
            ).where(ExperimentSessionModel.session_id == str(session_id))
        if dataset_id is not None:
            stmt = stmt.join(
                ExperimentDatasetModel,
                ExperimentDatasetModel.experiment_id == ExperimentModel.experiment_id,
            ).where(ExperimentDatasetModel.dataset_id == str(dataset_id))
            count_stmt = count_stmt.join(
                ExperimentDatasetModel,
                ExperimentDatasetModel.experiment_id == ExperimentModel.experiment_id,
            ).where(ExperimentDatasetModel.dataset_id == str(dataset_id))
        if project_id is not None:
            stmt = stmt.where(ExperimentModel.project_id == str(project_id))
            count_stmt = count_stmt.where(ExperimentModel.project_id == str(project_id))
        if project_ids is not None:
            project_values = uuid_values(project_ids)
            stmt = stmt.where(ExperimentModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(ExperimentModel.project_id.in_(project_values))
        if primary_question_id is not None:
            stmt = stmt.where(ExperimentModel.primary_question_id == str(primary_question_id))
            count_stmt = count_stmt.where(
                ExperimentModel.primary_question_id == str(primary_question_id)
            )
        if status is not None:
            stmt = stmt.where(ExperimentModel.status == status)
            count_stmt = count_stmt.where(ExperimentModel.status == status)
        pattern = substring_pattern(search)
        if pattern is not None:
            search_condition = or_(
                ExperimentModel.name.ilike(pattern, escape="\\"),
                ExperimentModel.description.ilike(pattern, escape="\\"),
            )
            stmt = stmt.where(search_condition)
            count_stmt = count_stmt.where(search_condition)
        if recent_first:
            stmt = stmt.order_by(
                ExperimentModel.created_at.desc(),
                ExperimentModel.experiment_id.desc(),
            )
        else:
            stmt = stmt.order_by(
                ExperimentModel.created_at,
                ExperimentModel.experiment_id,
            )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [experiment_from_model(row) for row in rows], total

    def has_session(self, experiment_id: UUID, session_id: UUID) -> bool:
        self._session.flush()
        return (
            self._session.get(
                ExperimentSessionModel,
                (str(experiment_id), str(session_id)),
            )
            is not None
        )

    def add_session(
        self,
        *,
        experiment_id: UUID,
        session_id: UUID,
        created_by: str | None,
        created_by_user_id: UUID | None,
        created_at: datetime,
    ) -> bool:
        values: dict[str, object] = {
            "experiment_id": experiment_id,
            "session_id": session_id,
            "created_by": created_by,
            "created_by_user_id": created_by_user_id,
            "created_at": created_at,
        }
        inserted = _insert_membership_ignoring_conflict(
            self._session,
            model=ExperimentSessionModel,
            values=values,
            index_elements=("experiment_id", "session_id"),
        )
        if inserted is not None:
            return inserted
        if self.has_session(experiment_id, session_id):
            return False
        self._session.add(ExperimentSessionModel(**values))
        self._session.flush()
        return True

    def remove_session(
        self,
        experiment_id: UUID,
        session_id: UUID,
    ) -> bool:
        self._session.flush()
        row = self._session.get(
            ExperimentSessionModel,
            (str(experiment_id), str(session_id)),
        )
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def has_dataset(self, experiment_id: UUID, dataset_id: UUID) -> bool:
        self._session.flush()
        return (
            self._session.get(
                ExperimentDatasetModel,
                (str(experiment_id), str(dataset_id)),
            )
            is not None
        )

    def add_dataset(
        self,
        *,
        experiment_id: UUID,
        dataset_id: UUID,
        created_by: str | None,
        created_by_user_id: UUID | None,
        created_at: datetime,
    ) -> bool:
        values: dict[str, object] = {
            "experiment_id": experiment_id,
            "dataset_id": dataset_id,
            "created_by": created_by,
            "created_by_user_id": created_by_user_id,
            "created_at": created_at,
        }
        inserted = _insert_membership_ignoring_conflict(
            self._session,
            model=ExperimentDatasetModel,
            values=values,
            index_elements=("experiment_id", "dataset_id"),
        )
        if inserted is not None:
            return inserted
        if self.has_dataset(experiment_id, dataset_id):
            return False
        self._session.add(ExperimentDatasetModel(**values))
        self._session.flush()
        return True

    def remove_dataset(
        self,
        experiment_id: UUID,
        dataset_id: UUID,
    ) -> bool:
        self._session.flush()
        row = self._session.get(
            ExperimentDatasetModel,
            (str(experiment_id), str(dataset_id)),
        )
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True

    def query_sessions(
        self,
        *,
        experiment_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Session], int]:
        self._session.flush()
        stmt = (
            select(SessionModel)
            .join(
                ExperimentSessionModel,
                ExperimentSessionModel.session_id == SessionModel.session_id,
            )
            .where(ExperimentSessionModel.experiment_id == str(experiment_id))
            .order_by(SessionModel.started_at, SessionModel.session_id)
        )
        count_stmt = (
            select(SessionModel.session_id)
            .join(
                ExperimentSessionModel,
                ExperimentSessionModel.session_id == SessionModel.session_id,
            )
            .where(ExperimentSessionModel.experiment_id == str(experiment_id))
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [session_from_model(row) for row in rows], total

    def query_datasets(
        self,
        *,
        experiment_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Dataset], int]:
        self._session.flush()
        stmt = (
            select(DatasetModel)
            .join(
                ExperimentDatasetModel,
                ExperimentDatasetModel.dataset_id == DatasetModel.dataset_id,
            )
            .where(ExperimentDatasetModel.experiment_id == str(experiment_id))
            .order_by(DatasetModel.created_at, DatasetModel.dataset_id)
        )
        count_stmt = (
            select(DatasetModel.dataset_id)
            .join(
                ExperimentDatasetModel,
                ExperimentDatasetModel.dataset_id == DatasetModel.dataset_id,
            )
            .where(ExperimentDatasetModel.experiment_id == str(experiment_id))
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return SQLAlchemyDatasetRepository(self._session).datasets_from_rows(rows), total
