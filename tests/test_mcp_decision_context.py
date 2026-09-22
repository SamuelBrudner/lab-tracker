"""MCP decision-context tool forwards person/window scope (review finding M37)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from lab_tracker.mcp_api_client import (
    LabTrackerAPIClient,
    LabTrackerAPIValidationError,
    MCPSettings,
)
from lab_tracker.mcp_tools import read as read_tools

AUTHOR_ID = "123e4567-e89b-12d3-a456-426614174000"
FAR_FUTURE = "2999-01-01T00:00:00+00:00"


def _capturing_client(bodies: list[dict[str, object]]) -> LabTrackerAPIClient:
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {}, "meta": {}})

    return LabTrackerAPIClient(
        MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )


def test_tool_forwards_created_by_since_until(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, object]] = []
    api_client = _capturing_client(bodies)
    read_tools.close_cached_read_client()
    monkeypatch.setattr(read_tools, "client_from_env", lambda: api_client)
    try:
        payload = read_tools.lab_tracker_get_decision_context(
            task_kind="progress_review",
            query="What did Alice do since the last meeting?",
            project_id=str(uuid.UUID(int=7)),
            created_by=AUTHOR_ID,
            since="2025-07-01T00:00:00Z",
            until="2025-07-31T23:59:59+00:00",
        )
    finally:
        read_tools.close_cached_read_client()

    assert "error" not in payload, payload
    assert bodies[0]["created_by"] == AUTHOR_ID
    assert bodies[0]["since"] == "2025-07-01T00:00:00+00:00"
    assert bodies[0]["until"] == "2025-07-31T23:59:59+00:00"


def test_client_omits_absent_scope_filters() -> None:
    bodies: list[dict[str, object]] = []
    api_client = _capturing_client(bodies)
    try:
        api_client.get_decision_context(task_kind="summary", query="state")
    finally:
        api_client.close()

    for key in ("created_by", "since", "until"):
        assert bodies[0].get(key) is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"created_by": "alice"}, "created_by must be a Lab Tracker UUID"),
        ({"since": "last tuesday"}, "since must be an ISO 8601 datetime"),
        ({"until": "2025-13-01"}, "until must be an ISO 8601 datetime"),
        (
            {"since": "2025-08-01T00:00:00+00:00", "until": "2025-07-01T00:00:00+00:00"},
            "since must not be later than until",
        ),
    ],
)
def test_client_rejects_invalid_scope_before_transport(
    kwargs: dict[str, str], message: str
) -> None:
    bodies: list[dict[str, object]] = []
    api_client = _capturing_client(bodies)
    try:
        with pytest.raises(LabTrackerAPIValidationError, match=message):
            api_client.get_decision_context(task_kind="progress_review", query="q", **kwargs)
    finally:
        api_client.close()
    assert bodies == []


def test_tool_returns_structured_error_for_invalid_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_client = _capturing_client([])
    read_tools.close_cached_read_client()
    monkeypatch.setattr(read_tools, "client_from_env", lambda: api_client)
    try:
        payload = read_tools.lab_tracker_get_decision_context(
            task_kind="progress_review",
            query="q",
            created_by="not-a-uuid",
        )
    finally:
        read_tools.close_cached_read_client()

    assert payload["error"]["code"] == "validation_error"
    assert "created_by" in payload["error"]["message"]


def test_progress_review_through_mcp_is_person_and_window_scoped(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = client.post(
        "/projects", json={"name": "MCP briefing project"}, headers=admin_auth_headers
    ).json()["data"]["project_id"]
    note_response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Ran the odor control."},
        headers=admin_auth_headers,
    )
    assert note_response.status_code == 201, note_response.text
    note_id = note_response.json()["data"]["note_id"]
    author = client.get("/auth/me", headers=admin_auth_headers).json()["data"]["user_id"]

    def bridge_to_application(request: httpx.Request) -> httpx.Response:
        response = client.request(
            request.method,
            request.url.path,
            json=json.loads(request.content),
            headers=admin_auth_headers,
        )
        return httpx.Response(response.status_code, json=response.json())

    api_client = LabTrackerAPIClient(
        MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(bridge_to_application),
    )
    read_tools.close_cached_read_client()
    monkeypatch.setattr(read_tools, "client_from_env", lambda: api_client)

    def briefing(**scope: str) -> dict[str, object]:
        payload = read_tools.lab_tracker_get_decision_context(
            task_kind="progress_review",
            query="What did this person do since the last meeting?",
            project_id=project_id,
            **scope,
        )
        assert "error" not in payload, payload
        data = payload["data"]
        assert isinstance(data, dict)
        return data

    try:
        in_window = briefing(created_by=author, since="2025-07-01T00:00:00+00:00")
        after_window = briefing(created_by=author, since=FAR_FUTURE)
        other_person = briefing(created_by=str(uuid.UUID(int=99)))
    finally:
        read_tools.close_cached_read_client()

    assert note_id in {note["note_id"] for note in in_window["notes"]}
    assert after_window["notes"] == []
    assert other_person["notes"] == []
