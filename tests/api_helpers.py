from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app import create_app
from lab_tracker.db import Base
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

# Registry of test-owned (engine, session) pairs awaiting deterministic
# teardown. Populated by the helpers here (and the two hand-rolled builders in
# the suite) and drained by the autouse fixture in conftest after every test, so
# SQLite connections close on both success and failure instead of leaking as
# GC-time ResourceWarnings. Per-process, so xdist workers stay independent. A
# None session marks an engine-only entry (e.g. an app engine whose lifespan
# shutdown never ran).
_TEST_RESOURCES: list[tuple[Engine, Session | None]] = []


def register_test_resources(engine: Engine, session: Session | None) -> None:
    """Enroll a test-owned engine/session for teardown by drain_test_resources."""
    _TEST_RESOURCES.append((engine, session))


def drain_test_resources() -> None:
    """Close every registered session and dispose its engine.

    Idempotent and defensive: a test that already closed its own session/engine
    is harmless, and one failing close never blocks the rest.
    """
    while _TEST_RESOURCES:
        engine, session = _TEST_RESOURCES.pop()
        try:
            if session is not None:
                session.close()
        finally:
            engine.dispose()


def app_test_client(**client_kwargs) -> TestClient:
    """A TestClient over a fresh app whose DB engine is disposed at teardown.

    ``TestClient(create_app())`` used without a ``with`` block never runs the
    app's lifespan shutdown, so its DB engine is never disposed and leaks a
    connection. This preserves that no-lifespan behavior while enrolling the
    engine for deterministic teardown via the autouse drain fixture.
    """

    app = create_app()
    register_test_resources(app.state.db_engine, None)
    return TestClient(app, **client_kwargs)


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
    # Keep the in-memory SQLite engine/session alive for the lifetime of the API
    # object (some tests read api._test_resources), and enroll them for
    # deterministic teardown after the test.
    api._test_resources = (engine, session)  # type: ignore[attr-defined]
    register_test_resources(engine, session)
    return api
