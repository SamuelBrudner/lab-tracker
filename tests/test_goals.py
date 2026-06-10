from __future__ import annotations

from uuid import uuid4

import pytest
from api_helpers import repository_backed_api
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import GoalLinkModel
from lab_tracker.errors import ValidationError
from lab_tracker.goals_attributes import validate_goal_attributes
from lab_tracker.models import (
    EntityRef,
    EntityType,
    GoalLinkStatus,
    GoalRelation,
    GoalType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphDraftSemanticType,
    QuestionStatus,
    QuestionType,
)


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def _api_project_question():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Goal Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Does goal tagging work?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    return api, actor, project, question


def test_goal_attribute_registry_known_types_and_other_escape_hatch():
    assert validate_goal_attributes(
        GoalType.PAPER,
        {"target_venue": "Neuron", "submission_deadline": "2026-08-01"},
    ) == {"target_venue": "Neuron", "submission_deadline": "2026-08-01"}

    with pytest.raises(ValueError):
        validate_goal_attributes(GoalType.PAPER, [])

    with pytest.raises(PydanticValidationError):
        validate_goal_attributes(GoalType.GRANT, {"typo_field": "nope"})

    assert validate_goal_attributes(GoalType.OTHER, {"custom": {"nested": True}}) == {
        "custom": {"nested": True}
    }


def test_goals_round_trip_links_and_reverse_lookup_through_repository():
    api, actor, project, question = _api_project_question()
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Goal paper",
        attributes={"target_venue": "Cell"},
        actor=actor,
    )
    link = api.link_node_to_goal(
        goal.goal_id,
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
        relation=GoalRelation.ADDRESSES,
        actor=actor,
    )

    reloaded = api.get_goal(goal.goal_id)
    assert reloaded.attributes == {"target_venue": "Cell"}
    assert reloaded.links[0].link_id == link.link_id
    assert reloaded.links[0].link_status == GoalLinkStatus.CANDIDATE
    assert reloaded.links[0].slot is None

    reverse = api.list_node_goals(
        project_id=project.project_id,
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
    )
    assert [item.goal_id for item in reverse] == [goal.goal_id]


def test_goal_links_use_empty_db_slot_to_enforce_unslotted_uniqueness():
    api, actor, project, question = _api_project_question()
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Unique link paper",
        actor=actor,
    )
    api.link_node_to_goal(
        goal.goal_id,
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
        relation=GoalRelation.ADDRESSES,
        actor=actor,
    )
    _, session = api._test_resources  # type: ignore[attr-defined]
    session.add(
        GoalLinkModel(
            link_id=str(uuid4()),
            goal_id=str(goal.goal_id),
            entity_type=EntityType.QUESTION.value,
            entity_id=str(question.question_id),
            relation=GoalRelation.ADDRESSES.value,
            link_status=GoalLinkStatus.CANDIDATE.value,
            slot="",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_goal_routes_create_link_reverse_lookup_and_goal_scoped_search(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = client.post(
        "/projects",
        json={"name": "Goal routes"},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does baseline evidence support Figure 3?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    ).json()["data"]["question_id"]

    invalid = client.post(
        f"/projects/{project_id}/goals",
        json={
            "goal_type": "paper",
            "title": "Invalid goal",
            "attributes": {"unknown_key": "blocked"},
        },
        headers=admin_auth_headers,
    )
    assert invalid.status_code == 422

    created = client.post(
        f"/projects/{project_id}/goals",
        json={
            "goal_type": "paper",
            "title": "Baseline manuscript",
            "attributes": {"target_venue": "eLife"},
        },
        headers=admin_auth_headers,
    )
    assert created.status_code == 201
    goal_id = created.json()["data"]["goal_id"]

    link = client.post(
        f"/goals/{goal_id}/links",
        json={
            "entity_type": "question",
            "entity_id": question_id,
            "relation": "candidate_figure",
            "slot": "Figure 3",
        },
        headers=admin_auth_headers,
    )
    assert link.status_code == 201
    link_payload = link.json()["data"]
    assert link_payload["link_status"] == "candidate"

    promoted = client.patch(
        f"/goals/{goal_id}/links/{link_payload['link_id']}",
        json={"link_status": "committed"},
        headers=admin_auth_headers,
    )
    assert promoted.status_code == 200
    assert promoted.json()["data"]["link_status"] == "committed"

    reverse = client.get(
        f"/projects/{project_id}/nodes/question/{question_id}/goals",
        headers=admin_auth_headers,
    )
    assert reverse.status_code == 200
    assert [item["goal_id"] for item in reverse.json()["data"]] == [goal_id]

    search = client.get(
        "/search",
        params={"q": "baseline", "project_id": project_id, "goal_id": goal_id},
        headers=admin_auth_headers,
    )
    assert search.status_code == 200
    assert [item["question_id"] for item in search.json()["data"]["questions"]] == [question_id]


def test_graph_draft_goal_operations_validate_and_apply_committed_links():
    api, actor, project, question = _api_project_question()
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Draftable paper",
        actor=actor,
    )
    invalid_create = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=1,
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.GOAL,
        semantic_type=GraphDraftSemanticType.SUGGEST_NEW_GOAL,
        payload={
            "project_id": str(project.project_id),
            "goal_type": "paper",
            "title": "Bad attributes",
            "attributes": {"typo": "reject"},
        },
    )
    with pytest.raises(ValidationError):
        api.graph_drafts.patch_validator.validate_operation(invalid_create, invalid_create.payload)

    link_operation = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=2,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.GOAL,
        semantic_type=GraphDraftSemanticType.LINK_NODE_TO_GOAL,
        target_entity_id=goal.goal_id,
        payload={
            "links": [
                {
                    "entity_type": "question",
                    "entity_id": str(question.question_id),
                    "relation": "addresses",
                }
            ]
        },
    )
    api.graph_drafts.patch_validator.validate_operation(link_operation, link_operation.payload)
    api.graph_drafts.patch_applier.apply_graph_operation(
        link_operation,
        ref_map={},
        actor=actor,
    )

    reloaded = api.get_goal(goal.goal_id)
    assert reloaded.links[0].link_status == GoalLinkStatus.COMMITTED
    assert reloaded.links[0].target.entity_id == question.question_id


def test_graph_draft_goal_update_validation_uses_existing_goal_type():
    api, actor, project, _question = _api_project_question()
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title="Draftable paper",
        attributes={"target_venue": "Cell"},
        actor=actor,
    )
    invalid_attributes = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=1,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.GOAL,
        semantic_type=GraphDraftSemanticType.UPDATE_GOAL,
        target_entity_id=goal.goal_id,
        payload={"attributes": {"typo_field": "blocked"}},
    )
    with pytest.raises(ValidationError, match="Goal attributes are invalid"):
        api.graph_drafts.patch_validator.validate_operation(
            invalid_attributes,
            invalid_attributes.payload,
        )

    invalid_type_change = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=2,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.GOAL,
        semantic_type=GraphDraftSemanticType.UPDATE_GOAL,
        target_entity_id=goal.goal_id,
        payload={"goal_type": "grant"},
    )
    with pytest.raises(ValidationError, match="Goal attributes are invalid"):
        api.graph_drafts.patch_validator.validate_operation(
            invalid_type_change,
            invalid_type_change.payload,
        )

    valid_type_change = GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=3,
        op=GraphChangeOp.UPDATE,
        entity_type=EntityType.GOAL,
        semantic_type=GraphDraftSemanticType.UPDATE_GOAL,
        target_entity_id=goal.goal_id,
        payload={"goal_type": "grant", "attributes": {"funding_agency": "NIH"}},
    )
    api.graph_drafts.patch_validator.validate_operation(
        valid_type_change,
        valid_type_change.payload,
    )
