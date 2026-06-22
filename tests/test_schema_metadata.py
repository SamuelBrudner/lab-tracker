from __future__ import annotations

import pytest

from lab_tracker.decision_context_constants import TASK_KIND_VALUES
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    ClaimStatus,
    ExplorationNodeStatus,
    ExplorationNodeType,
    GoalLinkStatus,
    GoalRelation,
    GoalType,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
)
from lab_tracker.schema_metadata import build_schema_description


def test_schema_description_exposes_required_fields_and_enum_values() -> None:
    payload = build_schema_description()

    question = payload["entities"]["question"]
    assert question["create"]["required_fields"] == [
        "project_id",
        "text",
        "question_type",
    ]
    assert question["create"]["fields"]["question_type"]["controlled_values"][
        "allowed_values"
    ] == [question_type.value for question_type in QuestionType]
    assert question["status"]["allowed_values"] == [
        status.value for status in QuestionStatus
    ]

    session = payload["entities"]["session"]
    assert session["create"]["fields"]["session_type"]["controlled_values"][
        "allowed_values"
    ] == [session_type.value for session_type in SessionType]
    assert session["status_lifecycle"][SessionStatus.ACTIVE.value] == [
        SessionStatus.ACTIVE.value,
        SessionStatus.CLOSED.value,
    ]


def test_schema_description_exposes_task_kinds_goal_values_and_free_viz_type() -> None:
    payload = build_schema_description(entity_type="goal")

    assert payload["assistant"]["decision_context"]["task_kind"]["allowed_values"] == list(
        TASK_KIND_VALUES
    )
    assert list(payload["entities"]) == ["goal"]
    assert payload["shared_enums"]["goal_type"]["allowed_values"] == [
        goal_type.value for goal_type in GoalType
    ]
    assert payload["shared_enums"]["goal_relation"]["allowed_values"] == [
        relation.value for relation in GoalRelation
    ]
    assert payload["shared_enums"]["goal_link_status"]["allowed_values"] == [
        status.value for status in GoalLinkStatus
    ]

    full_payload = build_schema_description()
    viz_type = full_payload["entities"]["visualization"]["create"]["fields"]["viz_type"]
    assert viz_type["controlled_values"] is None
    assert viz_type["convention"] == "free_string"


def test_schema_description_status_lifecycle_comes_from_service_rules() -> None:
    payload = build_schema_description(entity_type="claim")

    claim = payload["entities"]["claim"]
    assert claim["status"]["allowed_values"] == [
        status.value for status in ClaimStatus
    ]
    assert claim["status_lifecycle"][ClaimStatus.PROPOSED.value] == [
        ClaimStatus.PROPOSED.value,
        ClaimStatus.TESTING.value,
        ClaimStatus.SUPPORTED.value,
        ClaimStatus.REJECTED.value,
    ]
    assert claim["status_lifecycle"][ClaimStatus.TESTING.value] == [
        ClaimStatus.TESTING.value,
        ClaimStatus.SUPPORTED.value,
        ClaimStatus.REJECTED.value,
    ]
    assert claim["status_lifecycle"][ClaimStatus.SUPPORTED.value] == [
        ClaimStatus.SUPPORTED.value
    ]


def test_schema_description_exposes_exploration_node_metadata() -> None:
    payload = build_schema_description(entity_type="exploration_node")

    exploration = payload["entities"]["exploration_node"]
    assert exploration["create"]["required_fields"] == [
        "project_id",
        "node_type",
        "title",
        "target",
    ]
    assert exploration["create"]["fields"]["node_type"]["controlled_values"][
        "allowed_values"
    ] == [node_type.value for node_type in ExplorationNodeType]
    assert exploration["status"]["allowed_values"] == [
        status.value for status in ExplorationNodeStatus
    ]
    assert payload["shared_enums"]["exploration_node_type"]["allowed_values"] == [
        node_type.value for node_type in ExplorationNodeType
    ]


def test_schema_description_rejects_unknown_entity_type() -> None:
    with pytest.raises(ValidationError, match="Unknown schema entity_type"):
        build_schema_description(entity_type="figure")
