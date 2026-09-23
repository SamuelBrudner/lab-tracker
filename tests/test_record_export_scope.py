"""Record-export scope and supervision-edge loading."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from api_helpers import repository_backed_api
from fastapi.testclient import TestClient

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
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


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


def _register_user(client: TestClient, username: str) -> str:
    response = client.post("/auth/register", json={"username": username, "password": "secret"})
    assert response.status_code == 201, response.text
    return response.json()["data"]["user"]["user_id"]


def _supervise(
    client: TestClient,
    headers: dict[str, str],
    *,
    supervisor_user_id: str,
    supervisee_user_id: str,
) -> str:
    response = client.post(
        "/supervision-edges",
        json={
            "supervisor_user_id": supervisor_user_id,
            "supervisee_user_id": supervisee_user_id,
            "started_at": "2020-01-01T00:00:00+00:00",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["edge_id"]


def _record_loaded_supervision_edges(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    loaded: set[str] = set()
    original = SQLAlchemyLabTrackerRepository.query_supervision_edges

    def query_supervision_edges(self, **kwargs: Any):  # noqa: ANN001, ANN202
        edges, total = original(self, **kwargs)
        loaded.update(str(edge.edge_id) for edge in edges)
        return edges, total

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "query_supervision_edges",
        query_supervision_edges,
    )
    return loaded


def _people(document: object, user_id: str) -> list[dict[str, Any]]:
    if isinstance(document, list):
        return [person for item in document for person in _people(item, user_id)]
    if not isinstance(document, dict):
        return []
    found = [document] if document.get("userId") == user_id else []
    return found + [person for value in document.values() for person in _people(value, user_id)]


@pytest.mark.parametrize("document", ["dataset_provenance", "question_ara_artifact"])
def test_provenance_documents_load_only_their_people_supervision_edges(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    document: str,
) -> None:
    """L40: supervision is global, so documents must not load every edge."""

    headers = admin_auth_headers
    admin_user_id = client.get("/auth/me", headers=headers).json()["data"]["user_id"]
    suffix = uuid4().hex[:8]
    supervisor_user_id = _register_user(client, f"supervisor-{suffix}")
    unrelated_supervisor = _register_user(client, f"unrelated-supervisor-{suffix}")
    unrelated_supervisee = _register_user(client, f"unrelated-supervisee-{suffix}")
    relevant_edge = _supervise(
        client,
        headers,
        supervisor_user_id=supervisor_user_id,
        supervisee_user_id=admin_user_id,
    )
    unrelated_edge = _supervise(
        client,
        headers,
        supervisor_user_id=unrelated_supervisor,
        supervisee_user_id=unrelated_supervisee,
    )
    project_id = client.post(
        "/projects",
        json={"name": f"Supervision loading {suffix}"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Who supervised this work?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    if document == "dataset_provenance":
        dataset_id = client.post(
            "/datasets",
            json={"project_id": project_id, "primary_question_id": question_id},
            headers=headers,
        ).json()["data"]["dataset_id"]
        path = f"/datasets/{dataset_id}/provenance"
    else:
        path = f"/questions/{question_id}/ara-artifact"
    loaded = _record_loaded_supervision_edges(monkeypatch)

    response = client.get(path, headers=headers)

    assert response.status_code == 200, response.text
    assert unrelated_edge not in loaded
    assert relevant_edge in loaded
    creators = _people(response.json(), admin_user_id)
    assert creators
    for creator in creators:
        assert creator["actedOnBehalfOf"]["@id"].endswith(f"/agents/{supervisor_user_id}")
