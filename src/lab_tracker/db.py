"""Database configuration for lab tracker."""

from __future__ import annotations

from sqlalchemy import create_engine, event
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
    engine = create_engine(
        resolved.database_url,
        future=True,
        pool_pre_ping=True,
        connect_args=_connect_args(resolved.database_url),
    )
    if resolved.database_url.startswith("sqlite"):
        _configure_sqlite_engine(engine)
    return engine


def _configure_sqlite_engine(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


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
