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


def test_editor_group_creator_bootstraps_owner_and_manages_members(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    editor_token, editor_user_id = _register_user(
        client,
        f"group-editor-{uuid4().hex[:8]}",
        role="editor",
        headers=admin_auth_headers,
    )
    editor_headers = _auth_headers(editor_token)
    group_response = client.post(
        "/groups",
        json={
            "name": "Navigation lab",
            "description": "PI oversight group",
            "group_read_all": True,
        },
        headers=editor_headers,
    )
    assert group_response.status_code == 201
    group = group_response.json()["data"]

    list_response = client.get("/groups", headers=editor_headers)
    get_response = client.get(f"/groups/{group['group_id']}", headers=editor_headers)
    update_response = client.patch(
        f"/groups/{group['group_id']}",
        json={"name": "Updated navigation lab", "group_read_all": False},
        headers=editor_headers,
    )

    assert list_response.status_code == 200
    assert [item["group_id"] for item in list_response.json()["data"]] == [
        group["group_id"]
    ]
    assert get_response.status_code == 200
    assert update_response.status_code == 200
    assert update_response.json()["data"]["name"] == "Updated navigation lab"
    assert update_response.json()["data"]["group_read_all"] is False

    viewer_name = f"group-viewer-{uuid4().hex[:8]}"
    _, viewer_user_id = _register_user(client, viewer_name)
    create_member = client.post(
        f"/groups/{group['group_id']}/members",
        json={"username": viewer_name, "role": "viewer"},
        headers=editor_headers,
    )
    list_members = client.get(f"/groups/{group['group_id']}/members", headers=editor_headers)
    update_member = client.patch(
        f"/groups/{group['group_id']}/members/{viewer_user_id}",
        json={"role": "contributor"},
        headers=editor_headers,
    )
    delete_member = client.delete(
        f"/groups/{group['group_id']}/members/{viewer_user_id}",
        headers=editor_headers,
    )

    assert create_member.status_code == 201
    assert create_member.json()["data"]["username"] == viewer_name
    assert list_members.status_code == 200
    member_ids = {item["user_id"] for item in list_members.json()["data"]}
    assert {editor_user_id, viewer_user_id}.issubset(member_ids)
    assert update_member.status_code == 200
    assert update_member.json()["data"]["role"] == "contributor"
    assert delete_member.status_code == 200
    assert delete_member.json()["data"]["user_id"] == viewer_user_id


def test_group_member_management_requires_group_owner(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    editor_token, _ = _register_user(
        client,
        f"group-owner-{uuid4().hex[:8]}",
        role="editor",
        headers=admin_auth_headers,
    )
    editor_headers = _auth_headers(editor_token)
    group = client.post(
        "/groups",
        json={"name": "Owner managed group"},
        headers=editor_headers,
    ).json()["data"]
    viewer_name = f"group-member-{uuid4().hex[:8]}"
    viewer_token, viewer_user_id = _register_user(client, viewer_name)
    viewer_headers = _auth_headers(viewer_token)
    add_viewer = client.post(
        f"/groups/{group['group_id']}/members",
        json={"username": viewer_name, "role": "viewer"},
        headers=editor_headers,
    )
    assert add_viewer.status_code == 201

    get_response = client.get(f"/groups/{group['group_id']}", headers=viewer_headers)
    list_members = client.get(f"/groups/{group['group_id']}/members", headers=viewer_headers)
    patch_group = client.patch(
        f"/groups/{group['group_id']}",
        json={"description": "Forbidden edit"},
        headers=viewer_headers,
    )
    patch_member = client.patch(
        f"/groups/{group['group_id']}/members/{viewer_user_id}",
        json={"role": "owner"},
        headers=viewer_headers,
    )

    assert get_response.status_code == 200
    assert list_members.status_code == 401
    assert list_members.json()["error"]["message"] == "Group owner access required."
    assert patch_group.status_code == 401
    assert patch_member.status_code == 401


def test_group_routes_reject_last_owner_removal_and_allow_group_delete(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    editor_token, editor_user_id = _register_user(
        client,
        f"group-last-owner-{uuid4().hex[:8]}",
        role="editor",
        headers=admin_auth_headers,
    )
    editor_headers = _auth_headers(editor_token)
    group = client.post(
        "/groups",
        json={"name": "Last owner group"},
        headers=editor_headers,
    ).json()["data"]

    delete_last_owner = client.delete(
        f"/groups/{group['group_id']}/members/{editor_user_id}",
        headers=editor_headers,
    )
    delete_group = client.delete(f"/groups/{group['group_id']}", headers=editor_headers)
    get_deleted = client.get(f"/groups/{group['group_id']}", headers=admin_auth_headers)

    assert delete_last_owner.status_code == 422
    assert delete_last_owner.json()["error"]["message"] == "Groups must keep at least one owner."
    assert delete_group.status_code == 200
    assert delete_group.json()["data"]["group_id"] == group["group_id"]
    assert get_deleted.status_code == 404
