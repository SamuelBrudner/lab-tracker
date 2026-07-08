from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.auth import AuthContext, PrincipalType, Role, utc_now
from lab_tracker.config import Settings
from lab_tracker.db_models import GraphChangeSetModel
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.graph_drafting import (
    EXTERNAL_HARNESS_LAUNCH_TABLE,
    AgenticGraphDraftClient,
    AnthropicGraphDraftClient,
    GoogleGraphDraftClient,
    GraphDraftBatchResult,
    GraphDraftingError,
    HarnessDraftRequest,
    HarnessDraftRunResult,
    HarnessGraphDraftClient,
    HarnessVendorLaunch,
    OpenAIGraphDraftClient,
    SubprocessHarnessDraftRunner,
    _HarnessStdoutOverflowError,
    _run_bounded_harness_subprocess,
    _sanitized_harness_env,
    make_graph_draft_client,
)
from lab_tracker.models import (
    AcceptanceMode,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts.graph_drafts import operation_to_model


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
        extra_images: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "content_type": image_content_type,
                "draft_mode": draft_mode,
                "graph_context": graph_context,
                "image_bytes": image_bytes,
                "extra_images": extra_images or [],
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

    def draft_from_analysis_evidence(
        self,
        *,
        evidence_text: str,
        project_context: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "evidence_text": evidence_text,
                "project_context": project_context,
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


class _FakeHarnessMCP:
    def __init__(self) -> None:
        self.submitted_patch: dict[str, Any] | None = None

    @property
    def tool_trace(self) -> dict[str, Any]:
        return {
            "provider": "external_harness",
            "tool_call_count": 0,
            "max_tool_calls": 4,
            "tool_calls": [],
            "submit_graph_patch_calls": 1 if self.submitted_patch is not None else 0,
        }

    def tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "submit_graph_patch",
                "description": "Submit final patch.",
                "input_schema": {"type": "object"},
            }
        ]

    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert tool_name == "submit_graph_patch"
        assert arguments is not None
        self.submitted_patch = arguments["graph_patch"]
        return {"accepted": True}


def _harness_subprocess_request(
    *,
    command: tuple[str, ...],
    max_stdout_bytes: int,
    sandbox_profile: str = "operator_managed",
    egress_profile: str = "vendor_api_only",
    operator_command_provided: bool = True,
) -> HarnessDraftRequest:
    return HarnessDraftRequest(
        batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]},
        user_hint=None,
        prompt_text="Draft from one note.",
        instructions="Return a graph patch.",
        graph_patch_schema={"type": "object"},
        launch=HarnessVendorLaunch(
            selector="codex",
            display_name="Codex CLI",
            command=command,
            allowed_env_vars=("OPENAI_API_KEY",),
            egress_hosts=("api.openai.com",),
            native_tool_denies=("shell",),
        ),
        timeout_seconds=30,
        max_tool_calls=4,
        max_stdout_bytes=max_stdout_bytes,
        sandbox_profile=sandbox_profile,
        egress_profile=egress_profile,
        # A caller-supplied command stands in for an operator sandbox wrapper.
        operator_command_provided=operator_command_provided,
    )


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


def test_openai_graph_draft_client_embeds_extra_reviewer_images() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
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

    client.draft_from_note(
        graph_context={"project": {"name": "Context"}},
        source_artifacts=[{"transcript_text": "existing note text"}],
        extra_images=[{"image_bytes": b"reviewer-image", "content_type": "image/png"}],
    )

    content = requests[0]["input"][0]["content"]
    image_items = [item for item in content if item["type"] == "input_image"]
    assert len(image_items) == 1
    assert image_items[0]["image_url"].startswith("data:image/png;base64,")
    client.close()


def test_openai_graph_draft_client_rejects_non_image_extra() -> None:
    client = OpenAIGraphDraftClient(
        api_key="test-key",
        model="gpt-test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    with pytest.raises(GraphDraftingError):
        client.draft_from_note(
            graph_context={},
            source_artifacts=[{"transcript_text": "existing note text"}],
            extra_images=[{"image_bytes": b"data", "content_type": "application/pdf"}],
        )
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


def test_openai_graph_draft_client_drafts_from_analysis_evidence_sends_packet() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "output_text": json.dumps(
                    {
                        "summary": "analysis ok",
                        "uncertain_fields": [],
                        "clarification_requests": [],
                        "operations": [],
                    }
                )
            },
        )

    client = OpenAIGraphDraftClient(
        api_key="test-key",
        model="gpt-analysis-test",
        transport=httpx.MockTransport(handler),
    )

    result = client.draft_from_analysis_evidence(
        evidence_text="method_hash=abc123\nFitted firing rate vs contrast.",
        project_context={"project": {"id": "p1"}},
    )

    assert result["summary"] == "analysis ok"
    request = requests[0]
    assert request["model"] == "gpt-analysis-test"
    assert "analysis evidence" in request["instructions"]
    user_text = request["input"][0]["content"][0]["text"]
    assert "method_hash=abc123" in user_text
    assert "\"id\": \"p1\"" in user_text
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    client.close()


def test_openai_graph_draft_client_analysis_evidence_requires_text() -> None:
    client = OpenAIGraphDraftClient(
        api_key="test-key",
        model="gpt-analysis-test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )

    with pytest.raises(GraphDraftingError, match="evidence is empty"):
        client.draft_from_analysis_evidence(evidence_text="   ", project_context={})
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


def test_anthropic_graph_draft_client_drafts_note_and_batch() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "summary": "anthropic ok",
                                "uncertain_fields": [],
                                "clarification_requests": [],
                                "operations": [],
                            }
                        ),
                    }
                ]
            },
        )

    client = AnthropicGraphDraftClient(
        api_key="anthropic-key",
        model="claude-test",
        transport=httpx.MockTransport(handler),
    )

    note_result = client.draft_from_note(
        graph_context={"project": {"id": "p1"}},
        source_artifacts=[{"type": "text", "raw_content_preview": "fly 12"}],
    )
    batch_result = client.draft_from_batch(
        batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]},
    )

    assert note_result["summary"] == "anthropic ok"
    assert batch_result["summary"] == "anthropic ok"
    assert requests[0]["model"] == "claude-test"
    assert "json" in requests[0]["system"].lower()
    assert "daily batch" in requests[1]["system"]
    with pytest.raises(GraphDraftingError, match="does not support native audio"):
        client.transcribe_audio(
            audio_bytes=b"audio",
            filename="voice.webm",
            content_type="audio/webm",
        )
    client.close()


def test_google_graph_draft_client_drafts_and_transcribes() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/models/gemini-test:generateContent"
        requests.append(json.loads(request.content.decode("utf-8")))
        if len(requests) == 3:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "Fly 12 tracked cleanly."}]}}
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "summary": "google ok",
                                            "uncertain_fields": [],
                                            "clarification_requests": [],
                                            "operations": [],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    client = GoogleGraphDraftClient(
        api_key="google-key",
        model="gemini-test",
        transport=httpx.MockTransport(handler),
    )

    note_result = client.draft_from_note(
        graph_context={"project": {"id": "p1"}},
        source_artifacts=[{"type": "text", "raw_content_preview": "fly 12"}],
    )
    batch_result = client.draft_from_batch(
        batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]},
    )
    transcript = client.transcribe_audio(
        audio_bytes=b"audio",
        filename="voice.webm",
        content_type="audio/webm",
    )

    assert note_result["summary"] == "google ok"
    assert batch_result["summary"] == "google ok"
    assert transcript["text"] == "Fly 12 tracked cleanly."
    assert requests[0]["generationConfig"]["response_mime_type"] == "application/json"
    assert requests[2]["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "audio/webm"
    client.close()


def test_provider_factory_returns_anthropic_and_google_clients() -> None:
    anthropic = make_graph_draft_client(
        Settings(
            environment="local",
            auth_enabled=False,
            graph_draft_provider="anthropic",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-test",
        )
    )
    google = make_graph_draft_client(
        Settings(
            environment="local",
            auth_enabled=False,
            graph_draft_provider="google",
            google_api_key="google-key",
            google_model="gemini-test",
        )
    )
    try:
        assert isinstance(anthropic, AnthropicGraphDraftClient)
        assert anthropic.model == "claude-test"
        assert isinstance(google, GoogleGraphDraftClient)
        assert google.model == "gemini-test"
    finally:
        anthropic.close()
        google.close()


def test_make_graph_draft_client_returns_agentic_with_configured_base_provider() -> None:
    client = make_graph_draft_client(
        Settings(
            environment="local",
            auth_enabled=False,
            graph_draft_provider="agentic",
            graph_draft_agentic_base_provider="anthropic",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-test",
        )
    )
    try:
        assert isinstance(client, AgenticGraphDraftClient)
        assert client.model == "agentic:claude-test"
    finally:
        client.close()


def test_make_graph_draft_client_returns_external_harness_default_off() -> None:
    client = make_graph_draft_client(
        Settings(
            environment="local",
            auth_enabled=False,
            graph_draft_provider="external_harness",
            graph_draft_external_harness="codex",
        )
    )
    assert isinstance(client, HarnessGraphDraftClient)
    assert client.launch.selector == "codex"
    assert client.model == "external-harness:codex"


def test_external_harness_client_runs_injected_runner_through_submit_tool() -> None:
    patch = {
        "summary": "harness ok",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [],
    }

    class FakeExecutor:
        sensitivity_policy = "redact"

        def mcp_tool_specs(self) -> list[dict[str, Any]]:
            return [
                {
                    "name": "search",
                    "description": "Search scoped context.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }
            ]

        def anthropic_tool_specs(self) -> list[dict[str, Any]]:
            return self.mcp_tool_specs()

        def execute(self, tool_name: str, arguments: dict[str, Any] | None = None):
            assert tool_name == "search"
            assert arguments == {"query": "odor"}
            return {"data": [{"note_id": "note-1"}]}

    class FakeRunner:
        def __init__(self) -> None:
            self.request = None

        def run(self, *, request, mcp_server):  # noqa: ANN001
            self.request = request
            assert request.sandbox_profile == "operator_managed"
            assert request.egress_profile == "vendor_api_only"
            assert mcp_server.executor.sensitivity_policy == "omit"
            mcp_server.execute_tool("search", {"query": "odor"})
            mcp_server.execute_tool("submit_graph_patch", {"graph_patch": patch})
            return HarnessDraftRunResult(tool_trace=mcp_server.tool_trace)

    runner = FakeRunner()
    client = HarnessGraphDraftClient(
        launch=EXTERNAL_HARNESS_LAUNCH_TABLE["codex"],
        enabled=True,
        sandbox_profile="operator_managed",
        egress_profile="vendor_api_only",
        timeout_seconds=30,
        max_tool_calls=4,
        max_stdout_bytes=4096,
        runner=runner,
    )
    client.configure_live_read_tools(FakeExecutor())

    result = client.draft_from_batch(
        batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]},
    )

    assert result.graph_patch == patch
    assert result.tool_trace is not None
    assert result.tool_trace["provider"] == "external_harness"
    assert result.tool_trace["tool_call_count"] == 1
    assert result.tool_trace["submit_graph_patch_calls"] == 1
    assert runner.request is not None


def test_external_harness_client_fails_closed_when_disabled() -> None:
    client = HarnessGraphDraftClient(
        launch=EXTERNAL_HARNESS_LAUNCH_TABLE["codex"],
        enabled=False,
        sandbox_profile="operator_managed",
        egress_profile="vendor_api_only",
        timeout_seconds=30,
        max_tool_calls=4,
        max_stdout_bytes=4096,
    )

    with pytest.raises(GraphDraftingError, match="disabled"):
        client.draft_from_batch(
            batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]},
        )


def test_sanitized_harness_env_excludes_lab_tracker_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", "sqlite:///secret.db")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "shh")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("OPENAI_API_KEY", "vendor-only")
    env = _sanitized_harness_env(EXTERNAL_HARNESS_LAUNCH_TABLE["codex"])
    assert not any(key.startswith("LAB_TRACKER_") for key in env)
    assert "DATABASE_URL" not in env
    # the vendor's own API key IS forwarded so the child can reach the model
    assert env.get("OPENAI_API_KEY") == "vendor-only"


def test_external_harness_fails_closed_without_operator_wrapper() -> None:
    # operator_managed isolation is attested but no wrapper command was supplied:
    # refuse rather than spawn the bare vendor binary unisolated.
    request = _harness_subprocess_request(
        command=(sys.executable, "-c", "print('{}')"),
        max_stdout_bytes=4096,
        operator_command_provided=False,
    )
    with pytest.raises(GraphDraftingError, match="no wrapper command is configured"):
        SubprocessHarnessDraftRunner().run(request=request, mcp_server=_FakeHarnessMCP())


def test_external_harness_fails_closed_when_profiles_default() -> None:
    request = _harness_subprocess_request(
        command=(sys.executable, "-c", "print('{}')"),
        max_stdout_bytes=4096,
        sandbox_profile="disabled",
        egress_profile="disabled",
    )
    with pytest.raises(GraphDraftingError, match="sandbox profile is not established"):
        SubprocessHarnessDraftRunner().run(request=request, mcp_server=_FakeHarnessMCP())


def test_run_bounded_harness_subprocess_bounds_stdout() -> None:
    # A harness that floods stdout must be aborted at the cap, not buffered in RAM.
    with pytest.raises(_HarnessStdoutOverflowError):
        _run_bounded_harness_subprocess(
            command=[sys.executable, "-c", "print('x' * 100000)"],
            input_text="",
            cwd=os.getcwd(),
            env=dict(os.environ),
            timeout_seconds=30,
            max_stdout_bytes=1024,
            creationflags=0,
        )


def test_run_bounded_harness_subprocess_kills_on_timeout() -> None:
    # A hung harness must be killed near the deadline, not run for the full sleep.
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_bounded_harness_subprocess(
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            input_text="",
            cwd=os.getcwd(),
            env=dict(os.environ),
            timeout_seconds=1.0,
            max_stdout_bytes=4096,
            creationflags=0,
        )
    assert time.monotonic() - started < 15


class _FakeScopedHarnessExecutor:
    """Scoped-executor stand-in for the end-to-end harness runner test."""

    def __init__(self) -> None:
        self.sensitivity_policy = "redact"
        self.calls: list[str] = []

    def mcp_tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "search",
                "description": "Search scoped context.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        ]

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(name)
        return {"results": [{"note_id": "note-scoped-1"}]}


def _harness_client_with_command(command: tuple[str, ...]) -> HarnessGraphDraftClient:
    client = HarnessGraphDraftClient(
        launch=replace(EXTERNAL_HARNESS_LAUNCH_TABLE["claude_code"], command=command),
        enabled=True,
        sandbox_profile="operator_managed",
        egress_profile="vendor_api_only",
        timeout_seconds=60,
        max_tool_calls=4,
        max_stdout_bytes=65536,
        operator_command_provided=True,
    )
    client.configure_live_read_tools(_FakeScopedHarnessExecutor())
    return client


def test_external_harness_end_to_end_via_loopback_mcp() -> None:
    # Full runtime path: serve the scoped MCP on loopback, launch a stand-in CLI
    # that performs a live scoped read and submits a patch through the propose-only
    # tool, and capture that patch server-side (never trusted from stdout).
    fake_cli = str(pathlib.Path(__file__).parent / "_fake_harness_cli.py")
    client = _harness_client_with_command((sys.executable, fake_cli))
    result = client.draft_from_batch(
        batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]},
    )
    assert result.graph_patch["summary"] == "fake harness proposal"
    assert result.tool_trace["submit_graph_patch_calls"] == 1
    assert result.tool_trace["tool_call_count"] == 1  # one live scoped read


def test_external_harness_fails_closed_when_child_does_not_submit() -> None:
    # A child that exits cleanly without calling submit_graph_patch must not look
    # like a successful (empty) draft.
    client = _harness_client_with_command((sys.executable, "-c", "raise SystemExit(0)"))
    with pytest.raises(GraphDraftingError, match="did not submit a graph patch"):
        client.draft_from_batch(
            batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]},
        )


def test_anthropic_graph_draft_client_runs_bounded_tool_loop() -> None:
    requests: list[dict[str, Any]] = []

    class FakeToolExecutor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def anthropic_tool_specs(self) -> list[dict[str, Any]]:
            return [
                {
                    "name": "search",
                    "description": "Search scoped graph context.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": True,
                    },
                }
            ]

        def execute(self, tool_name: str, arguments: dict[str, Any] | None = None):
            args = dict(arguments or {})
            self.calls.append((tool_name, args))
            return {"data": {"notes": [{"note_id": "note-1"}]}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        payload = json.loads(request.content.decode("utf-8"))
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "search",
                            "input": {"query": "odor", "project_id": "p1"},
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "summary": "tool loop ok",
                                "uncertain_fields": [],
                                "clarification_requests": [],
                                "operations": [],
                            }
                        ),
                    }
                ],
            },
        )

    executor = FakeToolExecutor()
    client = AnthropicGraphDraftClient(
        api_key="anthropic-key",
        model="claude-test",
        transport=httpx.MockTransport(handler),
    )

    result = client.draft_from_batch_with_tools(
        batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]},
        tool_executor=executor,
        max_tool_calls=4,
    )

    assert isinstance(result, GraphDraftBatchResult)
    assert result.graph_patch["summary"] == "tool loop ok"
    assert result.tool_trace is not None
    assert result.tool_trace["tool_call_count"] == 1
    assert result.tool_trace["tool_calls"][0]["tool"] == "search"
    assert result.tool_trace["tool_calls"][0]["result_ids"] == [
        {"field": "note_id", "value": "note-1"}
    ]
    assert executor.calls == [("search", {"query": "odor", "project_id": "p1"})]
    assert len(requests) == 2
    tool_result_turn = requests[1]["messages"][-1]
    assert tool_result_turn["role"] == "user"
    assert "<untrusted_tool_result tool=\"search\">" in tool_result_turn["content"][0][
        "content"
    ][0]["text"]
    client.close()


def test_anthropic_and_google_clients_report_api_errors() -> None:
    anthropic = AnthropicGraphDraftClient(
        api_key="anthropic-key",
        model="claude-test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"error": {"message": "slow down"}})
        ),
    )
    google = GoogleGraphDraftClient(
        api_key="google-key",
        model="gemini-test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": {"message": "boom"}})
        ),
    )
    with pytest.raises(GraphDraftingError, match="slow down"):
        anthropic.draft_from_batch(
            batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]}
        )
    with pytest.raises(GraphDraftingError, match="boom"):
        google.draft_from_batch(
            batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]}
        )
    anthropic.close()
    google.close()


@pytest.mark.parametrize(
    ("client_factory", "provider_name"),
    [
        (
            lambda transport: OpenAIGraphDraftClient(
                api_key="openai-key",
                model="gpt-test",
                transport=transport,
            ),
            "OpenAI",
        ),
        (
            lambda transport: AnthropicGraphDraftClient(
                api_key="anthropic-key",
                model="claude-test",
                transport=transport,
            ),
            "Anthropic",
        ),
        (
            lambda transport: GoogleGraphDraftClient(
                api_key="google-key",
                model="gemini-test",
                transport=transport,
            ),
            "Google",
        ),
    ],
)
def test_graph_draft_clients_wrap_transport_errors(client_factory, provider_name) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("network stalled", request=request)

    draft_client = client_factory(httpx.MockTransport(handler))
    try:
        with pytest.raises(GraphDraftingError, match=f"{provider_name} request failed"):
            draft_client.draft_from_batch(
                batch_context={"mode": "graph_batch", "batch_notes": [{"id": "note-1"}]}
            )
    finally:
        draft_client.close()


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


def _analysis_note(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": (
                "# Analysis run\n\nmethod_hash=abc123 code_version=deadbeef\n"
                "Fitted firing rate vs contrast; slope significant."
            ),
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["note_id"]


def test_analysis_note_draft_stores_operations_and_context(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _analysis_note(client, admin_auth_headers, project_id)
    fake_client = FakeDraftClient(_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        f"/notes/{note_id}/analysis-graph-drafts", headers=admin_auth_headers
    )

    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["status"] == "ready"
    assert payload["draft_mode"] == "graph_context"
    assert payload["prompt_version"] == "analysis-graph-draft-v1"
    assert payload["source_note_id"] == note_id
    assert payload["source_content_type"] == "text/markdown"
    assert payload["context_packet"]["project"]["id"] == project_id
    assert [operation["entity_type"] for operation in payload["operations"]] == [
        "question",
        "note",
    ]
    # The model receives the full note evidence text, not just a preview.
    assert "method_hash=abc123" in fake_client.calls[0]["evidence_text"]
    assert fake_client.calls[0]["project_context"]["project"]["id"] == project_id
    assert fake_client.closed is True


def test_analysis_note_draft_records_model_failure(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _analysis_note(client, admin_auth_headers, project_id)
    fake_client = FakeDraftClient(error="model unavailable")
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        f"/notes/{note_id}/analysis-graph-drafts", headers=admin_auth_headers
    )

    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["status"] == "failed"
    assert payload["error_metadata"]["message"] == "model unavailable"
    assert payload["operations"] == []
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
                    "source_file_last_modified_ms": 1769904000000,
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
    assert context["source_note"]["metadata"]["source_file_name"] == "rig-note.jpg"
    assert (
        context["source_note"]["metadata"]["source_file_last_modified_at"]
        == "2026-02-01T00:00:00+00:00"
    )
    assert context["source_artifacts"][0]["metadata"]["source_file_name"] == "rig-note.jpg"
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


def test_graph_draft_commit_rejects_refs_to_rejected_operations(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )

    draft = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)
    assert draft.status_code == 201
    draft_payload = draft.json()["data"]
    change_set_id = draft_payload["change_set_id"]
    question_op, note_op = draft_payload["operations"]

    rejected = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{question_op['operation_id']}",
        json={"payload": question_op["payload"], "status": "rejected"},
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 200

    accepted = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{note_op['operation_id']}",
        json={"payload": note_op["payload"], "status": "accepted"},
        headers=admin_auth_headers,
    )
    assert accepted.status_code == 200

    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "commit dangling ref"},
        headers=admin_auth_headers,
    )

    assert commit.status_code == 422
    assert "unavailable operation ref(s): q1" in commit.json()["error"]["message"]


def test_graph_draft_commit_rejects_already_claimed_draft(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    draft = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)
    assert draft.status_code == 201
    change_set_id = draft.json()["data"]["change_set_id"]

    with client.app.state.db_session_factory() as session:
        row = session.get(GraphChangeSetModel, change_set_id)
        row.status = "committing"
        session.commit()

    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "commit claimed draft"},
        headers=admin_auth_headers,
    )

    assert commit.status_code == 422
    assert "already being committed" in commit.json()["error"]["message"]


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


def test_graph_draft_list_returns_paged_summaries_without_context_packets(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    draft_ids: list[str] = []
    for _ in range(3):
        note_id = _image_note(client, admin_auth_headers, project_id)
        response = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)
        assert response.status_code == 201
        draft = response.json()["data"]
        assert draft["context_packet"]
        assert len(draft["operations"]) == 2
        draft_ids.append(draft["change_set_id"])

    listed = client.get(
        f"/graph-drafts?project_id={project_id}&limit=1&offset=1",
        headers=admin_auth_headers,
    )

    assert listed.status_code == 200
    page = listed.json()
    assert page["meta"] == {"limit": 1, "offset": 1, "total": 3}
    assert len(page["data"]) == 1
    item = page["data"][0]
    assert item["change_set_id"] in draft_ids
    assert item["operation_count"] == 2
    assert item["source_note_count"] == 1
    assert "context_packet" not in item
    assert "operations" not in item

    detail = client.get(
        f"/graph-drafts/{item['change_set_id']}",
        headers=admin_auth_headers,
    )
    assert detail.status_code == 200
    detail_payload = detail.json()["data"]
    assert detail_payload["context_packet"]
    assert len(detail_payload["operations"]) == 2


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

    question = client.get(f"/questions/{question_id}", headers=admin_auth_headers)
    assert question.status_code == 200
    question_payload = question.json()["data"]
    assert question_payload["origin"] == "ai_suggested"
    assert question_payload["change_set_id"] == change_set_id
    assert question_payload["origin_provider"] == "openai"
    assert question_payload["origin_model"] == "fake-gpt"
    assert question_payload["origin_prompt_version"] == "multimodal-graph-draft-v1"

    notes = client.get(
        f"/notes?project_id={project_id}&target_entity_type=question&target_entity_id={question_id}",
        headers=admin_auth_headers,
    )
    assert notes.status_code == 200
    note_payload = notes.json()["data"][0]
    assert note_payload["targets"][0]["entity_id"] == question_id
    assert note_payload["origin"] == "ai_suggested"
    assert note_payload["change_set_id"] == change_set_id
    assert note_payload["origin_model"] == "fake-gpt"


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


def _revised_draft_patch(project_id: str) -> dict[str, Any]:
    """A revised patch with a single operation (was two), to prove regeneration."""
    return {
        "summary": "Revised per reviewer: keep only the protocol question.",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [_draft_patch(project_id)["operations"][0]],
    }


def test_revise_graph_draft_regenerates_operations_from_feedback(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    initial = FakeDraftClient(_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: initial
    created = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]
    change_set_id = created["change_set_id"]
    assert len(created["operations"]) == 2

    revised_client = FakeDraftClient(_revised_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: revised_client
    feedback = "Drop the note operation; keep only the protocol question."
    response = client.post(
        f"/graph-drafts/{change_set_id}/revise",
        data={"feedback": feedback},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["change_set_id"] == change_set_id
    assert body["status"] == "ready"
    assert len(body["operations"]) == 1
    assert all(op["status"] == "proposed" for op in body["operations"])
    assert body["summary"].startswith("Revised per reviewer")

    # The model was seeded with the feedback AND the prior operations.
    hint = revised_client.calls[0]["user_hint"]
    assert "REVISION REQUEST" in hint
    assert feedback in hint
    assert "Previously proposed operations" in hint
    assert "suggest_new_question" in hint


def test_revise_graph_draft_requires_feedback(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    change_set_id = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]["change_set_id"]

    response = client.post(
        f"/graph-drafts/{change_set_id}/revise",
        data={"feedback": "   "},
        headers=admin_auth_headers,
    )
    assert response.status_code == 422


def test_revise_graph_draft_transcribes_dictated_audio(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    initial = FakeDraftClient(_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: initial
    change_set_id = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]["change_set_id"]

    revised_client = FakeDraftClient(_revised_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: revised_client
    response = client.post(
        f"/graph-drafts/{change_set_id}/revise",
        files={"audio": ("feedback.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    # The dictated audio was transcribed and seeded into the model hint.
    assert len(revised_client.transcription_calls) == 1
    assert revised_client.transcription_calls[0]["content_type"] == "audio/webm"
    hint = revised_client.calls[0]["user_hint"]
    assert "Fly 12 tracked better after pulse onset." in hint


def test_revise_graph_draft_combines_typed_and_dictated_feedback(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    initial = FakeDraftClient(_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: initial
    change_set_id = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]["change_set_id"]

    revised_client = FakeDraftClient(_revised_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: revised_client
    response = client.post(
        f"/graph-drafts/{change_set_id}/revise",
        data={"feedback": "Also drop the dataset link."},
        files={"audio": ("feedback.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    hint = revised_client.calls[0]["user_hint"]
    assert "Also drop the dataset link." in hint
    assert "Fly 12 tracked better after pulse onset." in hint


def test_revise_graph_draft_passes_image_attachment_to_model(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    initial = FakeDraftClient(_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: initial
    change_set_id = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]["change_set_id"]

    revised_client = FakeDraftClient(_revised_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: revised_client
    response = client.post(
        f"/graph-drafts/{change_set_id}/revise",
        data={"feedback": "Use the corrected schematic I attached."},
        files={"attachments": ("schematic.png", b"fake-png-bytes", "image/png")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    extra_images = revised_client.calls[0]["extra_images"]
    assert len(extra_images) == 1
    assert extra_images[0]["content_type"] == "image/png"
    assert extra_images[0]["image_bytes"] == b"fake-png-bytes"
    # The attachment is referenced in the model hint for grounding.
    assert "schematic.png" in revised_client.calls[0]["user_hint"]


def test_revise_graph_draft_image_only_attachment_without_text(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    initial = FakeDraftClient(_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: initial
    change_set_id = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]["change_set_id"]

    revised_client = FakeDraftClient(_revised_draft_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: revised_client
    response = client.post(
        f"/graph-drafts/{change_set_id}/revise",
        files={"attachments": ("schematic.png", b"fake-png-bytes", "image/png")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    assert len(revised_client.calls[0]["extra_images"]) == 1


def test_revise_graph_draft_rejects_non_image_attachment(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    change_set_id = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]["change_set_id"]

    response = client.post(
        f"/graph-drafts/{change_set_id}/revise",
        data={"feedback": "Consider this spreadsheet."},
        files={"attachments": ("data.csv", b"a,b,c", "text/csv")},
        headers=admin_auth_headers,
    )
    assert response.status_code == 422


def test_revise_graph_draft_enforces_cumulative_cap_without_content_length(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    change_set_id = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]["change_set_id"]

    # Each file is under the cap; together they exceed it. Send the multipart
    # body as a generator so the request is chunked (no Content-Length) and the
    # pre-read header guard cannot fire — only the streaming cap can reject it.
    client.app.state.settings.max_upload_bytes = 1024
    boundary = "sizecapboundary"

    def part(name: str, filename: str, content_type: str, payload: bytes) -> bytes:
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
        )
        return head.encode() + payload + b"\r\n"

    body = (
        part("audio", "feedback.webm", "audio/webm", b"a" * 700)
        + part("attachments", "photo.png", "image/png", b"b" * 700)
        + f"--{boundary}--\r\n".encode()
    )

    response = client.post(
        f"/graph-drafts/{change_set_id}/revise",
        content=iter([body]),
        headers={
            **admin_auth_headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    assert response.status_code == 413


def test_revise_graph_draft_keeps_draft_on_model_failure(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    created = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]
    change_set_id = created["change_set_id"]
    assert len(created["operations"]) == 2

    failing = FakeDraftClient(error="model exploded")
    client.app.state.graph_draft_client_factory = lambda settings: failing
    response = client.post(
        f"/graph-drafts/{change_set_id}/revise",
        data={"feedback": "Try again with fewer operations."},
        headers=admin_auth_headers,
    )
    assert response.status_code == 422

    # The existing draft is left intact (operations + ready status preserved).
    after = client.get(
        f"/graph-drafts/{change_set_id}", headers=admin_auth_headers
    ).json()["data"]
    assert len(after["operations"]) == 2
    assert after["status"] == "ready"


def test_per_operation_accept_records_human_selected_mode(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    draft = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)
    change_set_id = draft.json()["data"]["change_set_id"]
    question_op = draft.json()["data"]["operations"][0]

    accepted = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{question_op['operation_id']}",
        json={"payload": question_op["payload"], "status": "accepted"},
        headers=admin_auth_headers,
    )
    assert accepted.status_code == 200
    op = next(
        item
        for item in accepted.json()["data"]["operations"]
        if item["operation_id"] == question_op["operation_id"]
    )
    assert op["acceptance_mode"] == "human_selected"
    assert op["accepted_at"] is not None
    assert op["accepted_by"] is not None


def test_accept_all_records_bulk_accepted_mode(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    draft = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)
    data = draft.json()["data"]
    change_set_id = data["change_set_id"]
    question_op = data["operations"][0]

    # Scrutinize one operation by hand first; it must stay marked as such.
    client.patch(
        f"/graph-drafts/{change_set_id}/operations/{question_op['operation_id']}",
        json={"payload": question_op["payload"], "status": "accepted"},
        headers=admin_auth_headers,
    )

    accepted_all = client.post(
        f"/graph-drafts/{change_set_id}/accept-all",
        headers=admin_auth_headers,
    )
    assert accepted_all.status_code == 200
    modes = {
        item["client_ref"]: item["acceptance_mode"]
        for item in accepted_all.json()["data"]["operations"]
    }
    # The hand-reviewed op keeps its human mark; the rest are honestly bulk-marked.
    assert modes["q1"] == "human_selected"
    assert modes["note1"] == "bulk_accepted"


def test_reopening_operation_clears_acceptance_mark(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _image_note(client, admin_auth_headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    draft = client.post(f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers)
    change_set_id = draft.json()["data"]["change_set_id"]
    question_op = draft.json()["data"]["operations"][0]
    op_url = f"/graph-drafts/{change_set_id}/operations/{question_op['operation_id']}"

    client.patch(
        op_url,
        json={"payload": question_op["payload"], "status": "accepted"},
        headers=admin_auth_headers,
    )
    reopened = client.patch(
        op_url,
        json={"payload": question_op["payload"], "status": "proposed"},
        headers=admin_auth_headers,
    )
    assert reopened.status_code == 200
    op = next(
        item
        for item in reopened.json()["data"]["operations"]
        if item["operation_id"] == question_op["operation_id"]
    )
    assert op["acceptance_mode"] is None
    assert op["accepted_at"] is None


# --- Autonomy guardrails (lab-tracker-09ok.1 / .2): drafting only, never commit ---


def _human_actor() -> AuthContext:
    """An interactive admin principal (the default principal_type is USER)."""
    return AuthContext(user_id=uuid4(), role=Role.ADMIN)


@contextmanager
def _autonomy_request_api(client: TestClient):
    """Yield a request-scoped API bound to a fresh session.

    Lets a test invoke service methods in-process with an arbitrary actor --
    the only way to exercise a SYSTEM (automation) principal, since no bearer
    token ever mints one.
    """
    session = client.app.state.db_session_factory()
    try:
        repository = SQLAlchemyLabTrackerRepository(session)
        yield client.app.state.lab_tracker_api.for_request(repository)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _note_graph_draft(
    client: TestClient, headers: dict[str, str]
) -> tuple[str, dict[str, Any]]:
    """Create a real graph draft over a note; return (change_set_id, first_op)."""
    project_id = _project(client, headers)
    note_id = _image_note(client, headers, project_id)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _draft_patch(project_id)
    )
    draft = client.post(f"/notes/{note_id}/graph-drafts", headers=headers)
    assert draft.status_code == 201
    data = draft.json()["data"]
    return data["change_set_id"], data["operations"][0]


def test_is_interactive_admits_only_human_sessions() -> None:
    user = uuid4()
    for principal in (PrincipalType.USER, PrincipalType.DEVICE):
        actor = AuthContext(user_id=user, role=Role.ADMIN, principal_type=principal)
        assert actor.is_interactive, principal
    for principal in (PrincipalType.SERVICE, PrincipalType.SYSTEM):
        actor = AuthContext(user_id=user, role=Role.ADMIN, principal_type=principal)
        assert not actor.is_interactive, principal


def _service_actor() -> AuthContext:
    """A writable admin service-token principal (an lpat_ token maps to this)."""
    return AuthContext(user_id=uuid4(), role=Role.ADMIN, principal_type=PrincipalType.SERVICE)


def test_service_actor_cannot_commit_graph_change_set(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    """A delegated service token is not a person operating the review gate."""
    change_set_id, _ = _note_graph_draft(client, admin_auth_headers)
    with _autonomy_request_api(client) as api, pytest.raises(AuthError):
        api.commit_graph_change_set(
            UUID(change_set_id), message="token commit", actor=_service_actor()
        )


def test_writable_service_token_cannot_accept_or_commit_graph_draft(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    """Regression for the review finding: a writable admin lpat_ token may read and
    draft, but must never operate the human review gate (accept/commit)."""
    change_set_id, _ = _note_graph_draft(client, admin_auth_headers)
    issued = client.post(
        "/auth/tokens",
        json={
            "label": "automation",
            "role": "admin",
            "read_only": False,
            "expires_at": (utc_now() + timedelta(days=7)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert issued.status_code == 201, issued.text
    token = {"Authorization": f"Bearer {issued.json()['data']['secret']}"}
    # The token is valid and may read the graph...
    assert client.get("/projects", headers=token).status_code == 200
    # ...but the accept and commit gates reject it.
    assert (
        client.post(f"/graph-drafts/{change_set_id}/accept-all", headers=token).status_code
        == 401
    )
    assert (
        client.post(
            f"/graph-drafts/{change_set_id}/commit",
            json={"message": "token commit"},
            headers=token,
        ).status_code
        == 401
    )


def test_system_actor_is_admin_but_not_interactive(client: TestClient) -> None:
    """Automation may DRAFT (admin-gated) yet never accept/commit (interactive-gated)."""
    authz = client.app.state.lab_tracker_api.project_authorization
    system = system_auth_context()
    assert authz.has_global_admin(system) is True
    authz.require_interactive(_human_actor(), action="Committing")
    with pytest.raises(AuthError):
        authz.require_interactive(system, action="Committing")


def test_system_actor_cannot_commit_graph_change_set(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    change_set_id, _ = _note_graph_draft(client, admin_auth_headers)
    with _autonomy_request_api(client) as api, pytest.raises(AuthError):
        api.commit_graph_change_set(
            UUID(change_set_id), message="auto-commit", actor=system_auth_context()
        )


def test_system_actor_cannot_bulk_accept_operations(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    change_set_id, _ = _note_graph_draft(client, admin_auth_headers)
    with _autonomy_request_api(client) as api, pytest.raises(AuthError):
        api.bulk_accept_graph_change_operations(
            UUID(change_set_id), actor=system_auth_context()
        )


def test_system_actor_cannot_accept_single_operation(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    change_set_id, operation = _note_graph_draft(client, admin_auth_headers)
    with _autonomy_request_api(client) as api, pytest.raises(AuthError):
        api.update_graph_change_operation(
            UUID(change_set_id),
            UUID(operation["operation_id"]),
            payload=operation["payload"],
            status=GraphChangeOperationStatus.ACCEPTED,
            actor=system_auth_context(),
        )


def test_human_owner_can_still_commit_after_automation_is_blocked(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    """The wall blocks automation without blocking the human review path."""
    change_set_id, _ = _note_graph_draft(client, admin_auth_headers)
    accept_all = client.post(
        f"/graph-drafts/{change_set_id}/accept-all", headers=admin_auth_headers
    )
    assert accept_all.status_code == 200
    with _autonomy_request_api(client) as api, pytest.raises(AuthError):
        api.commit_graph_change_set(
            UUID(change_set_id), message="auto", actor=system_auth_context()
        )
    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "human commit"},
        headers=admin_auth_headers,
    )
    assert commit.status_code == 200


def test_update_operation_rejects_auto_accepted_mode(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    change_set_id, operation = _note_graph_draft(client, admin_auth_headers)
    with _autonomy_request_api(client) as api, pytest.raises(ValidationError):
        api.update_graph_change_operation(
            UUID(change_set_id),
            UUID(operation["operation_id"]),
            payload=operation["payload"],
            status=GraphChangeOperationStatus.ACCEPTED,
            acceptance_mode=AcceptanceMode.AUTO_ACCEPTED,
            actor=_human_actor(),
        )


def test_repository_write_rejects_auto_accepted_operation() -> None:
    """Defense in depth: the persistence layer refuses the reserved mode."""
    operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=0,
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.QUESTION,
        status=GraphChangeOperationStatus.ACCEPTED,
        acceptance_mode=AcceptanceMode.AUTO_ACCEPTED,
    )
    with pytest.raises(ValidationError):
        operation_to_model(operation)


# --- Origin honesty: user_revised only when a human actually edited the op ---


def _link_note_update_draft(note_id: str, question_id: str) -> dict[str, Any]:
    """A one-operation draft that UPDATES a note to target an existing question."""
    return {
        "summary": "link the note to the question",
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
                    {"targets": [{"entity_type": "question", "entity_id": question_id}]}
                ),
                "rationale": "The note is about this question.",
                "confidence": 0.9,
                "source_refs": [],
            }
        ],
    }


def _project_note_question(
    client: TestClient, headers: dict[str, str]
) -> tuple[str, str, str]:
    project_id = _project(client, headers)
    note_id = _image_note(client, headers, project_id)
    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the protocol improve yield?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert question.status_code == 201, question.text
    return project_id, note_id, question.json()["data"]["question_id"]


def test_unedited_update_commits_ai_suggested_origin(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    """A bulk-accepted (unedited) UPDATE is an AI suggestion, not a human revision.

    Regression: the applier used to stamp every UPDATE user_revised regardless of
    edits, which made PROV-O export fabricate a prov:wasRevisionOf edge for a
    rubber-stamped operation.
    """
    _project_id, note_id, question_id = _project_note_question(client, admin_auth_headers)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _link_note_update_draft(note_id, question_id)
    )
    change_set_id = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]["change_set_id"]
    assert (
        client.post(
            f"/graph-drafts/{change_set_id}/accept-all", headers=admin_auth_headers
        ).status_code
        == 200
    )
    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "link note"},
        headers=admin_auth_headers,
    )
    assert commit.status_code == 200, commit.text
    note = client.get(f"/notes/{note_id}", headers=admin_auth_headers)
    assert note.status_code == 200
    assert note.json()["data"]["origin"] == "ai_suggested"


def test_edited_update_commits_user_revised_origin(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    """An UPDATE whose payload a reviewer actually edited is a genuine revision."""
    _project_id, note_id, question_id = _project_note_question(client, admin_auth_headers)
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(
        _link_note_update_draft(note_id, question_id)
    )
    draft = client.post(
        f"/notes/{note_id}/graph-drafts", headers=admin_auth_headers
    ).json()["data"]
    change_set_id = draft["change_set_id"]
    operation = draft["operations"][0]
    # Edit the payload so it differs from the AI's proposal (records edited_at).
    edited_payload = {
        "targets": [{"entity_type": "question", "entity_id": question_id}],
        "metadata": {"reviewed": True},
    }
    patch = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{operation['operation_id']}",
        json={"payload": edited_payload, "status": "accepted"},
        headers=admin_auth_headers,
    )
    assert patch.status_code == 200, patch.text
    commit = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "link note"},
        headers=admin_auth_headers,
    )
    assert commit.status_code == 200, commit.text
    note = client.get(f"/notes/{note_id}", headers=admin_auth_headers)
    assert note.status_code == 200
    assert note.json()["data"]["origin"] == "user_revised"
