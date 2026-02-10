from __future__ import annotations

from uuid import uuid4

import pytest

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import Note, Question, QuestionType
from lab_tracker.services.search_backends import (
    ChromaDBSearchBackend,
    SearchQuery,
    _TokenHashEmbeddingFunction,
)


pytest.importorskip("chromadb")


def test_chromadb_search_backend_upsert_search_delete(tmp_path):
    backend = ChromaDBSearchBackend(
        persist_path=tmp_path / "chroma",
        embedding_function=_TokenHashEmbeddingFunction(),
    )
    project_id = uuid4()
    other_project_id = uuid4()

    q1 = Question(
        question_id=uuid4(),
        project_id=project_id,
        text="baseline",
        question_type=QuestionType.DESCRIPTIVE,
    )
    q2 = Question(
        question_id=uuid4(),
        project_id=project_id,
        text="baseline signal",
        question_type=QuestionType.DESCRIPTIVE,
    )
    other = Question(
        question_id=uuid4(),
        project_id=other_project_id,
        text="baseline",
        question_type=QuestionType.DESCRIPTIVE,
    )
    note = Note(
        note_id=uuid4(),
        project_id=project_id,
        raw_content="Meeting notes: PV inhibition protocol",
        metadata={"owner": "Sam"},
    )

    backend.upsert_questions([q1, q2, other])
    backend.upsert_notes([note])

    # Project filtering.
    question_hits = backend.search_question_ids(
        SearchQuery(query="baseline", project_id=project_id)
    )
    assert question_hits[:2] == [q1.question_id, q2.question_id]

    # Allowed-id filtering.
    restricted = backend.search_question_ids(
        SearchQuery(query="baseline", project_id=project_id),
        question_ids=[q2.question_id],
    )
    assert restricted == [q2.question_id]

    # Notes should search over raw content + metadata values.
    assert backend.search_note_ids(SearchQuery(query="sam", project_id=project_id)) == [
        note.note_id
    ]

    backend.delete_questions([q1.question_id])
    remaining = backend.search_question_ids(SearchQuery(query="baseline", project_id=project_id))
    assert q1.question_id not in remaining


def test_api_auto_indexes_chromadb_backend_on_create_update_delete(tmp_path):
    backend = ChromaDBSearchBackend(
        persist_path=tmp_path / "chroma",
        embedding_function=_TokenHashEmbeddingFunction(),
    )
    api = LabTrackerAPI.in_memory(search_backend=backend)
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project = api.create_project("Search index project", actor=actor)

    question = api.create_question(
        project_id=project.project_id,
        text="alpha",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    assert backend.search_question_ids(
        SearchQuery(query="alpha", project_id=project.project_id)
    ) == [question.question_id]

    api.update_question(question.question_id, text="beta", actor=actor)
    assert backend.search_question_ids(
        SearchQuery(query="alpha", project_id=project.project_id)
    ) == []
    assert backend.search_question_ids(
        SearchQuery(query="beta", project_id=project.project_id)
    ) == [question.question_id]

    api.delete_question(question.question_id, actor=actor)
    assert backend.search_question_ids(
        SearchQuery(query="beta", project_id=project.project_id)
    ) == []

    note = api.create_note(
        project_id=project.project_id,
        raw_content="gamma",
        metadata={"owner": "sam"},
        actor=actor,
    )
    assert backend.search_note_ids(SearchQuery(query="gamma", project_id=project.project_id)) == [
        note.note_id
    ]

    api.update_note(note.note_id, transcribed_text="delta", actor=actor)
    assert backend.search_note_ids(SearchQuery(query="delta", project_id=project.project_id)) == [
        note.note_id
    ]

    api.delete_note(note.note_id, actor=actor)
    assert backend.search_note_ids(SearchQuery(query="delta", project_id=project.project_id)) == []
