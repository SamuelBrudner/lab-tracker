from __future__ import annotations

import base64
import json

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

    file_upload_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("data.bin", b"integration-test-data", "application/octet-stream")},
        headers=headers,
    )
    assert file_upload_response.status_code == 201

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


def test_analysis_commit_route_is_atomic_on_failure(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_id = client.post(
        "/projects",
        json={"name": "Atomic analysis"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does atomic commit hold?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
        },
        headers=headers,
    ).json()["data"]["dataset_id"]
    upload_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("data.bin", b"atomic-test", "application/octet-stream")},
        headers=headers,
    )
    assert upload_response.status_code == 201
    commit_dataset = client.patch(
        f"/datasets/{dataset_id}",
        json={"status": "committed"},
        headers=headers,
    )
    assert commit_dataset.status_code == 200

    analysis_response = client.post(
        "/analyses",
        json={
            "project_id": project_id,
            "dataset_ids": [dataset_id],
            "method_hash": "method-1",
            "code_version": "v1",
        },
        headers=headers,
    )
    assert analysis_response.status_code == 201
    analysis_id = analysis_response.json()["data"]["analysis_id"]

    commit_response = client.post(
        f"/analyses/{analysis_id}/commit",
        json={
            "environment_hash": "env-1",
            "claims": [
                {
                    "statement": "Signal is stable",
                    "confidence": 0.8,
                    "status": "supported",
                }
            ],
            "visualizations": [
                {
                    "viz_type": "line",
                    "file_path": "figs/signal.png",
                    "related_claim_ids": ["11111111-1111-1111-1111-111111111111"],
                }
            ],
        },
        headers=headers,
    )
    assert commit_response.status_code == 404

    analysis_get = client.get(f"/analyses/{analysis_id}", headers=headers)
    claims_get = client.get("/claims", params={"project_id": project_id}, headers=headers)
    visualizations_get = client.get(
        "/visualizations",
        params={"analysis_id": analysis_id},
        headers=headers,
    )

    assert analysis_get.status_code == 200
    assert analysis_get.json()["data"]["status"] == "staged"
    assert claims_get.status_code == 200
    assert claims_get.json()["data"] == []
    assert visualizations_get.status_code == 200
    assert visualizations_get.json()["data"] == []


def test_question_list_paginates_beyond_200_records(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Pagination Project"},
        headers=headers,
    ).json()["data"]["project_id"]

    for index in range(205):
        response = client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": f"Paginated question {index}",
                "question_type": "descriptive",
            },
            headers=headers,
        )
        assert response.status_code == 201

    page = client.get(
        "/questions",
        params={"project_id": project_id, "limit": 200, "offset": 200},
        headers=headers,
    )
    assert page.status_code == 200
    payload = page.json()
    assert payload["meta"] == {"limit": 200, "offset": 200, "total": 205}
    assert len(payload["data"]) == 5


def test_note_routes_support_target_filters_and_multipart_upload(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Notes Project"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Which dataset note is linked?",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={
            "project_id": project_id,
            "primary_question_id": question_id,
        },
        headers=headers,
    ).json()["data"]["dataset_id"]

    multipart_upload = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "transcribed_text": "typed capture",
            "targets": json.dumps(
                [
                    {
                        "entity_id": dataset_id,
                        "entity_type": "dataset",
                    }
                ]
            ),
            "metadata": json.dumps({"source": "camera"}),
        },
        files={"file": ("capture.txt", b"raw-capture", "text/plain")},
        headers=headers,
    )
    assert multipart_upload.status_code == 201
    multipart_payload = multipart_upload.json()["data"]
    assert multipart_payload["transcribed_text"] == "typed capture"
    assert multipart_payload["metadata"] == {"source": "camera"}
    assert multipart_payload["raw_asset"]["filename"] == "capture.txt"
    assert multipart_payload["targets"][0]["entity_type"] == "dataset"
    assert multipart_payload["targets"][0]["entity_id"] == dataset_id

    raw_download = client.get(
        f"/notes/{multipart_payload['note_id']}/raw",
        headers=headers,
    )
    assert raw_download.status_code == 200
    assert raw_download.content == b"raw-capture"

    legacy_upload = client.post(
        "/notes/upload",
        json={
            "content_base64": base64.b64encode(b"legacy-capture").decode("ascii"),
            "content_type": "text/plain",
            "filename": "legacy.txt",
            "project_id": project_id,
        },
        headers=headers,
    )
    assert legacy_upload.status_code == 201

    filtered = client.get(
        "/notes",
        params={
            "project_id": project_id,
            "target_entity_type": "dataset",
            "target_entity_id": dataset_id,
        },
        headers=headers,
    )
    assert filtered.status_code == 200
    filtered_payload = filtered.json()
    assert filtered_payload["meta"]["total"] == 1
    assert _ids(filtered_payload["data"], "note_id") == {multipart_payload["note_id"]}
