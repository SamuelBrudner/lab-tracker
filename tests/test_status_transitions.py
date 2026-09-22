from uuid import uuid4

import pytest
from api_helpers import repository_backed_api

from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    QuestionStatus,
    QuestionType,
    SessionStatus,
)
from lab_tracker.services.shared import (
    _ensure_analysis_status_transition,
    _ensure_claim_status_transition,
    _ensure_dataset_status_transition,
    _ensure_question_status_transition,
    _ensure_session_status_transition,
)


@pytest.mark.parametrize(
    ("ensure_transition", "current_status", "next_status"),
    [
        (
            _ensure_question_status_transition,
            QuestionStatus.STAGED,
            QuestionStatus.ACTIVE,
        ),
        (
            _ensure_dataset_status_transition,
            DatasetStatus.STAGED,
            DatasetStatus.COMMITTED,
        ),
        (
            _ensure_session_status_transition,
            SessionStatus.ACTIVE,
            SessionStatus.CLOSED,
        ),
        (
            _ensure_analysis_status_transition,
            AnalysisStatus.STAGED,
            AnalysisStatus.COMMITTED,
        ),
        (
            _ensure_claim_status_transition,
            ClaimStatus.PROPOSED,
            ClaimStatus.TESTING,
        ),
        (
            _ensure_claim_status_transition,
            ClaimStatus.TESTING,
            ClaimStatus.SUPPORTED,
        ),
    ],
)
def test_status_transition_wrappers_allow_existing_valid_transitions(
    ensure_transition,
    current_status,
    next_status,
):
    ensure_transition(current_status, next_status)


@pytest.mark.parametrize(
    ("ensure_transition", "current_status", "next_status", "message"),
    [
        (
            _ensure_question_status_transition,
            QuestionStatus.ACTIVE,
            QuestionStatus.STAGED,
            "Question status cannot transition from active to staged.",
        ),
        (
            _ensure_dataset_status_transition,
            DatasetStatus.ARCHIVED,
            DatasetStatus.COMMITTED,
            "Dataset status cannot transition from archived to committed.",
        ),
        (
            _ensure_session_status_transition,
            SessionStatus.CLOSED,
            SessionStatus.ACTIVE,
            "Session status cannot transition from closed to active.",
        ),
        (
            _ensure_analysis_status_transition,
            AnalysisStatus.ARCHIVED,
            AnalysisStatus.COMMITTED,
            "Analysis status cannot transition from archived to committed.",
        ),
        (
            _ensure_claim_status_transition,
            ClaimStatus.SUPPORTED,
            ClaimStatus.REJECTED,
            "Claim status cannot transition from supported to rejected.",
        ),
    ],
)
def test_status_transition_wrappers_preserve_invalid_transition_messages(
    ensure_transition,
    current_status,
    next_status,
    message,
):
    with pytest.raises(ValidationError, match=message):
        ensure_transition(current_status, next_status)


# --- M69: questions reach superseded only through the refactor command --------


def _question_api():
    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project = api.create_project("Question invariants", actor=actor)
    return api, actor, project.project_id


@pytest.mark.parametrize("current_status", [QuestionStatus.STAGED, QuestionStatus.ACTIVE])
def test_plain_status_update_cannot_supersede_a_question(current_status):
    api, actor, project_id = _question_api()
    question = api.create_question(
        project_id=project_id,
        text="Does a plain PATCH supersede?",
        question_type=QuestionType.DESCRIPTIVE,
        status=current_status,
        actor=actor,
    )

    with pytest.raises(ValidationError, match="refactor"):
        api.update_question(
            question.question_id,
            status=QuestionStatus.SUPERSEDED,
            actor=actor,
        )

    stored = api.get_question(question.question_id)
    assert stored.status == current_status
    assert stored.superseded_by_question_id is None


@pytest.mark.parametrize(
    ("status", "terminal_reason", "message"),
    [
        (QuestionStatus.SUPERSEDED, None, "refactor"),
        (
            QuestionStatus.ANSWERED,
            None,
            "Question status cannot transition from staged to answered.",
        ),
    ],
)
def test_question_create_runs_the_transition_table_from_staged(
    status,
    terminal_reason,
    message,
):
    api, actor, project_id = _question_api()

    with pytest.raises(ValidationError, match=message):
        api.create_question(
            project_id=project_id,
            text="Can I start already finished?",
            question_type=QuestionType.DESCRIPTIVE,
            status=status,
            terminal_reason=terminal_reason,
            actor=actor,
        )
    assert api.list_questions(project_id=project_id) == []


@pytest.mark.parametrize(
    ("status", "terminal_reason"),
    [
        (QuestionStatus.STAGED, None),
        (QuestionStatus.ACTIVE, None),
        (QuestionStatus.ABANDONED, "Out of scope for this grant."),
    ],
)
def test_question_create_accepts_statuses_reachable_from_staged(status, terminal_reason):
    api, actor, project_id = _question_api()

    question = api.create_question(
        project_id=project_id,
        text="Which statuses can a new question start in?",
        question_type=QuestionType.DESCRIPTIVE,
        status=status,
        terminal_reason=terminal_reason,
        actor=actor,
    )

    assert question.status == status


def test_http_question_supersede_via_patch_or_create_is_rejected(
    client,
    admin_auth_headers,
):
    project = client.post("/projects", json={"name": "HTTP invariants"}, headers=admin_auth_headers)
    assert project.status_code == 201
    project_id = project.json()["data"]["project_id"]

    created = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Superseded from birth?",
            "question_type": "descriptive",
            "status": "superseded",
        },
        headers=admin_auth_headers,
    )
    assert created.status_code == 422
    answered = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Answered from birth?",
            "question_type": "descriptive",
            "status": "answered",
        },
        headers=admin_auth_headers,
    )
    assert answered.status_code == 422

    active = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Active question",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    )
    assert active.status_code == 201
    question_id = active.json()["data"]["question_id"]
    patched = client.patch(
        f"/questions/{question_id}",
        json={"status": "superseded"},
        headers=admin_auth_headers,
    )
    assert patched.status_code == 422
    assert "refactor" in patched.json()["error"]["message"]
    stored = client.get(f"/questions/{question_id}", headers=admin_auth_headers)
    assert stored.json()["data"]["status"] == "active"
    assert stored.json()["data"]["superseded_by_question_id"] is None
