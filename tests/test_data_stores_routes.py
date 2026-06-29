from __future__ import annotations

from starlette.testclient import TestClient


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


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


def test_create_data_store_requires_contributor(client, scoped_project_member):
    response = client.post(
        "/data-stores",
        json=_store_payload(scoped_project_member.visible_project_id),
        headers=scoped_project_member.member_headers,
    )
    # A viewer cannot register a store.
    assert response.status_code == 401


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
    assert response.status_code == 401
