"""End-to-end omitted-versus-null PATCH behavior."""

from __future__ import annotations

from uuid import uuid4

import pytest
from api_helpers import repository_backed_api
from fastapi.testclient import TestClient

from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    EntityRef,
    EntityType,
    ExplorationNodeType,
    QuestionType,
    SessionStatus,
    SessionType,
)
from lab_tracker.patching import NOT_PROVIDED, is_provided, provided_fields
from lab_tracker.schemas import QuestionUpdate


def _project(client: TestClient, headers: dict[str, str], name: str = "Patch semantics") -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _question(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    **fields: object,
) -> dict:
    payload: dict[str, object] = {
        "project_id": project_id,
        "text": "What changes under PATCH?",
        "question_type": "descriptive",
    }
    payload.update(fields)
    response = client.post("/questions", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _dataset(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    question_id: str,
) -> dict:
    response = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_shared_patch_primitive_preserves_field_presence() -> None:
    omitted = QuestionUpdate()
    explicit_null = QuestionUpdate(hypothesis=None)

    assert repr(NOT_PROVIDED) == "NOT_PROVIDED"
    assert not is_provided(NOT_PROVIDED)
    assert provided_fields(omitted) == {}
    assert provided_fields(explicit_null) == {"hypothesis": None}


def test_question_patch_tristate_and_noop_versions(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    question = _question(
        client,
        admin_auth_headers,
        project_id,
        hypothesis="Initial hypothesis",
    )
    question_id = question["question_id"]
    versions_path = f"/questions/{question_id}/versions"

    initial_versions = client.get(versions_path, headers=admin_auth_headers).json()["meta"][
        "total"
    ]
    no_op = client.patch(
        f"/questions/{question_id}",
        json={},
        headers=admin_auth_headers,
    )
    assert no_op.status_code == 200, no_op.text
    assert no_op.json()["data"]["hypothesis"] == "Initial hypothesis"
    assert no_op.json()["data"]["updated_at"] == question["updated_at"]
    assert (
        client.get(versions_path, headers=admin_auth_headers).json()["meta"]["total"]
        == initial_versions
    )

    cleared = client.patch(
        f"/questions/{question_id}",
        json={"hypothesis": None},
        headers=admin_auth_headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["hypothesis"] is None
    assert (
        client.get(versions_path, headers=admin_auth_headers).json()["meta"]["total"]
        == initial_versions + 1
    )

    repeated_clear = client.patch(
        f"/questions/{question_id}",
        json={"hypothesis": None},
        headers=admin_auth_headers,
    )
    assert repeated_clear.status_code == 200, repeated_clear.text
    assert repeated_clear.json()["data"]["updated_at"] == cleared.json()["data"]["updated_at"]
    assert (
        client.get(versions_path, headers=admin_auth_headers).json()["meta"]["total"]
        == initial_versions + 1
    )

    rejected = client.patch(
        f"/questions/{question_id}",
        json={"text": None},
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 422
    assert (
        client.get(f"/questions/{question_id}", headers=admin_auth_headers).json()["data"][
            "text"
        ]
        == question["text"]
    )


def test_nullable_entity_fields_clear_without_touching_omitted_fields(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    question = _question(client, admin_auth_headers, project_id, status="active")
    dataset = _dataset(
        client,
        admin_auth_headers,
        project_id,
        question["question_id"],
    )
    analysis_response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset["dataset_id"]],
            "method_hash": "method-1",
            "code_version": "v1",
            "environment_hash": "env-1",
        },
        headers=admin_auth_headers,
    )
    assert analysis_response.status_code == 201, analysis_response.text
    analysis = analysis_response.json()["data"]
    claim_response = client.post(
        "/claims",
        json={
            "project_id": project_id,
            "statement": "The patch retains omitted values.",
            "confidence": 50,
            "falsification_criteria": "Initial criterion",
            "verification_plan": "Initial plan",
            "refuting_outcome": "Initial outcome",
        },
        headers=admin_auth_headers,
    )
    assert claim_response.status_code == 201, claim_response.text
    claim = claim_response.json()["data"]
    note_response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Raw note",
            "transcribed_text": "Transcript",
        },
        headers=admin_auth_headers,
    )
    assert note_response.status_code == 201, note_response.text
    note = note_response.json()["data"]
    visualization_response = client.post(
        "/visualizations",
        json={
            "analysis_id": analysis["analysis_id"],
            "viz_type": "line",
            "file_path": "figures/one.png",
            "caption": "Initial caption",
        },
        headers=admin_auth_headers,
    )
    assert visualization_response.status_code == 201, visualization_response.text
    visualization = visualization_response.json()["data"]

    analysis_patch = client.patch(
        f"/analyses/{analysis['analysis_id']}",
        json={"environment_hash": None},
        headers=admin_auth_headers,
    )
    assert analysis_patch.status_code == 200, analysis_patch.text
    assert analysis_patch.json()["data"]["environment_hash"] is None
    assert analysis_patch.json()["data"]["method_hash"] == "method-1"

    claim_patch = client.patch(
        f"/claims/{claim['claim_id']}",
        json={
            "falsification_criteria": None,
            "verification_plan": None,
            "refuting_outcome": None,
        },
        headers=admin_auth_headers,
    )
    assert claim_patch.status_code == 200, claim_patch.text
    assert claim_patch.json()["data"]["falsification_criteria"] is None
    assert claim_patch.json()["data"]["verification_plan"] is None
    assert claim_patch.json()["data"]["refuting_outcome"] is None
    assert claim_patch.json()["data"]["statement"] == claim["statement"]

    note_patch = client.patch(
        f"/notes/{note['note_id']}",
        json={"transcribed_text": None},
        headers=admin_auth_headers,
    )
    assert note_patch.status_code == 200, note_patch.text
    assert note_patch.json()["data"]["transcribed_text"] is None
    assert note_patch.json()["data"]["raw_content"] == "Raw note"

    visualization_patch = client.patch(
        f"/visualizations/{visualization['viz_id']}",
        json={"caption": None},
        headers=admin_auth_headers,
    )
    assert visualization_patch.status_code == 200, visualization_patch.text
    assert visualization_patch.json()["data"]["caption"] is None
    assert visualization_patch.json()["data"]["file_path"] == "figures/one.png"


def test_project_goal_and_goal_link_nullable_fields_clear(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    group_response = client.post(
        "/groups",
        json={"name": "Patch group"},
        headers=admin_auth_headers,
    )
    assert group_response.status_code == 201, group_response.text
    group_id = group_response.json()["data"]["group_id"]
    project_response = client.post(
        "/projects",
        json={"name": "Grouped project", "group_id": group_id},
        headers=admin_auth_headers,
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()["data"]
    project_patch = client.patch(
        f"/projects/{project['project_id']}",
        json={"group_id": None},
        headers=admin_auth_headers,
    )
    assert project_patch.status_code == 200, project_patch.text
    assert project_patch.json()["data"]["group_id"] is None
    assert project_patch.json()["data"]["name"] == "Grouped project"

    question = _question(
        client,
        admin_auth_headers,
        project["project_id"],
        status="active",
    )
    goal_response = client.post(
        f"/projects/{project['project_id']}/goals",
        json={
            "goal_type": "other",
            "title": "Patch goal",
            "target_date": "2027-01-01",
            "external_ref": "external-1",
        },
        headers=admin_auth_headers,
    )
    assert goal_response.status_code == 201, goal_response.text
    goal = goal_response.json()["data"]
    goal_patch = client.patch(
        f"/goals/{goal['goal_id']}",
        json={"target_date": None, "external_ref": None},
        headers=admin_auth_headers,
    )
    assert goal_patch.status_code == 200, goal_patch.text
    assert goal_patch.json()["data"]["target_date"] is None
    assert goal_patch.json()["data"]["external_ref"] is None
    assert goal_patch.json()["data"]["title"] == "Patch goal"

    link_response = client.post(
        f"/goals/{goal['goal_id']}/links",
        json={
            "entity_type": "question",
            "entity_id": question["question_id"],
            "relation": "addresses",
            "slot": "figure-1",
        },
        headers=admin_auth_headers,
    )
    assert link_response.status_code == 201, link_response.text
    link = link_response.json()["data"]
    link_patch = client.patch(
        f"/goals/{goal['goal_id']}/links/{link['link_id']}",
        json={"slot": None},
        headers=admin_auth_headers,
    )
    assert link_patch.status_code == 200, link_patch.text
    assert link_patch.json()["data"]["slot"] is None


def test_session_ended_at_distinguishes_omission_and_explicit_null(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    response = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201, response.text
    session = response.json()["data"]
    path = f"/sessions/{session['session_id']}"

    active_null = client.patch(path, json={"ended_at": None}, headers=admin_auth_headers)
    assert active_null.status_code == 200, active_null.text
    assert active_null.json()["data"]["ended_at"] is None
    assert active_null.json()["data"]["updated_at"] == session["updated_at"]

    invalid_close = client.patch(
        path,
        json={"status": "closed", "ended_at": None},
        headers=admin_auth_headers,
    )
    assert invalid_close.status_code == 422

    closed = client.patch(path, json={"status": "closed"}, headers=admin_auth_headers)
    assert closed.status_code == 200, closed.text
    assert closed.json()["data"]["ended_at"] is not None


def test_terminal_reason_null_cannot_break_terminal_invariant(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    question = _question(client, admin_auth_headers, project_id)
    path = f"/questions/{question['question_id']}"

    missing_reason = client.patch(
        path,
        json={"status": "abandoned"},
        headers=admin_auth_headers,
    )
    assert missing_reason.status_code == 422

    abandoned = client.patch(
        path,
        json={"status": "abandoned", "terminal_reason": "No longer testable."},
        headers=admin_auth_headers,
    )
    assert abandoned.status_code == 200, abandoned.text

    invalid_clear = client.patch(
        path,
        json={"terminal_reason": None},
        headers=admin_auth_headers,
    )
    assert invalid_clear.status_code == 422
    assert (
        client.get(path, headers=admin_auth_headers).json()["data"]["terminal_reason"]
        == "No longer testable."
    )


def test_exploration_nullable_fields_clear_only_when_result_remains_valid() -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project = api.create_project("Exploration PATCH", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Which path remains valid?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    claim = api.create_claim(
        project_id=project.project_id,
        statement="The claim can be invalidated.",
        confidence=50,
        actor=actor,
    )
    pivot = api.create_exploration_node(
        project_id=project.project_id,
        node_type=ExplorationNodeType.PIVOT,
        title="Switch target",
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
        trigger="New evidence",
        rationale="The original path failed.",
        invalidates_claim_id=claim.claim_id,
        actor=actor,
    )

    with pytest.raises(ValidationError):
        api.update_exploration_node(
            pivot.node_id,
            invalidates_claim_id=None,
            actor=actor,
        )

    replacement = api.create_exploration_node(
        project_id=project.project_id,
        node_type=ExplorationNodeType.DEAD_END,
        title="Failed path",
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
        hypothesis="The path would work.",
        failure_mode="It did not converge.",
        lesson="Use a different model.",
        actor=actor,
    )
    switched = api.update_exploration_node(
        pivot.node_id,
        invalidates_claim_id=None,
        invalidates_node_id=replacement.node_id,
        actor=actor,
    )
    assert switched.invalidates_claim_id is None
    assert switched.invalidates_node_id == replacement.node_id


def test_direct_service_rejects_null_for_non_nullable_patch_argument() -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project = api.create_project("Direct service PATCH", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Can direct callers collapse null?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )

    with pytest.raises(ValidationError, match="text must not be null"):
        api.update_question(question.question_id, text=None, actor=actor)

    unchanged = api.update_session(
        api.create_session(
            project_id=project.project_id,
            session_type=SessionType.OPERATIONAL,
            actor=actor,
        ).session_id,
        ended_at=None,
        actor=actor,
    )
    assert unchanged.status == SessionStatus.ACTIVE
    assert unchanged.ended_at is None
