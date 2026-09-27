"""Direct writes self-declare their origin; service tokens stamp their label."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from lab_tracker.auth import utc_now
from lab_tracker.models import EntityOrigin
from lab_tracker.routes.shared import ORIGIN_PROVIDER_MAX_LENGTH
from lab_tracker.schemas import (
    AnalysisCreate,
    ClaimCreate,
    DatasetCreate,
    EvidenceBundleRequest,
    GoalCreateFields,
    NoteCreate,
    QuestionCreate,
    VisualizationCreate,
)

PROJECT_ID = str(uuid4())
QUESTION_ID = str(uuid4())
DATASET_ID = str(uuid4())
ANALYSIS_ID = str(uuid4())

MINIMAL_PAYLOADS: list[tuple[type, dict[str, Any]]] = [
    (QuestionCreate, {"project_id": PROJECT_ID, "text": "Which?", "question_type": "other"}),
    (NoteCreate, {"project_id": PROJECT_ID, "raw_content": "note"}),
    (DatasetCreate, {"project_id": PROJECT_ID, "primary_question_id": QUESTION_ID}),
    (
        AnalysisCreate,
        {
            "project_id": PROJECT_ID,
            "dataset_ids": [DATASET_ID],
            "method_hash": "method-1",
            "code_version": "v1",
        },
    ),
    (ClaimCreate, {"project_id": PROJECT_ID, "statement": "claim", "confidence": 0.8}),
    (GoalCreateFields, {"goal_type": "paper", "title": "Goal"}),
    (
        VisualizationCreate,
        {"analysis_id": ANALYSIS_ID, "viz_type": "line", "file_path": "figures/plot.png"},
    ),
    (
        EvidenceBundleRequest,
        {"project_id": PROJECT_ID, "source_note": {"kind": "create", "raw_content": "note"}},
    ),
]


@pytest.mark.parametrize(
    ("schema", "payload"),
    MINIMAL_PAYLOADS,
    ids=[schema.__name__ for schema, _ in MINIMAL_PAYLOADS],
)
def test_create_schemas_default_origin_to_user_and_reject_review_origins(
    schema: type,
    payload: dict[str, Any],
) -> None:
    assert schema.model_validate(payload).origin == EntityOrigin.USER
    assert (
        schema.model_validate({**payload, "origin": "ai_executed"}).origin
        == EntityOrigin.AI_EXECUTED
    )
    for reserved in ("ai_suggested", "user_revised", "robot"):
        with pytest.raises(PydanticValidationError):
            schema.model_validate({**payload, "origin": reserved})


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Origin project"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _service_token_headers(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    *,
    label: str,
    project_id: str,
) -> dict[str, str]:
    # A personal token uses its own role, so its user needs project membership.
    user_id = client.get("/auth/me", headers=admin_auth_headers).json()["data"]["user_id"]
    membership = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text
    response = client.post(
        "/auth/tokens",
        json={
            "label": label,
            "role": "editor",
            "read_only": False,
            "scope": "all",
            "expires_at": (utc_now() + timedelta(days=7)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['data']['secret']}"}


def _create_every_entity(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    extra: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """POST each direct create route; return (read path, created payload) pairs."""

    created: list[tuple[str, dict[str, Any]]] = []

    def post(
        path: str, body: dict[str, Any], read_path: Callable[[dict[str, Any]], str]
    ) -> dict[str, Any]:
        response = client.post(path, json={**body, **extra}, headers=headers)
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        created.append((read_path(data), data))
        return data

    question = post(
        "/questions",
        {
            "project_id": project_id,
            "text": "Which origin is recorded?",
            "question_type": "descriptive",
            "status": "active",
        },
        lambda data: f"/questions/{data['question_id']}",
    )
    post(
        "/notes",
        {"project_id": project_id, "raw_content": "Origin note"},
        lambda data: f"/notes/{data['note_id']}",
    )
    dataset = post(
        "/datasets",
        {"project_id": project_id, "primary_question_id": question["question_id"]},
        lambda data: f"/datasets/{data['dataset_id']}",
    )
    analysis = post(
        "/analyses",
        {
            "project_id": project_id,
            "dataset_ids": [dataset["dataset_id"]],
            "method_hash": "method-1",
            "code_version": "v1",
        },
        lambda data: f"/analyses/{data['analysis_id']}",
    )
    post(
        "/claims",
        {"project_id": project_id, "statement": "Origin claim.", "confidence": 0.8},
        lambda data: f"/claims/{data['claim_id']}",
    )
    post(
        "/visualizations",
        {
            "analysis_id": analysis["analysis_id"],
            "viz_type": "line",
            "file_path": "figures/origin.png",
        },
        lambda data: f"/visualizations/{data['viz_id']}",
    )
    post(
        f"/projects/{project_id}/goals",
        {"goal_type": "paper", "title": "Project goal"},
        lambda data: f"/goals/{data['goal_id']}",
    )
    post(
        "/goals",
        {"project_id": project_id, "goal_type": "paper", "title": "Spanning goal"},
        lambda data: f"/goals/{data['goal_id']}",
    )
    return created


def test_direct_creates_persist_self_declared_origin(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)

    executed = _create_every_entity(
        client, admin_auth_headers, project_id, {"origin": "ai_executed"}
    )
    defaulted = _create_every_entity(client, admin_auth_headers, project_id, {})

    assert len(executed) == 8
    for read_path, data in executed:
        assert data["origin"] == "ai_executed", read_path
        assert data["origin_provider"] is None, read_path
        fetched = client.get(read_path, headers=admin_auth_headers)
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["data"]["origin"] == "ai_executed", read_path
    for read_path, data in defaulted:
        assert data["origin"] == "user", read_path
        assert data["origin_provider"] is None, read_path


def test_service_token_direct_writes_record_token_label(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    agent_headers = _service_token_headers(
        client, admin_auth_headers, label="Agent alpha", project_id=project_id
    )

    created = _create_every_entity(client, agent_headers, project_id, {"origin": "ai_executed"})

    for read_path, data in created:
        assert data["origin"] == "ai_executed", read_path
        assert data["origin_provider"] == "Agent alpha", read_path
        fetched = client.get(read_path, headers=admin_auth_headers)
        assert fetched.json()["data"]["origin_provider"] == "Agent alpha", read_path


def test_service_token_label_is_truncated_to_the_provider_column(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    long_label = "L" * 150
    agent_headers = _service_token_headers(
        client, admin_auth_headers, label=long_label, project_id=project_id
    )

    response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Long label capture"},
        headers=agent_headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["origin_provider"] == "L" * ORIGIN_PROVIDER_MAX_LENGTH
