from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient


class FakeDraftClient:
    model = "fake-gpt"

    def __init__(self, patch: dict[str, Any]) -> None:
        self.patch = patch

    def draft_from_note(self, **_: Any) -> dict[str, Any]:
        return self.patch

    def close(self) -> None:
        return None


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


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    text: str,
    status: str = "active",
) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": text,
            "question_type": "descriptive",
            "status": status,
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["question_id"]


def _create_staged_dataset(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    question_id: str,
) -> str:
    response = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["dataset_id"]


def _commit_dataset(client: TestClient, headers: dict[str, str], dataset_id: str) -> None:
    upload = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("portfolio.bin", b"portfolio-bytes", "application/octet-stream")},
        headers=headers,
    )
    assert upload.status_code == 201
    response = client.patch(
        f"/datasets/{dataset_id}",
        json={"status": "committed"},
        headers=headers,
    )
    assert response.status_code == 200


def _graph_draft_patch(project_id: str) -> dict[str, Any]:
    return {
        "summary": "Drafted portfolio activity",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "portfolio-note",
                "op": "create",
                "entity_type": "note",
                "semantic_type": "create_note",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "raw_content": "Portfolio graph draft activity.",
                    }
                ),
                "rationale": "Capture a follow-up note from the source.",
                "confidence": 0.9,
                "source_refs": [],
            }
        ],
    }


def _parse_utc_timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_portfolio_summary_aggregates_project_health_rows(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Portfolio project")
    owner_token = _register_user(client, "portfolio-owner")
    owner_headers = _auth_headers(owner_token)
    owner_response = client.post(
        f"/projects/{project_id}/members",
        json={"username": "portfolio-owner", "role": "owner"},
        headers=admin_auth_headers,
    )
    assert owner_response.status_code == 201

    active_question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="Which signal stays active?",
    )
    _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="Which staged question is still open?",
        status="staged",
    )
    committed_dataset_id = _create_staged_dataset(
        client,
        admin_auth_headers,
        project_id,
        active_question_id,
    )
    _commit_dataset(client, admin_auth_headers, committed_dataset_id)
    staged_dataset_id = _create_staged_dataset(
        client,
        admin_auth_headers,
        project_id,
        active_question_id,
    )

    analysis = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [staged_dataset_id],
            "method_hash": "portfolio-method",
            "code_version": "v1",
        },
        headers=admin_auth_headers,
    )
    assert analysis.status_code == 201
    claim = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Portfolio claim awaiting review.",
            "confidence": 50.0,
        },
        headers=admin_auth_headers,
    )
    assert claim.status_code == 201
    note = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Recent portfolio activity."},
        headers=admin_auth_headers,
    )
    assert note.status_code == 201

    response = client.get("/portfolio/summary", headers=owner_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["total"] == 1
    row = payload["data"][0]
    assert row["project_id"] == project_id
    assert row["name"] == "Portfolio project"
    assert row["status"] == "active"
    assert row["open_question_count"] == 2
    assert row["draft_dataset_count"] == 1
    assert row["committed_dataset_count"] == 1
    assert row["running_analysis_count"] == 1
    assert row["unreviewed_claim_count"] == 1
    assert row["last_activity_at"] is not None
    assert row["owners"] == [
        {
            "user_id": owner_response.json()["data"]["user_id"],
            "username": "portfolio-owner",
        }
    ]


def test_portfolio_summary_is_scoped_to_accessible_projects(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    visible_project_id = _create_project(client, admin_auth_headers, "Visible portfolio")
    hidden_project_id = _create_project(client, admin_auth_headers, "Hidden portfolio")
    viewer_headers = _auth_headers(_register_user(client, "portfolio-viewer"))
    membership = client.post(
        f"/projects/{visible_project_id}/members",
        json={"username": "portfolio-viewer", "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201

    visible_question_id = _create_question(
        client,
        admin_auth_headers,
        visible_project_id,
        text="Visible project question?",
    )
    hidden_question_id = _create_question(
        client,
        admin_auth_headers,
        hidden_project_id,
        text="Hidden project question?",
    )
    _create_staged_dataset(client, admin_auth_headers, visible_project_id, visible_question_id)
    _create_staged_dataset(client, admin_auth_headers, hidden_project_id, hidden_question_id)

    scoped_response = client.get("/portfolio/summary", headers=viewer_headers)
    admin_response = client.get("/portfolio/summary", headers=admin_auth_headers)

    assert scoped_response.status_code == 200
    assert [item["project_id"] for item in scoped_response.json()["data"]] == [
        visible_project_id
    ]
    assert scoped_response.json()["meta"]["total"] == 1

    assert admin_response.status_code == 200
    admin_project_ids = {item["project_id"] for item in admin_response.json()["data"]}
    assert {visible_project_id, hidden_project_id}.issubset(admin_project_ids)


def test_portfolio_summary_normalizes_graph_change_set_activity(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers, "Graph draft portfolio")
    note = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Source note for graph draft."},
        headers=admin_auth_headers,
    )
    assert note.status_code == 201
    note_id = note.json()["data"]["note_id"]
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _graph_draft_patch(project_id)
    )
    draft_response = client.post(
        f"/notes/{note_id}/graph-drafts",
        headers=admin_auth_headers,
    )
    assert draft_response.status_code == 201

    response = client.get("/portfolio/summary", headers=admin_auth_headers)

    assert response.status_code == 200
    rows = response.json()["data"]
    row = next(item for item in rows if item["project_id"] == project_id)
    assert _parse_utc_timestamp(row["last_activity_at"]) == _parse_utc_timestamp(
        draft_response.json()["data"]["updated_at"]
    )
