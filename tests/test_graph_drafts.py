from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from lab_tracker.graph_drafting import GraphDraftingError, OpenAIGraphDraftClient


class FakeDraftClient:
    model = "fake-gpt"

    def __init__(self, patch: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.patch = patch or {"summary": "empty", "operations": []}
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def draft_from_image(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        project_context: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "content_type": content_type,
                "image_bytes": image_bytes,
                "project_context": project_context,
            }
        )
        if self.error:
            raise GraphDraftingError(self.error)
        return self.patch

    def close(self) -> None:
        self.closed = True


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Draft Project"}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _image_note(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("whiteboard.jpg", b"fake-image-bytes", "image/jpeg")},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["note_id"]


def _draft_patch(project_id: str) -> dict[str, Any]:
    return {
        "summary": "Drafted project updates",
        "operations": [
            {
                "client_ref": "q1",
                "op": "create",
                "entity_type": "question",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "text": "Does the new whiteboard protocol improve yield?",
                        "question_type": "descriptive",
                        "status": "staged",
                    }
                ),
                "rationale": "The whiteboard states a protocol question.",
                "confidence": 0.82,
                "source_refs": [
                    {"label": "whiteboard", "quote": "improve yield?", "region": None}
                ],
            },
            {
                "client_ref": "note1",
                "op": "create",
                "entity_type": "note",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "raw_content": "Whiteboard mentions the yield protocol.",
                        "targets": [
                            {
                                "entity_type": "question",
                                "entity_id": {"$ref": "q1"},
                            }
                        ],
                    }
                ),
                "rationale": "Capture the image-derived summary as a note.",
                "confidence": 0.74,
                "source_refs": [
                    {"label": "whiteboard", "quote": "yield protocol", "region": None}
                ],
            },
        ],
    }


def test_openai_graph_draft_client_sends_responses_image_and_strict_schema() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "summary": "ok",
                        "operations": [],
                    }
                )
            },
        )

    client = OpenAIGraphDraftClient(
        api_key="test-key",
        model="gpt-test",
        transport=httpx.MockTransport(handler),
    )

    result = client.draft_from_image(
        image_bytes=b"image",
        content_type="image/png",
        project_context={"project": {"name": "Context"}},
    )

    assert result == {"summary": "ok", "operations": []}
    request = requests[0]
    assert request["model"] == "gpt-test"
    assert request["input"][0]["content"][1]["type"] == "input_image"
    assert request["input"][0]["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    client.close()


def test_openai_graph_draft_client_reports_setup_and_api_errors() -> None:
    missing_key = OpenAIGraphDraftClient(
        api_key="",
        model="gpt-test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )

    with pytest.raises(GraphDraftingError, match="OPENAI_API_KEY"):
        missing_key.draft_from_image(
            image_bytes=b"image",
            content_type="image/png",
            project_context={},
        )

    api_failure = OpenAIGraphDraftClient(
        api_key="test-key",
        model="gpt-test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": {"message": "boom"}})
        ),
    )

    with pytest.raises(GraphDraftingError, match="boom"):
        api_failure.draft_from_image(
            image_bytes=b"image",
            content_type="image/png",
            project_context={},
        )
    missing_key.close()
    api_failure.close()


def test_openai_graph_draft_client_reports_refusals_and_malformed_json() -> None:
    refusal = OpenAIGraphDraftClient(
        api_key="test-key",
        model="gpt-test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"output": [{"content": [{"type": "refusal", "refusal": "no"}]}]},
            )
        ),
    )

    with pytest.raises(GraphDraftingError, match="no"):
        refusal.draft_from_image(
            image_bytes=b"image",
            content_type="image/png",
            project_context={},
        )

    malformed = OpenAIGraphDraftClient(
        api_key="test-key",
        model="gpt-test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"output_text": "not json"})
        ),
    )

    with pytest.raises(GraphDraftingError, match="malformed"):
        malformed.draft_from_image(
            image_bytes=b"image",
            content_type="image/png",
            project_context={},
        )
    refusal.close()
    malformed.close()


def test_image_note_draft_stores_operations_and_context(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    fake_client = FakeDraftClient(_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)

    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["status"] == "ready"
    assert payload["source_note_id"] == note_id
    assert payload["source_content_type"] == "image/jpeg"
    assert [operation["entity_type"] for operation in payload["operations"]] == [
        "question",
        "note",
    ]
    assert payload["operations"][0]["status"] == "proposed"
    assert fake_client.calls[0]["content_type"] == "image/jpeg"
    assert fake_client.calls[0]["project_context"]["project"]["project_id"] == project_id
    assert fake_client.closed is True


def test_image_note_draft_rejects_missing_or_non_image_raw_asset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    text_note = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "plain note"},
        headers=admin_auth_headers,
    ).json()["data"]["note_id"]
    text_upload = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("capture.txt", b"text", "text/plain")},
        headers=admin_auth_headers,
    ).json()["data"]["note_id"]

    missing_raw = client.post(f"/notes/{text_note}/graph-drafts", headers=admin_auth_headers)
    non_image = client.post(f"/notes/{text_upload}/graph-drafts", headers=admin_auth_headers)

    assert missing_raw.status_code == 422
    assert "raw image asset" in missing_raw.json()["error"]["message"]
    assert non_image.status_code == 422
    assert "only supports image" in non_image.json()["error"]["message"]


def test_gpt_failure_returns_stored_failed_draft(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        error="LAB_TRACKER_OPENAI_API_KEY must be set before drafting graph changes."
    )

    response = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)

    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["status"] == "failed"
    assert "OPENAI_API_KEY" in payload["error_metadata"]["message"]

    listed = client.get(
        f"/graph-drafts?source_note_id={note_id}",
        headers=admin_auth_headers,
    )
    assert listed.status_code == 200
    assert listed.json()["data"][0]["change_set_id"] == payload["change_set_id"]


def test_malformed_or_unsupported_gpt_patch_returns_stored_failed_draft(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        {
            "summary": "bad",
            "operations": [
                {
                    "client_ref": "bad",
                    "op": "delete",
                    "entity_type": "question",
                    "target_entity_id": None,
                    "payload_json": "{}",
                    "rationale": "unsupported",
                    "confidence": 0.5,
                    "source_refs": [],
                }
            ],
        }
    )

    response = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)

    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["status"] == "failed"
    assert "invalid" in payload["error_metadata"]["message"]


def test_edit_accept_and_commit_resolves_refs_into_canonical_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    draft = client.post(
        f"/notes/{note_id}/graph-drafts",
        headers=admin_auth_headers,
    ).json()["data"]
    change_set_id = draft["change_set_id"]

    for operation in draft["operations"]:
        response = client.patch(
            f"/graph-drafts/{change_set_id}/operations/{operation['operation_id']}",
            json={"payload": operation["payload"], "status": "accepted"},
            headers=admin_auth_headers,
        )
        assert response.status_code == 200

    before_commit = client.get(
        f"/questions?project_id={project_id}&search=whiteboard&limit=50&offset=0",
        headers=admin_auth_headers,
    )
    assert before_commit.json()["data"] == []

    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "Commit whiteboard draft"},
        headers=admin_auth_headers,
    )

    assert commit.status_code == 200
    committed = commit.json()["data"]
    assert committed["status"] == "committed"
    assert [operation["status"] for operation in committed["operations"]] == [
        "applied",
        "applied",
    ]
    question_id = committed["operations"][0]["result_entity_id"]
    assert UUID(question_id)

    notes = client.get(
        f"/notes?project_id={project_id}&target_entity_type=question&target_entity_id={question_id}",
        headers=admin_auth_headers,
    )
    assert notes.status_code == 200
    assert notes.json()["data"][0]["targets"][0]["entity_id"] == question_id


def test_operation_payload_edit_validates_without_mutating_canonical_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    draft = client.post(
        f"/notes/{note_id}/graph-drafts",
        headers=admin_auth_headers,
    ).json()["data"]
    operation = draft["operations"][0]

    edited = client.patch(
        f"/graph-drafts/{draft['change_set_id']}/operations/{operation['operation_id']}",
        json={"payload": {"project_id": project_id}, "status": "accepted"},
        headers=admin_auth_headers,
    )

    assert edited.status_code == 200
    edited_operation = edited.json()["data"]["operations"][0]
    assert edited_operation["status"] == "proposed"
    assert "text" in edited_operation["error_metadata"]["message"]

    questions = client.get(
        f"/questions?project_id={project_id}&limit=50&offset=0",
        headers=admin_auth_headers,
    )
    assert questions.status_code == 200
    assert questions.json()["data"] == []


def test_commit_failure_rolls_back_canonical_changes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    failing_patch = _draft_patch(project_id)
    failing_patch["operations"][1] = {
        "client_ref": "dataset1",
        "op": "create",
        "entity_type": "dataset",
        "target_entity_id": None,
        "payload_json": json.dumps(
            {
                "project_id": project_id,
                "primary_question_id": {"$ref": "q1"},
                "status": "committed",
            }
        ),
        "rationale": "This should fail because the drafted question is not active.",
        "confidence": 0.6,
        "source_refs": [{"label": "whiteboard", "quote": "dataset", "region": None}],
    }
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(failing_patch)
    draft = client.post(
        f"/notes/{note_id}/graph-drafts",
        headers=admin_auth_headers,
    ).json()["data"]
    change_set_id = draft["change_set_id"]
    for operation in draft["operations"]:
        accepted = client.patch(
            f"/graph-drafts/{change_set_id}/operations/{operation['operation_id']}",
            json={"payload": operation["payload"], "status": "accepted"},
            headers=admin_auth_headers,
        )
        assert accepted.status_code == 200

    failed_commit = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "Should rollback"},
        headers=admin_auth_headers,
    )

    assert failed_commit.status_code == 422
    questions = client.get(
        f"/questions?project_id={project_id}&search=whiteboard&limit=50&offset=0",
        headers=admin_auth_headers,
    )
    assert questions.status_code == 200
    assert questions.json()["data"] == []
