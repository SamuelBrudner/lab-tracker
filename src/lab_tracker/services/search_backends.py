"""Search backends for questions and notes.

The default implementation performs in-memory casefold substring matching. The interface
is intentionally designed to support semantic/vector search backends (e.g. ChromaDB)
while keeping a safe local fallback for tests and development.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from typing import Iterable
from uuid import UUID

from lab_tracker.models import Note, Question


@dataclass(frozen=True)
class SearchQuery:
    query: str
    project_id: UUID | None = None
    limit: int | None = None
    offset: int = 0


class SearchBackend(ABC):
    """Index and search questions/notes."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Stable identifier for the backend implementation."""

    @abstractmethod
    def upsert_questions(self, questions: Iterable[Question]) -> None:
        """Insert or update questions in the index."""

    @abstractmethod
    def upsert_notes(self, notes: Iterable[Note]) -> None:
        """Insert or update notes in the index."""

    @abstractmethod
    def delete_questions(self, question_ids: Iterable[UUID]) -> None:
        """Remove questions from the index."""

    @abstractmethod
    def delete_notes(self, note_ids: Iterable[UUID]) -> None:
        """Remove notes from the index."""

    @abstractmethod
    def search_question_ids(
        self,
        query: SearchQuery,
        *,
        question_ids: Iterable[UUID] | None = None,
    ) -> list[UUID]:
        """Return matching question ids ordered by backend-defined relevance."""

    @abstractmethod
    def search_note_ids(
        self,
        query: SearchQuery,
        *,
        note_ids: Iterable[UUID] | None = None,
    ) -> list[UUID]:
        """Return matching note ids ordered by backend-defined relevance."""


def _normalized_query(query: str) -> str:
    return (query or "").casefold().strip()


@dataclass(frozen=True)
class _QuestionDoc:
    question_id: UUID
    project_id: UUID
    text: str
    hypothesis: str | None


@dataclass(frozen=True)
class _NoteDoc:
    note_id: UUID
    project_id: UUID
    raw_content: str
    transcribed_text: str | None
    metadata: dict[str, str]


def question_matches_substring(
    question: Question | _QuestionDoc,
    query: str,
) -> bool:
    needle = _normalized_query(query)
    if not needle:
        return False
    hypothesis = question.hypothesis
    return needle in question.text.casefold() or (
        hypothesis is not None and needle in hypothesis.casefold()
    )


def note_matches_substring(
    note: Note | _NoteDoc,
    query: str,
) -> bool:
    needle = _normalized_query(query)
    if not needle:
        return False
    if needle in note.raw_content.casefold():
        return True
    if note.transcribed_text and needle in note.transcribed_text.casefold():
        return True
    return any(needle in str(value).casefold() for value in note.metadata.values())


class InMemorySubstringSearchBackend(SearchBackend):
    """In-memory fallback backend using casefold substring matching."""

    backend_name = "in_memory_substring"

    def __init__(self) -> None:
        self._questions: dict[UUID, _QuestionDoc] = {}
        self._notes: dict[UUID, _NoteDoc] = {}

    def upsert_questions(self, questions: Iterable[Question]) -> None:
        for question in questions:
            self._questions[question.question_id] = _QuestionDoc(
                question_id=question.question_id,
                project_id=question.project_id,
                text=question.text,
                hypothesis=question.hypothesis,
            )

    def upsert_notes(self, notes: Iterable[Note]) -> None:
        for note in notes:
            self._notes[note.note_id] = _NoteDoc(
                note_id=note.note_id,
                project_id=note.project_id,
                raw_content=note.raw_content,
                transcribed_text=note.transcribed_text,
                metadata=dict(note.metadata),
            )

    def delete_questions(self, question_ids: Iterable[UUID]) -> None:
        for question_id in question_ids:
            self._questions.pop(question_id, None)

    def delete_notes(self, note_ids: Iterable[UUID]) -> None:
        for note_id in note_ids:
            self._notes.pop(note_id, None)

    def search_question_ids(
        self,
        query: SearchQuery,
        *,
        question_ids: Iterable[UUID] | None = None,
    ) -> list[UUID]:
        if not _normalized_query(query.query):
            return []
        allowed = set(question_ids) if question_ids is not None else None
        matches: list[UUID] = []
        for doc in self._questions.values():
            if query.project_id is not None and doc.project_id != query.project_id:
                continue
            if allowed is not None and doc.question_id not in allowed:
                continue
            if question_matches_substring(doc, query.query):
                matches.append(doc.question_id)
        return _slice(matches, limit=query.limit, offset=query.offset)

    def search_note_ids(
        self,
        query: SearchQuery,
        *,
        note_ids: Iterable[UUID] | None = None,
    ) -> list[UUID]:
        if not _normalized_query(query.query):
            return []
        allowed = set(note_ids) if note_ids is not None else None
        matches: list[UUID] = []
        for doc in self._notes.values():
            if query.project_id is not None and doc.project_id != query.project_id:
                continue
            if allowed is not None and doc.note_id not in allowed:
                continue
            if note_matches_substring(doc, query.query):
                matches.append(doc.note_id)
        return _slice(matches, limit=query.limit, offset=query.offset)


def _slice(values: list[UUID], *, limit: int | None, offset: int) -> list[UUID]:
    if offset < 0:
        offset = 0
    if limit is None:
        return values[offset:]
    if limit <= 0:
        return []
    return values[offset : offset + limit]


class _TokenHashEmbeddingFunction:
    """A simple embedding function with no external model/runtime dependencies.

    This is intentionally basic: hashed bag-of-words with L2 normalization.
    It exists to keep the ChromaDB backend usable in offline/local contexts.
    """

    def __init__(self, *, dimension: int = 256) -> None:
        if dimension <= 0:
            raise ValueError("dimension must be positive.")
        self._dimension = dimension
        self._token_re = re.compile(r"[A-Za-z0-9_]+")

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return [self._embed(text) for text in input]

    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return self.__call__(input)

    @staticmethod
    def name() -> str:
        return "token_hash"

    @staticmethod
    def build_from_config(config: dict[str, object]) -> "_TokenHashEmbeddingFunction":
        dimension = int(config.get("dimension", 256))
        return _TokenHashEmbeddingFunction(dimension=dimension)

    def get_config(self) -> dict[str, object]:
        return {"dimension": self._dimension}

    def is_legacy(self) -> bool:
        return False

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine", "l2", "ip"]

    def validate_config_update(
        self,
        old_config: dict[str, object],
        new_config: dict[str, object],
    ) -> None:
        return

    @staticmethod
    def validate_config(config: dict[str, object]) -> None:
        return

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimension
        tokens = self._token_re.findall((text or "").casefold())
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest, "big") % self._dimension
            vector[idx] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            inv_norm = 1.0 / norm
            vector = [value * inv_norm for value in vector]
        return vector


class ChromaDBSearchBackend(SearchBackend):
    """Persistent ChromaDB backend using collections: 'questions' and 'notes'."""

    backend_name = "chromadb"

    def __init__(
        self,
        *,
        persist_path: str | Path = ".lab-tracker/chromadb",
        embedding_function: object | None = None,
    ) -> None:
        try:
            import chromadb  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                "chromadb is not installed. "
                "Install it (pip install chromadb) or use InMemorySubstringSearchBackend."
            ) from exc
        resolved_path = Path(persist_path)
        resolved_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(resolved_path))
        if embedding_function is None:
            self._questions = self._client.get_or_create_collection(
                name="questions",
                metadata={"hnsw:space": "cosine"},
            )
            self._notes = self._client.get_or_create_collection(
                name="notes",
                metadata={"hnsw:space": "cosine"},
            )
        else:
            self._questions = self._client.get_or_create_collection(
                name="questions",
                embedding_function=embedding_function,
                metadata={"hnsw:space": "cosine"},
            )
            self._notes = self._client.get_or_create_collection(
                name="notes",
                embedding_function=embedding_function,
                metadata={"hnsw:space": "cosine"},
            )

    def upsert_questions(self, questions: Iterable[Question]) -> None:
        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, str]] = []
        for question in questions:
            ids.append(str(question.question_id))
            documents.append(_format_question_document(question))
            metadatas.append(
                {
                    "entity_id": str(question.question_id),
                    "project_id": str(question.project_id),
                    "status": question.status.value,
                }
            )
        if ids:
            self._questions.upsert(ids=ids, documents=documents, metadatas=metadatas)

    def upsert_notes(self, notes: Iterable[Note]) -> None:
        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, str]] = []
        for note in notes:
            ids.append(str(note.note_id))
            documents.append(_format_note_document(note))
            metadatas.append(
                {
                    "entity_id": str(note.note_id),
                    "project_id": str(note.project_id),
                    "status": note.status.value,
                }
            )
        if ids:
            self._notes.upsert(ids=ids, documents=documents, metadatas=metadatas)

    def delete_questions(self, question_ids: Iterable[UUID]) -> None:
        ids = [str(question_id) for question_id in question_ids]
        if ids:
            self._questions.delete(ids=ids)

    def delete_notes(self, note_ids: Iterable[UUID]) -> None:
        ids = [str(note_id) for note_id in note_ids]
        if ids:
            self._notes.delete(ids=ids)

    def search_question_ids(
        self,
        query: SearchQuery,
        *,
        question_ids: Iterable[UUID] | None = None,
    ) -> list[UUID]:
        return self._search_ids(self._questions, query, allowed_ids=question_ids)

    def search_note_ids(
        self,
        query: SearchQuery,
        *,
        note_ids: Iterable[UUID] | None = None,
    ) -> list[UUID]:
        return self._search_ids(self._notes, query, allowed_ids=note_ids)

    def _search_ids(
        self,
        collection: object,
        query: SearchQuery,
        *,
        allowed_ids: Iterable[UUID] | None,
    ) -> list[UUID]:
        needle = (query.query or "").strip()
        if not needle:
            return []
        if query.limit is not None and query.limit <= 0:
            return []
        resolved_offset = max(0, query.offset)
        allowed_list: list[str] | None = None
        if allowed_ids is not None:
            allowed_list = sorted({str(item) for item in allowed_ids})
            if not allowed_list:
                return []
        clauses: list[dict[str, object]] = []
        if query.project_id is not None:
            clauses.append({"project_id": str(query.project_id)})
        if allowed_list is not None:
            clauses.append({"entity_id": {"$in": allowed_list}})
        if not clauses:
            resolved_where = None
        elif len(clauses) == 1:
            resolved_where = clauses[0]
        else:
            resolved_where = {"$and": clauses}

        match_count = self._count_matches(collection, resolved_where, allowed_list)
        if match_count <= 0:
            return []
        requested = (
            match_count if query.limit is None else resolved_offset + (query.limit or 0)
        )
        n_results = min(match_count, max(0, requested))
        if n_results <= 0:
            return []
        query_kwargs: dict[str, object] = {
            "query_texts": [needle],
            "n_results": n_results,
        }
        if resolved_where is not None:
            query_kwargs["where"] = resolved_where
        try:
            result = collection.query(include=["distances"], **query_kwargs)  # type: ignore[attr-defined]
        except TypeError:
            result = collection.query(**query_kwargs)  # type: ignore[attr-defined]
        raw_ids = (result or {}).get("ids") or []
        raw_distances = (result or {}).get("distances") or []
        # Chroma returns a list of results per input query.
        hit_ids = raw_ids[0] if raw_ids else []
        hit_distances = raw_distances[0] if raw_distances else []
        if not hit_distances:
            filtered = list(hit_ids)
        else:
            # For cosine distance, 1.0 corresponds to no similarity (cosine similarity 0).
            pairs = zip(hit_ids, hit_distances, strict=False)
            filtered = [
                item for item, distance in pairs if distance is None or distance < 1.0
            ]
        resolved = [UUID(item) for item in filtered]
        return _slice(resolved, limit=query.limit, offset=query.offset)

    def _count_matches(
        self,
        collection: object,
        where: dict[str, object] | None,
        allowed_list: list[str] | None,
    ) -> int:
        if where is None:
            return int(collection.count())  # type: ignore[attr-defined]
        try:
            payload = collection.get(where=where, include=[])  # type: ignore[attr-defined]
        except Exception:
            payload = collection.get(where=where)  # type: ignore[attr-defined]
        ids = (payload or {}).get("ids") or []
        return len(ids)


def _format_question_document(question: Question) -> str:
    parts = [question.text]
    if question.hypothesis:
        parts.append(f"Hypothesis: {question.hypothesis}")
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _format_note_document(note: Note) -> str:
    parts = [note.raw_content]
    if note.transcribed_text:
        parts.append(note.transcribed_text)
    if note.metadata:
        items = "\n".join(f"{key}: {value}" for key, value in sorted(note.metadata.items()))
        parts.append(f"Metadata:\n{items}")
    rendered = "\n".join(part.strip() for part in parts if part and part.strip())
    return rendered or " "  # Some embedding functions expect a non-empty document.
