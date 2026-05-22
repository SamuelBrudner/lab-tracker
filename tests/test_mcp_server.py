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
    assert "lab_tracker_get_decision_context" in names
    assert "lab_tracker_list_datasets" in names
    assert "lab_tracker_list_analyses" in names
    assert "lab_tracker_list_claims" in names
    assert "lab_tracker_list_visualizations" in names
    assert "lab_tracker_create_note" in names


def test_fastmcp_registers_agent_consultation_policy_resource() -> None:
    resources = asyncio.run(mcp_server.server.list_resources())

    uris = {str(resource.uri) for resource in resources}
    assert "lab-tracker://agent-consultation-policy" in uris


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


def test_client_list_questions_sends_hierarchy_filters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/questions":
            assert request.url.params["project_id"] == "project-1"
            assert request.url.params["parent_question_id"] == "question-root"
            assert request.url.params["ancestor_question_id"] == "question-ancestor"
            return _json_response(200, {"data": [], "meta": {"total": 0}})
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = client.list_questions(
            project_id="project-1",
            parent_question_id="question-root",
            ancestor_question_id="question-ancestor",
        )
    finally:
        client.close()

    assert payload["data"] == []


def test_client_low_level_read_tools_call_retained_routes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/sessions":
            assert request.url.params["project_id"] == "project-1"
            assert request.url.params["session_type"] == "scientific"
            return _json_response(200, {"data": [{"session_id": "session-1"}]})
        if request.url.path == "/datasets":
            assert request.url.params["status"] == "committed"
            return _json_response(200, {"data": [{"dataset_id": "dataset-1"}]})
        if request.url.path == "/analyses":
            assert request.url.params["dataset_id"] == "dataset-1"
            return _json_response(200, {"data": [{"analysis_id": "analysis-1"}]})
        if request.url.path == "/claims":
            assert request.url.params["analysis_id"] == "analysis-1"
            return _json_response(200, {"data": [{"claim_id": "claim-1"}]})
        if request.url.path == "/visualizations":
            assert request.url.params["claim_id"] == "claim-1"
            return _json_response(200, {"data": [{"viz_id": "viz-1"}]})
        if request.url.path == "/datasets/dataset-1/provenance":
            return _json_response(200, {"data": {"@id": "dataset-1"}})
        if request.url.path == "/analyses/analysis-1/provenance":
            return _json_response(200, {"data": {"@id": "analysis-1"}})
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        assert client.list_sessions(
            project_id="project-1",
            session_type="scientific",
        )["data"][0]["session_id"] == "session-1"
        assert client.list_datasets(status="committed")["data"][0]["dataset_id"] == (
            "dataset-1"
        )
        assert client.list_analyses(dataset_id="dataset-1")["data"][0]["analysis_id"] == (
            "analysis-1"
        )
        assert client.list_claims(analysis_id="analysis-1")["data"][0]["claim_id"] == (
            "claim-1"
        )
        assert client.list_visualizations(claim_id="claim-1")["data"][0]["viz_id"] == (
            "viz-1"
        )
        assert client.get_dataset_provenance("dataset-1")["data"]["@id"] == "dataset-1"
        assert client.get_analysis_provenance("analysis-1")["data"]["@id"] == (
            "analysis-1"
        )
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/sessions",
        "/datasets",
        "/analyses",
        "/claims",
        "/visualizations",
        "/datasets/dataset-1/provenance",
        "/analyses/analysis-1/provenance",
    ]


def test_decision_context_posts_to_api_route() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/assistant/decision-context"
        body = json.loads(request.content.decode("utf-8"))
        assert body == {
            "task_kind": "research_writing",
            "query": "baseline controls",
            "project_id": "project-1",
            "limit": 5,
        }
        return _json_response(
            200,
            {
                "data": {
                    "task_kind": "research_writing",
                    "query": "baseline controls",
                },
                "meta": {"limit": 5},
            },
        )

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = client.get_decision_context(
            task_kind="research_writing",
            query="baseline controls",
            project_id="project-1",
            limit=5,
        )
    finally:
        client.close()

    assert payload["data"]["task_kind"] == "research_writing"
    assert len(requests) == 1


def test_decision_context_returns_unavailable_when_api_read_fails() -> None:
    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(
            lambda request: _json_response(503, {"error": {"message": "offline"}})
        ),
    )

    try:
        payload = client.get_decision_context(
            task_kind="research_writing",
            query="baseline",
            project_id="project-1",
        )
    finally:
        client.close()

    assert payload["error"]["code"] == "unavailable"
    assert "offline" in payload["error"]["detail"]


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


def test_create_note_rejects_invalid_status_before_api_request() -> None:
    requests: list[httpx.Request] = []

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(
            lambda request: requests.append(request)
            or _json_response(500, {"error": {"message": "unexpected request"}})
        ),
    )

    try:
        with pytest.raises(mcp_server.LabTrackerAPIError, match="Allowed note statuses"):
            client.create_note(
                project_id="project-1",
                raw_content="capture",
                status="active",
            )
    finally:
        client.close()

    assert requests == []


def test_create_note_sends_scalar_metadata_values_to_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/notes":
            body = json.loads(request.content.decode("utf-8"))
            assert body == {
                "project_id": "project-1",
                "raw_content": "capture",
                "metadata": {
                    "source": "mcp",
                    "trial": 7,
                    "verified": True,
                    "score": 1.5,
                },
                "status": "staged",
            }
            return _json_response(201, {"data": {"note_id": "note-1"}})
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = client.create_note(
            project_id="project-1",
            raw_content="capture",
            metadata={
                "source": "mcp",
                "trial": 7,
                "verified": True,
                "score": 1.5,
            },
            status="STAGED",
        )
    finally:
        client.close()

    assert payload == {"data": {"note_id": "note-1"}}
    assert [request.url.path for request in requests] == ["/notes"]


def test_create_note_rejects_nested_metadata_before_api_request() -> None:
    requests: list[httpx.Request] = []

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(
            lambda request: requests.append(request)
            or _json_response(500, {"error": {"message": "unexpected request"}})
        ),
    )

    try:
        with pytest.raises(mcp_server.LabTrackerAPIError, match="metadata values"):
            client.create_note(
                project_id="project-1",
                raw_content="capture",
                metadata={"nested": {"source": "mcp"}},
            )
    finally:
        client.close()

    assert requests == []


def test_authenticated_tool_requires_service_credentials_when_api_requires_auth() -> None:
    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(
            lambda request: _json_response(401, {"error": {"message": "Missing auth"}})
        ),
    )

    with pytest.raises(mcp_server.LabTrackerAPIError, match="LAB_TRACKER_MCP_USERNAME"):
        client.list_projects()

    client.close()


def test_authenticated_tool_without_credentials_can_use_auth_disabled_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/projects"
        assert "authorization" not in request.headers
        return _json_response(200, {"data": [{"project_id": "p1"}]})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        assert client.list_projects() == {"data": [{"project_id": "p1"}]}
    finally:
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
