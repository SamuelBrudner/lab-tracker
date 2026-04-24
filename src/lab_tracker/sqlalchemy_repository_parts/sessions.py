"""Session and acquisition-output SQLAlchemy repositories."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import AcquisitionOutputModel, SessionModel
from lab_tracker.models import AcquisitionOutput, Session
from lab_tracker.sqlalchemy_mappers import (
    acquisition_output_from_model,
    acquisition_output_to_model,
    apply_acquisition_output_to_model,
    apply_session_to_model,
    session_from_model,
    session_to_model,
)

from .common import SQLAlchemyModelRepository, apply_pagination, count_from_statement


class SQLAlchemySessionRepository(SQLAlchemyModelRepository[Session, SessionModel]):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=SessionModel,
            id_column=SessionModel.session_id,
            entity_id_getter=lambda entity: entity.session_id,
            to_model=session_to_model,
            from_model=session_from_model,
            apply_to_model=apply_session_to_model,
        )

    def query(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Session], int]:
        self._session.flush()
        stmt = select(SessionModel)
        count_stmt = select(SessionModel.session_id)
        if project_id is not None:
            stmt = stmt.where(SessionModel.project_id == str(project_id))
            count_stmt = count_stmt.where(SessionModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(SessionModel.status == status)
            count_stmt = count_stmt.where(SessionModel.status == status)
        if session_type is not None:
            stmt = stmt.where(SessionModel.session_type == session_type)
            count_stmt = count_stmt.where(SessionModel.session_type == session_type)
        stmt = stmt.order_by(SessionModel.started_at, SessionModel.session_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [session_from_model(row) for row in rows], total


class SQLAlchemyAcquisitionOutputRepository(
    SQLAlchemyModelRepository[AcquisitionOutput, AcquisitionOutputModel]
):
    def __init__(self, session: OrmSession) -> None:
        super().__init__(
            session,
            model_type=AcquisitionOutputModel,
            id_column=AcquisitionOutputModel.created_at,
            entity_id_getter=lambda entity: entity.output_id,
            to_model=acquisition_output_to_model,
            from_model=acquisition_output_from_model,
            apply_to_model=apply_acquisition_output_to_model,
        )

    def query(
        self,
        *,
        session_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[AcquisitionOutput], int]:
        self._session.flush()
        stmt = select(AcquisitionOutputModel)
        count_stmt = select(AcquisitionOutputModel.output_id)
        if session_id is not None:
            stmt = stmt.where(AcquisitionOutputModel.session_id == str(session_id))
            count_stmt = count_stmt.where(AcquisitionOutputModel.session_id == str(session_id))
        stmt = stmt.order_by(AcquisitionOutputModel.created_at, AcquisitionOutputModel.output_id)
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [acquisition_output_from_model(row) for row in rows], total
