"""Database configuration for lab tracker."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

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


SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
