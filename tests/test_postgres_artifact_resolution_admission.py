"""Real-PostgreSQL accounting for artifact-resolution request scopes."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock

import pytest
from api_helpers import TEST_STORE_AUTHORITY_GRANT_ID
from fastapi.testclient import TestClient
from http_security_fakes import (
    FakeAddressResolver,
    FakeHttpResponse,
    FakeSafeHttpClient,
)
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from store_authority_fakes import ExplodingSnapshotProvider

from lab_tracker.artifact_resolution import (
    HttpResolver,
    LocalFilesystemResolver,
    ResolverRegistry,
)
from lab_tracker.outbound_http import OutboundHttpDeadline, OutboundHttpPolicy
from lab_tracker.store_authority_registry import (
    STORE_AUTHORITY_CONFIG_SCHEMA,
    StoreAuthorityRegistry,
)

pytestmark = pytest.mark.postgres

REMOTE_STORE_HOST = "resolve.example.test"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _FailingLocalResolver(LocalFilesystemResolver):
    def resolve_within_root(self, *_args, **_kwargs):
        raise AssertionError("registered-store gate reached filesystem resolution")


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


class _BlockingAddressResolver:
    """Ordering seam that holds a request inside its first remote-I/O step."""

    def __init__(
        self,
        delegate: FakeAddressResolver,
        *,
        entered: Event,
        release: Event,
    ) -> None:
        self._delegate = delegate
        self._entered = entered
        self._release = release

    def resolve(
        self,
        hostname: str,
        port: int,
        *,
        deadline: OutboundHttpDeadline | None = None,
    ):
        self._entered.set()
        assert self._release.wait(timeout=10.0)
        return self._delegate.resolve(hostname, port, deadline=deadline)


class _RecordingResolverRegistry(ResolverRegistry):
    """Resolver-dispatch spy: a denied plan must never reach adapter selection."""

    def __init__(self, resolvers) -> None:
        super().__init__(resolvers)
        self.calls: list[object] = []

    def resolve_prepared(self, target, **kwargs):
        self.calls.append(target)
        return super().resolve_prepared(target, **kwargs)


def _create_project(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
) -> str:
    project_response = client.post("/projects", json={"name": name}, headers=headers)
    assert project_response.status_code == 201, project_response.text
    return project_response.json()["data"]["project_id"]


def _create_dataset_with_store_artifact(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    uri: str,
    content_hash: str,
) -> str:
    question_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the request release its PostgreSQL connection?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )
    assert question_response.status_code == 201, question_response.text
    dataset_response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_response.json()["data"]["question_id"],
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "source_system": "store",
                        "uri": uri,
                        "content_hash": content_hash,
                    }
                ]
            },
        },
        headers=headers,
    )
    assert dataset_response.status_code == 201, dataset_response.text
    return dataset_response.json()["data"]["dataset_id"]


def _create_remote_http_artifact(
    client: TestClient,
    headers: dict[str, str],
    *,
    suffix: str,
    data: bytes,
    capabilities: list[str] | None = None,
) -> tuple[str, dict[str, object], str]:
    """Register a remote HTTP store plus one dataset artifact inside it."""

    project_id = _create_project(
        client,
        headers,
        name=f"Postgres remote resolution {suffix}",
    )
    store_name = f"remote-artifacts-{suffix}"
    store_response = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": store_name,
            "kind": "http",
            "root": f"https://{REMOTE_STORE_HOST}/{suffix}/data/",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
            **({"capabilities": capabilities} if capabilities is not None else {}),
        },
        headers=headers,
    )
    assert store_response.status_code == 201, store_response.text
    dataset_id = _create_dataset_with_store_artifact(
        client,
        headers,
        project_id=project_id,
        uri=f"store://{store_name}/artifact.bin",
        content_hash=_sha256(data),
    )
    return project_id, store_response.json()["data"], dataset_id


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
                        "root": f"https://{REMOTE_STORE_HOST}/",
                        "capabilities": capabilities,
                    }
                ],
            },
            separators=(",", ":"),
        )
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

    project_id = _create_project(
        postgres_client,
        postgres_admin_auth_headers,
        name="Postgres one-slot artifact project",
    )
    store_response = postgres_client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "lab-fs",
            "kind": "local_fs",
            "root": str(tmp_path),
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=postgres_admin_auth_headers,
    )
    assert store_response.status_code == 201, store_response.text
    dataset_id = _create_dataset_with_store_artifact(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        uri="store://lab-fs/exp/artifact.bin",
        content_hash=_sha256(data),
    )

    original_registry = postgres_client.app.state.resolver_registry
    postgres_client.app.state.resolver_registry = ResolverRegistry(
        [_FailingLocalResolver(allowed_roots=[tmp_path])]
    )
    try:
        with _one_slot_request_factory(postgres_client) as pool_state:
            resolve_response = postgres_client.post(
                "/external-artifacts/resolve",
                json={"entity_type": "dataset", "entity_id": dataset_id},
                headers=postgres_admin_auth_headers,
            )
            assert pool_state.first_connection_released.wait(timeout=10)
            assert pool_state.first_session_closed.wait(timeout=10)

            follow_up_response = postgres_client.get(
                f"/projects/{project_id}",
                headers=postgres_admin_auth_headers,
            )
            assert follow_up_response.status_code == 200, follow_up_response.text
            assert pool_state.second_connection_acquired.is_set()
    finally:
        postgres_client.app.state.resolver_registry = original_registry

    assert resolve_response.status_code == 200, resolve_response.text
    body = resolve_response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."
    assert pool_state.checkout_attempts == 2
    assert pool_state.connection_checkins == 2
    assert pool_state.closed_session_indexes == [1, 2]


def test_postgres_store_resolution_releases_one_slot_before_slow_remote_http_io(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    data = b"postgres remote store artifact"
    project_id, _store, dataset_id = _create_remote_http_artifact(
        postgres_client,
        postgres_admin_auth_headers,
        suffix="slow",
        data=data,
    )
    fetch_entered = Event()
    release_fetch = Event()
    address_resolver = FakeAddressResolver({REMOTE_STORE_HOST: ["93.184.216.34"]})
    http_response = FakeHttpResponse(chunks=(data,))
    http_client = FakeSafeHttpClient((http_response,))

    original_registry = postgres_client.app.state.resolver_registry
    postgres_client.app.state.resolver_registry = ResolverRegistry(
        [
            HttpResolver(
                policy=OutboundHttpPolicy(
                    address_resolver=_BlockingAddressResolver(
                        address_resolver,
                        entered=fetch_entered,
                        release=release_fetch,
                    )
                ),
                client=http_client,
            )
        ]
    )
    resolve_client = TestClient(postgres_client.app, raise_server_exceptions=False)
    try:
        with (
            _one_slot_request_factory(postgres_client) as pool_state,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            resolve_future = executor.submit(
                resolve_client.post,
                "/external-artifacts/resolve",
                json={"entity_type": "dataset", "entity_id": dataset_id},
                headers=postgres_admin_auth_headers,
            )
            try:
                assert fetch_entered.wait(timeout=10.0)
                assert pool_state.first_connection_released.is_set()
                assert pool_state.first_session_closed.is_set()

                follow_up_response = postgres_client.get(
                    f"/projects/{project_id}",
                    headers=postgres_admin_auth_headers,
                )
                assert follow_up_response.status_code == 200, follow_up_response.text
                assert pool_state.second_connection_acquired.is_set()
            finally:
                release_fetch.set()
            resolve_response = resolve_future.result(timeout=10.0)
    finally:
        release_fetch.set()
        resolve_client.close()
        postgres_client.app.state.resolver_registry = original_registry

    assert resolve_response.status_code == 200, resolve_response.text
    body = resolve_response.json()["data"]
    assert body["status"] == "verified"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://remote-artifacts-slow/artifact.bin"
    assert body["observed_hash"] == _sha256(data)
    assert base64.b64decode(body["content_base64"]) == data
    assert address_resolver.calls == [(REMOTE_STORE_HOST, 443)]
    assert http_client.calls[0][1].absolute_url == (
        f"https://{REMOTE_STORE_HOST}/slow/data/artifact.bin"
    )
    assert http_response.iterated_chunks == 1
    assert http_response.closed is True
    assert pool_state.checkout_attempts == 2
    assert pool_state.connection_checkins == 2
    assert pool_state.closed_session_indexes == [1, 2]


@pytest.mark.parametrize("snapshot_kind", ["revoked", "mutated"])
def test_postgres_resolution_revalidates_after_release_and_denies_without_remote_io(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    snapshot_kind: str,
) -> None:
    data = b"postgres denied store artifact"
    project_id, store, dataset_id = _create_remote_http_artifact(
        postgres_client,
        postgres_admin_auth_headers,
        suffix=snapshot_kind,
        data=data,
    )
    snapshot = (
        StoreAuthorityRegistry.deny_all()
        if snapshot_kind == "revoked"
        else _changed_authority_snapshot(project_id=project_id, store=store)
    )
    provider_entered = Event()
    release_provider = Event()
    provider_calls = 0
    address_resolver = FakeAddressResolver({REMOTE_STORE_HOST: ["93.184.216.34"]})
    http_client = FakeSafeHttpClient(())

    original_provider = postgres_client.app.state.store_authority_snapshot_provider
    original_registry = postgres_client.app.state.resolver_registry

    pool_state: _OneSlotState

    def blocked_snapshot_provider() -> StoreAuthorityRegistry:
        nonlocal provider_calls
        provider_calls += 1
        assert pool_state.first_connection_released.is_set()
        assert pool_state.first_session_closed.is_set()
        provider_entered.set()
        assert release_provider.wait(timeout=10.0)
        return snapshot

    recording_registry = _RecordingResolverRegistry(
        [
            HttpResolver(
                policy=OutboundHttpPolicy(address_resolver=address_resolver),
                client=http_client,
            )
        ]
    )
    postgres_client.app.state.store_authority_snapshot_provider = (
        blocked_snapshot_provider
    )
    postgres_client.app.state.resolver_registry = recording_registry
    resolve_client = TestClient(postgres_client.app, raise_server_exceptions=False)
    try:
        with (
            _one_slot_request_factory(postgres_client) as pool_state,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            resolve_future = executor.submit(
                resolve_client.post,
                "/external-artifacts/resolve",
                json={"entity_type": "dataset", "entity_id": dataset_id},
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
            resolve_response = resolve_future.result(timeout=10.0)
    finally:
        release_provider.set()
        resolve_client.close()
        postgres_client.app.state.store_authority_snapshot_provider = original_provider
        postgres_client.app.state.resolver_registry = original_registry

    assert resolve_response.status_code == 200, resolve_response.text
    body = resolve_response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."
    assert provider_calls == 1
    assert recording_registry.calls == []
    assert address_resolver.calls == []
    assert http_client.calls == []
    assert pool_state.checkout_attempts == 2
    assert pool_state.connection_checkins == 2
    assert pool_state.closed_session_indexes == [1, 2]


def test_postgres_range_resolution_denies_undeclared_capability_before_capture(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    data = b"postgres undeclared range artifact"
    project_id, _store, dataset_id = _create_remote_http_artifact(
        postgres_client,
        postgres_admin_auth_headers,
        suffix="norange",
        data=data,
        capabilities=["bytes_by_path"],
    )
    address_resolver = FakeAddressResolver({REMOTE_STORE_HOST: ["93.184.216.34"]})
    http_client = FakeSafeHttpClient(())
    recording_registry = _RecordingResolverRegistry(
        [
            HttpResolver(
                policy=OutboundHttpPolicy(address_resolver=address_resolver),
                client=http_client,
            )
        ]
    )

    original_provider = postgres_client.app.state.store_authority_snapshot_provider
    original_registry = postgres_client.app.state.resolver_registry
    postgres_client.app.state.store_authority_snapshot_provider = (
        ExplodingSnapshotProvider()
    )
    postgres_client.app.state.resolver_registry = recording_registry
    try:
        with _one_slot_request_factory(postgres_client) as pool_state:
            resolve_response = postgres_client.post(
                "/external-artifacts/resolve",
                json={
                    "entity_type": "dataset",
                    "entity_id": dataset_id,
                    "byte_start": 0,
                    "byte_end": 3,
                },
                headers=postgres_admin_auth_headers,
            )
            assert pool_state.first_connection_released.wait(timeout=10)
            assert pool_state.first_session_closed.wait(timeout=10)

            follow_up_response = postgres_client.get(
                f"/projects/{project_id}",
                headers=postgres_admin_auth_headers,
            )
            assert follow_up_response.status_code == 200, follow_up_response.text
            assert pool_state.second_connection_acquired.is_set()
    finally:
        postgres_client.app.state.store_authority_snapshot_provider = original_provider
        postgres_client.app.state.resolver_registry = original_registry

    assert resolve_response.status_code == 200, resolve_response.text
    body = resolve_response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["source_system"] == "store"
    assert body["uri"] == "store://[redacted]"
    assert body["content_base64"] is None
    assert body["detail"] == "Store artifact could not be resolved."
    assert recording_registry.calls == []
    assert address_resolver.calls == []
    assert http_client.calls == []
    assert pool_state.checkout_attempts == 2
    assert pool_state.connection_checkins == 2
    assert pool_state.closed_session_indexes == [1, 2]
