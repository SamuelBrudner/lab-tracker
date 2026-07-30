from __future__ import annotations

from fastapi.testclient import TestClient


def _create_project(
    client: TestClient,
    headers: dict[str, str],
    name: str,
) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    text: str,
    status: str = "active",
) -> str:
    response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": text,
            "question_type": "descriptive",
            "status": status,
        },
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["question_id"]


def _create_experiment(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    question_id: str,
    *,
    name: str = "High-cardinality acquisition",
) -> dict:
    response = client.post(
        "/experiments",
        json={
            "project_id": project_id,
            "name": name,
            "description": "One scientific unit with many output files.",
            "primary_question_id": question_id,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _create_session(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    session_type: str = "operational",
    primary_question_id: str | None = None,
) -> dict:
    payload = {"project_id": project_id, "session_type": session_type}
    if primary_question_id is not None:
        payload["primary_question_id"] = primary_question_id
    response = client.post("/sessions", json=payload, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]


def _create_dataset(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    primary_question_id: str,
    *,
    secondary_question_ids: list[str] | None = None,
) -> dict:
    response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": primary_question_id,
            "secondary_question_ids": secondary_question_ids,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_experiment_crud_and_forward_only_lifecycle(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(client, admin_auth_headers, "Experiment CRUD")
    question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="What does this run test?",
    )
    experiment = _create_experiment(
        client,
        admin_auth_headers,
        project_id,
        question_id,
    )
    experiment_id = experiment["experiment_id"]

    assert experiment["primary_question_id"] == question_id
    assert experiment["status"] == "active"
    assert experiment["closed_at"] is None
    assert experiment["archived_at"] is None

    listed = client.get(
        "/experiments",
        params={"project_id": project_id, "primary_question_id": question_id},
        headers=admin_auth_headers,
    )
    assert listed.status_code == 200
    assert listed.json()["meta"]["total"] == 1

    renamed = client.patch(
        f"/experiments/{experiment_id}",
        json={"name": "Renamed experiment", "description": None},
        headers=admin_auth_headers,
    )
    assert renamed.status_code == 200
    assert renamed.json()["data"]["name"] == "Renamed experiment"
    assert renamed.json()["data"]["description"] == ""

    immutable_question = client.patch(
        f"/experiments/{experiment_id}",
        json={"primary_question_id": question_id},
        headers=admin_auth_headers,
    )
    assert immutable_question.status_code == 422
    assert (
        client.delete(
            f"/experiments/{experiment_id}",
            headers=admin_auth_headers,
        ).status_code
        == 405
    )

    assert (
        client.patch(
            f"/experiments/{experiment_id}",
            json={"status": "archived"},
            headers=admin_auth_headers,
        ).status_code
        == 422
    )
    closed = client.patch(
        f"/experiments/{experiment_id}",
        json={"status": "closed"},
        headers=admin_auth_headers,
    )
    assert closed.status_code == 200
    assert closed.json()["data"]["closed_at"] is not None
    archived = client.patch(
        f"/experiments/{experiment_id}",
        json={"status": "archived"},
        headers=admin_auth_headers,
    )
    assert archived.status_code == 200
    assert archived.json()["data"]["archived_at"] is not None
    assert (
        client.patch(
            f"/experiments/{experiment_id}",
            json={"name": "Cannot reopen"},
            headers=admin_auth_headers,
        ).status_code
        == 422
    )


def test_experiment_memberships_are_many_to_many_paginated_and_idempotent(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(client, admin_auth_headers, "Memberships")
    question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="Can work units share evidence?",
    )
    experiments = [
        _create_experiment(
            client,
            admin_auth_headers,
            project_id,
            question_id,
            name=f"Experiment {label}",
        )
        for label in ("A", "B")
    ]
    session = _create_session(client, admin_auth_headers, project_id)
    dataset = _create_dataset(
        client,
        admin_auth_headers,
        project_id,
        question_id,
    )

    for experiment in experiments:
        for _ in range(2):
            assert (
                client.put(
                    f"/experiments/{experiment['experiment_id']}/sessions/{session['session_id']}",
                    headers=admin_auth_headers,
                ).status_code
                == 200
            )
            assert (
                client.put(
                    f"/experiments/{experiment['experiment_id']}/datasets/{dataset['dataset_id']}",
                    headers=admin_auth_headers,
                ).status_code
                == 200
            )

    session_experiments = client.get(
        f"/sessions/{session['session_id']}/experiments",
        params={"limit": 1, "offset": 1},
        headers=admin_auth_headers,
    )
    dataset_experiments = client.get(
        f"/datasets/{dataset['dataset_id']}/experiments",
        headers=admin_auth_headers,
    )
    assert session_experiments.status_code == 200
    assert session_experiments.json()["meta"] == {
        "limit": 1,
        "offset": 1,
        "total": 2,
    }
    assert dataset_experiments.json()["meta"]["total"] == 2

    experiment_id = experiments[0]["experiment_id"]
    assert (
        client.delete(
            f"/experiments/{experiment_id}/sessions/{session['session_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/experiments/{experiment_id}/sessions/{session['session_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/sessions/{session['session_id']}/experiments",
            headers=admin_auth_headers,
        ).json()["meta"]["total"]
        == 1
    )


def test_experiment_references_guard_member_and_primary_question_mutations(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(
        client,
        admin_auth_headers,
        "Reference guards",
    )
    primary_question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="Which question must remain linked?",
    )
    alternate_question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="Which alternate question is available?",
    )
    experiment = _create_experiment(
        client,
        admin_auth_headers,
        project_id,
        primary_question_id,
    )
    session = _create_session(client, admin_auth_headers, project_id)
    dataset = _create_dataset(
        client,
        admin_auth_headers,
        project_id,
        alternate_question_id,
        secondary_question_ids=[primary_question_id],
    )
    experiment_base = f"/experiments/{experiment['experiment_id']}"

    assert (
        client.put(
            f"{experiment_base}/sessions/{session['session_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"{experiment_base}/datasets/{dataset['dataset_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )

    assert (
        client.delete(
            f"/sessions/{session['session_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )
    assert (
        client.delete(
            f"/datasets/{dataset['dataset_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )
    assert (
        client.delete(
            f"/questions/{primary_question_id}",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )
    question_link_update = client.patch(
        f"/datasets/{dataset['dataset_id']}",
        json={
            "question_links": [
                {
                    "question_id": alternate_question_id,
                    "role": "primary",
                }
            ]
        },
        headers=admin_auth_headers,
    )
    assert question_link_update.status_code == 422

    assert (
        client.delete(
            f"{experiment_base}/sessions/{session['session_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"{experiment_base}/datasets/{dataset['dataset_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/sessions/{session['session_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/datasets/{dataset['dataset_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )


def test_experiment_validates_member_project_and_question_semantics(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _create_project(client, admin_auth_headers, "Validation")
    other_project_id = _create_project(client, admin_auth_headers, "Other")
    question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="What belongs here?",
    )
    other_question_id = _create_question(
        client,
        admin_auth_headers,
        project_id,
        text="What belongs elsewhere?",
    )
    cross_project_question_id = _create_question(
        client,
        admin_auth_headers,
        other_project_id,
        text="What belongs to another project?",
    )

    cross_project_create = client.post(
        "/experiments",
        json={
            "project_id": project_id,
            "name": "Cross-project Experiment",
            "primary_question_id": cross_project_question_id,
        },
        headers=admin_auth_headers,
    )
    assert cross_project_create.status_code == 422

    experiment = _create_experiment(
        client,
        admin_auth_headers,
        project_id,
        question_id,
    )
    operational = _create_session(client, admin_auth_headers, project_id)
    mismatched_scientific = _create_session(
        client,
        admin_auth_headers,
        project_id,
        session_type="scientific",
        primary_question_id=other_question_id,
    )
    cross_project_session = _create_session(
        client,
        admin_auth_headers,
        other_project_id,
    )
    matching_dataset = _create_dataset(
        client,
        admin_auth_headers,
        project_id,
        other_question_id,
        secondary_question_ids=[question_id],
    )
    mismatched_dataset = _create_dataset(
        client,
        admin_auth_headers,
        project_id,
        other_question_id,
    )
    base = f"/experiments/{experiment['experiment_id']}"

    assert (
        client.put(
            f"{base}/sessions/{operational['session_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"{base}/sessions/{mismatched_scientific['session_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"{base}/sessions/{cross_project_session['session_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"{base}/datasets/{matching_dataset['dataset_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"{base}/datasets/{mismatched_dataset['dataset_id']}",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )

    wrong_promotion = client.post(
        f"/sessions/{operational['session_id']}/promote",
        json={"primary_question_id": other_question_id},
        headers=admin_auth_headers,
    )
    assert wrong_promotion.status_code == 422
