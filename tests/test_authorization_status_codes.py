"""HTTP status contract separating authentication from authorization failures.

``401 auth_error`` means the credential itself was missing or rejected, so
clients may refresh it or sign the user out. ``403 forbidden`` means a valid
principal lacks permission, so clients must keep the credential and surface the
denial instead. Targeted reads of inaccessible records stay opaque ``404``.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.auth import Role
from lab_tracker.errors import AuthError, PermissionDeniedError


def _register_user(client: TestClient, role: Role) -> tuple[dict[str, str], str]:
    username = f"{role.value}-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=role,
    )
    login = client.post("/auth/login", json={"username": username, "password": "secret"})
    assert login.status_code == 200
    token = login.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}, str(user.user_id)


def _assert_forbidden(response, message: str) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["error"] == {
        "code": "forbidden",
        "message": message,
        "issues": None,
    }


def test_permission_denied_error_is_an_auth_error_subtype() -> None:
    # Existing ``except AuthError`` call sites (opaque-read conversion, goal
    # visibility checks) must keep catching authorization denials.
    assert issubclass(PermissionDeniedError, AuthError)


def test_project_contributor_denial_is_403_not_credential_rejection(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member: Any,
) -> None:
    editor_headers, editor_user_id = _register_user(client, Role.EDITOR)
    add_viewer = client.post(
        f"/projects/{scoped_project_member.visible_project_id}/members",
        json={"user_id": editor_user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert add_viewer.status_code == 201

    response = client.post(
        "/notes",
        json={
            "project_id": scoped_project_member.visible_project_id,
            "raw_content": "viewer cannot write here",
        },
        headers=editor_headers,
    )

    _assert_forbidden(response, "Project contributor access required.")


def test_project_owner_denial_is_403(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member: Any,
) -> None:
    response = client.post(
        f"/projects/{scoped_project_member.visible_project_id}/members",
        json={"user_id": scoped_project_member.member_user_id, "role": "owner"},
        headers=scoped_project_member.member_headers,
    )

    _assert_forbidden(response, "Project owner access required.")


def test_global_role_denial_is_403(
    client: TestClient,
    viewer_user: Any,
) -> None:
    response = client.post(
        "/projects",
        json={"name": "viewer cannot create projects"},
        headers=viewer_user.headers,
    )

    _assert_forbidden(response, "Insufficient role.")


def test_admin_only_route_denial_is_403(
    client: TestClient,
) -> None:
    editor_headers, _ = _register_user(client, Role.EDITOR)

    response = client.get("/auth/users", headers=editor_headers)

    _assert_forbidden(response, "Admin privileges required.")


def test_inaccessible_targeted_read_stays_opaque_404(
    client: TestClient,
    scoped_project_member: Any,
) -> None:
    response = client.get(
        f"/projects/{scoped_project_member.hidden_project_id}",
        headers=scoped_project_member.member_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_missing_and_invalid_credentials_stay_401(client: TestClient) -> None:
    missing = client.get("/projects")
    invalid = client.get("/projects", headers={"Authorization": "Bearer not-a-token"})

    for response in (missing, invalid):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "auth_error"
