from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


class FakeBatchDraftClient:
    provider = "fake"
    model = "fake-batch-model"

    def draft_from_batch(
        self,
        *,
        batch_context: dict[str, Any],
        user_hint: str | None = None,
    ) -> dict[str, Any]:
        return {
            "summary": "Scoped batch draft",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
            "note_dispositions": [
                {
                    "note_id": str(note_id),
                    "disposition": "no_change",
                    "reason": "considered by fake drafter",
                    "evidence_quote": "",
                    "client_refs": [],
                }
                for note_id in batch_context.get("note_ids_requiring_disposition") or []
            ],
        }

    def close(self) -> None:
        return None


def _post_created(
    client: TestClient,
    path: str,
    *,
    json: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    response = client.post(path, json=json, headers=headers)
    assert response.status_code == 201, response.json()
    return response.json()["data"]


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    label: str,
) -> str:
    return _post_created(
        client,
        "/questions",
        json={
            "project_id": project_id,
            "text": f"{label} scoped question?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    )["question_id"]


def _create_note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    label: str,
) -> str:
    return _post_created(
        client,
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": f"{label} scoped note.",
            "status": "staged",
        },
        headers=headers,
    )["note_id"]


def _create_dataset(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    label: str,
) -> str:
    question_id = _create_question(client, headers, project_id, label)
    return _post_created(
        client,
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    )["dataset_id"]


def _create_analysis(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    label: str,
) -> str:
    dataset_id = _create_dataset(client, headers, project_id, label)
    return _post_created(
        client,
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": f"{label}-method",
            "code_version": "v1",
        },
        headers=headers,
    )["analysis_id"]


def _create_batch(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    label: str,
) -> str:
    _create_note(client, headers, project_id, label)
    client.app.state.graph_draft_client_factory = lambda settings: FakeBatchDraftClient()
    run = _post_created(
        client,
        "/batches/run-now",
        json={"project_id": project_id},
        headers=headers,
    )
    assert run["change_set_id"] is not None
    return run["change_set_id"]


def _create_entity_for_endpoint(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    endpoint: str,
    label: str,
) -> str:
    match endpoint:
        case "/sessions":
            return _post_created(
                client,
                endpoint,
                json={"project_id": project_id, "session_type": "operational"},
                headers=headers,
            )["session_id"]
        case "/datasets":
            return _create_dataset(client, headers, project_id, label)
        case "/analyses":
            return _create_analysis(client, headers, project_id, label)
        case "/claims":
            return _post_created(
                client,
                endpoint,
                json={
                    "project_id": project_id,
                    "statement": f"{label} scoped claim.",
                    "confidence": 50,
                },
                headers=headers,
            )["claim_id"]
        case "/notes":
            return _create_note(client, headers, project_id, label)
        case "/visualizations":
            analysis_id = _create_analysis(client, headers, project_id, label)
            return _post_created(
                client,
                endpoint,
                json={
                    "analysis_id": analysis_id,
                    "viz_type": "line",
                    "file_path": f"{label}-plot.png",
                },
                headers=headers,
            )["viz_id"]
        case "/goals":
            return _post_created(
                client,
                endpoint,
                json={
                    "project_id": project_id,
                    "goal_type": "other",
                    "title": f"{label} scoped goal",
                },
                headers=headers,
            )["goal_id"]
        case "/batches":
            return _create_batch(client, headers, project_id, label)
    raise AssertionError(f"Unhandled endpoint case: {endpoint}")


@pytest.mark.parametrize(
    ("endpoint", "id_field"),
    [
        ("/sessions", "session_id"),
        ("/datasets", "dataset_id"),
        ("/analyses", "analysis_id"),
        ("/claims", "claim_id"),
        ("/notes", "note_id"),
        ("/visualizations", "viz_id"),
        ("/goals", "goal_id"),
        ("/batches", "change_set_id"),
    ],
)
def test_project_scoped_list_endpoints_hide_non_member_project_rows(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
    endpoint: str,
    id_field: str,
) -> None:
    visible_id = _create_entity_for_endpoint(
        client,
        admin_auth_headers,
        scoped_project_member.visible_project_id,
        endpoint,
        "visible",
    )
    hidden_id = _create_entity_for_endpoint(
        client,
        admin_auth_headers,
        scoped_project_member.hidden_project_id,
        endpoint,
        "hidden",
    )

    response = client.get(endpoint, headers=scoped_project_member.member_headers)

    assert response.status_code == 200
    payload = response.json()
    returned_ids = [item[id_field] for item in payload["data"]]
    assert returned_ids == [visible_id]
    assert hidden_id not in returned_ids
    assert payload["meta"]["total"] == 1
