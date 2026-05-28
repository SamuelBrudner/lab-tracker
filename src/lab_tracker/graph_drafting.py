"""Graph draft generation clients and provider factory.

The ``GraphDraftClient`` protocol defines the surface every model provider
implements; ``make_graph_draft_client`` picks the active implementation
from ``settings.graph_draft_provider``. The existing OpenAI-backed client
is the default. Anthropic and Google variants are tracked as separate
beads (lab-tracker-dvt, lab-tracker-z66) and slot in by registering a new
branch in the factory.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Protocol, runtime_checkable

import httpx

from lab_tracker.config import Settings

PROMPT_VERSION = "multimodal-graph-draft-v1"
BATCH_PROMPT_VERSION = "daily-batch-graph-draft-v1"
PROVIDER = "openai"

SEMANTIC_TYPES = [
    "create_entity",
    "update_entity",
    "create_note",
    "link_note_to_question",
    "link_note_to_session",
    "link_note_to_dataset",
    "link_note_to_analysis",
    "suggest_new_question",
    "suggest_new_dataset",
    "suggest_followup",
    "request_clarification",
]


class GraphDraftingError(RuntimeError):
    """Raised when GPT graph drafting cannot produce a usable patch."""


def graph_patch_response_schema() -> dict[str, Any]:
    region_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "x": {"type": "number"},
            "y": {"type": "number"},
            "width": {"type": "number"},
            "height": {"type": "number"},
        },
        "additionalProperties": False,
        "required": ["x", "y", "width", "height"],
    }
    source_ref_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "quote": {"type": "string"},
            "region": {"anyOf": [region_schema, {"type": "null"}]},
        },
        "additionalProperties": False,
        "required": ["label", "quote", "region"],
    }
    operation_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "client_ref": {"type": ["string", "null"]},
            "op": {"type": "string", "enum": ["create", "update"]},
            "entity_type": {
                "type": "string",
                "enum": [
                    "project",
                    "question",
                    "dataset",
                    "note",
                    "session",
                    "analysis",
                    "claim",
                    "visualization",
                ],
            },
            "semantic_type": {"type": "string", "enum": SEMANTIC_TYPES},
            "target_entity_id": {"type": ["string", "null"]},
            "payload_json": {
                "type": "string",
                "description": (
                    "A JSON object string containing the API payload for this operation."
                ),
            },
            "rationale": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "source_refs": {"type": "array", "items": source_ref_schema},
        },
        "additionalProperties": False,
        "required": [
            "client_ref",
            "op",
            "entity_type",
            "semantic_type",
            "target_entity_id",
            "payload_json",
            "rationale",
            "confidence",
            "source_refs",
        ],
    }
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "uncertain_fields": {"type": "array", "items": {"type": "string"}},
            "clarification_requests": {"type": "array", "items": {"type": "string"}},
            "operations": {"type": "array", "items": operation_schema},
        },
        "additionalProperties": False,
        "required": ["summary", "uncertain_fields", "clarification_requests", "operations"],
    }


@runtime_checkable
class GraphDraftClient(Protocol):
    """Provider-agnostic surface for graph draft generation.

    Implementations: OpenAI (this file). Anthropic and Google are tracked
    as separate beads. ``transcribe_audio`` is optional on providers that
    do not natively expose transcription; if so, the implementation should
    raise ``GraphDraftingError`` with a clear message so callers fall back
    to a configured transcription provider.
    """

    def draft_from_note(
        self,
        *,
        graph_context: dict[str, Any] | None = ...,
        user_hint: str | None = ...,
        draft_mode: str = ...,
        project_context: dict[str, Any] | None = ...,
        source_artifacts: list[dict[str, Any]] | None = ...,
        image_bytes: bytes | None = ...,
        image_content_type: str | None = ...,
    ) -> dict[str, Any]:
        ...

    def draft_from_batch(
        self,
        *,
        batch_context: dict[str, Any],
        user_hint: str | None = ...,
    ) -> dict[str, Any]:
        ...

    def transcribe_audio(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        prompt: str | None = ...,
    ) -> dict[str, Any]:
        ...

    def close(self) -> None:
        ...


class OpenAIGraphDraftClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        transcription_model: str = "gpt-4o-mini-transcribe",
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.transcription_model = transcription_model
        self._api_key = api_key.strip()
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAIGraphDraftClient:
        return cls(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            transcription_model=settings.openai_transcription_model,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.openai_timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

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
        return self.draft_from_note(
            image_bytes=image_bytes,
            image_content_type=content_type,
            graph_context=graph_context,
            user_hint=user_hint,
            draft_mode=draft_mode,
            project_context=project_context,
            source_artifacts=[
                {
                    "type": "image",
                    "content_type": content_type,
                    "label": "source image",
                }
            ],
        )

    def draft_from_note(
        self,
        *,
        graph_context: dict[str, Any] | None = None,
        user_hint: str | None = None,
        draft_mode: str = "graph_context",
        project_context: dict[str, Any] | None = None,
        source_artifacts: list[dict[str, Any]] | None = None,
        image_bytes: bytes | None = None,
        image_content_type: str | None = None,
    ) -> dict[str, Any]:
        if not self._api_key:
            raise GraphDraftingError(
                "LAB_TRACKER_OPENAI_API_KEY must be set before drafting graph changes."
            )
        resolved_context = graph_context if graph_context is not None else project_context or {}
        artifacts = list(source_artifacts or [])
        has_text_source = any(
            str(item.get("transcript_text") or item.get("raw_content_preview") or "").strip()
            for item in artifacts
        )
        if not image_bytes and not has_text_source:
            raise GraphDraftingError("Source note has no image or transcript text to draft from.")
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    "Draft Lab Tracker graph updates from these source artifact(s).\n"
                    f"Draft mode: {draft_mode}\n"
                    f"User hint: {user_hint or '(none)'}\n"
                    "Source artifacts:\n"
                    f"{json.dumps(artifacts, sort_keys=True)}\n"
                    "Graph context packet:\n"
                    f"{json.dumps(resolved_context, sort_keys=True)}"
                ),
            }
        ]
        if image_bytes:
            if not image_content_type:
                raise GraphDraftingError("Source image content type is required.")
            image_url = _data_url(image_bytes=image_bytes, content_type=image_content_type)
            content.append({"type": "input_image", "image_url": image_url})
        response = self._client.post(
            "/responses",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "instructions": _instructions(),
                "input": [
                    {
                        "role": "user",
                        "content": content,
                    }
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "lab_tracker_graph_patch",
                        "schema": graph_patch_response_schema(),
                        "strict": True,
                    }
                },
            },
        )
        if response.status_code >= 400:
            raise GraphDraftingError(_response_error(response))
        payload = _response_json(response)
        output_text = _extract_output_text(payload)
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise GraphDraftingError("GPT returned malformed graph patch JSON.") from exc
        if not isinstance(parsed, dict):
            raise GraphDraftingError("GPT returned a non-object graph patch.")
        operations = parsed.get("operations")
        if not isinstance(operations, list):
            raise GraphDraftingError("GPT graph patch did not include an operations list.")
        return parsed

    def draft_from_batch(
        self,
        *,
        batch_context: dict[str, Any],
        user_hint: str | None = None,
    ) -> dict[str, Any]:
        if not self._api_key:
            raise GraphDraftingError(
                "LAB_TRACKER_OPENAI_API_KEY must be set before drafting batch graph changes."
            )
        batch_notes = batch_context.get("batch_notes") or []
        if not batch_notes:
            raise GraphDraftingError("Batch context contains no notes to draft from.")
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    "Draft Lab Tracker graph updates for the staged notes in this batch.\n"
                    f"Batch size: {len(batch_notes)} notes\n"
                    f"User hint: {user_hint or '(none)'}\n"
                    "Batch context packet:\n"
                    f"{json.dumps(batch_context, sort_keys=True)}"
                ),
            }
        ]
        response = self._client.post(
            "/responses",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "instructions": _batch_instructions(),
                "input": [{"role": "user", "content": content}],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "lab_tracker_graph_patch",
                        "schema": graph_patch_response_schema(),
                        "strict": True,
                    }
                },
            },
        )
        if response.status_code >= 400:
            raise GraphDraftingError(_response_error(response))
        payload = _response_json(response)
        output_text = _extract_output_text(payload)
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise GraphDraftingError("GPT returned malformed graph patch JSON.") from exc
        if not isinstance(parsed, dict):
            raise GraphDraftingError("GPT returned a non-object graph patch.")
        operations = parsed.get("operations")
        if not isinstance(operations, list):
            raise GraphDraftingError("GPT graph patch did not include an operations list.")
        return parsed

    def transcribe_audio(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        if not self._api_key:
            raise GraphDraftingError(
                "LAB_TRACKER_OPENAI_API_KEY must be set before transcribing voice notes."
            )
        if not audio_bytes:
            raise GraphDraftingError("Source audio is empty.")
        data: dict[str, str] = {
            "model": self.transcription_model,
            "response_format": "json",
        }
        if prompt and prompt.strip():
            data["prompt"] = prompt.strip()
        response = self._client.post(
            "/audio/transcriptions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            data=data,
            files={"file": (filename, audio_bytes, content_type)},
        )
        if response.status_code >= 400:
            raise GraphDraftingError(_response_error(response))
        payload = _response_json(response)
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise GraphDraftingError("Transcription response did not include text.")
        return payload


def make_graph_draft_client(settings: Settings) -> GraphDraftClient:
    """Return the active graph-draft client for ``settings.graph_draft_provider``.

    Raises ``GraphDraftingError`` for unknown providers so misconfiguration
    fails fast at app startup rather than at first model call.
    """
    provider = (settings.graph_draft_provider or "openai").strip().lower()
    if provider == "openai":
        return OpenAIGraphDraftClient.from_settings(settings)
    raise GraphDraftingError(
        f"Unknown graph_draft_provider '{provider}'. Configured providers: openai."
    )


def _batch_instructions() -> str:
    return _instructions() + (
        "\n\nThe input is a daily batch of staged notes the user has already "
        "captured, grouped per project. Treat the batch as a whole: propose "
        "linkages between notes and existing questions/sessions/datasets where "
        "the evidence supports it; when several notes describe the same "
        "observation, propose a single consolidated note rather than duplicates; "
        "and surface ambiguities via uncertain_fields or clarification_requests "
        "rather than guessing. Every operation is a draft for human review; "
        "nothing commits without explicit acceptance."
    )


def _instructions() -> str:
    return (
        "You convert lab notebook photos, whiteboard images, voice-note transcripts, "
        "and photo plus voice bundles into proposed Lab Tracker graph changes. Propose "
        "only changes that are supported by the source artifacts and context. "
        "Distinguish what was transcribed from what you infer. "
        "Use the graph context to resolve ambiguous references. Prefer linking to "
        "existing entities by their provided IDs over creating duplicates. Do not invent "
        "IDs. If an alias says a question was superseded, prefer the replacement question "
        "ID for new work unless the user explicitly asks for historical provenance. "
        "Do not link new notes, sessions, datasets, or analyses to superseded questions "
        "when a replacement is provided. If the context is insufficient, mark uncertainty "
        "or request clarification. "
        "Use create or update operations for project, question, note, session, dataset, "
        "analysis, claim, or visualization entities. Use payload_json as a JSON object "
        "string matching the existing Lab Tracker API request shape. For questions, prefer "
        "small atomic experimental, method, control, or analysis questions linked under "
        "broader motivating questions with parent_question_ids. If the image supports a "
        "new broad question and child question, create the parent first with a client_ref "
        "such as \"parent_question\", then set the child payload's parent_question_ids to "
        "[{\"$ref\":\"parent_question\"}]. For created objects that later operations "
        "should reference, set client_ref to a short stable name and use {\"$ref\":\"name\"} "
        "inside later payload_json fields. Set semantic_type to the closest specific "
        "allowed semantic operation label. Use create_entity only for generic create "
        "operations and update_entity only for generic update operations when no narrower "
        "semantic label fits. Never claim a canonical update happened; these are drafts "
        "for human review. Preserve uploaded image and audio notes as provenance sources "
        "and return uncertainty explicitly."
    )


def _data_url(*, image_bytes: bytes, content_type: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise GraphDraftingError("OpenAI returned non-JSON content.") from exc
    if not isinstance(payload, dict):
        raise GraphDraftingError("OpenAI returned a non-object response.")
    return payload


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"OpenAI returned HTTP {response.status_code}: {response.text}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
    return f"OpenAI returned HTTP {response.status_code}: {payload}"


def _extract_output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for item in payload.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "refusal":
                raise GraphDraftingError(str(content.get("refusal") or "GPT refused the request."))
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return text
    raise GraphDraftingError("OpenAI response did not include graph patch text.")
