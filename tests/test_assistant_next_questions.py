"""Pure-function tests for the next-questions ranking payload."""

from __future__ import annotations

import pytest

from lab_tracker.assistant_next_questions import build_next_questions_payload


def _goal() -> dict[str, object]:
    return {
        "goal_id": "goal-1",
        "project_id": "project-1",
        "title": "Submit odor paper",
        "status": "planned",
        "links": [
            {"entity_type": "question", "entity_id": "question-1", "link_status": "committed"}
        ],
    }


def _question() -> dict[str, object]:
    return {
        "question_id": "question-1",
        "project_id": "project-1",
        "text": "Which control supports the odor claim?",
        "status": "active",
    }


@pytest.mark.parametrize("claim_status", ["proposed", "testing", "rejected"])
def test_unsupported_claims_do_not_answer_a_question(claim_status: str) -> None:
    claims = [
        {
            "claim_id": "claim-1",
            "status": claim_status,
            "answers_question_ids": ["question-1"],
        }
    ]

    payload = build_next_questions_payload([_goal()], [_question()], claims)

    assert [item["question"]["question_id"] for item in payload["data"]] == ["question-1"]


def test_supported_claim_answers_a_question() -> None:
    claims = [
        {
            "claim_id": "claim-1",
            "status": "supported",
            "answers_question_ids": ["question-1"],
        }
    ]

    payload = build_next_questions_payload([_goal()], [_question()], claims)

    assert payload["data"] == []
    assert "unanswered" in payload["meta"]["empty_reason"]
