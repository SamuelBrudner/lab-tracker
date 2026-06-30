"""HTTP integration tests for lpat_ personal access tokens."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.auth import Role, utc_now


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _create_token(
    client: TestClient,
    headers: dict[str, str],
    *,
    label: str = "Copilot",
    role: str = "viewer",
    read_only: bool = True,
) -> dict:
    response = client.post(
        "/auth/tokens",
        json={
            "label": label,
            "role": role,
            "read_only": read_only,
            "expires_at": (utc_now() + timedelta(days=7)).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_create_list_and_revoke_personal_access_token(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    issued = _create_token(client, admin_auth_headers, role="admin")

    assert issued["secret"].startswith("lpat_")
    assert issued["role"] == "admin"
    assert issued["read_only"] is True

    listed = client.get("/auth/tokens", headers=admin_auth_headers)
    assert listed.status_code == 200, listed.text
    listed_token = listed.json()["data"][0]
    assert listed_token["token_id"] == issued["token_id"]
    assert "secret" not in listed_token

    revoked = client.delete(
        f"/auth/tokens/{issued['token_id']}",
        headers=admin_auth_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"]["revoked_at"] is not None


def test_read_only_personal_access_token_can_read_but_not_write_or_auth(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_response = client.post(
        "/projects",
        json={"name": "PAT Read"},
        headers=admin_auth_headers,
    )
    assert project_response.status_code == 201, project_response.text
    issued = _create_token(client, admin_auth_headers, role="admin", read_only=True)
    pat_headers = _bearer(issued["secret"])

    listing = client.get("/projects", headers=pat_headers)
    decision_context = client.post(
        "/assistant/decision-context",
        json={
            "task_kind": "plot",
            "query": "plot choice",
            "project_id": project_response.json()["data"]["project_id"],
        },
        headers=pat_headers,
    )
    forbidden_auth = client.get("/auth/me", headers=pat_headers)
    write_attempts = [
        client.post("/projects", json={"name": "Blocked"}, headers=pat_headers),
        client.post(
            "/datasets",
            json={
                "project_id": str(uuid4()),
                "commit_hash": "abc123",
                "primary_question_id": str(uuid4()),
            },
            headers=pat_headers,
        ),
        client.post(
            "/analyses",
            json={
                "dataset_id": str(uuid4()),
                "analysis_type": "notebook",
                "summary": "blocked",
            },
            headers=pat_headers,
        ),
        client.post(
            "/claims",
            json={
                "analysis_id": str(uuid4()),
                "text": "blocked",
                "status": "proposed",
            },
            headers=pat_headers,
        ),
        client.post(
            "/visualizations",
            json={
                "analysis_id": str(uuid4()),
                "viz_type": "figure",
                "file_path": "blocked.png",
            },
            headers=pat_headers,
        ),
    ]

    assert listing.status_code == 200, listing.text
    assert decision_context.status_code == 200, decision_context.text
    assert decision_context.json()["data"]["task_kind"] == "plot"
    assert [response.status_code for response in write_attempts] == [403] * len(
        write_attempts
    )
    assert {response.json()["error"]["code"] for response in write_attempts} == {
        "service_forbidden"
    }
    assert forbidden_auth.status_code == 403


def test_write_enabled_token_uses_capped_role_not_live_user_role(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    username = f"viewer-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=Role.VIEWER,
    )
    login = client.post(
        "/auth/login",
        json={"username": username, "password": "secret"},
    )
    assert login.status_code == 200, login.text
    viewer_headers = _bearer(login.json()["data"]["access_token"])
    issued = _create_token(
        client,
        viewer_headers,
        role="admin",
        read_only=False,
    )
    assert issued["role"] == "viewer"

    client.app.state.auth_service.update_user(user.user_id, role=Role.ADMIN)
    create_with_capped_token = client.post(
        "/projects",
        json={"name": "Still blocked"},
        headers=_bearer(issued["secret"]),
    )
    create_with_admin = client.post(
        "/projects",
        json={"name": "Promoted admin can write"},
        headers=admin_auth_headers,
    )

    assert create_with_capped_token.status_code == 403
    assert create_with_admin.status_code == 201


def test_write_enabled_editor_token_can_write(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    issued = _create_token(
        client,
        admin_auth_headers,
        role="editor",
        read_only=False,
    )

    response = client.post(
        "/projects",
        json={"name": "From editor token"},
        headers=_bearer(issued["secret"]),
    )

    assert response.status_code == 201, response.text


def test_invalid_personal_access_token_attempts_are_rate_limited(
    client: TestClient,
):
    client.app.state.pat_rate_limiter.max_attempts = 2
    headers = _bearer("lpat_missing")

    first = client.get("/projects", headers=headers)
    second = client.get("/projects", headers=headers)
    third = client.get("/projects", headers=headers)

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429
