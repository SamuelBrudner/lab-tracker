"""Real-PostgreSQL accounting for data-store health request scopes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from lab_tracker.store_health import (
    CachedStoreHealthProbe,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)

pytestmark = pytest.mark.postgres


class _BlockingStoreProbe:
    def __init__(
        self,
        *,
        expected_store_id: str,
        first_connection_released: Event,
        first_session_closed: Event,
        entered: Event,
        release: Event,
    ) -> None:
        self._expected_store_id = expected_store_id
        self._first_connection_released = first_connection_released
        self._first_session_closed = first_session_closed
        self._entered = entered
        self._release = release

    def __call__(self, target: StoreProbeTarget) -> StoreHealth:
        assert isinstance(target, StoreProbeTarget)
        assert str(target.store_id) == self._expected_store_id
        assert target.name == "one-slot-health"
        assert target.root == "/detached/health/root"
        assert self._first_connection_released.is_set()
        assert self._first_session_closed.is_set()
        self._entered.set()
        if not self._release.wait(timeout=10):
            raise AssertionError("Timed out waiting to release the health probe.")
        return StoreHealth(StoreHealthStatus.HEALTHY)


def test_postgres_health_releases_one_slot_pool_before_probe_io(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    project_response = postgres_client.post(
        "/projects",
        json={"name": "Postgres one-slot health project"},
        headers=postgres_admin_auth_headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["data"]["project_id"]
    store_response = postgres_client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "one-slot-health",
            "kind": "local_fs",
            "root": "/detached/health/root",
        },
        headers=postgres_admin_auth_headers,
    )
    assert store_response.status_code == 201, store_response.text
    store_id = store_response.json()["data"]["store_id"]

    first_connection_released = Event()
    first_session_closed = Event()
    probe_entered = Event()
    release_probe = Event()
    original_checker = postgres_client.app.state.store_health_checker
    postgres_client.app.state.store_health_checker = CachedStoreHealthProbe(
        _BlockingStoreProbe(
            expected_store_id=store_id,
            first_connection_released=first_connection_released,
            first_session_closed=first_session_closed,
            entered=probe_entered,
            release=release_probe,
        )
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
        health_future = executor.submit(
            postgres_client.get,
            f"/data-stores/{store_id}/health",
            headers=postgres_admin_auth_headers,
        )
        assert probe_entered.wait(timeout=10)
        assert first_connection_released.wait(timeout=10)
        assert first_session_closed.wait(timeout=10)

        follow_up_future = executor.submit(
            postgres_client.get,
            f"/projects/{project_id}",
            headers=postgres_admin_auth_headers,
        )
        follow_up_response = follow_up_future.result(timeout=10)
        assert follow_up_response.status_code == 200, follow_up_response.text
        assert unrelated_connection_acquired.is_set()
        assert health_future.done() is False

        release_probe.set()
        health_response = health_future.result(timeout=10)
    finally:
        release_probe.set()
        executor.shutdown(wait=True)
        postgres_client.app.state.db_session_factory = original_factory
        postgres_client.app.state.store_health_checker = original_checker
        bounded_engine.dispose()

    assert health_response.status_code == 200, health_response.text
    assert health_response.json()["data"] == {
        "store_id": store_id,
        "kind": "local_fs",
        "status": "healthy",
        "detail": None,
    }
    assert checkout_attempts == 2
    assert connection_checkins == 2
    assert closed_session_indexes == [1, 2]
