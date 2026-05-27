from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from lab_tracker.api import LabTrackerAPI
from lab_tracker.db import Base
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def repository_backed_api(
    *,
    raw_storage: LocalNoteStorage | None = None,
) -> LabTrackerAPI:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    session = session_factory()
    api = LabTrackerAPI(
        raw_storage=raw_storage,
        repository=SQLAlchemyLabTrackerRepository(session),
    )
    # Keep the in-memory SQLite engine/session alive for the lifetime of the API object.
    api._test_resources = (engine, session)  # type: ignore[attr-defined]
    return api
