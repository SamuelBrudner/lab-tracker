"""Shared transport + streaming upload behavior (lab-tracker-ggzs.5)."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from lab_tracker.mcp_api_client import (
    LabTrackerAPIClient,
    LabTrackerAPIValidationError,
    MCPSettings,
)
from lab_tracker_client import LabTracker, LTValidationError
from lab_tracker_client.transport import HttpTransport

_client_module = sys.modules["lab_tracker_client.client"]


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def test_both_clients_share_the_http_transport() -> None:
    sdk = LabTracker(base_url="http://testserver")
    mcp = LabTrackerAPIClient(MCPSettings(base_url="http://testserver"))
    try:
        assert isinstance(sdk._transport, HttpTransport)
        assert isinstance(mcp._transport, HttpTransport)
    finally:
        sdk.close()
        mcp.close()


def test_upload_note_file_rejects_oversize_before_any_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_client_module, "MAX_UPLOAD_BYTES", 4)
    big = tmp_path / "big.bin"
    big.write_bytes(b"0123456789")  # 10 bytes > 4-byte limit

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("oversize upload must be rejected before any request")

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as lt, pytest.raises(LTValidationError, match="over the"):
        lt.upload_note_file(project_id="project-1", file_path=big)


def test_upload_note_file_streams_without_reading_the_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence.md"
    evidence.write_text("streamed body", encoding="utf-8")

    # If the upload path ever slurped the whole file into memory, it would call
    # read_bytes; make that fail so a passing test proves it streams the handle.
    def _forbid_read_bytes(self: Path) -> bytes:
        raise AssertionError("upload must stream the file, not read it into memory")

    monkeypatch.setattr(Path, "read_bytes", _forbid_read_bytes)

    uploaded: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            uploaded.append(request.content)
            return _json_response(201, {"data": {"note_id": "note-streamed"}})
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(handler)
    ) as lt:
        note = lt.upload_note_file(project_id="project-1", file_path=evidence)

    assert note.id == "note-streamed"
    assert b"streamed body" in uploaded[0]


def test_streaming_upload_reopens_the_file_on_401_retry(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.md"
    evidence.write_text("retryable body", encoding="utf-8")
    upload_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/login":
            return _json_response(200, {"data": {"access_token": "token"}})
        if path == "/notes/upload-file":
            upload_bodies.append(request.content)
            if len(upload_bodies) == 1:
                return _json_response(401, {"error": {"message": "expired"}})
            return _json_response(201, {"data": {"note_id": "note-retried"}})
        return _json_response(404, {"error": {"message": "not found"}})

    with LabTracker(
        base_url="http://testserver",
        username="u",
        password="p",
        transport=httpx.MockTransport(handler),
    ) as lt:
        note = lt.upload_note_file(project_id="project-1", file_path=evidence)

    assert note.id == "note-retried"
    # Both attempts carried the file bytes: the retry re-opened the handle
    # rather than replaying a consumed stream.
    assert len(upload_bodies) == 2
    assert all(b"retryable body" in body for body in upload_bodies)


def test_upload_visualization_file_rejects_oversize_and_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    viz = tmp_path / "figure.png"
    viz.write_bytes(b"streamed viz bytes")

    def _forbid_read_bytes(self: Path) -> bytes:
        raise AssertionError("visualization upload must stream, not read into memory")

    uploaded: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/login":
            return _json_response(200, {"data": {"access_token": "token"}})
        if path.endswith("/file"):
            uploaded.append(request.content)
            return _json_response(200, {"data": {"viz_id": "viz-1"}})
        return _json_response(404, {"error": {"message": "not found"}})

    monkeypatch.setattr(Path, "read_bytes", _forbid_read_bytes)
    client = LabTrackerAPIClient(
        MCPSettings(base_url="http://testserver", username="u", password="p"),
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.upload_visualization_file(viz_id="viz-1", file_path=str(viz))
        assert result["data"]["viz_id"] == "viz-1"
        assert b"streamed viz bytes" in uploaded[0]

        # Oversize is rejected before transfer (upload passes its module-level
        # MAX_UPLOAD_BYTES explicitly, so patching it here takes effect).
        import lab_tracker.mcp_api_client as mcp_module

        monkeypatch.setattr(mcp_module, "MAX_UPLOAD_BYTES", 4)
        with pytest.raises(LabTrackerAPIValidationError, match="over the"):
            client.upload_visualization_file(viz_id="viz-1", file_path=str(viz))
    finally:
        client.close()
