from __future__ import annotations

import json

import pytest

from lab_tracker.services.embedding_providers import (
    OpenAIEmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
    resolve_embedding_provider,
)


def test_resolve_embedding_provider_defaults_to_chroma():
    assert resolve_embedding_provider(None) is None
    assert resolve_embedding_provider("") is None
    assert resolve_embedding_provider("default") is None
    assert resolve_embedding_provider("chroma") is None
    assert resolve_embedding_provider("chroma_default") is None


def test_resolve_embedding_provider_unknown_raises():
    with pytest.raises(ValueError, match="Unknown embedding provider"):
        resolve_embedding_provider("does-not-exist")


def test_sentence_transformer_provider_embeds_with_optional_dependency(monkeypatch):
    calls: list[str] = []

    class FakeModel:
        def __init__(self, model_name: str) -> None:
            calls.append(model_name)

        def encode(self, texts: list[str]):
            return [[len(text)] for text in texts]

    class FakeSentenceTransformersModule:
        SentenceTransformer = FakeModel

    import lab_tracker.services.embedding_providers as module

    monkeypatch.setattr(
        module.importlib,
        "import_module",
        lambda name: FakeSentenceTransformersModule if name == "sentence_transformers" else None,
    )

    provider = SentenceTransformerEmbeddingProvider()
    assert provider.embed(["abc", ""]) == [[3.0], [0.0]]
    assert calls == ["all-MiniLM-L6-v2"]


def test_openai_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LAB_TRACKER_OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        OpenAIEmbeddingProvider()


def test_openai_provider_parses_response_and_preserves_order(monkeypatch):
    import lab_tracker.services.embedding_providers as module

    returned = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [2.0, 3.0]},
        ]
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(returned).encode("utf-8")

    def fake_urlopen(request, timeout):  # noqa: ANN001
        headers = {k.lower(): v for k, v in request.header_items()}
        assert request.get_method() == "POST"
        assert headers["authorization"] == "Bearer test-key"
        assert headers["content-type"] == "application/json"
        assert timeout == 30.0
        return FakeResponse()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    provider = OpenAIEmbeddingProvider(api_key="test-key")
    assert provider.embed(["first", "second"]) == [[2.0, 3.0], [0.0, 1.0]]

