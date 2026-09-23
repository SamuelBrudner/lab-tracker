"""Record-export scope and supervision-edge loading."""

from __future__ import annotations

from uuid import UUID

import pytest
from api_helpers import repository_backed_api

from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import (
    EntityRef,
    EntityType,
    GoalRelation,
    GoalType,
    QuestionStatus,
    QuestionType,
)
from lab_tracker.services.goal_service import GoalLinkSpec


def _actor() -> AuthContext:
    return AuthContext(user_id=UUID(int=1), role=Role.ADMIN)


def test_goal_artifact_refuses_an_empty_project_scope_instead_of_exporting_unscoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L111: an empty scope means "nothing", never "every project"."""

    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Scoped export", actor=actor)
    question = api.create_question(
        project.project_id,
        "Which records belong to the goal?",
        QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Scoped paper",
        links=[
            GoalLinkSpec(
                target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
                relation=GoalRelation.ADDRESSES,
            )
        ],
        actor=actor,
    )
    monkeypatch.setattr(
        api.record_exports.goals,
        "require_goal_read",
        lambda _goal, *, actor=None: set(),
    )

    with pytest.raises(ValueError, match="at least one project"):
        api.record_exports.export_goal_artifact(
            goal_id=goal.goal_id,
            base_url="http://example.test",
            actor=actor,
        )
