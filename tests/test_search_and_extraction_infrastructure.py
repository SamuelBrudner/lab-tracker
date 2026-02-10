from __future__ import annotations

from uuid import uuid4

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import Note, Question, QuestionStatus, QuestionType
from lab_tracker.services.extraction_backends import (
    QuestionCandidate,
    QuestionExtractionBackend,
    RegexQuestionExtractionBackend,
)
from lab_tracker.services.search_backends import (
    InMemorySubstringSearchBackend,
    SearchQuery,
)


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def test_regex_question_extraction_backend_extracts_candidates():
    backend = RegexQuestionExtractionBackend()
    note = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content=(
            "Q: Does PV inhibition broaden tuning\n"
            "- Can we see layer-specific effects?\n"
            "Question: What is the baseline distribution\n"
            "Notes: check controls"
        ),
    )

    candidates = backend.extract_questions(note)

    assert {candidate.text for candidate in candidates} == {
        "Does PV inhibition broaden tuning",
        "Can we see layer-specific effects?",
        "What is the baseline distribution",
    }
    assert all(0.0 <= candidate.confidence <= 1.0 for candidate in candidates)


def test_extract_questions_from_note_uses_pluggable_backend():
    class StubBackend(QuestionExtractionBackend):
        backend_name = "stub"

        def extract_questions(
            self, note: Note, *, raw_asset_bytes: bytes | None = None
        ) -> list[QuestionCandidate]:
            assert note.note_id is not None
            assert raw_asset_bytes is None
            return [
                QuestionCandidate(text="What is A?", confidence=0.9),
                QuestionCandidate(text="What is A?", confidence=0.8),
                QuestionCandidate(text="What is B?  ", confidence=0.7),
            ]

    api = LabTrackerAPI.in_memory(question_extraction_backend=StubBackend())
    actor = _actor()
    project = api.create_project("Search project", actor=actor)
    note = api.create_note(
        project_id=project.project_id,
        raw_content="not used by stub backend",
        actor=actor,
    )

    staged = api.extract_questions_from_note(note.note_id, actor=actor)

    assert [question.text for question in staged] == ["What is A?", "What is B?"]
    assert all(question.status == QuestionStatus.STAGED for question in staged)


def test_in_memory_search_backend_casefold_substring_matching():
    backend = InMemorySubstringSearchBackend()
    project_id = uuid4()
    other_project_id = uuid4()
    question = Question(
        question_id=uuid4(),
        project_id=project_id,
        text="Does PV inhibition broaden tuning",
        question_type=QuestionType.DESCRIPTIVE,
        hypothesis="PV inhibition broadens tuning.",
    )
    other = Question(
        question_id=uuid4(),
        project_id=other_project_id,
        text="Unrelated question",
        question_type=QuestionType.DESCRIPTIVE,
    )
    note = Note(
        note_id=uuid4(),
        project_id=project_id,
        raw_content="Meeting notes: PV inhibition protocol",
        metadata={"owner": "Sam"},
    )
    backend.upsert_questions([question, other])
    backend.upsert_notes([note])

    assert backend.search_question_ids(SearchQuery(query="pv")) == [question.question_id]
    assert backend.search_question_ids(SearchQuery(query="PV", project_id=project_id)) == [
        question.question_id
    ]
    assert backend.search_note_ids(SearchQuery(query="protocol")) == [note.note_id]
    assert backend.search_note_ids(SearchQuery(query="sam")) == [note.note_id]


def test_api_search_and_list_questions_delegate_to_backend():
    backend = InMemorySubstringSearchBackend()
    api = LabTrackerAPI.in_memory(search_backend=backend)
    actor = _actor()
    project = api.create_project("Project", actor=actor)
    q1 = api.create_question(
        project_id=project.project_id,
        text="What is the baseline distribution?",
        hypothesis="Baseline differs by condition",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    q2 = api.create_question(
        project_id=project.project_id,
        text="How stable is the signal?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )

    assert api.search_questions("baseline", project_id=project.project_id) == [q1]
    assert api.list_questions(project_id=project.project_id, search="baseline") == [q1]

    api.update_question(q2.question_id, text="Signal baseline stability", actor=actor)
    results = api.list_questions(project_id=project.project_id, search="baseline")
    assert [question.question_id for question in results] == [q1.question_id, q2.question_id]
