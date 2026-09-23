from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient


def _upload_raw_note(
    client: TestClient,
    headers: dict[str, str],
    content: bytes,
) -> str:
    project_id = client.post(
        "/projects",
        json={"name": "Raw download streaming"},
        headers=headers,
    ).json()["data"]["project_id"]
    response = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("capture.bin", content, "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["note_id"]


def test_binary_raw_download_streams_without_loading_the_whole_asset(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"0123456789" * 50_000
    note_id = _upload_raw_note(client, admin_auth_headers, content)
    storage = client.app.state.raw_note_storage

    def fail_whole_read(_storage_id):  # noqa: ANN001, ANN202
        raise AssertionError("binary downloads must stream, not read the whole asset")

    monkeypatch.setattr(storage, "read", fail_whole_read)

    response = client.get(f"/notes/{note_id}/raw", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    assert response.content == content
    assert response.headers["content-length"] == str(len(content))
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == 'attachment; filename="capture.bin"'


def test_binary_raw_download_of_missing_blob_is_not_found_before_streaming(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    note_id = _upload_raw_note(client, admin_auth_headers, b"raw-capture")
    storage_root = client.app.state.raw_note_storage._base_path
    for blob in storage_root.iterdir():
        blob.unlink()

    response = client.get(f"/notes/{note_id}/raw", headers=admin_auth_headers)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["message"] == "Raw note content not found."


def test_json_raw_download_keeps_base64_envelope(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    note_id = _upload_raw_note(client, admin_auth_headers, b"raw-capture")

    response = client.get(
        f"/notes/{note_id}/raw",
        headers={**admin_auth_headers, "Accept": "application/json"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert base64.b64decode(data["content_base64"]) == b"raw-capture"
    assert data["size_bytes"] == len(b"raw-capture")
