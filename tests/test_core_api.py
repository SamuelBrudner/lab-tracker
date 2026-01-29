from uuid import uuid4

import pytest

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, AuthService, Role
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.models import QuestionLinkRole, QuestionSource, QuestionStatus, QuestionType, SessionType


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def test_project_question_dataset_flow():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="What is the baseline distribution?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    dataset = api.create_dataset(
        project_id=project.project_id,
        commit_hash="abc123",
        primary_question_id=question.question_id,
        actor=actor,
    )
    assert dataset.primary_question_id == question.question_id
    assert any(link.role == QuestionLinkRole.PRIMARY for link in dataset.question_links)


def test_dataset_requires_primary_question():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    with pytest.raises(ValidationError):
        api.create_dataset(
            project_id=project.project_id,
            commit_hash="abc123",
            primary_question_id=None,  # type: ignore[arg-type]
            actor=actor,
        )


def test_auth_service_register_and_authenticate():
    service = AuthService()
    user = service.register_user("sam", "secret", Role.ADMIN)
    authenticated = service.authenticate("sam", "secret")
    assert authenticated.user_id == user.user_id


def test_role_required_for_writes():
    api = LabTrackerAPI()
    viewer = _actor(Role.VIEWER)
    with pytest.raises(AuthError):
        api.create_project("Nope", actor=viewer)


def test_scientific_session_requires_question():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    with pytest.raises(ValidationError):
        api.create_session(
            project_id=project.project_id,
            session_type=SessionType.SCIENTIFIC,
            actor=actor,
        )


def test_extract_questions_from_note_stages_questions():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    note = api.create_note(
        project_id=project.project_id,
        raw_content=(
            "Q: Does PV inhibition broaden tuning\n"
            "- Can we see layer-specific effects?\n"
            "Question: What is the baseline distribution\n"
            "Notes: check controls"
        ),
        actor=actor,
    )

    questions = api.extract_questions_from_note(note.note_id, actor=actor)

    assert {question.text for question in questions} == {
        "Does PV inhibition broaden tuning",
        "Can we see layer-specific effects?",
        "What is the baseline distribution",
    }
    assert all(question.status == QuestionStatus.STAGED for question in questions)
    assert all(question.created_from == QuestionSource.API for question in questions)
    assert all(question.created_by and str(note.note_id) in question.created_by for question in questions)
    assert api.extract_questions_from_note(note.note_id, actor=actor) == []
