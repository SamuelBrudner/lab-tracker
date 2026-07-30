from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app import create_app
from lab_tracker.db import Base
from lab_tracker.models import StoreKind
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.store_authority_registry import (
    STORE_AUTHORITY_CONFIG_SCHEMA,
    GroupStoreScope,
    ProjectStoreScope,
    StoreAuthorityRegistry,
    StoreAuthorityRegistryError,
)
from lab_tracker.store_authority_use import FixedStoreAuthoritySnapshotProvider

TEST_STORE_AUTHORITY_GRANT_ID = "test-store-authority"


class ExactCandidateTestStoreAuthority:
    """Test-only authority that grants only the exact candidate presented.

    Existing feature tests exercise store behavior rather than operator
    configuration. They still supply the explicit test grant ID required by
    production, while this adapter builds a real one-candidate registry so the
    registry remains the component that creates the sealed proof.
    """

    def __call__(self) -> ExactCandidateTestStoreAuthority:
        """Provide this test-only dynamic authority at the use-time boundary."""

        return self

    def authorize(
        self,
        *,
        grant_id: str,
        scope: ProjectStoreScope | GroupStoreScope,
        candidate: Any,
        capabilities: Any,
    ):
        if grant_id != TEST_STORE_AUTHORITY_GRANT_ID:
            return None
        scope_payload = (
            {"project_id": str(scope.project_id)}
            if isinstance(scope, ProjectStoreScope)
            else {"group_id": str(scope.group_id)}
        )
        grant: dict[str, object] = {
            "grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
            "scope": scope_payload,
            "kind": candidate.kind.value,
            "root": candidate.root,
            "capabilities": [capability.value for capability in capabilities],
        }
        if candidate.kind in {
            StoreKind.SSH,
            StoreKind.S3,
            StoreKind.GCS,
            StoreKind.AZURE_BLOB,
            StoreKind.DROPBOX,
            StoreKind.GDRIVE,
            StoreKind.BOX,
            StoreKind.ONEDRIVE,
            StoreKind.RCLONE,
        }:
            grant["remote"] = candidate.credential_ref or candidate.name
            grant["credential_mode"] = (
                "credential_ref"
                if candidate.credential_ref is not None
                else "name_fallback"
            )
        raw = json.dumps(
            {
                "schema": STORE_AUTHORITY_CONFIG_SCHEMA,
                "grants": [grant],
            },
            separators=(",", ":"),
        )
        try:
            registry = StoreAuthorityRegistry.from_json(raw)
        except (AttributeError, StoreAuthorityRegistryError):
            return None
        return registry.authorize(
            grant_id=grant_id,
            scope=scope,
            candidate=candidate,
            capabilities=capabilities,
        )


def install_exact_candidate_store_authority(application) -> None:
    """Install the test-only registration authority on an already-built app."""

    authority = ExactCandidateTestStoreAuthority()
    root_api = application.state.lab_tracker_api
    root_api._store_authority_registry = authority
    root_api.data_stores.store_authority_registry = authority
    application.state.store_authority_snapshot_provider = authority


def install_use_time_store_authority(
    application,
    registry: StoreAuthorityRegistry,
) -> None:
    """Replace only the *use-time* provider with a real fixed snapshot.

    Registration keeps the permissive exact-candidate authority so a test can
    still create the store it wants to exercise, while the use-time boundary
    sees exactly ``registry``.  Passing a registry that does not cover a store
    expresses operator revocation or a narrowed boundary -- the case the shared
    fixture's self-authorizing provider cannot represent.
    """

    application.state.store_authority_snapshot_provider = (
        FixedStoreAuthoritySnapshotProvider(registry)
    )


# Registry of test-owned resources awaiting deterministic
# teardown. Populated by the helpers here (and the two hand-rolled builders in
# the suite) and drained by the autouse fixture in conftest after every test, so
# SQLite connections close on both success and failure instead of leaking as
# GC-time ResourceWarnings. Per-process, so xdist workers stay independent.
_TEST_RESOURCES: list[
    tuple[Engine, Session | None, Callable[[], None] | None]
] = []


def register_test_resources(
    engine: Engine,
    session: Session | None,
    cleanup: Callable[[], None] | None = None,
) -> None:
    """Enroll test resources for teardown by :func:`drain_test_resources`."""

    _TEST_RESOURCES.append((engine, session, cleanup))


def drain_test_resources() -> None:
    """Close every registered session and dispose its engine.

    Idempotent and defensive: a test that already closed its own resources is
    harmless, and one failing close never blocks the remaining cleanup.
    """

    failure: BaseException | None = None
    while _TEST_RESOURCES:
        engine, session, cleanup = _TEST_RESOURCES.pop()
        operations: tuple[Callable[[], None] | None, ...] = (
            session.close if session is not None else None,
            engine.dispose,
            cleanup,
        )
        for operation in operations:
            if operation is None:
                continue
            try:
                operation()
            except BaseException as exc:
                if failure is None:
                    failure = exc
    if failure is not None:
        raise failure


def app_test_client(**client_kwargs) -> TestClient:
    """A TestClient over a fresh app whose DB engine is disposed at teardown.

    ``TestClient(create_app())`` used without a ``with`` block never runs the
    app's lifespan shutdown, so its DB engine is never disposed and leaks a
    connection. This preserves that no-lifespan behavior while enrolling the
    engine and app-owned Git health directory for deterministic teardown via the
    autouse drain fixture.
    """

    app = create_app()
    register_test_resources(
        app.state.db_engine,
        None,
        app.state.cleanup_git_health_workdir,
    )
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
