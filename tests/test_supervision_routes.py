from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_user(
    client: TestClient,
    username: str,
    *,
    role: str = "viewer",
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    response = client.post(
        "/auth/register",
        json={"username": username, "password": "secret", "role": role},
        headers=headers or {},
    )
    assert response.status_code == 201
    data = response.json()["data"]
    return data["access_token"], data["user"]["user_id"]


def test_admin_manages_supervision_edges(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    _, supervisor_user_id = _register_user(
        client,
        f"supervisor-{uuid4().hex[:8]}",
    )
    _, supervisee_user_id = _register_user(
        client,
        f"supervisee-{uuid4().hex[:8]}",
    )
    started_at = "2026-01-01T00:00:00+00:00"
    ended_at = "2026-02-01T00:00:00+00:00"

    create_response = client.post(
        "/supervision-edges",
        json={
            "supervisor_user_id": supervisor_user_id,
            "supervisee_user_id": supervisee_user_id,
            "started_at": started_at,
        },
        headers=admin_auth_headers,
    )
    assert create_response.status_code == 201
    edge = create_response.json()["data"]

    duplicate_response = client.post(
        "/supervision-edges",
        json={
            "supervisor_user_id": supervisor_user_id,
            "supervisee_user_id": supervisee_user_id,
            "started_at": "2026-01-15T00:00:00+00:00",
        },
        headers=admin_auth_headers,
    )
    active_response = client.get(
        "/supervision-edges",
        params={"supervisee_user_id": supervisee_user_id, "active_only": True},
        headers=admin_auth_headers,
    )
    close_response = client.patch(
        f"/supervision-edges/{edge['edge_id']}",
        json={"ended_at": ended_at},
        headers=admin_auth_headers,
    )
    successor_response = client.post(
        "/supervision-edges",
        json={
            "supervisor_user_id": supervisor_user_id,
            "supervisee_user_id": supervisee_user_id,
            "started_at": ended_at,
        },
        headers=admin_auth_headers,
    )
    as_of_response = client.get(
        "/supervision-edges",
        params={
            "supervisee_user_id": supervisee_user_id,
            "as_of": "2026-01-15T00:00:00+00:00",
        },
        headers=admin_auth_headers,
    )
    successor_edge_id = successor_response.json()["data"]["edge_id"]
    delete_response = client.delete(
        f"/supervision-edges/{successor_edge_id}",
        headers=admin_auth_headers,
    )
    deleted_get_response = client.get(
        f"/supervision-edges/{successor_edge_id}",
        headers=admin_auth_headers,
    )

    assert duplicate_response.status_code == 409
    assert active_response.status_code == 200
    assert [item["edge_id"] for item in active_response.json()["data"]] == [edge["edge_id"]]
    assert close_response.status_code == 200
    assert close_response.json()["data"]["ended_at"] == "2026-02-01T00:00:00Z"
    assert successor_response.status_code == 201
    assert as_of_response.status_code == 200
    assert [item["edge_id"] for item in as_of_response.json()["data"]] == [edge["edge_id"]]
    assert delete_response.status_code == 200
    assert deleted_get_response.status_code == 404


def test_supervision_edge_management_requires_write_role(
    client: TestClient,
):
    viewer_token, supervisor_user_id = _register_user(
        client,
        f"viewer-supervisor-{uuid4().hex[:8]}",
    )
    _, supervisee_user_id = _register_user(
        client,
        f"viewer-supervisee-{uuid4().hex[:8]}",
    )

    response = client.post(
        "/supervision-edges",
        json={
            "supervisor_user_id": supervisor_user_id,
            "supervisee_user_id": supervisee_user_id,
            "started_at": "2026-01-01T00:00:00+00:00",
        },
        headers=_auth_headers(viewer_token),
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Insufficient role."
