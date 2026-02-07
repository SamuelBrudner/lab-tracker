"""FastAPI dependency helpers for persistence wiring."""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def get_db_session(request: Request) -> Session:
    session = getattr(request.state, "db_session", None)
    if session is None:
        raise RuntimeError("Database session is not available on request state.")
    return session


def get_sqlalchemy_repository(
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> SQLAlchemyLabTrackerRepository:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    request.state.lab_tracker_repository = repository
    return repository
