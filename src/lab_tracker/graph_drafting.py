"""GPT-backed graph draft generation."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from lab_tracker.config import Settings

PROMPT_VERSION = "image-graph-draft-v1"
PROVIDER = "openai"


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
            "operations": {"type": "array", "items": operation_schema},
        },
        "additionalProperties": False,
        "required": ["summary", "operations"],
    }


class OpenAIGraphDraftClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key.strip()
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenAIGraphDraftClient":
        return cls(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
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
        project_context: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._api_key:
            raise GraphDraftingError(
                "LAB_TRACKER_OPENAI_API_KEY must be set before drafting graph changes."
            )
        if not image_bytes:
            raise GraphDraftingError("Source image is empty.")
        image_url = _data_url(image_bytes=image_bytes, content_type=content_type)
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
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Draft Lab Tracker graph updates from this image. "
                                    "Use this current project context:\n"
                                    f"{json.dumps(project_context, sort_keys=True)}"
                                ),
                            },
                            {"type": "input_image", "image_url": image_url},
                        ],
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


def _instructions() -> str:
    return (
        "You convert lab notebook or whiteboard images into proposed Lab Tracker graph "
        "changes. Propose only changes that are supported by the image and context. "
        "Use create or update operations for project, question, note, session, dataset, "
        "analysis, claim, or visualization entities. Use payload_json as a JSON object "
        "string matching the existing Lab Tracker API request shape. For created objects "
        "that later operations should reference, set client_ref to a short stable name "
        "and use {\"$ref\":\"name\"} inside later payload_json fields. Never claim a "
        "canonical update happened; these are drafts for human review."
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
