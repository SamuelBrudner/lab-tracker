from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from lab_tracker.graph_drafting import GraphDraftingError


class FakeBatchDraftClient:
    provider = "fake"
    model = "fake-batch-model"

    def __init__(
        self,
        patch: dict[str, Any] | None = None,
        *,
        fail_attempts: int = 0,
        error: str = "temporary model outage",
    ) -> None:
        self.patch = patch or {
            "summary": "empty",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
        }
        self.fail_attempts = fail_attempts
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def draft_from_batch(
        self,
        *,
        batch_context: dict[str, Any],
        user_hint: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"batch_context": batch_context, "user_hint": user_hint})
        if len(self.calls) <= self.fail_attempts:
            raise GraphDraftingError(self.error)
        return self.patch

    def close(self) -> None:
        self.closed = True


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Batch Project"}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _note(client: TestClient, headers: dict[str, str], project_id: str, text: str) -> str:
    response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": text, "status": "staged"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["note_id"]


def _batch_patch(project_id: str) -> dict[str, Any]:
    return {
        "summary": "Batch drafted one question",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "batch_question",
                "op": "create",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "text": "Do pooled notes support a merged observation?",
                        "question_type": "descriptive",
                        "status": "staged",
                    }
                ),
                "rationale": "Multiple staged notes point at the same follow-up.",
                "confidence": 0.82,
                "source_refs": [],
            }
        ],
    }


def test_run_now_persists_pending_batch_with_source_traceability(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_a = _note(client, admin_auth_headers, project_id, "Gel photo A looked clean.")
    note_b = _note(client, admin_auth_headers, project_id, "Voice memo: same gel, lane 2.")
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "user_hint": "merge gel notes"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    run = response.json()["data"]
    assert run["status"] == "ready"
    assert run["note_count"] == 2
    assert run["change_set_id"]
    assert fake_client.closed is True
    assert fake_client.calls[0]["user_hint"] == "merge gel notes"

    draft = client.get(f"/batches/{run['change_set_id']}", headers=admin_auth_headers)
    assert draft.status_code == 200
    payload = draft.json()["data"]
    assert payload["draft_mode"] == "graph_batch"
    assert payload["status"] == "ready"
    assert set(payload["source_note_ids"]) == {note_a, note_b}
    assert payload["source_note_count"] == 2
    assert payload["operations"][0]["source_refs"][0]["source_note_ids"] == [note_a, note_b]

    operation = payload["operations"][0]
    rejected = client.patch(
        f"/graph-drafts/{run['change_set_id']}/operations/{operation['operation_id']}",
        json={
            "payload": operation["payload"],
            "review_note": "Already covered by the existing question queue.",
            "status": "rejected",
        },
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 200
    rejected_operation = rejected.json()["data"]["operations"][0]
    assert rejected_operation["status"] == "rejected"
    assert rejected_operation["review_note"] == "Already covered by the existing question queue."
    assert (
        rejected_operation["error_metadata"]["review_note"]
        == "Already covered by the existing question queue."
    )

    listed = client.get(f"/batches?project_id={project_id}", headers=admin_auth_headers)
    assert listed.status_code == 200
    assert listed.json()["data"][0]["change_set_id"] == run["change_set_id"]


def test_batch_run_is_idempotent_for_same_window(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "One staged note.")
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client
    window = {
        "project_id": project_id,
        "since": "2026-01-01T00:00:00Z",
        "until": "2026-12-31T00:00:00Z",
    }

    first = client.post("/batches/run-now", json=window, headers=admin_auth_headers)
    second = client.post("/batches/run-now", json=window, headers=admin_auth_headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["data"]["run_id"] == first.json()["data"]["run_id"]
    assert second.json()["data"]["change_set_id"] == first.json()["data"]["change_set_id"]
    assert len(fake_client.calls) == 1


def test_batch_retry_and_dead_letter_paths_are_persisted(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Retry this staged note.")
    retry_client = FakeBatchDraftClient(_batch_patch(project_id), fail_attempts=2)
    client.app.state.graph_draft_client_factory = lambda settings: retry_client

    retry_response = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "until": "2026-12-30T00:00:00Z"},
        headers=admin_auth_headers,
    )

    assert retry_response.status_code == 201
    assert retry_response.json()["data"]["status"] == "ready"
    assert len(retry_client.calls) == 3

    bad_patch = {
        "summary": "bad",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": None,
                "op": "delete",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
                "target_entity_id": None,
                "payload_json": "{}",
                "rationale": "Unsupported op.",
                "confidence": 0.2,
                "source_refs": [],
            }
        ],
    }
    _note(client, admin_auth_headers, project_id, "This note triggers invalid JSON shape.")
    dead_letter_client = FakeBatchDraftClient(bad_patch)
    client.app.state.graph_draft_client_factory = lambda settings: dead_letter_client

    failed_response = client.post(
        "/batches/run-now",
        json={
            "project_id": project_id,
            "since": "2026-01-01T00:00:00Z",
            "until": "2026-12-31T00:00:00Z",
        },
        headers=admin_auth_headers,
    )

    assert failed_response.status_code == 201
    failed_run = failed_response.json()["data"]
    assert failed_run["status"] == "failed"
    assert failed_run["error_metadata"]["category"] == "validation_error"
    draft = client.get(f"/batches/{failed_run['change_set_id']}", headers=admin_auth_headers)
    assert draft.json()["data"]["status"] == "failed"
    assert draft.json()["data"]["error_metadata"]["input_snapshot"]["source_note_ids"]


def test_empty_batch_window_skips_without_creating_change_set(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    run = response.json()["data"]
    assert run["status"] == "skipped"
    assert run["change_set_id"] is None
    assert run["note_count"] == 0
    assert fake_client.calls == []


def test_batch_cadence_settings_are_user_visible_and_rescheduled(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)

    defaults = client.get(
        f"/projects/{project_id}/graph-draft-batch-settings",
        headers=admin_auth_headers,
    )
    assert defaults.status_code == 200
    assert defaults.json()["data"]["cadence_minutes"] == 1440
    assert defaults.json()["data"]["next_run_at"]

    updated = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={
            "enabled": True,
            "cadence_minutes": 720,
            "run_at_local_time": "07:30",
            "timezone_name": "America/New_York",
        },
        headers=admin_auth_headers,
    )
    assert updated.status_code == 200
    payload = updated.json()["data"]
    assert payload["cadence_minutes"] == 720
    assert payload["run_at_local_time"] == "07:30"
    assert payload["next_run_at"]
