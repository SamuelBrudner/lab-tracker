from __future__ import annotations

from uuid import uuid4

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import Note, Question, QuestionType
from lab_tracker.services.shared import note_matches_substring, question_matches_substring


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def test_direct_substring_matchers_casefold_and_scope_fields():
    project_id = uuid4()
    question = Question(
        question_id=uuid4(),
        project_id=project_id,
        text="Does PV inhibition broaden tuning",
        question_type=QuestionType.DESCRIPTIVE,
        hypothesis="PV inhibition broadens tuning.",
    )
    note = Note(
        note_id=uuid4(),
        project_id=project_id,
        raw_content="Meeting notes: PV inhibition protocol",
        transcribed_text="Follow-up summary mentions gamma changes",
        metadata={"owner": "Sam"},
    )

    assert question_matches_substring(question, "pv")
    assert question_matches_substring(question, "BROADENS")
    assert not question_matches_substring(question, "unrelated")
    assert note_matches_substring(note, "protocol")
    assert note_matches_substring(note, "gamma")
    assert not note_matches_substring(note, "sam")


def test_api_search_uses_direct_substring_matching_and_pagination():
    api = LabTrackerAPI.in_memory()
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
    note = api.create_note(
        project_id=project.project_id,
        raw_content="capture log",
        transcribed_text="Baseline drift was not observed",
        metadata={"owner": "Sam"},
        actor=actor,
    )

    assert api.search_questions("baseline", project_id=project.project_id) == [q1]
    assert api.list_questions(project_id=project.project_id, search="baseline") == [q1]
    assert api.search_notes("baseline", project_id=project.project_id) == [note]
    assert api.search_notes("sam", project_id=project.project_id) == []

    api.update_question(q2.question_id, text="Signal baseline stability", actor=actor)
    results = api.search_questions("baseline", project_id=project.project_id, limit=1, offset=1)
    assert [question.question_id for question in results] == [q2.question_id]
