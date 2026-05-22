from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_user(client: TestClient, username: str, password: str = "secret") -> str:
    response = client.post("/auth/register", json={"username": username, "password": password})
    assert response.status_code == 201
    return response.json()["data"]["access_token"]


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
    assert denied_project.status_code == 401

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
