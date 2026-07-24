from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from lab_tracker.auth import utc_now
from lab_tracker.db_models import ProjectMembershipModel, UsageEventModel
from lab_tracker.sqlalchemy_repository_parts.core import (
    SQLAlchemyProjectMembershipRepository,
)


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


def _create_project(
    client: TestClient,
    headers: dict[str, str],
    name: str,
    *,
    group_id: str | None = None,
) -> str:
    payload = {"name": name}
    if group_id is not None:
        payload["group_id"] = group_id
    response = client.post("/projects", json=payload, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


@dataclass(frozen=True, slots=True)
class GroupReadScope:
    group_id: str
    member_headers: dict[str, str]
    outsider_headers: dict[str, str]


@pytest.fixture
def group_read_scope(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> GroupReadScope:
    group_response = client.post(
        "/groups",
        json={"name": f"Opaque group {uuid4().hex[:8]}"},
        headers=admin_auth_headers,
    )
    assert group_response.status_code == 201, group_response.text
    group_id = group_response.json()["data"]["group_id"]

    member_name = f"opaque-group-member-{uuid4().hex[:8]}"
    member_token, _ = _register_user(client, member_name)
    outsider_token, _ = _register_user(
        client,
        f"opaque-group-outsider-{uuid4().hex[:8]}",
    )
    membership_response = client.post(
        f"/groups/{group_id}/members",
        json={"username": member_name, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert membership_response.status_code == 201, membership_response.text

    return GroupReadScope(
        group_id=group_id,
        member_headers=_auth_headers(member_token),
        outsider_headers=_auth_headers(outsider_token),
    )


def test_direct_group_read_is_opaque_to_outsiders_and_visible_to_members(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    group_read_scope: GroupReadScope,
) -> None:
    existing = client.get(
        f"/groups/{group_read_scope.group_id}",
        headers=group_read_scope.outsider_headers,
    )
    missing = client.get(
        f"/groups/{uuid4()}",
        headers=group_read_scope.outsider_headers,
    )

    assert existing.status_code == missing.status_code == 404
    assert existing.json() == missing.json() == {
        "error": {
            "code": "not_found",
            "message": "Project group does not exist.",
            "issues": None,
        }
    }

    for headers in (group_read_scope.member_headers, admin_auth_headers):
        response = client.get(f"/groups/{group_read_scope.group_id}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["group_id"] == group_read_scope.group_id


def test_direct_group_read_preserves_auth_capability_and_route_validation(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    group_read_scope: GroupReadScope,
) -> None:
    missing_auth = client.get(f"/groups/{group_read_scope.group_id}")
    invalid_auth = client.get(
        f"/groups/{group_read_scope.group_id}",
        headers={"Authorization": "Bearer invalid-token"},
    )
    issued = client.post(
        "/auth/tokens",
        json={
            "label": "Opaque group capability test",
            "role": "admin",
            "read_only": False,
            "scope": "batch_run_due",
            "expires_at": (utc_now() + timedelta(days=1)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert issued.status_code == 201, issued.text
    scoped = client.get(
        f"/groups/{group_read_scope.group_id}",
        headers={
            "Authorization": f"Bearer {issued.json()['data']['secret']}",
        },
    )
    malformed = client.get("/groups/not-a-uuid", headers=admin_auth_headers)

    assert missing_auth.status_code == 401
    assert invalid_auth.status_code == 401
    assert scoped.status_code == 403
    assert scoped.json()["error"] == {
        "code": "service_forbidden",
        "message": "Not permitted for this token.",
        "issues": None,
    }
    assert malformed.status_code == 404


def test_group_read_opacity_does_not_change_group_mutation_authorization(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    group_read_scope: GroupReadScope,
) -> None:
    member_list = client.get(
        f"/groups/{group_read_scope.group_id}/members",
        headers=group_read_scope.member_headers,
    )
    member_patch = client.patch(
        f"/groups/{group_read_scope.group_id}",
        json={"description": "Forbidden edit"},
        headers=group_read_scope.member_headers,
    )
    member_delete = client.delete(
        f"/groups/{group_read_scope.group_id}",
        headers=group_read_scope.member_headers,
    )

    assert member_list.status_code == 401
    assert member_patch.status_code == 401
    assert member_delete.status_code == 401

    after_denial = client.get(
        f"/groups/{group_read_scope.group_id}",
        headers=admin_auth_headers,
    )
    assert after_denial.status_code == 200
    assert after_denial.json()["data"]["description"] == ""

    admin_list = client.get(
        f"/groups/{group_read_scope.group_id}/members",
        headers=admin_auth_headers,
    )
    admin_patch = client.patch(
        f"/groups/{group_read_scope.group_id}",
        json={"description": "Authorized edit"},
        headers=admin_auth_headers,
    )
    admin_delete = client.delete(
        f"/groups/{group_read_scope.group_id}",
        headers=admin_auth_headers,
    )

    assert admin_list.status_code == 200
    assert admin_patch.status_code == 200
    assert admin_patch.json()["data"]["description"] == "Authorized edit"
    assert admin_delete.status_code == 200


def test_denied_and_missing_group_reads_do_not_emit_usage_events(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    group_read_scope: GroupReadScope,
) -> None:
    client.app.state.settings.usage_events = True

    denied = client.get(
        f"/groups/{group_read_scope.group_id}",
        headers=group_read_scope.outsider_headers,
    )
    missing = client.get(
        f"/groups/{uuid4()}",
        headers=group_read_scope.outsider_headers,
    )
    assert denied.status_code == missing.status_code == 404

    with client.app.state.db_session_factory() as session:
        usage_events = session.scalars(select(UsageEventModel)).all()
    assert usage_events == []

    authorized = client.get(
        f"/groups/{group_read_scope.group_id}",
        headers=admin_auth_headers,
    )
    assert authorized.status_code == 200
    with client.app.state.db_session_factory() as session:
        usage_events = session.scalars(select(UsageEventModel)).all()
    assert len(usage_events) == 1
    assert usage_events[0].verb == "view"
    assert usage_events[0].resource_type == "project_group"
    assert str(usage_events[0].resource_id) == group_read_scope.group_id


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


def test_group_owner_bulk_onboards_and_offboards_project_memberships(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    editor_token, _ = _register_user(
        client,
        f"group-bulk-owner-{uuid4().hex[:8]}",
        role="editor",
        headers=admin_auth_headers,
    )
    editor_headers = _auth_headers(editor_token)
    group = client.post(
        "/groups",
        json={"name": "Bulk membership group"},
        headers=editor_headers,
    ).json()["data"]
    project_ids = {
        _create_project(
            client,
            admin_auth_headers,
            "Bulk grouped project A",
            group_id=group["group_id"],
        ),
        _create_project(
            client,
            admin_auth_headers,
            "Bulk grouped project B",
            group_id=group["group_id"],
        ),
    }
    viewer_name = f"group-bulk-member-{uuid4().hex[:8]}"
    viewer_token, viewer_user_id = _register_user(client, viewer_name)
    viewer_headers = _auth_headers(viewer_token)

    onboard = client.post(
        f"/groups/{group['group_id']}/project-memberships",
        json={"username": viewer_name, "role": "contributor"},
        headers=editor_headers,
    )
    visible_after_onboard = client.get("/projects", headers=viewer_headers)
    offboard = client.delete(
        f"/groups/{group['group_id']}/project-memberships/{viewer_user_id}",
        headers=editor_headers,
    )
    visible_after_offboard = client.get("/projects", headers=viewer_headers)

    assert onboard.status_code == 200
    onboarded = onboard.json()["data"]
    assert {item["project_id"] for item in onboarded} == project_ids
    assert {item["role"] for item in onboarded} == {"contributor"}
    assert {item["project_id"] for item in visible_after_onboard.json()["data"]} == project_ids
    assert offboard.status_code == 200
    assert {item["project_id"] for item in offboard.json()["data"]} == project_ids
    assert visible_after_offboard.json()["data"] == []


def test_group_bulk_offboard_rejects_sole_project_owner_removal(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    editor_token, _ = _register_user(
        client,
        f"group-bulk-guard-owner-{uuid4().hex[:8]}",
        role="editor",
        headers=admin_auth_headers,
    )
    editor_headers = _auth_headers(editor_token)
    group = client.post(
        "/groups",
        json={"name": "Bulk guard group"},
        headers=editor_headers,
    ).json()["data"]
    project_id = _create_project(
        client,
        admin_auth_headers,
        "Bulk guarded project",
        group_id=group["group_id"],
    )
    viewer_name = f"group-bulk-sole-owner-{uuid4().hex[:8]}"
    viewer_token, viewer_user_id = _register_user(client, viewer_name)
    viewer_headers = _auth_headers(viewer_token)
    onboard = client.post(
        f"/groups/{group['group_id']}/project-memberships",
        json={"username": viewer_name, "role": "owner"},
        headers=editor_headers,
    )
    assert onboard.status_code == 200

    offboard = client.delete(
        f"/groups/{group['group_id']}/project-memberships/{viewer_user_id}",
        headers=editor_headers,
    )
    still_visible = client.get("/projects", headers=viewer_headers)

    assert offboard.status_code == 422
    assert offboard.json()["error"]["message"].startswith(
        "Cannot remove the last owner from projects:"
    )
    assert [item["project_id"] for item in still_visible.json()["data"]] == [project_id]


def test_group_bulk_offboard_rechecks_owner_count_after_lock(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
):
    editor_token, editor_user_id = _register_user(
        client,
        f"group-bulk-race-owner-{uuid4().hex[:8]}",
        role="editor",
        headers=admin_auth_headers,
    )
    editor_headers = _auth_headers(editor_token)
    group = client.post(
        "/groups",
        json={"name": "Bulk offboard race group"},
        headers=editor_headers,
    ).json()["data"]
    project_id = _create_project(
        client,
        editor_headers,
        "Bulk offboard race project",
        group_id=group["group_id"],
    )
    target_name = f"group-bulk-race-target-{uuid4().hex[:8]}"
    _, target_user_id = _register_user(client, target_name)
    onboard = client.post(
        f"/groups/{group['group_id']}/project-memberships",
        json={"username": target_name, "role": "owner"},
        headers=editor_headers,
    )
    assert onboard.status_code == 200

    original_lock = SQLAlchemyProjectMembershipRepository.lock_project_owners

    def remove_other_owner_after_lock(
        self: SQLAlchemyProjectMembershipRepository,
        locked_project_id,
    ) -> None:
        original_lock(self, locked_project_id)
        if str(locked_project_id) != project_id:
            return
        self._session.execute(
            delete(ProjectMembershipModel).where(
                ProjectMembershipModel.project_id == project_id,
                ProjectMembershipModel.user_id == editor_user_id,
            )
        )
        self._session.flush()

    monkeypatch.setattr(
        SQLAlchemyProjectMembershipRepository,
        "lock_project_owners",
        remove_other_owner_after_lock,
    )

    offboard = client.delete(
        f"/groups/{group['group_id']}/project-memberships/{target_user_id}",
        headers=editor_headers,
    )
    members = client.get(f"/projects/{project_id}/members", headers=editor_headers)

    assert offboard.status_code == 422
    assert offboard.json()["error"]["message"].startswith(
        "Cannot remove the last owner from projects:"
    )
    owner_user_ids = {
        item["user_id"] for item in members.json()["data"] if item["role"] == "owner"
    }
    assert {editor_user_id, target_user_id}.issubset(owner_user_ids)


def test_group_bulk_upsert_rejects_sole_project_owner_demotion(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    editor_token, _ = _register_user(
        client,
        f"group-bulk-demote-owner-{uuid4().hex[:8]}",
        role="editor",
        headers=admin_auth_headers,
    )
    editor_headers = _auth_headers(editor_token)
    group = client.post(
        "/groups",
        json={"name": "Bulk demotion guard group"},
        headers=editor_headers,
    ).json()["data"]
    project_id = _create_project(
        client,
        admin_auth_headers,
        "Bulk demotion guarded project",
        group_id=group["group_id"],
    )
    viewer_name = f"group-bulk-demotee-{uuid4().hex[:8]}"
    _, viewer_user_id = _register_user(client, viewer_name)
    onboard = client.post(
        f"/groups/{group['group_id']}/project-memberships",
        json={"username": viewer_name, "role": "owner"},
        headers=editor_headers,
    )
    assert onboard.status_code == 200

    demote = client.post(
        f"/groups/{group['group_id']}/project-memberships",
        json={"username": viewer_name, "role": "viewer"},
        headers=editor_headers,
    )
    members = client.get(f"/projects/{project_id}/members", headers=editor_headers)

    assert demote.status_code == 422
    assert demote.json()["error"]["message"].startswith(
        "Cannot demote the last owner from projects:"
    )
    matching_members = [
        item for item in members.json()["data"] if item["user_id"] == viewer_user_id
    ]
    assert [item["role"] for item in matching_members] == ["owner"]
