"""Search backend factory.

The service layer depends only on the SearchBackend interface. This module wires
runtime configuration (Settings / env vars) to a concrete backend implementation.
"""

from __future__ import annotations

from lab_tracker.config import Settings
from lab_tracker.services.embedding_providers import resolve_embedding_provider
from lab_tracker.services.search_backends import (
    ChromaDBSearchBackend,
    InMemorySubstringSearchBackend,
    SearchBackend,
)


def build_search_backend(settings: Settings) -> SearchBackend:
    """Instantiate the configured SearchBackend implementation."""

    backend = (settings.search_backend or "").strip().casefold()
    if backend in {"", "default"}:
        backend = "in_memory_substring"

    if backend in {"in_memory_substring", "in-memory-substring", "substring", "memory"}:
        return InMemorySubstringSearchBackend()

    if backend in {"chromadb", "chroma", "chroma_db"}:
        embedding_function = resolve_embedding_provider(settings.embedding_provider)
        return ChromaDBSearchBackend(
            persist_path=settings.chromadb_persist_path,
            embedding_function=embedding_function,
        )

    raise ValueError(
        f"Unknown search backend: {settings.search_backend!r}. "
        "Expected 'in_memory_substring' or 'chromadb'."
    )

