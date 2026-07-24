import base64
import hashlib
from pathlib import Path

from starlette.testclient import TestClient

from lab_tracker.artifact_resolution import LocalFilesystemResolver, ResolverRegistry


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
        content_hash=_sha256(b"what was recorded at capture"),
    )

    response = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "drifted"


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
