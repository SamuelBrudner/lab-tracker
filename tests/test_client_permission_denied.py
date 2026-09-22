"""Client handling of 403 permission denials versus 401 credential rejections.

A ``403 forbidden`` means the credential is valid but lacks permission: the lt
SDK and the MCP client must surface the denial without re-logging in, replaying
the request, or telling the user the token was rejected. A ``401`` keeps the
existing refresh-once behaviour.
"""

from __future__ import annotations

import httpx
import pytest

from lab_tracker import mcp_api_client
from lab_tracker_client import LabTracker, LTAPIError, LTPermissionDeniedError

_FORBIDDEN = {
    "error": {
        "code": "forbidden",
        "message": "Project contributor access required.",
        "issues": None,
    }
}


def _forbidden_handler(seen: list[tuple[str, str, str | None]]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"data": {"access_token": "token-1"}})
        return httpx.Response(403, json=_FORBIDDEN)

    return handler


def test_lt_supplied_token_403_is_permission_error_not_token_rejection() -> None:
    seen: list[tuple[str, str, str | None]] = []

    with (
        LabTracker(
            base_url="http://testserver",
            access_token="lpat_valid",
            transport=httpx.MockTransport(_forbidden_handler(seen)),
        ) as lt,
        pytest.raises(LTPermissionDeniedError) as excinfo,
    ):
        lt.commit_note("note-1")

    assert str(excinfo.value) == "Project contributor access required."
    assert "rejected" not in str(excinfo.value)
    assert isinstance(excinfo.value, LTAPIError)
    # No refresh/login and no replay: exactly one request with the same token.
    assert seen == [("PATCH", "/notes/note-1", "Bearer lpat_valid")]


def test_lt_login_credentials_403_does_not_relogin_and_replay() -> None:
    seen: list[tuple[str, str, str | None]] = []

    with (
        LabTracker(
            base_url="http://testserver",
            username="svc",
            password="secret",
            transport=httpx.MockTransport(_forbidden_handler(seen)),
        ) as lt,
        pytest.raises(LTPermissionDeniedError),
    ):
        lt.commit_note("note-1")

    assert seen == [
        ("POST", "/auth/login", None),
        ("PATCH", "/notes/note-1", "Bearer token-1"),
    ]


def test_lt_streaming_upload_403_is_permission_error(tmp_path) -> None:
    seen: list[tuple[str, str, str | None]] = []
    upload = tmp_path / "note.txt"
    upload.write_text("content", encoding="utf-8")

    with (
        LabTracker(
            base_url="http://testserver",
            access_token="lpat_valid",
            transport=httpx.MockTransport(_forbidden_handler(seen)),
        ) as lt,
        pytest.raises(LTPermissionDeniedError, match="contributor access required"),
    ):
        lt.upload_note_file(project_id="project-1", file_path=upload)

    assert seen == [("POST", "/notes/upload-file", "Bearer lpat_valid")]


def test_mcp_static_key_403_is_permission_error_without_credential_advice() -> None:
    seen: list[tuple[str, str, str | None]] = []
    client = mcp_api_client.LabTrackerAPIClient(
        mcp_api_client.MCPSettings(base_url="http://testserver", api_key="lpat_valid"),
        transport=httpx.MockTransport(_forbidden_handler(seen)),
    )

    try:
        with pytest.raises(mcp_api_client.LabTrackerAPIPermissionError) as excinfo:
            client.list_projects()
    finally:
        client.close()

    error = excinfo.value
    assert not isinstance(error, mcp_api_client.LabTrackerAPIAuthError)
    assert error.status_code == 403
    assert error.code == "forbidden"
    assert "LAB_TRACKER_MCP_API_KEY" not in str(error)
    assert seen == [("GET", "/projects", "Bearer lpat_valid")]

    envelope = mcp_api_client.lab_tracker_api_error("lab_tracker_list_projects", error)
    assert envelope["error"]["code"] == "forbidden"
    assert envelope["error"]["status_code"] == 403
    assert envelope["next_action"]["action"] == "request_access"


def test_mcp_401_remains_auth_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "auth_error", "message": "Invalid personal access token."}},
        )

    client = mcp_api_client.LabTrackerAPIClient(
        mcp_api_client.MCPSettings(base_url="http://testserver", api_key="lpat_revoked"),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(mcp_api_client.LabTrackerAPIAuthError) as excinfo:
            client.list_projects()
    finally:
        client.close()

    assert excinfo.value.status_code == 401
    envelope = mcp_api_client.lab_tracker_api_error("lab_tracker_list_projects", excinfo.value)
    assert envelope["next_action"]["action"] == "revise_request_or_credentials"
