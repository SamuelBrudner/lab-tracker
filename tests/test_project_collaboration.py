from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from lab_tracker.auth import Role


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_user(client: TestClient, username: str, password: str = "secret") -> str:
    token, _ = _register_user_with_id(client, username, password=password)
    return token


def _register_user_with_id(
    client: TestClient,
    username: str,
    password: str = "secret",
) -> tuple[str, str]:
    response = client.post("/auth/register", json={"username": username, "password": password})
    assert response.status_code == 201
    data = response.json()["data"]
    return data["access_token"], data["user"]["user_id"]


def _register_user_with_role(
    client: TestClient,
    username: str,
    role: str,
    password: str = "secret",
) -> tuple[str, str]:
    user = client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role(role),
    )
    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login_response.status_code == 200
    return login_response.json()["data"]["access_token"], str(user.user_id)


def _create_project(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


class FakeDraftClient:
    model = "fake-gpt"

    def __init__(self, patch: dict[str, Any]) -> None:
        self.patch = patch

    def draft_from_note(self, **_: Any) -> dict[str, Any]:
        return self.patch

    def close(self) -> None:
        return None


def _draft_patch(project_id: str) -> dict[str, Any]:
    return {
        "summary": "Add temporal odor coding follow-up note.",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "op": "create",
                "entity_type": "note",
                "semantic_type": "create_note",
                "target_entity_id": None,
                "client_ref": "new_note",
                "payload_json": (
                    '{"project_id":"'
                    + project_id
                    + '","raw_content":"Follow up temporal odor coding analysis."}'
                ),
                "rationale": "The source note asks for a follow-up analysis record.",
                "confidence": 0.9,
                "source_refs": [],
            }
        ],
    }


def test_project_membership_scopes_reads_and_contributor_notes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    undergrad_headers = _auth_headers(_register_user(client, "undergrad"))
    temporal_project = _create_project(client, admin_auth_headers, "Temporal odor coding")
    other_project = _create_project(client, admin_auth_headers, "Other project")

    add_member = client.post(
        f"/projects/{temporal_project}/members",
        json={"username": "undergrad", "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert add_member.status_code == 201
    assert add_member.json()["data"]["role"] == "contributor"

    list_projects = client.get("/projects", headers=undergrad_headers)
    assert list_projects.status_code == 200
    assert [project["project_id"] for project in list_projects.json()["data"]] == [
        temporal_project
    ]

    denied_project = client.get(f"/projects/{other_project}", headers=undergrad_headers)
    missing_project = client.get(f"/projects/{uuid4()}", headers=undergrad_headers)
    assert denied_project.status_code == missing_project.status_code == 404
    assert denied_project.json() == missing_project.json() == {
        "error": {
            "code": "not_found",
            "message": "Project does not exist.",
            "issues": None,
        }
    }

    create_note = client.post(
        "/notes",
        json={
            "project_id": temporal_project,
            "raw_content": "Odor timing evidence from today's analysis.",
        },
        headers=undergrad_headers,
    )
    assert create_note.status_code == 201
    assert create_note.json()["data"]["created_by"]

    denied_note = client.post(
        "/notes",
        json={"project_id": other_project, "raw_content": "Should not land."},
        headers=undergrad_headers,
    )
    assert denied_note.status_code == 401

    search = client.get(
        "/search",
        params={"q": "odor timing"},
        headers=undergrad_headers,
    )
    assert search.status_code == 200
    assert len(search.json()["data"]["notes"]) == 1

    context = client.post(
        "/assistant/decision-context",
        json={"task_kind": "summary", "query": "odor timing"},
        headers=undergrad_headers,
    )
    assert context.status_code == 200
    assert context.json()["data"]["scope"]["project"]["project_id"] == temporal_project


def test_project_member_patch_updates_existing_member_without_creating_new_one(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    member_token, member_user_id = _register_user_with_id(client, "membership-patch-target")
    member_headers = _auth_headers(member_token)
    project_id = _create_project(client, admin_auth_headers, "Membership patch")

    create_member = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": member_user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert create_member.status_code == 201

    denied_note = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Viewer cannot write yet."},
        headers=member_headers,
    )
    assert denied_note.status_code == 401

    update_member = client.patch(
        f"/projects/{project_id}/members/{member_user_id}",
        json={"role": "contributor"},
        headers=admin_auth_headers,
    )

    assert update_member.status_code == 200
    assert update_member.json()["data"]["role"] == "contributor"
    allowed_note = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Contributor can write now."},
        headers=member_headers,
    )
    assert allowed_note.status_code == 201
    list_members = client.get(f"/projects/{project_id}/members", headers=admin_auth_headers)
    matching_members = [
        item for item in list_members.json()["data"] if item["user_id"] == member_user_id
    ]
    assert [item["role"] for item in matching_members] == ["contributor"]


def test_project_member_patch_rejects_nonmembers_and_unknown_users(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    _, nonmember_user_id = _register_user_with_id(client, "membership-patch-nonmember")
    unknown_user_id = str(uuid4())
    project_id = _create_project(client, admin_auth_headers, "Membership patch rejects")

    nonmember_update = client.patch(
        f"/projects/{project_id}/members/{nonmember_user_id}",
        json={"role": "contributor"},
        headers=admin_auth_headers,
    )
    unknown_user_update = client.patch(
        f"/projects/{project_id}/members/{unknown_user_id}",
        json={"role": "viewer"},
        headers=admin_auth_headers,
    )

    assert nonmember_update.status_code == 404
    assert nonmember_update.json()["error"]["message"] == "Project membership does not exist."
    assert unknown_user_update.status_code == 404
    assert unknown_user_update.json()["error"]["message"] == "User does not exist."
    list_members = client.get(f"/projects/{project_id}/members", headers=admin_auth_headers)
    assert list_members.status_code == 200
    assert list_members.json()["data"] == []


def test_project_member_create_checks_owner_before_resolving_user(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    nonowner_token, _ = _register_user_with_id(client, "member-create-nonowner")
    _register_user_with_id(client, "member-create-target")
    nonowner_headers = _auth_headers(nonowner_token)
    project_id = _create_project(client, admin_auth_headers, "Membership create ordering")

    missing_user = client.post(
        f"/projects/{project_id}/members",
        json={"username": "missing-member-create-target", "role": "viewer"},
        headers=nonowner_headers,
    )
    existing_user = client.post(
        f"/projects/{project_id}/members",
        json={"username": "member-create-target", "role": "viewer"},
        headers=nonowner_headers,
    )

    assert missing_user.status_code == 401
    assert existing_user.status_code == 401
    assert missing_user.json()["error"]["message"] == "Project owner access required."
    assert existing_user.json()["error"]["message"] == "Project owner access required."


def test_project_member_delete_revokes_project_access(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    member_token, member_user_id = _register_user_with_id(client, "membership-delete-target")
    member_headers = _auth_headers(member_token)
    project_id = _create_project(client, admin_auth_headers, "Membership delete")
    create_member = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": member_user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert create_member.status_code == 201
    visible_before = client.get(f"/projects/{project_id}", headers=member_headers)
    assert visible_before.status_code == 200

    delete_member = client.delete(
        f"/projects/{project_id}/members/{member_user_id}",
        headers=admin_auth_headers,
    )

    assert delete_member.status_code == 200
    assert delete_member.json()["data"]["user_id"] == member_user_id
    hidden_after = client.get(f"/projects/{project_id}", headers=member_headers)
    missing_project = client.get(f"/projects/{uuid4()}", headers=member_headers)
    assert hidden_after.status_code == missing_project.status_code == 404
    assert hidden_after.json() == missing_project.json()
    list_members = client.get(f"/projects/{project_id}/members", headers=admin_auth_headers)
    assert member_user_id not in {item["user_id"] for item in list_members.json()["data"]}


def test_project_member_patch_and_delete_keep_at_least_one_owner(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    owner_token, owner_user_id = _register_user_with_role(
        client,
        "membership-sole-owner",
        "editor",
    )
    owner_headers = _auth_headers(owner_token)
    project_id = _create_project(client, owner_headers, "Membership sole owner")

    demote_owner = client.patch(
        f"/projects/{project_id}/members/{owner_user_id}",
        json={"role": "viewer"},
        headers=owner_headers,
    )
    delete_owner = client.delete(
        f"/projects/{project_id}/members/{owner_user_id}",
        headers=owner_headers,
    )

    assert demote_owner.status_code == 422
    assert demote_owner.json()["error"]["message"] == "Projects must keep at least one owner."
    assert delete_owner.status_code == 422
    assert delete_owner.json()["error"]["message"] == "Projects must keep at least one owner."
    list_members = client.get(f"/projects/{project_id}/members", headers=owner_headers)
    assert list_members.status_code == 200
    assert [
        item["role"] for item in list_members.json()["data"] if item["user_id"] == owner_user_id
    ] == ["owner"]


def test_project_contributor_can_use_core_write_routes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    contributor_headers = _auth_headers(_register_user(client, "core-contributor"))
    viewer_headers = _auth_headers(_register_user(client, "core-viewer"))
    project_id = _create_project(client, admin_auth_headers, "Contributor write surface")

    add_contributor = client.post(
        f"/projects/{project_id}/members",
        json={"username": "core-contributor", "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert add_contributor.status_code == 201
    add_viewer = client.post(
        f"/projects/{project_id}/members",
        json={"username": "core-viewer", "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert add_viewer.status_code == 201

    denied_question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can viewers mutate project records?",
            "question_type": "descriptive",
        },
        headers=viewer_headers,
    )
    assert denied_question.status_code == 401

    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can project contributors write retained records?",
            "question_type": "descriptive",
        },
        headers=contributor_headers,
    )
    assert question.status_code == 201
    question_id = question.json()["data"]["question_id"]

    session = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=contributor_headers,
    )
    assert session.status_code == 201

    dataset = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=contributor_headers,
    )
    assert dataset.status_code == 201
    dataset_id = dataset.json()["data"]["dataset_id"]

    file_upload = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("capture.bin", b"capture-bytes", "application/octet-stream")},
        headers=contributor_headers,
    )
    assert file_upload.status_code == 201

    analysis = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "v1",
        },
        headers=contributor_headers,
    )
    assert analysis.status_code == 201
    analysis_id = analysis.json()["data"]["analysis_id"]

    claim = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Contributor-created claim.",
            "confidence": 0.5,
            "supported_by_analysis_ids": [analysis_id],
        },
        headers=contributor_headers,
    )
    assert claim.status_code == 201

    visualization = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis_id,
            "viz_type": "figure",
            "file_path": "figures/contributor.png",
        },
        headers=contributor_headers,
    )
    assert visualization.status_code == 201
    viz_id = visualization.json()["data"]["viz_id"]

    viz_upload = client.post(
        f"/visualizations/{viz_id}/file",
        files={"file": ("figure.png", b"figure-bytes", "image/png")},
        headers=contributor_headers,
    )
    assert viz_upload.status_code == 201


def test_contributor_submits_graph_change_set_for_admin_review(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    undergrad_headers = _auth_headers(_register_user(client, "graph-undergrad"))
    project_id = _create_project(client, admin_auth_headers, "Temporal odor coding")
    client.post(
        f"/projects/{project_id}/members",
        json={"username": "graph-undergrad", "role": "contributor"},
        headers=admin_auth_headers,
    )
    note_response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Please add a follow-up graph note."},
        headers=undergrad_headers,
    )
    assert note_response.status_code == 201
    note_id = note_response.json()["data"]["note_id"]
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )

    draft_response = client.post(f"/notes/{note_id}/graph-drafts", headers=undergrad_headers)
    assert draft_response.status_code == 201
    draft = draft_response.json()["data"]
    assert draft["status"] == "ready"
    assert draft["created_by_username"] == "graph-undergrad"

    operation = draft["operations"][0]
    accepted = client.patch(
        f"/graph-drafts/{draft['change_set_id']}/operations/{operation['operation_id']}",
        json={"status": "accepted"},
        headers=undergrad_headers,
    )
    assert accepted.status_code == 200

    denied_commit = client.post(
        f"/graph-drafts/{draft['change_set_id']}/commit",
        json={"message": "undergrad self merge"},
        headers=undergrad_headers,
    )
    assert denied_commit.status_code == 401

    submitted = client.post(
        f"/graph-drafts/{draft['change_set_id']}/submit",
        headers=undergrad_headers,
    )
    assert submitted.status_code == 200
    assert submitted.json()["data"]["status"] == "submitted"
    assert submitted.json()["data"]["submitted_by_username"] == "graph-undergrad"

    committed = client.post(
        f"/graph-drafts/{draft['change_set_id']}/commit",
        json={"message": "merge undergrad graph proposal"},
        headers=admin_auth_headers,
    )
    assert committed.status_code == 200
    payload = committed.json()["data"]
    assert payload["status"] == "committed"
    assert payload["committed_by_username"]
