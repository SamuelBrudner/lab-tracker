"""Embedding providers for semantic/vector search backends.

The core API defaults to simple substring search today, but vector search backends
(e.g. ChromaDB) need a way to generate embeddings.

Configuration is intentionally environment-driven:
- Set LAB_TRACKER_EMBEDDING_PROVIDER to "openai" or "sentence_transformers"
- If unset / "chroma_default", let ChromaDB handle embeddings with its default embedder.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import importlib
import json
import os
import urllib.error
import urllib.request
from typing import Any, Iterable, Sequence


DEFAULT_EMBEDDING_PROVIDER = "chroma_default"


class EmbeddingProvider(ABC):
    """Generate embeddings for a batch of texts."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Stable identifier for the provider implementation."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return a vector for each input text, preserving input order."""

    def name(self) -> str:
        return self.backend_name

    def embed_query(self, input: Sequence[str]) -> list[list[float]]:  # noqa: A002
        return self.__call__(input)

    @classmethod
    def build_from_config(cls, config: dict[str, Any]) -> "EmbeddingProvider":
        return cls(**config)

    def get_config(self) -> dict[str, Any]:
        return {}

    def is_legacy(self) -> bool:
        return False

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine", "l2", "ip"]

    def validate_config_update(
        self,
        old_config: dict[str, Any],
        new_config: dict[str, Any],
    ) -> None:
        return

    @staticmethod
    def validate_config(config: dict[str, Any]) -> None:
        return

    def __call__(self, input: Sequence[str]) -> list[list[float]]:  # noqa: A002
        """ChromaDB-style embedding function compatibility."""

        return self.embed(list(input))


class SentenceTransformerEmbeddingProvider(EmbeddingProvider):
    """Local embedding provider backed by sentence-transformers."""

    backend_name = "sentence_transformers"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name
        self._model: Any | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._get_model()
        embeddings = model.encode(texts)
        return _as_float_vectors(embeddings)

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            st = importlib.import_module("sentence_transformers")
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "sentence-transformers is required for the 'sentence_transformers' embedding "
                "provider. Install with `pip install 'lab-tracker[embeddings]'`."
            ) from exc
        self._model = st.SentenceTransformer(self._model_name)
        return self._model

    def get_config(self) -> dict[str, Any]:
        return {"model_name": self._model_name}


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Embedding provider that calls the OpenAI embeddings API."""

    backend_name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "text-embedding-3-small",
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
        batch_size: int = 96,
    ) -> None:
        resolved_key = (
            api_key
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("LAB_TRACKER_OPENAI_API_KEY")
        )
        if not resolved_key:
            raise ValueError(
                "OpenAI embeddings require an API key. Set OPENAI_API_KEY "
                "or LAB_TRACKER_OPENAI_API_KEY."
            )
        self._api_key = resolved_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._batch_size = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._batch_size <= 0:
            raise ValueError("batch_size must be > 0.")
        embeddings: list[list[float]] = []
        for batch in _chunk(texts, self._batch_size):
            embeddings.extend(self._embed_batch(batch))
        return embeddings

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        url = f"{self._base_url}/embeddings"
        payload = json.dumps({"model": self._model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method="POST")
        request.add_header("Authorization", f"Bearer {self._api_key}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"OpenAI embeddings request failed ({exc.code} {exc.reason}): {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI embeddings request failed: {exc.reason}") from exc
        data = json.loads(body)
        items = data.get("data")
        if not isinstance(items, list):
            raise RuntimeError("Unexpected OpenAI embeddings response: missing 'data' list.")
        ordered = sorted(items, key=lambda item: int(item.get("index", 0)))
        embeddings: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding")
            if not isinstance(vector, list):
                raise RuntimeError(
                    "Unexpected OpenAI embeddings response: missing embedding vector."
                )
            embeddings.append([float(value) for value in vector])
        if len(embeddings) != len(texts):
            raise RuntimeError(
                "Unexpected OpenAI embeddings response: embedding count does not match input."
            )
        return embeddings

    def get_config(self) -> dict[str, Any]:
        return {
            "model": self._model,
            "base_url": self._base_url,
            "timeout_seconds": self._timeout_seconds,
            "batch_size": self._batch_size,
        }


def resolve_embedding_provider(provider_name: str | None) -> EmbeddingProvider | None:
    """Resolve a provider from a configured name.

    Returns None when the caller should defer to ChromaDB's default embedder.
    """

    normalized = (provider_name or "").strip().casefold()
    if normalized in {"", "default", "chroma", "chroma_default"}:
        return None
    if normalized in {"sentence_transformers", "sentence-transformer", "sentence-transformers"}:
        return SentenceTransformerEmbeddingProvider()
    if normalized == "openai":
        return OpenAIEmbeddingProvider()
    raise ValueError(f"Unknown embedding provider: {provider_name!r}")


def _chunk(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _as_float_vectors(embeddings: Any) -> list[list[float]]:
    """Convert common embedding container types into `list[list[float]]`."""

    if embeddings is None:
        return []

    # sentence-transformers typically returns numpy arrays; avoid importing numpy explicitly.
    if hasattr(embeddings, "tolist"):
        embeddings = embeddings.tolist()

    if isinstance(embeddings, list):
        if not embeddings:
            return []
        if isinstance(embeddings[0], (int, float)):
            return [[float(value) for value in embeddings]]
        return [[float(value) for value in row] for row in embeddings]

    # Fall back to treating it as a generic sequence of sequences.
    try:
        return [[float(value) for value in row] for row in embeddings]
    except TypeError as exc:
        raise TypeError(f"Unsupported embedding container type: {type(embeddings)!r}") from exc
