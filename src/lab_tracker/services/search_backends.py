"""Search backends for questions and notes."""

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
