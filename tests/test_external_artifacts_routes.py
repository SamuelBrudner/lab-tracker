import base64
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from http_security_fakes import (
    FakeAddressResolver,
    FakeClock,
    FakeHttpResponse,
    FakeSafeHttpClient,
)
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from starlette.testclient import TestClient

from lab_tracker.artifact_resolution import (
    HttpResolver,
    LocalFilesystemResolver,
    ResolverRegistry,
)
from lab_tracker.outbound_http import OutboundHttpPolicy


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _install_local_registry(client: TestClient, allowed_root: Path) -> None:
    client.app.state.resolver_registry = ResolverRegistry(
        [LocalFilesystemResolver(allowed_roots=[allowed_root])]
    )


def _create_dataset_with_artifact(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    uri: str,
    content_hash: str,
    source_system: str = "local",
) -> str:
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which artifact resolves?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "source_system": source_system,
                        "uri": uri,
                        "content_hash": content_hash,
                    }
                ]
            },
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["dataset_id"]


def test_resolve_endpoint_redacts_denied_http_credentials_and_target(
    client,
    admin_auth_headers,
):
    secret = "hunter2"
    project_id = client.post(
        "/projects",
        json={"name": "Denied HTTP target"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="http",
        uri=f"http://user:{secret}@169.254.169.254/latest?token={secret}",
        content_hash=_sha256(b"x"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["uri"] == "http(s)://[redacted]"
    assert body["content_base64"] is None
    assert secret not in response.text
    assert "169.254.169.254" not in response.text


def test_resolve_endpoint_deadline_discards_partial_body_and_closes_session(
    client,
    admin_auth_headers,
):
    clock = FakeClock()
    stream_entered = threading.Event()
    expire_stream = threading.Event()

    def wait_then_expire(_index: int) -> None:
        stream_entered.set()
        assert expire_stream.wait(timeout=5.0)
        clock.advance(2.0)

    response = FakeHttpResponse(
        chunks=(b"partial secret bytes",),
        on_chunk=wait_then_expire,
    )
    http_client = FakeSafeHttpClient((response,))
    client.app.state.resolver_registry = ResolverRegistry(
        [
            HttpResolver(
                policy=OutboundHttpPolicy(
                    address_resolver=FakeAddressResolver(
                        {"slow.example": ["93.184.216.34"]}
                    )
                ),
                client=http_client,
                deadline_seconds=1.0,
                clock=clock,
            )
        ]
    )
    project_id = client.post(
        "/projects",
        json={"name": "Deadline cleanup project"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        source_system="http",
        uri="https://slow.example/private-result.bin",
        content_hash=_sha256(b"partial secret bytes"),
    )

    original_factory = client.app.state.db_session_factory
    pool_lock = threading.Lock()
    second_checkout_waiting = threading.Event()
    second_connection_acquired = threading.Event()
    first_connection_released = threading.Event()
    checkout_attempts = 0

    class OneSlotSignalingPool(QueuePool):
        def _do_get(self):
            nonlocal checkout_attempts
            with pool_lock:
                checkout_attempts += 1
                attempt = checkout_attempts
            if attempt == 2:
                second_checkout_waiting.set()
            connection = super()._do_get()
            if attempt == 2:
                assert first_connection_released.is_set()
                second_connection_acquired.set()
            return connection

    bounded_engine = create_engine(
        client.app.state.settings.database_url,
        future=True,
        pool_pre_ping=True,
        poolclass=OneSlotSignalingPool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5.0,
        connect_args={"check_same_thread": False},
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

    factory_lock = threading.Lock()
    factory_calls = 0
    closed_session_indexes: list[int] = []

    def tracking_session_factory():
        nonlocal factory_calls
        with factory_lock:
            factory_calls += 1
            session_index = factory_calls
        session = bounded_factory()
        original_close = session.close

        def tracked_close() -> None:
            try:
                original_close()
            finally:
                closed_session_indexes.append(session_index)

        session.close = tracked_close
        return session

    client.app.state.db_session_factory = tracking_session_factory
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        resolve_future = executor.submit(
            client.post,
            "/external-artifacts/resolve",
            json={"entity_type": "dataset", "entity_id": dataset_id},
            headers=admin_auth_headers,
        )
        assert stream_entered.wait(timeout=5.0)

        follow_up_future = executor.submit(
            client.get,
            f"/projects/{project_id}",
            headers=admin_auth_headers,
        )
        assert second_checkout_waiting.wait(timeout=5.0)
        assert follow_up_future.done() is False

        expire_stream.set()
        resolve_response = resolve_future.result(timeout=5.0)
        follow_up = follow_up_future.result(timeout=5.0)
    finally:
        expire_stream.set()
        executor.shutdown(wait=True)
        client.app.state.db_session_factory = original_factory
        bounded_engine.dispose()

    assert resolve_response.status_code == 200, resolve_response.text
    body = resolve_response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["content_base64"] is None
    assert body["returned_bytes"] == 0
    assert "partial secret bytes" not in resolve_response.text
    assert "slow.example" not in resolve_response.text
    assert response.closed is True
    assert first_connection_released.is_set()
    assert second_connection_acquired.is_set()
    assert sorted(closed_session_indexes) == [1, 2]
    assert follow_up.status_code == 200, follow_up.text


def test_dataset_write_rejects_malformed_http_uri_without_server_error(
    client,
    admin_auth_headers,
):
    project_id = client.post(
        "/projects",
        json={"name": "Malformed HTTP target"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can malformed references be stored?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]
    response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "source_system": "http",
                        "uri": "https://[::1",
                        "content_hash": _sha256(b"x"),
                    }
                ]
            },
        },
        headers=admin_auth_headers,
    )

    assert response.status_code == 422, response.text
    assert "well-formed IRI" in response.json()["error"]["message"]


def test_resolve_endpoint_returns_verified_content(client, admin_auth_headers, tmp_path):
    data = b"differential expression matrix"
    artifact = tmp_path / "result.txt"
    artifact.write_bytes(data)
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Resolve project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri=artifact.as_uri(),
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id, "artifact_index": 0},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "verified"
    assert body["observed_hash"] == _sha256(data)
    assert base64.b64decode(body["content_base64"]) == data
    assert body["entity_type"] == "dataset"


def test_resolve_endpoint_reports_drift(client, admin_auth_headers, tmp_path):
    recorded_hash = _sha256(b"what was recorded at capture")
    artifact = tmp_path / "result.txt"
    artifact.write_bytes(b"actual bytes on disk")
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Drift project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri=artifact.as_uri(),
        content_hash=recorded_hash,
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "drifted"
    assert body["uri"] == artifact.as_uri()
    assert body["expected_hash"] == recorded_hash
    assert body["observed_hash"] == _sha256(b"actual bytes on disk")
    assert body["content_type"] == "text/plain"
    assert body["size_bytes"] == len(b"actual bytes on disk")
    assert body["content_base64"] is None
    assert body["returned_bytes"] == 0
    assert body["truncated"] is False
    assert body["detail"] == "Recomputed hash does not match content_hash."
    assert base64.b64encode(b"actual bytes on disk").decode("ascii") not in response.text


def test_resolve_endpoint_missing_index_is_404(client, admin_auth_headers, tmp_path):
    artifact = tmp_path / "result.txt"
    artifact.write_bytes(b"x")
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Index project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri=artifact.as_uri(),
        content_hash=_sha256(b"x"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id, "artifact_index": 5},
        headers=admin_auth_headers,
    )

    assert response.status_code == 404


def test_resolve_endpoint_rejects_unauthorized_caller(
    client, scoped_project_member, admin_auth_headers, tmp_path
):
    data = b"hidden project data"
    artifact = tmp_path / "secret.txt"
    artifact.write_bytes(data)
    _install_local_registry(client, tmp_path)

    # Admin creates a dataset in a project the member cannot read.
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=scoped_project_member.hidden_project_id,
        uri=artifact.as_uri(),
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=scoped_project_member.member_headers,
    )

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "Dataset does not exist.",
            "issues": None,
        }
    }
    assert "content_base64" not in response.text


def _create_store(client, headers, *, project_id, name, kind, root) -> None:
    response = client.post(
        "/data-stores",
        json={"project_id": project_id, "name": name, "kind": kind, "root": root},
        headers=headers,
    )
    assert response.status_code == 201, response.text


def test_resolve_endpoint_resolves_store_locator(client, admin_auth_headers, tmp_path):
    data = b"object addressed relative to a store"
    (tmp_path / "exp").mkdir()
    (tmp_path / "exp" / "x.txt").write_bytes(data)
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Store locator project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="lab-fs",
        kind="local_fs",
        root=str(tmp_path),
    )
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri="store://lab-fs/exp/x.txt",
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "verified"
    assert base64.b64decode(body["content_base64"]) == data
    # The materialized concrete URI is reported.
    assert body["uri"] == (tmp_path / "exp" / "x.txt").as_uri()


def test_resolve_endpoint_uses_store_field_form(client, admin_auth_headers, tmp_path):
    data = b"resolved through the structured store fields"
    (tmp_path / "exp").mkdir()
    (tmp_path / "exp" / "x.txt").write_bytes(data)
    _install_local_registry(client, tmp_path)

    project_id = client.post(
        "/projects", json={"name": "Field form project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="lab-fs",
        kind="local_fs",
        root=str(tmp_path),
    )
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Field form?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]
    # The structured store_name/locator fields drive resolution; the URI names a
    # different (non-existent) store to prove the fields take precedence.
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
            "status": "committed",
            "commit_manifest": {
                "external_artifacts": [
                    {
                        "source_system": "store",
                        "uri": "store://wrong-store/exp/x.txt",
                        "content_hash": _sha256(data),
                        "store_name": "lab-fs",
                        "locator": "exp/x.txt",
                    }
                ]
            },
        },
        headers=admin_auth_headers,
    ).json()["data"]["dataset_id"]

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["status"] == "verified"
    assert base64.b64decode(body["content_base64"]) == data


def test_resolve_endpoint_unknown_store_is_unresolved(client, admin_auth_headers, tmp_path):
    _install_local_registry(client, tmp_path)
    project_id = client.post(
        "/projects", json={"name": "Missing store project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri="store://nonexistent/x.txt",
        content_hash=_sha256(b"x"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert "nonexistent" in (body["detail"] or "")
    assert body["content_base64"] is None
    assert body["returned_bytes"] == 0


def test_resolve_endpoint_denies_local_paths_without_configured_roots(
    client, admin_auth_headers, tmp_path
):
    data = b"unconfigured"
    artifact = tmp_path / "result.txt"
    artifact.write_bytes(data)
    # No resolver_registry installed -> registry_from_env with no allowed roots.

    project_id = client.post(
        "/projects", json={"name": "Default-deny project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri=artifact.as_uri(),
        content_hash=_sha256(data),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "unresolved"
    assert body["content_base64"] is None
    assert body["returned_bytes"] == 0
