from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient


def _count_suffix(root: Path, suffix: str) -> int:
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob(f"*{suffix}"))


def test_dataset_file_upload_list_delete_flow(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_response = client.post(
        "/projects",
        json={"name": "Dataset file upload", "description": "integration"},
        headers=headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]

    question_response = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can datasets accept file attachments?",
            "question_type": "descriptive",
        },
        headers=headers,
    )
    assert question_response.status_code == 201
    question_id = question_response.json()["data"]["question_id"]

    dataset_response = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    )
    assert dataset_response.status_code == 201
    dataset_id = dataset_response.json()["data"]["dataset_id"]

    storage_backend = client.app.state.file_storage_backend
    base_path = Path(storage_backend.base_path)
    assert _count_suffix(base_path, ".bin") == 0
    assert _count_suffix(base_path, ".json") == 0

    content = b"hello world"
    checksum = hashlib.sha256(content).hexdigest()

    upload_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("example.txt", content, "text/plain")},
        headers=headers,
    )
    assert upload_response.status_code == 201
    file_payload = upload_response.json()["data"]
    assert file_payload["path"] == "example.txt"
    assert file_payload["checksum"] == checksum
    assert file_payload["size_bytes"] == len(content)
    assert "file_id" in file_payload
    file_id = file_payload["file_id"]

    assert _count_suffix(base_path, ".bin") == 1
    assert _count_suffix(base_path, ".json") == 1

    list_response = client.get(
        f"/datasets/{dataset_id}/files",
        headers=headers,
    )
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["meta"]["total"] == 1
    assert list_payload["data"][0]["file_id"] == file_id

    delete_response = client.delete(
        f"/datasets/{dataset_id}/files/{file_id}",
        headers=headers,
    )
    assert delete_response.status_code == 200

    assert _count_suffix(base_path, ".bin") == 0
    assert _count_suffix(base_path, ".json") == 0

    list_after_delete = client.get(
        f"/datasets/{dataset_id}/files",
        headers=headers,
    )
    assert list_after_delete.status_code == 200
    assert list_after_delete.json()["meta"]["total"] == 0


def test_dataset_file_download_streams_raw_bytes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_id = client.post(
        "/projects",
        json={"name": "Dataset file download", "description": "integration"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Can I download attached files?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]

    content = b"hello world"
    upload = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("example.txt", content, "text/plain")},
        headers=headers,
    )
    assert upload.status_code == 201
    file_id = upload.json()["data"]["file_id"]

    download = client.get(
        f"/datasets/{dataset_id}/files/{file_id}/download",
        headers=headers,
    )
    assert download.status_code == 200
    assert download.content == content
    assert download.headers["content-disposition"] == 'attachment; filename="example.txt"'
    assert download.headers["content-length"] == str(len(content))
    assert download.headers["content-type"].startswith("text/plain")


def test_dataset_file_download_requires_auth(client: TestClient):
    response = client.get(f"/datasets/{uuid4()}/files/{uuid4()}/download")
    assert response.status_code == 401


def test_dataset_file_upload_rejects_duplicate_path(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_id = client.post("/projects", json={"name": "Dup path"}, headers=headers).json()[
        "data"
    ]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Do we prevent duplicate file paths?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]

    first = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("duplicate.txt", b"first", "text/plain")},
        headers=headers,
    )
    assert first.status_code == 201

    second = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("duplicate.txt", b"second", "text/plain")},
        headers=headers,
    )
    assert second.status_code == 409


def test_dataset_file_upload_requires_staged_dataset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_id = client.post(
        "/projects",
        json={"name": "Status gate"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Do we block attachments after commit?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    # Activate the question (Birth Requirement demands active question).
    activate_response = client.patch(
        f"/questions/{question_id}",
        json={"status": "active"},
        headers=headers,
    )
    assert activate_response.status_code == 200

    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]

    # Attach a file first so the commit validation passes.
    attach_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("data.bin", b"real-data", "application/octet-stream")},
        headers=headers,
    )
    assert attach_response.status_code == 201

    commit_response = client.patch(
        f"/datasets/{dataset_id}",
        json={"status": "committed"},
        headers=headers,
    )
    assert commit_response.status_code == 200

    upload_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("blocked.txt", b"nope", "text/plain")},
        headers=headers,
    )
    assert upload_response.status_code == 422
