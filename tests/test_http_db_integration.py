from __future__ import annotations

from fastapi.testclient import TestClient


def _ids(payload: list[dict[str, object]], key: str) -> set[str]:
    return {str(item[key]) for item in payload}


def test_core_entity_crud_routes_use_database_persistence(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_response = client.post(
        "/projects",
        json={"name": "CRUD Integration", "description": "initial"},
        headers=headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]

    project_get_response = client.get(f"/projects/{project_id}", headers=headers)
    assert project_get_response.status_code == 200
    assert project_get_response.json()["data"]["name"] == "CRUD Integration"

    project_list_response = client.get("/projects", headers=headers)
    assert project_list_response.status_code == 200
    assert project_id in _ids(project_list_response.json()["data"], "project_id")

    project_update_response = client.patch(
        f"/projects/{project_id}",
        json={"description": "updated"},
        headers=headers,
    )
    assert project_update_response.status_code == 200
    assert project_update_response.json()["data"]["description"] == "updated"

    question_create_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the integration path persist?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert question_create_response.status_code == 201
    question_id = question_create_response.json()["data"]["question_id"]

    question_get_response = client.get(f"/questions/{question_id}", headers=headers)
    assert question_get_response.status_code == 200
    assert question_get_response.json()["data"]["project_id"] == project_id

    question_list_response = client.get(
        "/questions",
        params={"project_id": project_id},
        headers=headers,
    )
    assert question_list_response.status_code == 200
    assert question_id in _ids(question_list_response.json()["data"], "question_id")

    question_update_response = client.patch(
        f"/questions/{question_id}",
        json={"status": "active", "text": "Does the database-backed API persist?"},
        headers=headers,
    )
    assert question_update_response.status_code == 200
    assert question_update_response.json()["data"]["status"] == "active"

    dataset_create_response = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
        },
        headers=headers,
    )
    assert dataset_create_response.status_code == 201
    dataset_id = dataset_create_response.json()["data"]["dataset_id"]

    dataset_get_response = client.get(f"/datasets/{dataset_id}", headers=headers)
    assert dataset_get_response.status_code == 200
    assert dataset_get_response.json()["data"]["project_id"] == project_id

    dataset_list_response = client.get(
        "/datasets",
        params={"project_id": project_id},
        headers=headers,
    )
    assert dataset_list_response.status_code == 200
    assert dataset_id in _ids(dataset_list_response.json()["data"], "dataset_id")

    dataset_update_response = client.patch(
        f"/datasets/{dataset_id}",
        json={"status": "committed"},
        headers=headers,
    )
    assert dataset_update_response.status_code == 200
    assert dataset_update_response.json()["data"]["status"] == "committed"

    note_create_response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "capture log"},
        headers=headers,
    )
    assert note_create_response.status_code == 201
    note_id = note_create_response.json()["data"]["note_id"]

    note_get_response = client.get(f"/notes/{note_id}", headers=headers)
    assert note_get_response.status_code == 200
    assert note_get_response.json()["data"]["project_id"] == project_id

    note_list_response = client.get(
        "/notes",
        params={"project_id": project_id},
        headers=headers,
    )
    assert note_list_response.status_code == 200
    assert note_id in _ids(note_list_response.json()["data"], "note_id")

    note_update_response = client.patch(
        f"/notes/{note_id}",
        json={"transcribed_text": "transcribed capture", "metadata": {"source": "integration"}},
        headers=headers,
    )
    assert note_update_response.status_code == 200
    note_payload = note_update_response.json()["data"]
    assert note_payload["transcribed_text"] == "transcribed capture"
    assert note_payload["metadata"] == {"source": "integration"}

    note_delete_response = client.delete(f"/notes/{note_id}", headers=headers)
    assert note_delete_response.status_code == 200
    assert client.get(f"/notes/{note_id}", headers=headers).status_code == 404

    dataset_delete_response = client.delete(f"/datasets/{dataset_id}", headers=headers)
    assert dataset_delete_response.status_code == 200
    assert client.get(f"/datasets/{dataset_id}", headers=headers).status_code == 404

    question_delete_response = client.delete(f"/questions/{question_id}", headers=headers)
    assert question_delete_response.status_code == 200
    assert client.get(f"/questions/{question_id}", headers=headers).status_code == 404

    project_delete_response = client.delete(f"/projects/{project_id}", headers=headers)
    assert project_delete_response.status_code == 200
    assert client.get(f"/projects/{project_id}", headers=headers).status_code == 404
