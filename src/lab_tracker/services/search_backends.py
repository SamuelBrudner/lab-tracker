"""Search backends for questions and notes.

The default implementation performs in-memory casefold substring matching. The interface
is intentionally designed to support semantic/vector search backends (e.g. ChromaDB)
while keeping a safe local fallback for tests and development.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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
        needle = (query.query or "").casefold().strip()
        if not needle:
            return []
        allowed = set(question_ids) if question_ids is not None else None
        matches: list[UUID] = []
        for doc in self._questions.values():
            if query.project_id is not None and doc.project_id != query.project_id:
                continue
            if allowed is not None and doc.question_id not in allowed:
                continue
            hypothesis = doc.hypothesis
            if needle in doc.text.casefold() or (
                hypothesis is not None and needle in hypothesis.casefold()
            ):
                matches.append(doc.question_id)
        return _slice(matches, limit=query.limit, offset=query.offset)

    def search_note_ids(
        self,
        query: SearchQuery,
        *,
        note_ids: Iterable[UUID] | None = None,
    ) -> list[UUID]:
        needle = (query.query or "").casefold().strip()
        if not needle:
            return []
        allowed = set(note_ids) if note_ids is not None else None
        matches: list[UUID] = []
        for doc in self._notes.values():
            if query.project_id is not None and doc.project_id != query.project_id:
                continue
            if allowed is not None and doc.note_id not in allowed:
                continue
            if needle in doc.raw_content.casefold():
                matches.append(doc.note_id)
                continue
            if doc.transcribed_text and needle in doc.transcribed_text.casefold():
                matches.append(doc.note_id)
                continue
            if any(needle in value.casefold() for value in doc.metadata.values()):
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
