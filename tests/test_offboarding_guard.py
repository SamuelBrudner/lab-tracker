from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.auth import Role


def _login_headers(client: TestClient, username: str, password: str = "secret") -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _register_user(
    client: TestClient,
    *,
    role: Role = Role.ADMIN,
) -> tuple[dict[str, str], str]:
    username = f"offboarding-{role.value}-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=role,
    )
    return _login_headers(client, username), str(user.user_id)


def _create_attributed_question(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    label: str,
) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": f"{label} question",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["question_id"]


def test_project_membership_revoke_requires_export_for_attributed_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    source_headers, source_user_id = _register_user(client)
    project_id = client.post(
        "/projects",
        json={"name": "Direct offboarding"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    member = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": source_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert member.status_code == 201
    _create_attributed_question(
        client,
        source_headers,
        project_id=project_id,
        label="handoff",
    )

    blocked = client.delete(
        f"/projects/{project_id}/members/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert blocked.status_code == 422
    assert "Export or reassign" in blocked.json()["error"]["message"]

    export = client.get(
        f"/record-exports/users/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert export.status_code == 200
    assert export.json()["data"]["export_event_id"]

    removed = client.delete(
        f"/projects/{project_id}/members/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert removed.status_code == 200


def test_group_bulk_offboard_allows_reassignment_before_revoke(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    source_headers, source_user_id = _register_user(client)
    _, successor_user_id = _register_user(client, role=Role.VIEWER)
    group_id = client.post(
        "/groups",
        json={"name": "Bulk offboarding"},
        headers=admin_auth_headers,
    ).json()["data"]["group_id"]
    project_id = client.post(
        "/projects",
        json={"name": "Bulk project", "group_id": group_id},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    onboard = client.post(
        f"/groups/{group_id}/project-memberships",
        json={"user_id": source_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert onboard.status_code == 200
    _create_attributed_question(
        client,
        source_headers,
        project_id=project_id,
        label="bulk",
    )

    blocked = client.delete(
        f"/groups/{group_id}/project-memberships/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert blocked.status_code == 422

    reassignment = client.post(
        "/ownership-reassignments",
        json={
            "from_user_id": source_user_id,
            "to_user_id": successor_user_id,
            "reason": "Offboarding",
        },
        headers=admin_auth_headers,
    )
    assert reassignment.status_code == 201

    removed = client.delete(
        f"/groups/{group_id}/project-memberships/{source_user_id}",
        headers=admin_auth_headers,
    )
    assert removed.status_code == 200
    assert [item["user_id"] for item in removed.json()["data"]] == [source_user_id]
