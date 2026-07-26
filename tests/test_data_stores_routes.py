from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from http_security_fakes import FakeAddressResolver, FakeSafeHttpClient
from starlette.testclient import TestClient

from lab_tracker.artifact_resolution import LocalFilesystemResolver, ResolverRegistry
from lab_tracker.bounded_subprocess import ProcessResult
from lab_tracker.http_store_health import HttpStoreHealthProbe
from lab_tracker.outbound_http import OutboundHttpPolicy
from lab_tracker.rclone_remote_policy import RcloneRemotePolicy
from lab_tracker.rclone_store_health import RcloneStoreHealthProbe
from lab_tracker.store_health import (
    HTTP_STORE_HEALTH_FAILURE_DETAIL,
    RCLONE_STORE_HEALTH_FAILURE_DETAIL,
    CachedStoreHealthProbe,
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _install_local_registry(client: TestClient, allowed_root: Path) -> None:
    client.app.state.resolver_registry = ResolverRegistry(
        [LocalFilesystemResolver(allowed_roots=[allowed_root])]
    )


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _create_dataset_with_artifact(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    uri: str,
    content_hash: str,
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
                    {"source_system": "store", "uri": uri, "content_hash": content_hash}
                ]
            },
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["dataset_id"]


def _store_payload(project_id: str, **overrides) -> dict:
    payload = {
        "project_id": project_id,
        "name": "lab-onedrive",
        "kind": "onedrive",
        "root": "/OneDrive/experiments",
        "credential_ref": "onedrive-remote",
        "is_default": True,
    }
    payload.update(overrides)
    return payload


def _create_group(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/groups", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["group_id"]


def _create_project_in_group(
    client: TestClient, headers: dict[str, str], name: str, group_id: str
) -> str:
    response = client.post(
        "/projects", json={"name": name, "group_id": group_id}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _data_store_not_found_body() -> dict[str, object]:
    return {
        "error": {
            "code": "not_found",
            "message": "Data store does not exist.",
            "issues": None,
        }
    }


def test_create_and_get_data_store(client, admin_auth_headers):
    project_id = _create_project(client, admin_auth_headers, "Store project")

    created = client.post(
        "/data-stores", json=_store_payload(project_id), headers=admin_auth_headers
    )
    assert created.status_code == 201, created.text
    body = created.json()["data"]
    assert body["name"] == "lab-onedrive"
    assert body["kind"] == "onedrive"
    assert body["is_default"] is True
    # capabilities defaulted from kind
    assert "bytes_by_path" in body["capabilities"]
    store_id = body["store_id"]

    fetched = client.get(f"/data-stores/{store_id}", headers=admin_auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["data"]["store_id"] == store_id


def test_create_data_store_defaults_capabilities_for_s3(client, admin_auth_headers):
    project_id = _create_project(client, admin_auth_headers, "S3 project")
    payload = _store_payload(
        project_id, name="s3-archive", kind="s3", root="s3://lab-archive", is_default=False
    )

    created = client.post("/data-stores", json=payload, headers=admin_auth_headers)

    assert created.status_code == 201
    caps = created.json()["data"]["capabilities"]
    assert "versioned_snapshot" in caps


def test_list_data_stores_scoped_to_project(client, admin_auth_headers):
    project_id = _create_project(client, admin_auth_headers, "List project")
    client.post(
        "/data-stores", json=_store_payload(project_id), headers=admin_auth_headers
    )
    client.post(
        "/data-stores",
        json=_store_payload(
            project_id, name="s3-archive", kind="s3", root="s3://a", is_default=False
        ),
        headers=admin_auth_headers,
    )

    listed = client.get(
        "/data-stores", params={"project_id": project_id}, headers=admin_auth_headers
    )
    assert listed.status_code == 200
    names = {item["name"] for item in listed.json()["data"]}
    assert names == {"lab-onedrive", "s3-archive"}


def test_duplicate_store_name_in_project_conflicts(client, admin_auth_headers):
    project_id = _create_project(client, admin_auth_headers, "Dup project")
    first = client.post(
        "/data-stores", json=_store_payload(project_id), headers=admin_auth_headers
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/data-stores",
        json=_store_payload(project_id, root="/elsewhere"),
        headers=admin_auth_headers,
    )
    assert duplicate.status_code == 409


def test_group_store_inherited_by_project_listing(client, admin_auth_headers):
    group_id = _create_group(client, admin_auth_headers, "Lab group")
    project_id = _create_project_in_group(client, admin_auth_headers, "Member project", group_id)

    created = client.post(
        "/data-stores",
        json={
            "group_id": group_id,
            "name": "lab-shared-s3",
            "kind": "s3",
            "root": "s3://lab-shared",
        },
        headers=admin_auth_headers,
    )
    assert created.status_code == 201, created.text

    # The project lists the inherited group store as part of its effective set.
    listed = client.get(
        "/data-stores", params={"project_id": project_id}, headers=admin_auth_headers
    )
    assert listed.status_code == 200
    names = {item["name"] for item in listed.json()["data"]}
    assert "lab-shared-s3" in names


def test_group_store_resolves_for_project(client, admin_auth_headers, tmp_path):
    data = b"shared lab object"
    (tmp_path / "exp").mkdir()
    (tmp_path / "exp" / "x.txt").write_bytes(data)
    _install_local_registry(client, tmp_path)

    group_id = _create_group(client, admin_auth_headers, "Resolving lab")
    project_id = _create_project_in_group(client, admin_auth_headers, "Lab project", group_id)
    client.post(
        "/data-stores",
        json={"group_id": group_id, "name": "lab-fs", "kind": "local_fs", "root": str(tmp_path)},
        headers=admin_auth_headers,
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
    # Resolved through the inherited group store.
    assert body["status"] == "verified"
    assert base64.b64decode(body["content_base64"]) == data


def test_create_group_store_requires_group_owner(client, scoped_project_member, admin_auth_headers):
    group_id = _create_group(client, admin_auth_headers, "Owned lab")

    # The scoped member is not a member/owner of this group.
    response = client.post(
        "/data-stores",
        json={"group_id": group_id, "name": "lab-fs", "kind": "s3", "root": "s3://x"},
        headers=scoped_project_member.member_headers,
    )
    assert response.status_code == 401


def test_create_data_store_requires_exactly_one_scope(client, admin_auth_headers):
    project_id = _create_project(client, admin_auth_headers, "Scope project")

    neither = client.post(
        "/data-stores",
        json={"name": "x", "kind": "s3", "root": "s3://x"},
        headers=admin_auth_headers,
    )
    assert neither.status_code == 422

    both = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "group_id": project_id,
            "name": "x",
            "kind": "s3",
            "root": "s3://x",
        },
        headers=admin_auth_headers,
    )
    assert both.status_code == 422


def test_create_local_fs_store_rejects_non_absolute_roots_before_persistence(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "Invalid local roots")
    invalid_roots = (
        "relative/path",
        "../sibling",
        "~/expanded-by-host",
        "C:drive-relative",
        r"\\server\share",
        r"\\?\C:\device-path",
        " /whitespace-repaired-root ",
    )

    for index, root in enumerate(invalid_roots):
        name = f"invalid-local-{index}"
        response = client.post(
            "/data-stores",
            json=_store_payload(
                project_id,
                name=name,
                kind="local_fs",
                root=root,
                is_default=False,
            ),
            headers=admin_auth_headers,
        )

        assert response.status_code == 422, response.text
        assert response.json()["error"]["message"] == (
            "Local filesystem store root must be a supported absolute local path."
        )

    listed = client.get(
        "/data-stores",
        params={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == []


@pytest.mark.parametrize(
    "name",
    (
        "has space",
        "-leading",
        ".leading",
        "_leading",
        "unicode-é",
        "a" * 64,
        " valid",
        "valid ",
    ),
)
def test_create_data_store_rejects_noncanonical_names_without_persistence(
    client,
    admin_auth_headers,
    tmp_path,
    name,
):
    project_id = _create_project(client, admin_auth_headers, f"Invalid store {name!r}")

    response = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name=name,
            kind="local_fs",
            root=str(tmp_path),
            is_default=False,
        ),
        headers=admin_auth_headers,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["message"].startswith(
        "Local filesystem store name must use 1-63 ASCII"
    )
    listed = client.get(
        "/data-stores",
        params={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == []


def test_local_root_validation_does_not_change_other_store_kinds(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "Remote root semantics")

    response = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name=" legacy remote ",
            kind="onedrive",
            root="experiments/current",
            is_default=False,
        ),
        headers=admin_auth_headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["name"] == "legacy remote"
    assert response.json()["data"]["root"] == "experiments/current"


@pytest.mark.skipif(os.name == "nt", reason="Windows aliases trailing root spaces")
def test_local_root_is_persisted_exactly_without_whitespace_repair(
    client,
    admin_auth_headers,
    tmp_path,
):
    root = tmp_path / "store with trailing space "
    root.mkdir()
    exact_data = b"bytes from the exact registered root"
    (root / "artifact.bin").write_bytes(exact_data)
    repaired_root = Path(str(root).rstrip())
    repaired_root.mkdir()
    (repaired_root / "artifact.bin").write_bytes(b"wrong trimmed-root bytes")
    _install_local_registry(client, tmp_path)
    project_id = _create_project(client, admin_auth_headers, "Exact local root")

    response = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name="exact-local-root",
            kind="local_fs",
            root=str(root),
            is_default=False,
        ),
        headers=admin_auth_headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["root"] == str(root)
    dataset_id = _create_dataset_with_artifact(
        client,
        admin_auth_headers,
        project_id=project_id,
        uri="store://exact-local-root/artifact.bin",
        content_hash=_sha256(exact_data),
    )

    resolved = client.post(
        "/external-artifacts/resolve",
        json={"entity_type": "dataset", "entity_id": dataset_id},
        headers=admin_auth_headers,
    )

    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["data"]["status"] == "verified"
    assert base64.b64decode(resolved.json()["data"]["content_base64"]) == exact_data


def test_create_data_store_requires_contributor(client, scoped_project_member):
    response = client.post(
        "/data-stores",
        json=_store_payload(scoped_project_member.visible_project_id),
        headers=scoped_project_member.member_headers,
    )
    # A viewer cannot register a store.
    assert response.status_code == 401


def test_data_store_health_local_fs(client, admin_auth_headers, tmp_path):
    project_id = _create_project(client, admin_auth_headers, "Health project")
    store_id = client.post(
        "/data-stores",
        json=_store_payload(project_id, name="lab-fs", kind="local_fs", root=str(tmp_path)),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    healthy = client.get(f"/data-stores/{store_id}/health", headers=admin_auth_headers)
    assert healthy.status_code == 200, healthy.text
    assert healthy.json()["data"]["status"] == "healthy"


def test_data_store_health_local_fs_missing_root(client, admin_auth_headers, tmp_path):
    project_id = _create_project(client, admin_auth_headers, "Unhealthy project")
    missing = str(tmp_path / "does-not-exist")
    store_id = client.post(
        "/data-stores",
        json=_store_payload(project_id, name="lab-fs", kind="local_fs", root=missing),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    response = client.get(f"/data-stores/{store_id}/health", headers=admin_auth_headers)
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "unreachable"
    assert body["kind"] == "local_fs"


def test_git_store_health_uses_installed_policy_without_leaking_credentials(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "Git health project")
    secret = "git-health-secret-must-not-leak"
    store_id = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name="analysis-repository",
            kind="git",
            root=f"https://operator:{secret}@git.example/lab/repository.git",
            credential_ref=None,
        ),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    workdir = client.app.state.git_health_workdir
    assert workdir.is_dir()
    assert list(workdir.iterdir()) == []
    assert not (workdir / ".git").exists()

    response = client.get(
        f"/data-stores/{store_id}/health",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "unreachable"
    assert secret not in response.text
    assert list(workdir.iterdir()) == []
    assert not (workdir / ".git").exists()


def test_rclone_store_health_route_denies_before_process_and_redacts_target(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "Rclone health project")
    secret = "rclone-health-secret-must-not-leak"
    store_id = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name="remote-archive",
            kind="rclone",
            root="/private/archive",
            credential_ref=secret,
        ),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        def run(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return ProcessResult(
                returncode=0,
                stdout=b"",
                stdout_bytes=0,
                stderr_bytes=0,
            )

    executor = RecordingExecutor()
    client.app.state.store_health_checker = CachedStoreHealthProbe(
        RcloneStoreHealthProbe(
            policy=RcloneRemotePolicy.deny_all(),
            executor=executor,
        )
    )

    response = client.get(
        f"/data-stores/{store_id}/health",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "unreachable"
    assert body["detail"] == RCLONE_STORE_HEALTH_FAILURE_DETAIL
    assert secret not in response.text
    assert executor.calls == []


def test_http_store_health_route_redacts_the_authoritative_selected_target(
    client,
    admin_auth_headers,
):
    project_id = _create_project(client, admin_auth_headers, "HTTP health project")
    secret = "http-health-secret-must-not-leak"
    store_id = client.post(
        "/data-stores",
        json=_store_payload(
            project_id,
            name="remote-http-store",
            kind="http",
            root="https://allowed.example/safe-root",
            endpoint=f"https://operator:{secret}@denied.example/private",
            credential_ref=f"vault:{secret}",
        ),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]
    dns = FakeAddressResolver({"allowed.example": ["93.184.216.34"]})
    http_client = FakeSafeHttpClient(())
    client.app.state.store_health_checker = CachedStoreHealthProbe(
        HttpStoreHealthProbe(
            policy=OutboundHttpPolicy(address_resolver=dns),
            client=http_client,
        )
    )

    response = client.get(
        f"/data-stores/{store_id}/health",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "store_id": store_id,
        "kind": "http",
        "status": "unreachable",
        "detail": HTTP_STORE_HEALTH_FAILURE_DETAIL,
    }
    assert secret not in response.text
    assert "denied.example" not in response.text
    assert "allowed.example" not in response.text
    assert dns.calls == []
    assert http_client.calls == []


def test_data_store_health_denied_for_unauthorized_project(
    client, scoped_project_member, admin_auth_headers, tmp_path
):
    store_id = client.post(
        "/data-stores",
        json=_store_payload(
            scoped_project_member.hidden_project_id,
            name="lab-fs",
            kind="local_fs",
            root=str(tmp_path),
        ),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    response = client.get(
        f"/data-stores/{store_id}/health", headers=scoped_project_member.member_headers
    )
    assert response.status_code == 404
    assert response.json() == _data_store_not_found_body()


def test_get_data_store_denied_for_unauthorized_project(
    client, scoped_project_member, admin_auth_headers
):
    store_id = client.post(
        "/data-stores",
        json=_store_payload(scoped_project_member.hidden_project_id),
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    response = client.get(
        f"/data-stores/{store_id}", headers=scoped_project_member.member_headers
    )
    assert response.status_code == 404
    assert response.json() == _data_store_not_found_body()


def _register_group_member(client, admin_auth_headers, group_id: str) -> dict[str, str]:
    """Register a fresh viewer-role user and add them to the group."""

    from lab_tracker.auth import Role

    username = f"group-member-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username, password="secret", role=Role.VIEWER
    )
    membership = client.post(
        f"/groups/{group_id}/members",
        json={"user_id": str(user.user_id), "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201
    login = client.post(
        "/auth/login", json={"username": username, "password": "secret"}
    )
    token = login.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_group_member_can_read_group_scoped_store(
    client, admin_auth_headers, tmp_path
):
    """A group-scoped store has project_id=None; authorization must check the
    group scope, not deny every non-admin via ensure_project_read(None)."""

    group_id = _create_group(client, admin_auth_headers, "Readable lab")
    store_id = client.post(
        "/data-stores",
        json={
            "group_id": group_id,
            "name": "lab-fs",
            "kind": "local_fs",
            "root": str(tmp_path),
        },
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]
    member_headers = _register_group_member(client, admin_auth_headers, group_id)

    get_response = client.get(f"/data-stores/{store_id}", headers=member_headers)
    health_response = client.get(
        f"/data-stores/{store_id}/health", headers=member_headers
    )

    assert get_response.status_code == 200
    assert get_response.json()["data"]["group_id"] == group_id
    assert health_response.status_code == 200
    assert health_response.json()["data"]["status"] == "healthy"


def test_group_scoped_store_still_denied_for_non_member(
    client, admin_auth_headers, scoped_project_member, tmp_path
):
    group_id = _create_group(client, admin_auth_headers, "Private lab")
    store_id = client.post(
        "/data-stores",
        json={
            "group_id": group_id,
            "name": "lab-fs",
            "kind": "local_fs",
            "root": str(tmp_path),
        },
        headers=admin_auth_headers,
    ).json()["data"]["store_id"]

    response = client.get(
        f"/data-stores/{store_id}", headers=scoped_project_member.member_headers
    )
    health = client.get(
        f"/data-stores/{store_id}/health",
        headers=scoped_project_member.member_headers,
    )
    missing_store_id = uuid4()
    missing = client.get(
        f"/data-stores/{missing_store_id}",
        headers=scoped_project_member.member_headers,
    )
    missing_health = client.get(
        f"/data-stores/{missing_store_id}/health",
        headers=scoped_project_member.member_headers,
    )

    assert {
        response.status_code,
        health.status_code,
        missing.status_code,
        missing_health.status_code,
    } == {404}
    assert (
        response.json()
        == health.json()
        == missing.json()
        == missing_health.json()
        == _data_store_not_found_body()
    )
