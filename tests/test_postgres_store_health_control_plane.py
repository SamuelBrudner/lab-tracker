"""Real-PostgreSQL accounting for remote store-health request scopes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event, Lock

import pytest
from api_helpers import TEST_STORE_AUTHORITY_GRANT_ID
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from lab_tracker.store_authority_registry import (
    STORE_AUTHORITY_CONFIG_SCHEMA,
    StoreAuthorityRegistry,
)
from lab_tracker.store_health import (
    STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    CachedStoreHealthProbe,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)

pytestmark = pytest.mark.postgres


@dataclass(slots=True)
class _OneSlotState:
    first_connection_released: Event = field(default_factory=Event)
    first_session_closed: Event = field(default_factory=Event)
    second_connection_acquired: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    checkout_attempts: int = 0
    connection_checkins: int = 0
    factory_calls: int = 0
    closed_session_indexes: list[int] = field(default_factory=list)


@contextmanager
def _one_slot_request_factory(
    client: TestClient,
) -> Iterator[_OneSlotState]:
    """Replace request sessions with one real PostgreSQL pool slot."""

    state = _OneSlotState()
    original_factory = client.app.state.db_session_factory

    class _OneSlotSignalingPool(QueuePool):
        def _do_get(self):
            with state.lock:
                state.checkout_attempts += 1
                attempt = state.checkout_attempts
            connection = super()._do_get()
            if attempt == 2:
                assert state.first_connection_released.is_set()
                state.second_connection_acquired.set()
            return connection

    bounded_engine = create_engine(
        client.app.state.settings.database_url,
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

    @event.listens_for(bounded_engine, "checkin")
    def record_connection_release(*_args: object) -> None:
        with state.lock:
            state.connection_checkins += 1
            checkin = state.connection_checkins
        if checkin == 1:
            state.first_connection_released.set()

    def tracking_session_factory():
        with state.lock:
            state.factory_calls += 1
            session_index = state.factory_calls
        session = bounded_factory()
        original_close = session.close

        def tracked_close() -> None:
            try:
                original_close()
            finally:
                with state.lock:
                    state.closed_session_indexes.append(session_index)
                if session_index == 1:
                    state.first_session_closed.set()

        session.close = tracked_close
        return session

    client.app.state.db_session_factory = tracking_session_factory
    try:
        yield state
    finally:
        client.app.state.db_session_factory = original_factory
        bounded_engine.dispose()


def _create_remote_http_store(
    client: TestClient,
    headers: dict[str, str],
    *,
    suffix: str,
) -> tuple[str, str, dict[str, object]]:
    project_response = client.post(
        "/projects",
        json={"name": f"Postgres remote health {suffix}"},
        headers=headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["data"]["project_id"]
    store_response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": f"remote-health-{suffix}",
            "kind": "http",
            "root": f"https://health.example.test/{suffix}/data/",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=headers,
    )
    assert store_response.status_code == 201, store_response.text
    return project_id, store_response.json()["data"]["store_id"], store_response.json()[
        "data"
    ]


def _changed_authority_snapshot(
    *,
    project_id: str,
    store: dict[str, object],
) -> StoreAuthorityRegistry:
    """Build a containing grant whose changed boundary has a new fingerprint."""

    capabilities = store["capabilities"]
    assert isinstance(capabilities, list)
    return StoreAuthorityRegistry.from_json(
        json.dumps(
            {
                "schema": STORE_AUTHORITY_CONFIG_SCHEMA,
                "grants": [
                    {
                        "grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
                        "scope": {"project_id": project_id},
                        "kind": "http",
                        "root": "https://health.example.test/",
                        "capabilities": capabilities,
                    }
                ],
            },
            separators=(",", ":"),
        )
    )


def test_postgres_health_releases_one_slot_before_slow_remote_probe(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    project_id, store_id, _store = _create_remote_http_store(
        postgres_client,
        postgres_admin_auth_headers,
        suffix="slow",
    )
    probe_entered = Event()
    release_probe = Event()
    targets: list[StoreProbeTarget] = []

    original_checker = postgres_client.app.state.store_health_checker

    def slow_remote_probe(target: StoreProbeTarget) -> StoreHealth:
        targets.append(target)
        probe_entered.set()
        assert release_probe.wait(timeout=10.0)
        return StoreHealth(StoreHealthStatus.HEALTHY, "remote probe completed")

    postgres_client.app.state.store_health_checker = CachedStoreHealthProbe(
        slow_remote_probe
    )
    health_client = TestClient(postgres_client.app, raise_server_exceptions=False)
    try:
        with (
            _one_slot_request_factory(postgres_client) as pool_state,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            health_future = executor.submit(
                health_client.get,
                f"/data-stores/{store_id}/health",
                headers=postgres_admin_auth_headers,
            )
            try:
                assert probe_entered.wait(timeout=10.0)
                assert pool_state.first_connection_released.is_set()
                assert pool_state.first_session_closed.is_set()

                follow_up_response = postgres_client.get(
                    f"/projects/{project_id}",
                    headers=postgres_admin_auth_headers,
                )
                assert follow_up_response.status_code == 200, follow_up_response.text
                assert pool_state.second_connection_acquired.is_set()
            finally:
                release_probe.set()
            health_response = health_future.result(timeout=10.0)
    finally:
        health_client.close()
        postgres_client.app.state.store_health_checker = original_checker

    assert health_response.status_code == 200, health_response.text
    assert health_response.json()["data"] == {
        "store_id": store_id,
        "kind": "http",
        "status": "healthy",
        "detail": "remote probe completed",
    }
    assert len(targets) == 1
    assert targets[0].kind.value == "http"
    assert targets[0].authority_binding_identity is not None
    assert pool_state.checkout_attempts == 2
    assert pool_state.connection_checkins == 2
    assert pool_state.closed_session_indexes == [1, 2]


@pytest.mark.parametrize("snapshot_kind", ["revoked", "mutated"])
def test_postgres_health_revalidates_after_release_and_denies_without_probe(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    snapshot_kind: str,
) -> None:
    project_id, store_id, store = _create_remote_http_store(
        postgres_client,
        postgres_admin_auth_headers,
        suffix=snapshot_kind,
    )
    snapshot = (
        StoreAuthorityRegistry.deny_all()
        if snapshot_kind == "revoked"
        else _changed_authority_snapshot(project_id=project_id, store=store)
    )
    provider_entered = Event()
    release_provider = Event()
    provider_calls = 0
    checker_calls: list[StoreProbeTarget] = []

    original_provider = (
        postgres_client.app.state.store_authority_snapshot_provider
    )
    original_checker = postgres_client.app.state.store_health_checker

    pool_state: _OneSlotState

    def blocked_snapshot_provider() -> StoreAuthorityRegistry:
        nonlocal provider_calls
        provider_calls += 1
        assert pool_state.first_connection_released.is_set()
        assert pool_state.first_session_closed.is_set()
        provider_entered.set()
        assert release_provider.wait(timeout=10.0)
        return snapshot

    def forbidden_remote_probe(target: StoreProbeTarget) -> StoreHealth:
        checker_calls.append(target)
        raise AssertionError("revoked authority reached remote health I/O")

    postgres_client.app.state.store_authority_snapshot_provider = (
        blocked_snapshot_provider
    )
    postgres_client.app.state.store_health_checker = forbidden_remote_probe
    health_client = TestClient(postgres_client.app, raise_server_exceptions=False)
    try:
        with (
            _one_slot_request_factory(postgres_client) as pool_state,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            health_future = executor.submit(
                health_client.get,
                f"/data-stores/{store_id}/health",
                headers=postgres_admin_auth_headers,
            )
            try:
                assert provider_entered.wait(timeout=10.0)
                follow_up_response = postgres_client.get(
                    f"/projects/{project_id}",
                    headers=postgres_admin_auth_headers,
                )
                assert follow_up_response.status_code == 200, follow_up_response.text
                assert pool_state.second_connection_acquired.is_set()
            finally:
                release_provider.set()
            health_response = health_future.result(timeout=10.0)
    finally:
        release_provider.set()
        health_client.close()
        postgres_client.app.state.store_authority_snapshot_provider = original_provider
        postgres_client.app.state.store_health_checker = original_checker

    assert health_response.status_code == 200, health_response.text
    assert health_response.json()["data"] == {
        "store_id": store_id,
        "kind": "http",
        "status": "unsupported",
        "detail": STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    }
    assert provider_calls == 1
    assert checker_calls == []
    assert pool_state.checkout_attempts == 2
    assert pool_state.connection_checkins == 2
    assert pool_state.closed_session_indexes == [1, 2]
