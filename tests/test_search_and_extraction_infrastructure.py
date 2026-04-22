from __future__ import annotations

from uuid import uuid4

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import Note, Question, QuestionType
from lab_tracker.services.search_backends import (
    InMemorySubstringSearchBackend,
    SearchQuery,
)


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)
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
