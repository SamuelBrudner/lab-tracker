"""Database configuration for lab tracker."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from lab_tracker.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def _connect_args(database_url: str) -> dict[str, object]:
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


def get_engine(settings: Settings | None = None) -> Engine:
    resolved = settings or get_settings()
    return create_engine(
        resolved.database_url,
        future=True,
        pool_pre_ping=True,
        connect_args=_connect_args(resolved.database_url),
    )


def get_session_factory(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine or get_engine(settings),
        class_=Session,
        autoflush=False,
        autocommit=False,
        future=True,
    )


SessionLocal = get_session_factory()
