from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lab_tracker.db_models import ExperimentDatasetModel, ExperimentSessionModel
from lab_tracker.sqlalchemy_repository_parts.experiments import (
    SQLAlchemyExperimentRepository,
)


def test_membership_puts_ignore_a_concurrent_winner(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = client.post(
        "/projects",
        json={"name": "Experiment membership race"},
        headers=admin_auth_headers,
    )
    project_id = project.json()["data"]["project_id"]
    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Are membership PUTs idempotent under a stale read?",
            "question_type": "method_dev",
            "status": "active",
        },
        headers=admin_auth_headers,
    )
    question_id = question.json()["data"]["question_id"]
    experiment = client.post(
        "/experiments",
        json={
            "project_id": project_id,
            "name": "Concurrent membership",
            "primary_question_id": question_id,
        },
        headers=admin_auth_headers,
    ).json()["data"]
    session = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=admin_auth_headers,
    ).json()["data"]
    dataset = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
        },
        headers=admin_auth_headers,
    ).json()["data"]

    session_path = (
        f"/experiments/{experiment['experiment_id']}/sessions/{session['session_id']}"
    )
    dataset_path = (
        f"/experiments/{experiment['experiment_id']}/datasets/{dataset['dataset_id']}"
    )
    assert client.put(session_path, headers=admin_auth_headers).status_code == 200
    assert client.put(dataset_path, headers=admin_auth_headers).status_code == 200

    monkeypatch.setattr(
        SQLAlchemyExperimentRepository,
        "has_session",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        SQLAlchemyExperimentRepository,
        "has_dataset",
        lambda *_args, **_kwargs: False,
    )
    assert client.put(session_path, headers=admin_auth_headers).status_code == 200
    assert client.put(dataset_path, headers=admin_auth_headers).status_code == 200

    with client.app.state.db_session_factory() as db_session:
        assert (
            db_session.scalar(
                select(func.count()).select_from(ExperimentSessionModel)
            )
            == 1
        )
        assert (
            db_session.scalar(
                select(func.count()).select_from(ExperimentDatasetModel)
            )
            == 1
        )
