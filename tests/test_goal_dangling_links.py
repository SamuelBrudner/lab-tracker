"""A goal link whose target vanished must not break goal listings (M58)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from api_helpers import repository_backed_api
from fastapi.testclient import TestClient

from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import GoalLinkModel, QuestionModel
from lab_tracker.models import (
    DatasetCommitManifestInput,
    DatasetFile,
    EntityRef,
    EntityType,
    GoalRelation,
    GoalType,
    QuestionStatus,
    QuestionType,
    SessionType,
)

_GOAL_SERVICE_LOGGER = "lab_tracker.services.goal_service"


def _post(client: TestClient, headers: dict[str, str], path: str, payload: dict[str, Any]):
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _question(client: TestClient, headers: dict[str, str], project_id: str, text: str) -> str:
    question = _post(
        client,
        headers,
        "/questions",
        {"project_id": project_id, "text": text, "question_type": "descriptive"},
    )
    return question["question_id"]


def _goal(client: TestClient, headers: dict[str, str], project_id: str, title: str) -> str:
    goal = _post(
        client,
        headers,
        f"/projects/{project_id}/goals",
        {"goal_type": "paper", "title": title},
    )
    return goal["goal_id"]


def _link(
    client: TestClient,
    headers: dict[str, str],
    goal_id: str,
    question_id: str,
) -> str:
    link = _post(
        client,
        headers,
        f"/goals/{goal_id}/links",
        {"entity_type": "question", "entity_id": question_id, "relation": "addresses"},
    )
    return link["link_id"]


def _listed_goal_ids(response) -> list[str]:  # noqa: ANN001
    assert response.status_code == 200, response.text
    return [goal["goal_id"] for goal in response.json()["data"]]


def test_goal_listings_skip_and_log_a_goal_with_a_dangling_link(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = admin_auth_headers
    project_id = _post(client, headers, "/projects", {"name": "Dangling goal listing"})[
        "project_id"
    ]
    vanished_question_id = _question(client, headers, project_id, "Deleted underneath?")
    shared_question_id = _question(client, headers, project_id, "Still here?")
    broken_goal_id = _goal(client, headers, project_id, "Goal with a dangling link")
    healthy_goal_id = _goal(client, headers, project_id, "Healthy goal")
    dangling_link_id = _link(client, headers, broken_goal_id, vanished_question_id)
    _link(client, headers, broken_goal_id, shared_question_id)
    _link(client, headers, healthy_goal_id, shared_question_id)

    # Bypass the delete path's goal-link cleanup, as a concurrent link create
    # or an out-of-band row removal would.
    with client.app.state.db_session_factory() as session:
        question_row = session.get(QuestionModel, vanished_question_id)
        assert question_row is not None
        session.delete(question_row)
        session.commit()
        assert session.get(GoalLinkModel, dangling_link_id) is not None

    caplog.set_level(logging.WARNING, logger=_GOAL_SERVICE_LOGGER)
    listings = {
        "project": client.get(f"/projects/{project_id}/goals", headers=headers),
        "all": client.get("/goals", headers=headers),
        "filtered": client.get("/goals", params={"project_id": project_id}, headers=headers),
        "node": client.get(
            f"/projects/{project_id}/nodes/question/{shared_question_id}/goals",
            headers=headers,
        ),
    }

    for name, response in listings.items():
        assert _listed_goal_ids(response) == [healthy_goal_id], name
    dangling_records = [
        record
        for record in caplog.records
        if record.name == _GOAL_SERVICE_LOGGER and dangling_link_id in record.getMessage()
    ]
    assert len(dangling_records) == len(listings)
    for record in dangling_records:
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert broken_goal_id in message
        assert f"question {vanished_question_id}" in message


def test_goal_listing_still_raises_when_the_failure_is_not_a_dangling_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lab_tracker.errors import NotFoundError
    from lab_tracker.services.goal_service import GoalService

    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project = api.create_project("Real errors surface", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Linked question",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    goal = api.create_goal(project.project_id, goal_type=GoalType.PAPER, title="G", actor=actor)
    api.link_node_to_goal(
        goal.goal_id,
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id),
        relation=GoalRelation.ADDRESSES,
        actor=actor,
    )

    calls = 0
    original = GoalService._require_goal_read

    def flaky_scope(self, goal, *, actor=None):  # noqa: ANN001, ANN202
        nonlocal calls
        calls += 1
        if calls == 1:
            raise NotFoundError("Project does not exist.")
        return original(self, goal, actor=actor)

    monkeypatch.setattr(GoalService, "_require_goal_read", flaky_scope)

    with pytest.raises(NotFoundError, match="^Project does not exist\\.$"):
        api.goals.list_visible_goals(project_id=project.project_id, actor=actor)


def _question_target(api, actor, project_id: UUID) -> EntityRef:  # noqa: ANN001
    question = api.create_question(
        project_id=project_id,
        text="Goal-linked question",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    return EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id)


def _dataset_target(api, actor, project_id: UUID) -> EntityRef:  # noqa: ANN001
    question = _question_target(api, actor, project_id)
    dataset = api.create_dataset(
        project_id=project_id,
        primary_question_id=question.entity_id,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")]
        ),
        actor=actor,
    )
    return EntityRef(entity_type=EntityType.DATASET, entity_id=dataset.dataset_id)


def _note_target(api, actor, project_id: UUID) -> EntityRef:  # noqa: ANN001
    note = api.create_note(project_id, "Goal-linked note", actor=actor)
    return EntityRef(entity_type=EntityType.NOTE, entity_id=note.note_id)


def _session_target(api, actor, project_id: UUID) -> EntityRef:  # noqa: ANN001
    session = api.create_session(project_id, SessionType.OPERATIONAL, actor=actor)
    return EntityRef(entity_type=EntityType.SESSION, entity_id=session.session_id)


def _claim_target(api, actor, project_id: UUID) -> EntityRef:  # noqa: ANN001
    claim = api.create_claim(project_id, "Goal-linked claim", 0.5, actor=actor)
    return EntityRef(entity_type=EntityType.CLAIM, entity_id=claim.claim_id)


_TARGET_FACTORIES: dict[str, tuple[Callable[..., EntityRef], str]] = {
    "question": (_question_target, "delete_question"),
    "dataset": (_dataset_target, "delete_dataset"),
    "note": (_note_target, "delete_note"),
    "session": (_session_target, "delete_session"),
    "claim": (_claim_target, "delete_claim"),
}


@pytest.mark.parametrize("target_kind", sorted(_TARGET_FACTORIES))
def test_deleting_a_goal_link_target_removes_the_link(target_kind: str) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project = api.create_project(f"Goal cleanup {target_kind}", actor=actor)
    factory, delete_method = _TARGET_FACTORIES[target_kind]
    target = factory(api, actor, project.project_id)
    goal = api.create_goal(
        project.project_id,
        goal_type=GoalType.PAPER,
        title=f"{target_kind} goal",
        actor=actor,
    )
    link = api.link_node_to_goal(
        goal.goal_id,
        target=target,
        relation=GoalRelation.ADDRESSES,
        actor=actor,
    )
    _, session = api._test_resources  # type: ignore[attr-defined]

    getattr(api, delete_method)(target.entity_id, actor=actor)

    assert api.get_goal(goal.goal_id).links == []
    assert session.get(GoalLinkModel, str(link.link_id)) is None
    assert [
        listed.goal_id
        for listed in api.goals.list_visible_goals(project_id=project.project_id, actor=actor)
    ] == [goal.goal_id]
