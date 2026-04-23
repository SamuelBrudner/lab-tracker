"""Session and acquisition-output SQLAlchemy repositories."""

from __future__ import annotations

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

from .common import SQLAlchemyModelRepository


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
