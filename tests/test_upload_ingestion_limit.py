"""Upload size limits must hold while the request body streams in (M18).

Starlette's multipart parser spools file parts to disk with no size cap, so a
limit that runs in the route handler only fires after the whole upload was
received. These tests drive the ASGI app directly with a chunked body (no
Content-Length) and count how many bytes the app pulls from the "client".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

_BOUNDARY = "ingestionlimitboundary"
_CHUNK_BYTES = 16 * 1024


def _multipart_chunks(
    *,
    project_id: str,
    payload_bytes: int,
) -> Iterator[bytes]:
    yield (
        f"--{_BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="project_id"\r\n\r\n'
        f"{project_id}\r\n"
        f"--{_BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    remaining = payload_bytes
    while remaining > 0:
        size = min(_CHUNK_BYTES, remaining)
        yield b"x" * size
        remaining -= size
    yield f"\r\n--{_BOUNDARY}--\r\n".encode()


class _ASGIResult:
    def __init__(self, status: int, body: bytes, pulled_bytes: int) -> None:
        self.status = status
        self.body = body
        self.pulled_bytes = pulled_bytes

    def json(self) -> Any:
        return json.loads(self.body)


def _post_streamed(
    client: TestClient,
    path: str,
    *,
    headers: dict[str, str],
    chunks: Iterator[bytes],
    content_length: int | None = None,
) -> _ASGIResult:
    """POST ``chunks`` one ASGI message at a time, counting bytes the app reads."""

    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    raw_headers.append(
        (b"content-type", f"multipart/form-data; boundary={_BOUNDARY}".encode())
    )
    if content_length is not None:
        raw_headers.append((b"content-length", str(content_length).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), *raw_headers],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "extensions": {},
        "state": client.app_state.copy(),
    }
    pulled = 0
    body_done = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal pulled, body_done
        if body_done:
            return {"type": "http.disconnect"}
        chunk = next(chunks, None)
        if chunk is None:
            body_done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        pulled += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    async def run() -> None:
        await client.app(scope, receive, send)

    assert client.portal is not None
    client.portal.call(run)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return _ASGIResult(start["status"], body, pulled)


@pytest.fixture()
def note_upload_project(client: TestClient, admin_auth_headers: dict[str, str]) -> str:
    response = client.post(
        "/projects",
        json={"name": "Upload ingestion limit"},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


@pytest.fixture()
def storage_writes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    writes: list[str] = []
    storage = client.app.state.raw_note_storage
    original_store_stream = storage.store_stream

    def spy_store_stream(*args: Any, **kwargs: Any) -> Any:
        writes.append("store_stream")
        return original_store_stream(*args, **kwargs)

    monkeypatch.setattr(storage, "store_stream", spy_store_stream)
    return writes


@pytest.mark.parametrize("endpoint", ["/notes/upload-file", "/notes/quick-capture"])
def test_chunked_multipart_upload_is_rejected_before_the_body_is_spooled(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    note_upload_project: str,
    storage_writes: list[str],
    endpoint: str,
) -> None:
    max_bytes = 64 * 1024
    client.app.state.settings.max_upload_bytes = max_bytes

    result = _post_streamed(
        client,
        endpoint,
        headers=admin_auth_headers,
        chunks=_multipart_chunks(project_id=note_upload_project, payload_bytes=3 * 1024 * 1024),
    )

    assert result.status == 413, result.body
    assert result.json()["error"]["code"] == "payload_too_large"
    assert str(max_bytes) in result.json()["error"]["message"]
    # The app stops reading at the first chunk past the limit.
    assert result.pulled_bytes <= max_bytes + _CHUNK_BYTES
    assert storage_writes == []


def test_chunked_dataset_file_upload_is_rejected_before_the_body_is_spooled(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    note_upload_project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = client.post(
        "/questions",
        json={
            "project_id": note_upload_project,
            "text": "Is the dataset upload bounded?",
            "question_type": "descriptive",
        },
        headers=admin_auth_headers,
    )
    assert question.status_code == 201
    dataset = client.post(
        "/datasets",
        json={
            "project_id": note_upload_project,
            "primary_question_id": question.json()["data"]["question_id"],
        },
        headers=admin_auth_headers,
    )
    assert dataset.status_code == 201
    dataset_id = dataset.json()["data"]["dataset_id"]
    writes: list[str] = []
    file_storage = client.app.state.file_storage_backend
    for method_name in ("store", "store_stream", "store_chunks"):
        if hasattr(file_storage, method_name):
            monkeypatch.setattr(
                file_storage,
                method_name,
                lambda *_args, _name=method_name, **_kwargs: writes.append(_name),
            )
    max_bytes = 64 * 1024
    client.app.state.settings.max_upload_bytes = max_bytes

    result = _post_streamed(
        client,
        f"/datasets/{dataset_id}/files",
        headers=admin_auth_headers,
        chunks=_multipart_chunks(project_id=note_upload_project, payload_bytes=3 * 1024 * 1024),
    )

    assert result.status == 413, result.body
    assert result.json()["error"]["code"] == "payload_too_large"
    assert result.pulled_bytes <= max_bytes + _CHUNK_BYTES
    assert writes == []


def test_declared_oversized_content_length_is_rejected_without_reading_the_body(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    note_upload_project: str,
    storage_writes: list[str],
) -> None:
    max_bytes = 64 * 1024
    client.app.state.settings.max_upload_bytes = max_bytes

    result = _post_streamed(
        client,
        "/notes/upload-file",
        headers=admin_auth_headers,
        chunks=_multipart_chunks(project_id=note_upload_project, payload_bytes=3 * 1024 * 1024),
        content_length=3 * 1024 * 1024 + 512,
    )

    assert result.status == 413, result.body
    assert result.json()["error"]["code"] == "payload_too_large"
    assert result.pulled_bytes == 0
    assert storage_writes == []


def test_chunked_multipart_upload_within_the_limit_is_stored_intact(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    note_upload_project: str,
    storage_writes: list[str],
) -> None:
    client.app.state.settings.max_upload_bytes = 256 * 1024
    payload_bytes = 100 * 1024

    result = _post_streamed(
        client,
        "/notes/upload-file",
        headers=admin_auth_headers,
        chunks=_multipart_chunks(project_id=note_upload_project, payload_bytes=payload_bytes),
    )

    assert result.status == 201, result.body
    note = result.json()["data"]
    assert note["raw_asset"]["size_bytes"] == payload_bytes
    assert storage_writes == ["store_stream"]
    raw = client.get(f"/notes/{note['note_id']}/raw", headers=admin_auth_headers)
    assert raw.status_code == 200
    assert raw.content == b"x" * payload_bytes
