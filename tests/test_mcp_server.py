from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lab_tracker import mcp_server


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def test_fastmcp_registers_lab_tracker_tools() -> None:
    tools = asyncio.run(mcp_server.server.list_tools())

    names = {tool.name for tool in tools}
    assert "lab_tracker_health" in names
    assert "lab_tracker_readiness" in names
    assert "lab_tracker_search" in names
    assert "lab_tracker_create_note" in names


def test_client_service_login_sends_bearer_auth_to_protected_routes() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/auth/login":
            return _json_response(200, {"data": {"access_token": "token-1"}})
        if request.url.path == "/projects":
            return _json_response(200, {"data": [], "meta": {"total": 0}})
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(
            base_url="http://testserver",
            username="mcp-user",
            password="mcp-pass",
        ),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = client.list_projects()
    finally:
        client.close()

    assert payload["data"] == []
    assert seen == [
        ("POST", "/auth/login", None),
        ("GET", "/projects", "Bearer token-1"),
    ]


def test_client_retries_once_after_expired_token() -> None:
    calls: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/auth/login":
            token = f"token-{sum(1 for _, path, _ in calls if path == '/auth/login')}"
            return _json_response(200, {"data": {"access_token": token}})
        if request.url.path == "/projects":
            if request.headers.get("authorization") == "Bearer token-1":
                return _json_response(401, {"error": {"message": "expired"}})
            return _json_response(200, {"data": [{"project_id": "p1"}]})
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(
            base_url="http://testserver",
            username="mcp-user",
            password="mcp-pass",
        ),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = client.list_projects()
    finally:
        client.close()

    assert payload["data"] == [{"project_id": "p1"}]
    assert calls == [
        ("POST", "/auth/login", None),
        ("GET", "/projects", "Bearer token-1"),
        ("POST", "/auth/login", None),
        ("GET", "/projects", "Bearer token-2"),
    ]


def test_create_project_uses_api_validation_path() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/login":
            return _json_response(200, {"data": {"access_token": "token-1"}})
        if request.url.path == "/projects":
            body = json.loads(request.content.decode("utf-8"))
            assert body == {"name": "Neuron Map", "description": "demo"}
            assert request.headers["authorization"] == "Bearer token-1"
            return _json_response(201, {"data": {"name": "Neuron Map"}})
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(
            base_url="http://testserver",
            username="mcp-user",
            password="mcp-pass",
        ),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = client.create_project(name="Neuron Map", description="demo")
    finally:
        client.close()

    assert payload == {"data": {"name": "Neuron Map"}}
    assert [request.url.path for request in requests] == ["/auth/login", "/projects"]


def test_authenticated_tool_requires_service_credentials() -> None:
    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(lambda request: _json_response(500, {})),
    )

    with pytest.raises(mcp_server.LabTrackerAPIError, match="LAB_TRACKER_MCP_USERNAME"):
        client.list_projects()

    client.close()


def test_public_health_does_not_require_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        assert "authorization" not in request.headers
        return _json_response(200, {"status": "ok"})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        assert client.health() == {"status": "ok"}
    finally:
        client.close()
