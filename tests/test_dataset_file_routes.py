from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lab_tracker.auth import Role


def _count_suffix(root: Path, suffix: str) -> int:
    if not root.exists():
        return 0
    return sum(1 for _ in root.rglob(f"*{suffix}"))


def _admin_headers(client: TestClient) -> dict[str, str]:
    username = f"admin-{uuid4().hex[:8]}"
    password = "secret"
    client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.ADMIN,
    )
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _fail_next_commit(monkeypatch) -> None:
    original_commit = Session.commit
    state = {"failed": False}

    def _commit_once(self, *args, **kwargs):  # noqa: ANN001, ANN202
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("forced commit failure")
        return original_commit(self, *args, **kwargs)

    monkeypatch.setattr(Session, "commit", _commit_once)


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


def test_dataset_commit_requires_attached_file(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers

    project_id = client.post(
        "/projects",
        json={"name": "Commit validation"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Do we require a dataset file before commit?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    activate_response = client.patch(
        f"/questions/{question_id}",
        json={"status": "active"},
        headers=headers,
    )
    assert activate_response.status_code == 200

    dataset_response = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    )
    assert dataset_response.status_code == 201
    dataset_payload = dataset_response.json()["data"]
    dataset_id = dataset_payload["dataset_id"]
    staged_hash = dataset_payload["commit_hash"]

    commit_without_file = client.patch(
        f"/datasets/{dataset_id}",
        json={"status": "committed"},
        headers=headers,
    )
    assert commit_without_file.status_code == 422

    attach_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("data.bin", b"real-data", "application/octet-stream")},
        headers=headers,
    )
    assert attach_response.status_code == 201

    commit_with_file = client.patch(
        f"/datasets/{dataset_id}",
        json={"status": "committed"},
        headers=headers,
    )
    assert commit_with_file.status_code == 200
    committed_payload = commit_with_file.json()["data"]
    assert committed_payload["status"] == "committed"
    assert committed_payload["commit_hash"] != staged_hash


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


def test_dataset_file_upload_cleans_up_storage_when_request_commit_fails(app, monkeypatch):
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = _admin_headers(client)
        project_id = client.post(
            "/projects",
            json={"name": "Upload rollback"},
            headers=headers,
        ).json()["data"]["project_id"]
        question_id = client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": "Does upload rollback clean storage?",
                "question_type": "descriptive",
            },
            headers=headers,
        ).json()["data"]["question_id"]
        dataset_id = client.post(
            "/datasets",
            json={"project_id": project_id, "primary_question_id": question_id},
            headers=headers,
        ).json()["data"]["dataset_id"]

        storage_root = Path(client.app.state.file_storage_backend.base_path)
        _fail_next_commit(monkeypatch)

        response = client.post(
            f"/datasets/{dataset_id}/files",
            files={"file": ("rollback.txt", b"rollback-bytes", "text/plain")},
            headers=headers,
        )

        assert response.status_code == 500
        assert _count_suffix(storage_root, ".bin") == 0
        assert _count_suffix(storage_root, ".json") == 0

        listed = client.get(f"/datasets/{dataset_id}/files", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["meta"]["total"] == 0


def test_dataset_file_delete_preserves_storage_when_request_commit_fails(app, monkeypatch):
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = _admin_headers(client)
        project_id = client.post(
            "/projects",
            json={"name": "Delete rollback"},
            headers=headers,
        ).json()["data"]["project_id"]
        question_id = client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": "Does delete rollback preserve storage?",
                "question_type": "descriptive",
            },
            headers=headers,
        ).json()["data"]["question_id"]
        dataset_id = client.post(
            "/datasets",
            json={"project_id": project_id, "primary_question_id": question_id},
            headers=headers,
        ).json()["data"]["dataset_id"]
        upload_response = client.post(
            f"/datasets/{dataset_id}/files",
            files={"file": ("rollback.txt", b"rollback-bytes", "text/plain")},
            headers=headers,
        )
        assert upload_response.status_code == 201
        file_id = upload_response.json()["data"]["file_id"]

        storage_root = Path(client.app.state.file_storage_backend.base_path)
        assert _count_suffix(storage_root, ".bin") == 1
        assert _count_suffix(storage_root, ".json") == 1

        _fail_next_commit(monkeypatch)
        response = client.delete(
            f"/datasets/{dataset_id}/files/{file_id}",
            headers=headers,
        )

        assert response.status_code == 500
        assert _count_suffix(storage_root, ".bin") == 1
        assert _count_suffix(storage_root, ".json") == 1

        listed = client.get(f"/datasets/{dataset_id}/files", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["meta"]["total"] == 1


def test_dataset_delete_removes_attached_files_from_storage(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    headers = admin_auth_headers
    project_id = client.post(
        "/projects",
        json={"name": "Dataset delete cleanup"},
        headers=headers,
    ).json()["data"]["project_id"]
    question_id = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does dataset delete remove stored files?",
            "question_type": "descriptive",
        },
        headers=headers,
    ).json()["data"]["question_id"]
    dataset_id = client.post(
        "/datasets",
        json={"project_id": project_id, "primary_question_id": question_id},
        headers=headers,
    ).json()["data"]["dataset_id"]

    upload_response = client.post(
        f"/datasets/{dataset_id}/files",
        files={"file": ("cleanup.txt", b"cleanup-bytes", "text/plain")},
        headers=headers,
    )
    assert upload_response.status_code == 201

    storage_root = Path(client.app.state.file_storage_backend.base_path)
    assert _count_suffix(storage_root, ".bin") == 1
    assert _count_suffix(storage_root, ".json") == 1

    delete_response = client.delete(f"/datasets/{dataset_id}", headers=headers)
    assert delete_response.status_code == 200
    assert _count_suffix(storage_root, ".bin") == 0
    assert _count_suffix(storage_root, ".json") == 0
