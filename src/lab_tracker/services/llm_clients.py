"""LLM client interfaces.

The project uses LLMs for tasks like question extraction. This module isolates the
HTTP/API details behind a small interface so other backends (local models, hosted
providers) can be added later without changing business logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
import os
import urllib.error
import urllib.request
from typing import Any


class LLMClient(ABC):
    """Minimal chat-style LLM client."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Stable identifier for the LLM provider implementation."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = 900,
    ) -> str:
        """Return the assistant content for the provided messages."""


class OpenAIChatCompletionsClient(LLMClient):
    """OpenAI Chat Completions API client.

    Uses the REST API directly via ``urllib`` to avoid adding a hard dependency on
    the OpenAI SDK.
    """

    backend_name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
    ) -> None:
        resolved_key = (
            api_key or os.getenv("OPENAI_API_KEY") or os.getenv("LAB_TRACKER_OPENAI_API_KEY")
        )
        if not resolved_key:
            raise ValueError(
                "OpenAI chat completions require an API key. "
                "Set OPENAI_API_KEY or LAB_TRACKER_OPENAI_API_KEY."
            )
        self._api_key = resolved_key
        self._model = (model or "").strip() or "gpt-4o-mini"
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = 900,
    ) -> str:
        url = f"{self._base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": float(temperature),
        }
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Authorization", f"Bearer {self._api_key}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"OpenAI chat completion failed ({exc.code} {exc.reason}): {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI chat completion failed: {exc.reason}") from exc

        data = json.loads(raw)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("Unexpected OpenAI chat completion response: missing 'choices'.")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError("Unexpected OpenAI chat completion response: missing 'message'.")
        content = message.get("content")
        if not isinstance(content, str):
            raise RuntimeError("Unexpected OpenAI chat completion response: missing 'content'.")
        return content
