from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from lab_tracker.config import Settings
from lab_tracker.graph_drafting import (
    GraphDraftingError,
    OpenAIGraphDraftClient,
    make_graph_draft_client,
)


class FakeDraftClient:
    model = "fake-gpt"
    transcription_model = "fake-transcribe"

    def __init__(self, patch: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.patch = patch or {
            "summary": "empty",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
        }
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.transcription_calls: list[dict[str, Any]] = []
        self.closed = False

    def draft_from_note(
        self,
        *,
        graph_context: dict[str, Any] | None = None,
        user_hint: str | None = None,
        draft_mode: str = "graph_context",
        source_artifacts: list[dict[str, Any]] | None = None,
        image_bytes: bytes | None = None,
        image_content_type: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "content_type": image_content_type,
                "draft_mode": draft_mode,
                "graph_context": graph_context,
                "image_bytes": image_bytes,
                "source_artifacts": source_artifacts or [],
                "user_hint": user_hint,
            }
        )
        if self.error:
            raise GraphDraftingError(self.error)
        return self.patch

    def draft_from_image(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        graph_context: dict[str, Any] | None = None,
        user_hint: str | None = None,
        draft_mode: str = "graph_context",
        project_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "content_type": content_type,
                "draft_mode": draft_mode,
                "graph_context": graph_context,
                "image_bytes": image_bytes,
                "project_context": project_context,
                "user_hint": user_hint,
            }
        )
        if self.error:
            raise GraphDraftingError(self.error)
        return self.patch

    def transcribe_audio(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        self.transcription_calls.append(
            {
                "audio_bytes": audio_bytes,
                "content_type": content_type,
                "filename": filename,
                "prompt": prompt,
            }
        )
        return {"text": "Fly 12 tracked better after pulse onset."}

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
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "q1",
                "op": "create",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
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
                "semantic_type": "create_note",
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


def _hierarchy_draft_patch(project_id: str) -> dict[str, Any]:
    return {
        "summary": "Drafted a broad question with an atomic child.",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "broad_question",
                "op": "create",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "text": "How does odor timing shape navigation?",
                        "question_type": "descriptive",
                        "status": "staged",
                    }
                ),
                "rationale": "The whiteboard frames a broad motivating question.",
                "confidence": 0.84,
                "source_refs": [
                    {"label": "whiteboard", "quote": "odor timing", "region": None}
                ],
            },
            {
                "client_ref": "atomic_question",
                "op": "create",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "text": "Which odor-gap feature changes forward locomotion?",
                        "question_type": "hypothesis_driven",
                        "status": "staged",
                        "parent_question_ids": [{"$ref": "broad_question"}],
                    }
                ),
                "rationale": "The child isolates one testable behavior from the broad question.",
                "confidence": 0.77,
                "source_refs": [
                    {"label": "whiteboard", "quote": "forward locomotion", "region": None}
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
                        "uncertain_fields": [],
                        "clarification_requests": [],
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

    assert result == {
        "summary": "ok",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [],
    }
    request = requests[0]
    assert request["model"] == "gpt-test"
    assert "parent_question_ids" in request["instructions"]
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


def test_openai_graph_draft_client_transcribes_audio_with_configured_model() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/audio/transcriptions"
        body = request.content
        assert b'name="model"' in body
        assert b"gpt-transcribe-test" in body
        assert b'name="response_format"' in body
        assert b"json" in body
        assert b'name="file"; filename="voice.webm"' in body
        return httpx.Response(200, json={"text": "Fly 12 tracked well."})

    client = OpenAIGraphDraftClient(
        api_key="test-key",
        model="gpt-test",
        transcription_model="gpt-transcribe-test",
        transport=httpx.MockTransport(handler),
    )

    result = client.transcribe_audio(
        audio_bytes=b"audio",
        filename="voice.webm",
        content_type="audio/webm",
    )

    assert result["text"] == "Fly 12 tracked well."
    assert requests[0].headers["authorization"] == "Bearer test-key"
    client.close()


def test_openai_graph_draft_client_drafts_from_batch_sends_packet() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content.decode("utf-8"))
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "summary": "batch ok",
                        "uncertain_fields": [],
                        "clarification_requests": [],
                        "operations": [],
                    }
                )
            },
        )

    client = OpenAIGraphDraftClient(
        api_key="test-key",
        model="gpt-batch-test",
        transport=httpx.MockTransport(handler),
    )

    batch_context = {
        "mode": "graph_batch",
        "batch_notes": [
            {"id": "11111111-1111-1111-1111-111111111111", "preview": "fly 12"},
            {"id": "22222222-2222-2222-2222-222222222222", "preview": "fly 13"},
        ],
        "projects": [],
    }
    result = client.draft_from_batch(
        batch_context=batch_context,
        user_hint="prefer linking over creating",
    )

    assert result["summary"] == "batch ok"
    request = requests[0]
    assert request["model"] == "gpt-batch-test"
    assert "daily batch" in request["instructions"]
    user_text = request["input"][0]["content"][0]["text"]
    assert "Batch size: 2 notes" in user_text
    assert "prefer linking over creating" in user_text
    assert "graph_batch" in user_text
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    client.close()


def test_openai_graph_draft_client_draft_from_batch_requires_notes() -> None:
    client = OpenAIGraphDraftClient(
        api_key="test-key",
        model="gpt-batch-test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )

    with pytest.raises(GraphDraftingError, match="no notes"):
        client.draft_from_batch(batch_context={"mode": "graph_batch", "batch_notes": []})
    client.close()


def test_openai_graph_draft_client_draft_from_batch_requires_api_key() -> None:
    client = OpenAIGraphDraftClient(
        api_key="",
        model="gpt-batch-test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )

    with pytest.raises(GraphDraftingError, match="OPENAI_API_KEY"):
        client.draft_from_batch(
            batch_context={"mode": "graph_batch", "batch_notes": [{"id": "x"}]}
        )
    client.close()


def test_make_graph_draft_client_returns_openai_by_default() -> None:
    settings = Settings(
        environment="local",
        auth_enabled=False,
        graph_draft_provider="openai",
        openai_api_key="test-key",
        openai_model="gpt-test",
    )
    client = make_graph_draft_client(settings)
    try:
        assert isinstance(client, OpenAIGraphDraftClient)
        assert client.model == "gpt-test"
    finally:
        client.close()


def test_make_graph_draft_client_rejects_unknown_provider() -> None:
    settings = Settings(
        environment="local",
        auth_enabled=False,
        graph_draft_provider="palantir",
    )
    with pytest.raises(GraphDraftingError, match="palantir"):
        make_graph_draft_client(settings)


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
    assert payload["draft_mode"] == "graph_context"
    assert payload["summary"] == "Drafted project updates"
    assert payload["uncertain_fields"] == []
    assert payload["clarification_requests"] == []
    assert payload["source_note_id"] == note_id
    assert payload["source_content_type"] == "image/jpeg"
    assert payload["context_packet"]["project"]["id"] == project_id
    assert [operation["entity_type"] for operation in payload["operations"]] == [
        "question",
        "note",
    ]
    assert [operation["semantic_type"] for operation in payload["operations"]] == [
        "suggest_new_question",
        "create_note",
    ]
    assert payload["operations"][0]["status"] == "proposed"
    assert fake_client.calls[0]["content_type"] == "image/jpeg"
    assert fake_client.calls[0]["draft_mode"] == "graph_context"
    assert fake_client.calls[0]["graph_context"]["project"]["id"] == project_id
    assert fake_client.calls[0]["source_artifacts"][0]["type"] == "image"
    assert fake_client.calls[0]["user_hint"] is None
    assert fake_client.closed is True


def test_graph_context_packet_includes_selected_targets_and_recent_neighborhood(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    parent_question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "How do plume statistics shape navigation?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    child_question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can flies climb temporal odor gradients?",
            "question_type": "hypothesis_driven",
            "status": "active",
            "parent_question_ids": [parent_question["question_id"]],
        },
        headers=admin_auth_headers,
    ).json()["data"]
    recent_note = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Earlier Rig 2 note about smoother plume condition.",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    session = client.post(
        "/sessions",
        json={
            "project_id": project_id,
            "session_type": "operational",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    dataset = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": child_question["question_id"],
            "commit_manifest": {"source_session_id": session["session_id"]},
            "status": "staged",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    analysis = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset["dataset_id"]],
            "method_hash": "turning-after-pulse",
            "code_version": "abc123",
            "status": "staged",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    claim = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "Turning appears stronger after pulse onset.",
            "confidence": 62,
            "status": "proposed",
            "supported_by_dataset_ids": [dataset["dataset_id"]],
            "supported_by_analysis_ids": [analysis["analysis_id"]],
        },
        headers=admin_auth_headers,
    ).json()["data"]
    visualization = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis["analysis_id"],
            "viz_type": "timeseries",
            "file_path": "plots/turning-after-pulse.png",
            "caption": "Turning after pulse onset.",
            "related_claim_ids": [claim["claim_id"]],
        },
        headers=admin_auth_headers,
    ).json()["data"]
    note_upload = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "targets": json.dumps(
                [
                    {
                        "entity_type": "question",
                        "entity_id": child_question["question_id"],
                    },
                    {"entity_type": "session", "entity_id": session["session_id"]},
                    {"entity_type": "dataset", "entity_id": dataset["dataset_id"]},
                ]
            ),
            "metadata": json.dumps(
                {
                    "capture_source": "mobile_capture",
                    "capture_hint": "Rig 2 Fly 12",
                }
            ),
        },
        files={"file": ("rig-note.jpg", b"fake-image-bytes", "image/jpeg")},
        headers=admin_auth_headers,
    )
    assert note_upload.status_code == 201
    note_id = note_upload.json()["data"]["note_id"]
    fake_client = FakeDraftClient()
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        f"/notes/{note_id}/graph-drafts",
        json={"user_hint": "same gradient protocol as last week"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    context = response.json()["data"]["context_packet"]
    assert context["mode"] == "graph_context"
    assert context["user_hint"] == "same gradient protocol as last week"
    assert context["current_user"]["role"] == "admin"
    assert context["project"]["id"] == project_id
    assert context["source_note"]["metadata"]["capture_source"] == "mobile_capture"
    assert {
        (target["entity_type"], target["entity_id"]) for target in context["selected_targets"]
    } == {
        ("question", child_question["question_id"]),
        ("session", session["session_id"]),
        ("dataset", dataset["dataset_id"]),
    }
    assert any(
        item["id"] == child_question["question_id"]
        and item["parent_question_ids"] == [parent_question["question_id"]]
        for item in context["active_or_staged_questions"]
    )
    assert any(item["id"] == recent_note["note_id"] for item in context["recent_notes"])
    assert any(item["id"] == session["session_id"] for item in context["recent_sessions"])
    assert any(item["id"] == dataset["dataset_id"] for item in context["recent_datasets"])
    assert any(item["id"] == analysis["analysis_id"] for item in context["recent_analyses"])
    assert any(item["id"] == claim["claim_id"] for item in context["recent_claims"])
    assert any(
        item["id"] == visualization["viz_id"] for item in context["recent_visualizations"]
    )
    summary = context["context_summary"]
    assert summary["approximate_size_bytes"] > 0
    assert summary["counts"]["projects"] == 1
    assert summary["counts"]["active_or_staged_questions"] == 2
    assert summary["counts"]["recent_notes"] >= 1
    assert summary["counts"]["recent_sessions"] >= 1
    assert summary["counts"]["recent_datasets"] >= 1
    assert summary["counts"]["recent_analyses"] >= 1
    assert summary["counts"]["recent_claims"] >= 1
    assert summary["counts"]["recent_visualizations"] >= 1
    assert summary["counts"]["known_aliases"] >= 1
    assert summary["source_artifact_counts"] == {"image": 1}
    assert {
        (target["entity_type"], target["entity_id"]) for target in summary["selected_targets"]
    } == {
        ("question", child_question["question_id"]),
        ("session", session["session_id"]),
        ("dataset", dataset["dataset_id"]),
    }
    assert all(target["label"] for target in summary["selected_targets"])
    assert fake_client.calls[0]["graph_context"] == context
    assert fake_client.calls[0]["user_hint"] == "same gradient protocol as last week"


def test_graph_context_packet_includes_supersession_aliases(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    source_question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does lifecycle nuance explain the ATF4 phenotype?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    refactor = client.post(
        f"/questions/{source_question['question_id']}/refactor",
        json={
            "replacement": {
                "text": "Which ATF4 arbitration comparison is testable first?",
                "question_type": "hypothesis_driven",
                "status": "active",
            },
            "reason": "Use a narrower, testable current framing.",
        },
        headers=admin_auth_headers,
    )
    assert refactor.status_code == 201
    replacement_question = refactor.json()["data"]["replacement_question"]
    note_upload = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "targets": json.dumps(
                [
                    {
                        "entity_type": "question",
                        "entity_id": source_question["question_id"],
                    }
                ]
            ),
        },
        files={"file": ("atf4-note.jpg", b"fake-image-bytes", "image/jpeg")},
        headers=admin_auth_headers,
    )
    assert note_upload.status_code == 201
    fake_client = FakeDraftClient()
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        f"/notes/{note_upload.json()['data']['note_id']}/graph-drafts",
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    context = response.json()["data"]["context_packet"]
    active_question_ids = {item["id"] for item in context["active_or_staged_questions"]}
    assert replacement_question["question_id"] in active_question_ids
    assert source_question["question_id"] not in active_question_ids
    assert any(
        alias["entity_type"] == "question"
        and alias["entity_id"] == replacement_question["question_id"]
        and alias.get("superseded_entity_id") == source_question["question_id"]
        and alias.get("relationship") == "superseded_alias_for_replacement"
        and source_question["text"] in alias["aliases"]
        for alias in context["known_aliases"]
    )
    assert fake_client.calls[0]["graph_context"] == context


def test_image_only_draft_requires_explicit_mode_and_records_warning_context(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    fake_client = FakeDraftClient()
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        f"/notes/{note_id}/graph-drafts",
        json={"mode": "image_only", "user_hint": "ignore graph context"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["status"] == "ready"
    assert payload["draft_mode"] == "image_only"
    assert payload["context_packet"]["mode"] == "image_only"
    assert "explicitly requested" in payload["context_packet"]["warning"]
    assert payload["context_packet"]["context_summary"]["counts"]["source_artifacts"] == 1
    assert "project" not in payload["context_packet"]
    assert fake_client.calls[0]["draft_mode"] == "image_only"
    assert fake_client.calls[0]["graph_context"]["mode"] == "image_only"
    assert fake_client.calls[0]["user_hint"] == "ignore graph context"


def test_graph_draft_rejects_untranscribed_voice_and_unsupported_raw_asset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    voice_upload = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("voice.webm", b"audio", "audio/webm")},
        headers=admin_auth_headers,
    ).json()["data"]["note_id"]
    text_upload = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("capture.txt", b"text", "text/plain")},
        headers=admin_auth_headers,
    ).json()["data"]["note_id"]

    untranscribed_voice = client.post(
        f"/notes/{voice_upload}/graph-drafts",
        headers=admin_auth_headers,
    )
    non_image = client.post(f"/notes/{text_upload}/graph-drafts", headers=admin_auth_headers)

    assert untranscribed_voice.status_code == 422
    assert "editable transcript" in untranscribed_voice.json()["error"]["message"]
    assert non_image.status_code == 422
    assert "raw image asset, text note, or voice transcript" in non_image.json()["error"]["message"]


def test_voice_note_transcription_stores_editable_transcript(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    voice_note = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "metadata": json.dumps(
                {
                    "capture_source": "mobile_capture",
                    "capture_kind": "voice",
                    "voice_note_type": "Observation",
                }
            ),
        },
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    ).json()["data"]
    fake_client = FakeDraftClient()
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        f"/notes/{voice_note['note_id']}/transcript",
        json={"prompt": "Rig 2 Fly 12"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["transcribed_text"] == "Fly 12 tracked better after pulse onset."
    assert payload["metadata"]["transcript_status"] == "ready"
    assert payload["metadata"]["transcript_model"] == "fake-transcribe"
    assert payload["metadata"]["transcript_source_storage_id"] == voice_note["raw_asset"][
        "storage_id"
    ]
    assert fake_client.transcription_calls[0]["audio_bytes"] == b"fake-audio-bytes"
    assert fake_client.transcription_calls[0]["content_type"] == "audio/webm"
    assert fake_client.transcription_calls[0]["prompt"] == "Rig 2 Fly 12"
    assert fake_client.closed is True

    edited = client.patch(
        f"/notes/{voice_note['note_id']}",
        json={"transcribed_text": "Edited transcript"},
        headers=admin_auth_headers,
    )

    assert edited.status_code == 200
    assert edited.json()["data"]["transcribed_text"] == "Edited transcript"


def test_photo_voice_bundle_draft_uses_image_and_voice_transcript_context(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    bundle_id = "bundle-1"
    image_note = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "metadata": json.dumps(
                {
                    "capture_source": "mobile_capture",
                    "capture_kind": "image",
                    "capture_bundle_id": bundle_id,
                }
            ),
        },
        files={"file": ("notebook.jpg", b"fake-image-bytes", "image/jpeg")},
        headers=admin_auth_headers,
    ).json()["data"]
    voice_note = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "transcribed_text": "Same protocol as last week. Fly 12 tracked better.",
            "metadata": json.dumps(
                {
                    "capture_source": "mobile_capture",
                    "capture_kind": "voice",
                    "capture_bundle_id": bundle_id,
                    "voice_note_type": "Observation",
                }
            ),
        },
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    ).json()["data"]
    fake_client = FakeDraftClient()
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        f"/notes/{image_note['note_id']}/graph-drafts",
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    payload = response.json()["data"]
    artifacts = payload["context_packet"]["source_artifacts"]
    assert {artifact["type"] for artifact in artifacts} == {"image", "audio"}
    assert any(
        artifact["note_id"] == voice_note["note_id"]
        and artifact["transcript_text"] == "Same protocol as last week. Fly 12 tracked better."
        for artifact in artifacts
    )
    assert payload["source_content_type"] == "image/jpeg"
    assert fake_client.calls[0]["image_bytes"] == b"fake-image-bytes"
    assert fake_client.calls[0]["content_type"] == "image/jpeg"
    assert fake_client.calls[0]["source_artifacts"] == artifacts


def test_saved_photo_voice_bundle_review_transcribes_drafts_and_commits(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    bundle_id = "saved-bundle-1"
    image_note = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "metadata": json.dumps(
                {
                    "capture_source": "mobile_capture",
                    "capture_mode": "bundle",
                    "capture_kind": "image",
                    "capture_review_status": "pending_review",
                    "capture_bundle_id": bundle_id,
                    "capture_hint": "Rig 2 Fly 12",
                }
            ),
        },
        files={"file": ("notebook.jpg", b"fake-image-bytes", "image/jpeg")},
        headers=admin_auth_headers,
    ).json()["data"]
    voice_note = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "metadata": json.dumps(
                {
                    "capture_source": "mobile_capture",
                    "capture_mode": "bundle",
                    "capture_kind": "voice",
                    "capture_review_status": "pending_review",
                    "capture_bundle_id": bundle_id,
                    "voice_note_type": "Observation",
                    "transcript_status": "pending",
                }
            ),
        },
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    ).json()["data"]
    fake_client = FakeDraftClient(_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    blocked = client.post(
        f"/notes/{image_note['note_id']}/graph-drafts",
        headers=admin_auth_headers,
    )

    assert blocked.status_code == 422
    assert "editable transcript" in blocked.json()["error"]["message"]
    assert fake_client.calls == []

    transcript = client.post(
        f"/notes/{voice_note['note_id']}/transcript",
        json={"prompt": "Rig 2 Fly 12"},
        headers=admin_auth_headers,
    )

    assert transcript.status_code == 200
    assert transcript.json()["data"]["transcribed_text"] == (
        "Fly 12 tracked better after pulse onset."
    )
    assert fake_client.transcription_calls[0]["audio_bytes"] == b"fake-audio-bytes"

    draft = client.post(
        f"/notes/{image_note['note_id']}/graph-drafts",
        json={"user_hint": "Same protocol as last week."},
        headers=admin_auth_headers,
    )

    assert draft.status_code == 201
    draft_payload = draft.json()["data"]
    assert draft_payload["status"] == "ready"
    assert draft_payload["source_content_type"] == "image/jpeg"
    artifacts = draft_payload["context_packet"]["source_artifacts"]
    assert {artifact["type"] for artifact in artifacts} == {"image", "audio"}
    assert any(
        artifact["note_id"] == voice_note["note_id"]
        and artifact["transcript_text"] == "Fly 12 tracked better after pulse onset."
        and artifact["transcript_is_derived"] is True
        for artifact in artifacts
    )
    assert fake_client.calls[0]["graph_context"] == draft_payload["context_packet"]
    assert fake_client.calls[0]["source_artifacts"] == artifacts
    assert fake_client.calls[0]["user_hint"] == "Same protocol as last week."

    change_set_id = draft_payload["change_set_id"]
    for operation in draft_payload["operations"]:
        accepted = client.patch(
            f"/graph-drafts/{change_set_id}/operations/{operation['operation_id']}",
            json={"payload": operation["payload"], "status": "accepted"},
            headers=admin_auth_headers,
        )
        assert accepted.status_code == 200

    committed = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "Commit saved photo and voice bundle"},
        headers=admin_auth_headers,
    )

    assert committed.status_code == 200
    committed_payload = committed.json()["data"]
    assert committed_payload["status"] == "committed"
    assert [operation["status"] for operation in committed_payload["operations"]] == [
        "applied",
        "applied",
    ]
    question_id = committed_payload["operations"][0]["result_entity_id"]
    linked_notes = client.get(
        f"/notes?project_id={project_id}&target_entity_type=question&target_entity_id={question_id}",
        headers=admin_auth_headers,
    )
    assert linked_notes.status_code == 200
    assert linked_notes.json()["data"][0]["targets"][0]["entity_id"] == question_id


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
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [
                {
                    "client_ref": "bad",
                    "op": "delete",
                    "entity_type": "question",
                    "semantic_type": "suggest_new_question",
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


def test_generic_semantic_entity_operations_validate_operation_direction(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        {
            "summary": "generic create",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [
                {
                    "client_ref": "generic_note",
                    "op": "create",
                    "entity_type": "note",
                    "semantic_type": "create_entity",
                    "target_entity_id": None,
                    "payload_json": json.dumps(
                        {
                            "project_id": project_id,
                            "raw_content": "Generic note create is still supported.",
                        }
                    ),
                    "rationale": "No narrower semantic label fits.",
                    "confidence": 0.7,
                    "source_refs": [],
                }
            ],
        }
    )

    response = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)

    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["status"] == "ready"
    assert payload["operations"][0]["semantic_type"] == "create_entity"

    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        {
            "summary": "bad generic create",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [
                {
                    "client_ref": None,
                    "op": "update",
                    "entity_type": "note",
                    "semantic_type": "create_entity",
                    "target_entity_id": note_id,
                    "payload_json": json.dumps({"metadata": {"reviewed": True}}),
                    "rationale": "The semantic label disagrees with the op.",
                    "confidence": 0.7,
                    "source_refs": [],
                }
            ],
        }
    )

    rejected = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)

    assert rejected.status_code == 201
    failed_payload = rejected.json()["data"]
    assert failed_payload["status"] == "failed"
    assert "create_entity requires create op" in failed_payload["error_metadata"]["message"]


def test_model_output_with_unknown_existing_entity_id_returns_stored_failed_draft(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    unknown_question_id = "55555555-5555-4555-8555-555555555555"
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        {
            "summary": "bad link",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [
                {
                    "client_ref": "link1",
                    "op": "update",
                    "entity_type": "note",
                    "semantic_type": "link_note_to_question",
                    "target_entity_id": note_id,
                    "payload_json": json.dumps(
                        {
                            "targets": [
                                {
                                    "entity_type": "question",
                                    "entity_id": unknown_question_id,
                                }
                            ]
                        }
                    ),
                    "rationale": "The model referenced a question ID not in context.",
                    "confidence": 0.8,
                    "source_refs": [],
                }
            ],
        }
    )

    response = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)

    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["status"] == "failed"
    assert "unknown question ID" in payload["error_metadata"]["message"]


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


def test_commit_resolves_question_parent_refs(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _hierarchy_draft_patch(project_id)
    )
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

    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "Commit question hierarchy"},
        headers=admin_auth_headers,
    )

    assert commit.status_code == 200
    committed = commit.json()["data"]
    parent_id = committed["operations"][0]["result_entity_id"]
    child_id = committed["operations"][1]["result_entity_id"]

    child = client.get(f"/questions/{child_id}", headers=admin_auth_headers)

    assert child.status_code == 200
    assert child.json()["data"]["parent_question_ids"] == [parent_id]


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
        "semantic_type": "suggest_new_dataset",
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
