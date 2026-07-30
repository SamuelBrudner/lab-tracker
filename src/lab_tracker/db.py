"""Database configuration for lab tracker."""

from __future__ import annotations

import sqlite3

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
        configure_sqlite_engine(engine)
    return engine


def configure_sqlite_engine(engine: Engine) -> None:
    """Attach Lab Tracker's SQLite invariants before the first connection."""

    @event.listens_for(engine, "connect", insert=True)
    def _configure_connection(
        dbapi_connection: sqlite3.Connection,
        _connection_record: object,
    ) -> None:
        configure_sqlite_connection(dbapi_connection)


def configure_sqlite_connection(dbapi_connection: sqlite3.Connection) -> None:
    """Install the transaction and safety invariants used by SQLite sessions.

    Python's sqlite3 driver currently defaults to legacy transaction control,
    where SELECT does not physically begin a transaction. A future Python
    release intends to default to PEP 249 transaction control instead. Pinning
    the legacy mode explicitly keeps authorization reads outside a SQLite
    snapshot so a later, post-authorization ``BEGIN IMMEDIATE`` can reserve the
    writer without risking ``SQLITE_BUSY_SNAPSHOT``.
    """

    legacy_transaction_control = getattr(
        sqlite3,
        "LEGACY_TRANSACTION_CONTROL",
        None,
    )
    if legacy_transaction_control is not None and hasattr(dbapi_connection, "autocommit"):
        dbapi_connection.autocommit = legacy_transaction_control
        # A fresh PEP 249-mode connection already owns a deferred physical
        # transaction. End that initial transaction after switching modes so
        # subsequent authorization SELECTs inherit legacy read behavior.
        if dbapi_connection.in_transaction:
            dbapi_connection.rollback()

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
