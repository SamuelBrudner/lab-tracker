"""FastAPI dependency helpers for persistence wiring."""

from __future__ import annotations

from contextvars import ContextVar

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from lab_tracker.repository import LabTrackerRepository
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

_ACTIVE_REPOSITORY: ContextVar[LabTrackerRepository | None] = ContextVar(
    "lab_tracker_active_repository",
    default=None,
)


def get_db_session(request: Request) -> Session:
    session = getattr(request.state, "db_session", None)
    if session is None:
        raise RuntimeError("Database session is not available on request state.")
    return session


def get_active_repository() -> LabTrackerRepository | None:
    return _ACTIVE_REPOSITORY.get()


def set_active_repository(repository: LabTrackerRepository | None) -> None:
    _ACTIVE_REPOSITORY.set(repository)


def get_sqlalchemy_repository(
    request: Request,
    db_session: Session = Depends(get_db_session),
) -> SQLAlchemyLabTrackerRepository:
    repository = getattr(request.state, "lab_tracker_repository", None)
    if repository is None:
        repository = SQLAlchemyLabTrackerRepository(db_session)
        request.state.lab_tracker_repository = repository
    return repository
