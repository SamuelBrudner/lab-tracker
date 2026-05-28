import pytest

from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    QuestionStatus,
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
