"""Real-PostgreSQL accounting for artifact-resolution request scopes."""

from __future__ import annotations

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from lab_tracker.artifact_resolution import (
    DEFAULT_MAX_BYTES,
    LocalFilesystemResolver,
    LocalStoreResolutionTarget,
    ResolvedArtifact,
    ResolverRegistry,
)

pytestmark = pytest.mark.postgres


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _BlockingLocalResolver(LocalFilesystemResolver):
    def __init__(
        self,
        *,
        allowed_root: Path,
        expected_uri: str,
        first_connection_released: Event,
        first_session_closed: Event,
        entered: Event,
        release: Event,
    ) -> None:
        super().__init__(allowed_roots=[allowed_root])
        self._allowed_root = allowed_root
        self._expected_uri = expected_uri
        self._first_connection_released = first_connection_released
        self._first_session_closed = first_session_closed
        self._entered = entered
        self._release = release

    def resolve_within_root(
        self,
        target: LocalStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        # The store lookup and materialization must finish before the request
        # releases its database scope and reaches any external resolver.
        assert target.logical_reference.source_system == "store"
        assert target.logical_reference.uri == self._expected_uri
        assert target.store_root == str(self._allowed_root)
        assert target.locator.path == "exp/artifact.bin"
        assert self._first_connection_released.is_set()
        assert self._first_session_closed.is_set()
        self._entered.set()
        if not self._release.wait(timeout=10):
            raise AssertionError("Timed out waiting to release the test resolver.")
        return super().resolve_within_root(
            target,
            max_bytes=max_bytes,
            byte_range=byte_range,
        )


def test_postgres_store_resolution_releases_one_slot_pool_before_external_io(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    data = b"postgres one-slot store artifact"
    artifact = tmp_path / "exp" / "artifact.bin"
    artifact.parent.mkdir()
    artifact.write_bytes(data)

    project_response = postgres_client.post(
        "/projects",
        json={"name": "Postgres one-slot artifact project"},
        headers=postgres_admin_auth_headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["data"]["project_id"]
    store_response = postgres_client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "lab-fs",
            "kind": "local_fs",
            "root": str(tmp_path),
        },
        headers=postgres_admin_auth_headers,
    )
    assert store_response.status_code == 201, store_response.text
    question_response = postgres_client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the request release its PostgreSQL connection?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=postgres_admin_auth_headers,
    )
    assert question_response.status_code == 201, question_response.text
    dataset_response = postgres_client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_response.json()["data"]["question_id"],
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "source_system": "store",
                        "uri": "store://lab-fs/exp/artifact.bin",
                        "content_hash": _sha256(data),
                    }
                ]
            },
        },
        headers=postgres_admin_auth_headers,
    )
    assert dataset_response.status_code == 201, dataset_response.text
    dataset_id = dataset_response.json()["data"]["dataset_id"]

    first_connection_released = Event()
    first_session_closed = Event()
    resolver_entered = Event()
    release_resolver = Event()
    original_registry = postgres_client.app.state.resolver_registry
    postgres_client.app.state.resolver_registry = ResolverRegistry(
        [
            _BlockingLocalResolver(
                allowed_root=tmp_path,
                expected_uri="store://lab-fs/exp/artifact.bin",
                first_connection_released=first_connection_released,
                first_session_closed=first_session_closed,
                entered=resolver_entered,
                release=release_resolver,
            )
        ]
    )

    original_factory = postgres_client.app.state.db_session_factory
    pool_lock = Lock()
    unrelated_connection_acquired = Event()
    checkout_attempts = 0

    class _OneSlotSignalingPool(QueuePool):
        def _do_get(self):
            nonlocal checkout_attempts
            with pool_lock:
                checkout_attempts += 1
                attempt = checkout_attempts
            connection = super()._do_get()
            if attempt == 2:
                assert first_connection_released.is_set()
                unrelated_connection_acquired.set()
            return connection

    bounded_engine = create_engine(
        postgres_client.app.state.settings.database_url,
        future=True,
        pool_pre_ping=True,
        poolclass=_OneSlotSignalingPool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5.0,
    )
    bounded_factory = sessionmaker(
        bind=bounded_engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    connection_checkins = 0

    @event.listens_for(bounded_engine, "checkin")
    def record_connection_release(*_args) -> None:
        nonlocal connection_checkins
        with pool_lock:
            connection_checkins += 1
            checkin = connection_checkins
        if checkin == 1:
            first_connection_released.set()

    factory_calls = 0
    closed_session_indexes: list[int] = []

    def tracking_session_factory():
        nonlocal factory_calls
        with pool_lock:
            factory_calls += 1
            session_index = factory_calls
        session = bounded_factory()
        original_close = session.close

        def tracked_close() -> None:
            try:
                original_close()
            finally:
                with pool_lock:
                    closed_session_indexes.append(session_index)
                if session_index == 1:
                    first_session_closed.set()

        session.close = tracked_close
        return session

    postgres_client.app.state.db_session_factory = tracking_session_factory
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        resolve_future = executor.submit(
            postgres_client.post,
            "/external-artifacts/resolve",
            json={"entity_type": "dataset", "entity_id": dataset_id},
            headers=postgres_admin_auth_headers,
        )
        assert resolver_entered.wait(timeout=10)
        assert first_connection_released.wait(timeout=10)

        follow_up_future = executor.submit(
            postgres_client.get,
            f"/projects/{project_id}",
            headers=postgres_admin_auth_headers,
        )
        follow_up_response = follow_up_future.result(timeout=10)
        assert follow_up_response.status_code == 200, follow_up_response.text
        assert unrelated_connection_acquired.is_set()
        assert resolve_future.done() is False

        release_resolver.set()
        resolve_response = resolve_future.result(timeout=10)
    finally:
        release_resolver.set()
        executor.shutdown(wait=True)
        postgres_client.app.state.db_session_factory = original_factory
        postgres_client.app.state.resolver_registry = original_registry
        bounded_engine.dispose()

    assert resolve_response.status_code == 200, resolve_response.text
    body = resolve_response.json()["data"]
    assert body["status"] == "verified"
    assert body["uri"] == "store://lab-fs/exp/artifact.bin"
    assert base64.b64decode(body["content_base64"]) == data
    assert first_connection_released.is_set()
    assert first_session_closed.is_set()
    assert unrelated_connection_acquired.is_set()
    assert checkout_attempts == 2
    assert connection_checkins == 2
    assert sorted(closed_session_indexes) == [1, 2]
