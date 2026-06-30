from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import tomllib

from lab_tracker import mcp_server
from lab_tracker.cli import _cursor_mcp_json, _mcp_json
from lab_tracker.decision_context_constants import code_facing_idioms
from lab_tracker.mcp_tools import READ_TOOLS, WRITE_TOOLS


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def test_fastmcp_registers_lab_tracker_tools() -> None:
    tools = asyncio.run(mcp_server.server.list_tools())

    names = {tool.name for tool in tools}
    tools_by_name = {tool.name: tool for tool in tools}
    assert "lab_tracker_health" in names
    assert "lab_tracker_readiness" in names
    assert "lab_tracker_describe_schema" in names
    assert "lab_tracker_search" in names
    assert "lab_tracker_get_decision_context" in names
    assert "CALL THIS FIRST" in (
        tools_by_name["lab_tracker_get_decision_context"].description or ""
    )
    assert "lab_tracker_next_questions" in names
    assert "lab_tracker_list_datasets" in names
    assert "lab_tracker_list_analyses" in names
    assert "lab_tracker_list_claims" in names
    assert "lab_tracker_list_visualizations" in names
    assert "lab_tracker_list_goals" in names
    assert "lab_tracker_get_goal" in names
    assert "lab_tracker_list_node_goals" in names
    assert "lab_tracker_publication_readiness" in names
    assert "lab_tracker_get_claim_provenance" in names
    assert "lab_tracker_export_goal_artifact" in names
    assert "lab_tracker_export_question_subtree" in names
    assert "lab_tracker_create_note" in names
    assert "lab_tracker_create_dataset" in names
    assert "lab_tracker_create_analysis" in names
    assert "lab_tracker_create_claim" in names
    assert "lab_tracker_create_visualization" in names
    assert "lab_tracker_create_goal" in names
    assert "lab_tracker_update_goal" in names
    assert "lab_tracker_link_node_to_goal" in names
    assert "lab_tracker_upload_visualization_file" in names
    assert "lab_tracker_record_evidence_bundle" in names
    assert "lab_tracker_list_question_refactors" in names
    assert mcp_server.server.instructions is not None
    assert "CALL THIS FIRST before research-facing decisions" in (
        mcp_server.server.instructions
    )
    assert "what to plot" in mcp_server.server.instructions
    assert "AI can suggest; only a person commits" in mcp_server.server.instructions


def test_fastmcp_tool_annotations_mark_reads_and_writes_for_copilot() -> None:
    tools = asyncio.run(mcp_server.server.list_tools())
    tools_by_name = {tool.name: tool for tool in tools}

    health = tools_by_name["lab_tracker_health"]
    assert health.annotations is not None
    assert health.annotations.title == "Lab Tracker Health"
    assert health.annotations.readOnlyHint is True
    assert health.annotations.openWorldHint is True

    refactor_history = tools_by_name["lab_tracker_list_question_refactors"]
    assert refactor_history.annotations is not None
    assert refactor_history.annotations.readOnlyHint is True
    assert "lab_tracker_list_question_refactors" in {tool.__name__ for tool in READ_TOOLS}
    assert "lab_tracker_list_question_refactors" not in {tool.__name__ for tool in WRITE_TOOLS}

    create_note = tools_by_name["lab_tracker_create_note"]
    assert create_note.annotations is not None
    assert create_note.annotations.readOnlyHint is False
    assert create_note.annotations.destructiveHint is False

    refactor = tools_by_name["lab_tracker_refactor_question"]
    update_goal = tools_by_name["lab_tracker_update_goal"]
    assert refactor.annotations is not None
    assert refactor.annotations.destructiveHint is True
    assert update_goal.annotations is not None
    assert update_goal.annotations.destructiveHint is True

    evidence_bundle = tools_by_name["lab_tracker_record_evidence_bundle"]
    description = evidence_bundle.description or ""
    assert description.startswith("Preview a dataset-analysis-claim-visualization evidence bundle")
    assert "defaults to dry-run" in description


def test_fastmcp_registers_agent_consultation_policy_resource() -> None:
    resources = asyncio.run(mcp_server.server.list_resources())

    uris = {str(resource.uri) for resource in resources}
    assert "lab-tracker://agent-consultation-policy" in uris
    assert "lab-tracker://code-conventions" in uris


def test_code_conventions_resource_matches_package_generator() -> None:
    assert mcp_server.lab_tracker_code_conventions() == code_facing_idioms()
    assert "lab-tracker://code-conventions" in mcp_server.server.instructions
    instructions = (mcp_server.server.instructions or "").lower()
    for forbidden in ("pip install", "execute", "curl "):
        assert forbidden not in instructions


def test_copilot_mcp_configs_use_servers_schema() -> None:
    for config_path in (Path(".vscode/mcp.json"), Path("mcp.visualstudio.json")):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        server = config["servers"]["lab-tracker"]
        env = server["env"]

        assert "mcpServers" not in config
        assert server["type"] == "stdio"
        assert server["command"] == "lt-mcp"
        assert env["LAB_TRACKER_MCP_BASE_URL"] == "http://127.0.0.1:8000"
        assert env["LAB_TRACKER_MCP_API_KEY"] == "${input:lt-token}"
        assert env["LAB_TRACKER_MCP_USERNAME"] == "${input:lt-username}"
        assert env["LAB_TRACKER_MCP_PASSWORD"] == "${input:lt-password}"


def test_committed_mcp_json_matches_init_template() -> None:
    assert json.loads(Path(".mcp.json").read_text(encoding="utf-8")) == json.loads(_mcp_json())


def test_committed_cursor_mcp_json_uses_mcpservers_and_matches_template() -> None:
    config = json.loads(Path(".cursor/mcp.json").read_text(encoding="utf-8"))

    # Cursor uses the top-level mcpServers shape, not Copilot's `servers` schema.
    assert "servers" not in config
    assert config["mcpServers"]["lab-tracker"]["command"] == "lt-mcp"
    assert config == json.loads(_cursor_mcp_json())


def test_mcp_target_guard_allows_loopback_without_probe(monkeypatch) -> None:
    def fail_client(_settings):
        raise AssertionError("loopback targets should not probe readiness")

    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", fail_client)

    mcp_server._ensure_mcp_target_safe(
        mcp_server.MCPSettings(base_url="http://127.0.0.1:8000")
    )


def test_mcp_target_guard_refuses_remote_auth_disabled_target(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, _settings):
            self.closed = False

        def readiness(self):
            return {"status": "ok", "auth": {"enabled": False}}

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", FakeClient)

    with pytest.raises(SystemExit, match="auth-disabled"):
        mcp_server._ensure_mcp_target_safe(
            mcp_server.MCPSettings(base_url="http://lab.example.test")
        )


def test_mcp_runtime_settings_from_env_for_streamable_http(monkeypatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("LAB_TRACKER_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("LAB_TRACKER_MCP_PORT", "9000")
    monkeypatch.setenv("LAB_TRACKER_MCP_PATH", "mcp")

    settings = mcp_server.MCPServerRuntimeSettings.from_env()

    assert settings.transport == "streamable-http"
    assert settings.host == "127.0.0.1"
    assert settings.port == 9000
    assert settings.path == "/mcp"


def test_mcp_runtime_settings_reject_invalid_transport_and_port(monkeypatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_MCP_TRANSPORT", "sse")
    with pytest.raises(SystemExit, match="LAB_TRACKER_MCP_TRANSPORT"):
        mcp_server.MCPServerRuntimeSettings.from_env()

    monkeypatch.setenv("LAB_TRACKER_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("LAB_TRACKER_MCP_PORT", "not-a-port")
    with pytest.raises(SystemExit, match="LAB_TRACKER_MCP_PORT"):
        mcp_server.MCPServerRuntimeSettings.from_env()


def test_build_server_for_streamable_http_uses_private_stateless_settings() -> None:
    runtime_server = mcp_server.build_server(
        mcp_server.MCPServerRuntimeSettings(
            transport="streamable-http",
            host="127.0.0.1",
            port=9000,
            path="/mcp",
        )
    )

    assert runtime_server.settings.host == "127.0.0.1"
    assert runtime_server.settings.port == 9000
    assert runtime_server.settings.streamable_http_path == "/mcp"
    assert runtime_server.settings.stateless_http is True
    assert runtime_server.settings.json_response is True


def test_main_runs_streamable_http_transport_from_env(monkeypatch) -> None:
    guard_calls: list[str] = []
    built_settings: list[mcp_server.MCPServerRuntimeSettings] = []
    run_calls: list[str] = []

    class FakeServer:
        def run(self, transport: str) -> None:
            run_calls.append(transport)

    def fake_build(settings: mcp_server.MCPServerRuntimeSettings | None = None):
        assert settings is not None
        built_settings.append(settings)
        return FakeServer()

    monkeypatch.setenv("LAB_TRACKER_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("LAB_TRACKER_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("LAB_TRACKER_MCP_PORT", "9000")
    monkeypatch.setenv("LAB_TRACKER_MCP_PATH", "/mcp")
    monkeypatch.setattr(mcp_server, "_ensure_mcp_target_safe", lambda: guard_calls.append("guard"))
    monkeypatch.setattr(mcp_server, "build_server", fake_build)

    mcp_server.main()

    assert guard_calls == ["guard"]
    assert [settings.transport for settings in built_settings] == ["streamable-http"]
    assert run_calls == ["streamable-http"]


def test_hosted_mcp_compose_and_caddy_configs_are_private_read_only() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    caddy = Path("deploy/mcp/Caddyfile").read_text(encoding="utf-8")

    assert "  mcp:" in compose
    assert "LAB_TRACKER_MCP_TRANSPORT: streamable-http" in compose
    assert "LAB_TRACKER_MCP_BASE_URL: ${LAB_TRACKER_MCP_BASE_URL:-http://app:8000}" in compose
    assert "LAB_TRACKER_MCP_API_KEY: ${LT_MCP_READONLY_TOKEN:" in compose
    assert '"127.0.0.1:${LAB_TRACKER_MCP_HOST_PORT:-9000}:${LAB_TRACKER_MCP_PORT:-8000}"' in compose
    assert "LAB_TRACKER_MCP_USERNAME" not in compose.split("  postgres:", 1)[0]

    assert "respond @badorigin 403" in caddy
    assert "respond @badhost 421" in caddy
    assert "request>headers>Authorization delete" in caddy
    assert "Access-Control-Allow-Origin" not in caddy


def test_lt_mcp_console_entrypoint_is_packaged() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["lt-mcp"] == "lab_tracker.mcp_server:main"
    assert callable(mcp_server.main)


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


def test_client_static_api_key_skips_login_and_sends_bearer() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/auth/login":
            return _json_response(500, {"error": {"message": "login should not run"}})
        if request.url.path == "/projects":
            return _json_response(200, {"data": [], "meta": {"total": 0}})
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(
            base_url="http://testserver",
            username="mcp-user",
            password="mcp-pass",
            api_key="lpat_secret",
        ),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = client.list_projects()
    finally:
        client.close()

    assert payload["data"] == []
    assert seen == [("GET", "/projects", "Bearer lpat_secret")]


def test_client_static_api_key_does_not_retry_revoked_token() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        return _json_response(401, {"error": {"message": "revoked lpat_secret"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver", api_key="lpat_secret"),
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(mcp_server.LabTrackerAPIAuthError, match="revoked"):
            client.list_projects()
    finally:
        client.close()

    assert seen == [("GET", "/projects", "Bearer lpat_secret")]


def test_mcp_settings_from_env_accepts_api_key_aliases(monkeypatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_MCP_API_KEY", "lpat_primary")
    monkeypatch.setenv("LAB_TRACKER_MCP_TOKEN", "lpat_alias")
    assert mcp_server.MCPSettings.from_env().api_key == "lpat_primary"

    monkeypatch.delenv("LAB_TRACKER_MCP_API_KEY")
    assert mcp_server.MCPSettings.from_env().api_key == "lpat_alias"


def test_mcp_api_error_redacts_bearer_and_lpat_secrets() -> None:
    exc = mcp_server.LabTrackerAPIUnavailableError(
        "GET failed with Authorization: Bearer lpat_supersecret",
        issues=[{"message": "token lpat_supersecret failed"}],
    )

    payload = mcp_server.lab_tracker_api_error("lab_tracker_readiness", exc)

    message = payload["error"]["message"]
    issues = payload["error"]["issues"]
    assert "lpat_supersecret" not in message
    assert "Bearer [REDACTED]" in message
    assert "lpat_supersecret" not in json.dumps(issues)


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
        if request.url.path == "/claims/claim-1/provenance":
            return _json_response(200, {"data": {"@id": "claim-1"}})
        if request.url.path == "/goals/goal-1/ara-artifact/src":
            return _json_response(200, {"data": {"@id": "goal-src"}})
        if request.url.path == "/questions/question-1/ara-artifact":
            return _json_response(200, {"data": {"@id": "question-artifact"}})
        if request.url.path == "/projects/project-1/publication-readiness":
            return _json_response(
                200,
                {
                    "data": {
                        "project_id": "project-1",
                        "unsupported_claims": [],
                        "ungrounded_questions": [],
                        "orphaned_entities": [],
                        "broken_external_refs": [],
                        "seal_level": "ara_l1",
                    }
                },
            )
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
        assert client.get_claim_provenance("claim-1")["data"]["@id"] == "claim-1"
        assert client.export_goal_artifact("goal-1", layer="src")["data"]["@id"] == (
            "goal-src"
        )
        assert client.export_question_subtree("question-1")["data"]["@id"] == (
            "question-artifact"
        )
        assert client.publication_readiness("project-1")["data"]["seal_level"] == "ara_l1"
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
        "/claims/claim-1/provenance",
        "/goals/goal-1/ara-artifact/src",
        "/questions/question-1/ara-artifact",
        "/projects/project-1/publication-readiness",
    ]


def test_client_describe_schema_calls_api_discovery_route() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/schema/describe":
            assert request.url.params["entity_type"] == "claim"
            return _json_response(
                200,
                {
                    "data": {
                        "entities": {
                            "claim": {
                                "status": {
                                    "allowed_values": ["proposed", "supported", "rejected"]
                                }
                            }
                        }
                    }
                },
            )
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = client.describe_schema(entity_type="claim")
    finally:
        client.close()

    assert payload["data"]["entities"]["claim"]["status"]["allowed_values"] == [
        "proposed",
        "supported",
        "rejected",
    ]
    assert [request.url.path for request in requests] == ["/schema/describe"]


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

    assert payload["error"]["code"] == "lab_tracker_unavailable"
    assert payload["error"]["message"].startswith("Lab Tracker unavailable")
    assert payload["next_action"]["action"] == "proceed_without_graph_context"
    assert "offline" in payload["error"]["detail"]


def test_decision_context_preserves_validation_error_details() -> None:
    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(
            lambda request: _json_response(
                422,
                {
                    "error": {
                        "code": "request_validation_error",
                        "message": "Request validation failed.",
                        "issues": [{"field": "limit", "message": "Input should be <= 50"}],
                    }
                },
            )
        ),
    )

    try:
        with pytest.raises(mcp_server.LabTrackerAPIValidationError) as exc_info:
            client.get_decision_context(
                task_kind="research_writing",
                query="baseline",
                project_id="project-1",
            )
    finally:
        client.close()

    exc = exc_info.value
    assert exc.status_code == 422
    assert exc.code == "request_validation_error"
    assert exc.issues == [{"field": "limit", "message": "Input should be <= 50"}]
    assert "limit: Input should be <= 50" in str(exc)


def test_read_tool_returns_unavailable_contract_when_api_is_down(monkeypatch) -> None:
    from lab_tracker.mcp_tools import read as read_tools

    class BrokenClient:
        def list_projects(self, **_kwargs):
            raise mcp_server.LabTrackerAPIUnavailableError("HTTP 502 bad gateway")

        def close(self) -> None:
            return None

    monkeypatch.setattr(read_tools, "client_from_env", lambda: BrokenClient())

    payload = read_tools.lab_tracker_list_projects()

    assert payload["error"]["code"] == "lab_tracker_unavailable"
    assert payload["error"]["operation"] == "lab_tracker_list_projects"
    assert payload["data"] is None
    assert payload["next_action"]["action"] == "proceed_without_graph_context"


def test_read_tool_returns_structured_api_errors(monkeypatch) -> None:
    from lab_tracker.mcp_tools import read as read_tools

    class InvalidClient:
        def get_decision_context(self, **_kwargs):
            raise mcp_server.LabTrackerAPIValidationError(
                "Request validation failed. Issues: limit: Input should be <= 50.",
                status_code=422,
                code="request_validation_error",
                issues=[{"field": "limit", "message": "Input should be <= 50"}],
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(read_tools, "client_from_env", lambda: InvalidClient())

    payload = read_tools.lab_tracker_get_decision_context(
        task_kind="research_writing",
        query="baseline",
        limit=100,
    )

    assert payload["error"]["code"] == "request_validation_error"
    assert payload["error"]["status_code"] == 422
    assert payload["error"]["issues"] == [{"field": "limit", "message": "Input should be <= 50"}]
    assert payload["next_action"]["action"] == "revise_request_or_credentials"
    read_tools.close_cached_read_client()


def test_read_tools_reuse_cached_client(monkeypatch) -> None:
    from lab_tracker.mcp_tools import read as read_tools

    class FakeClient:
        def __init__(self) -> None:
            self.close_count = 0

        def list_projects(self, **_kwargs):
            return {"data": [{"project_id": "project-1"}]}

        def list_goals(self, **_kwargs):
            return {"data": [{"goal_id": "goal-1"}]}

        def close(self) -> None:
            self.close_count += 1

    clients: list[FakeClient] = []

    def factory() -> FakeClient:
        client = FakeClient()
        clients.append(client)
        return client

    read_tools.close_cached_read_client()
    monkeypatch.setattr(read_tools, "client_from_env", factory)

    try:
        assert read_tools.lab_tracker_list_projects()["data"][0]["project_id"] == "project-1"
        assert read_tools.lab_tracker_list_goals()["data"][0]["goal_id"] == "goal-1"
        assert len(clients) == 1
        assert clients[0].close_count == 0
    finally:
        read_tools.close_cached_read_client()

    assert clients[0].close_count == 1


def test_next_questions_ranks_goal_linked_active_questions() -> None:
    requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, dict(request.url.params.multi_items())))
        if request.url.path == "/goals":
            status = request.url.params.get("status")
            if status == "planned":
                return _json_response(
                    200,
                    {
                        "data": [
                            {
                                "goal_id": "goal-1",
                                "project_id": "project-1",
                                "title": "Submit odor paper",
                                "status": "planned",
                                "links": [
                                    {
                                        "entity_type": "question",
                                        "entity_id": "question-1",
                                        "link_status": "committed",
                                    }
                                ],
                            }
                        ],
                        "meta": {"total": 1},
                    },
                )
            return _json_response(200, {"data": [], "meta": {"total": 0}})
        if request.url.path == "/questions":
            if request.url.params.get("status") == "active":
                return _json_response(
                    200,
                    {
                        "data": [
                            {
                                "question_id": "question-1",
                                "project_id": "project-1",
                                "text": "Which control supports the odor claim?",
                                "status": "active",
                                "hypothesis": "The solvent control is clean.",
                            },
                            {
                                "question_id": "question-2",
                                "project_id": "project-1",
                                "text": "Unlinked active question",
                                "status": "active",
                            },
                        ],
                        "meta": {"total": 2},
                    },
                )
            return _json_response(200, {"data": [], "meta": {"total": 0}})
        if request.url.path == "/claims":
            return _json_response(200, {"data": [], "meta": {"total": 0}})
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = client.next_questions(limit=2)
    finally:
        client.close()

    assert payload["data"][0]["question"]["question_id"] == "question-1"
    assert payload["data"][0]["score"] > payload["data"][1]["score"]
    assert payload["next_action"]["tool"] == "lab_tracker_get_decision_context"
    assert payload["meta"]["ranking"].startswith("direct goal-question links")
    assert [path for path, _params in requests] == [
        "/goals",
        "/goals",
        "/questions",
        "/questions",
        "/claims",
    ]


def test_write_tool_appends_structured_next_action(monkeypatch) -> None:
    from lab_tracker.mcp_tools import write as write_tools

    class FakeClient:
        def create_dataset(self, **_kwargs):
            return {"data": {"dataset_id": "dataset-1"}}

        def close(self) -> None:
            return None

    monkeypatch.setattr(write_tools, "client_from_env", lambda: FakeClient())

    payload = write_tools.lab_tracker_create_dataset(
        project_id="project-1",
        primary_question_id="question-1",
    )

    assert payload["data"]["dataset_id"] == "dataset-1"
    assert payload["next_action"]["tool"] == "lab_tracker_create_analysis"


def test_write_tool_returns_structured_api_errors(monkeypatch) -> None:
    from lab_tracker.mcp_tools import write as write_tools

    class InvalidClient:
        def create_note(self, **_kwargs):
            raise mcp_server.LabTrackerAPIValidationError(
                "Request validation failed. Issues: raw_content: Required.",
                status_code=422,
                code="request_validation_error",
                issues=[{"field": "raw_content", "message": "Required"}],
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(write_tools, "client_from_env", lambda: InvalidClient())

    payload = write_tools.lab_tracker_create_note(
        project_id="project-1",
        raw_content="",
    )

    assert payload["error"]["code"] == "request_validation_error"
    assert payload["error"]["status_code"] == 422
    assert payload["error"]["issues"] == [{"field": "raw_content", "message": "Required"}]
    assert payload["data"] is None
    assert payload["next_action"]["action"] == "revise_request_or_credentials"


def test_write_tool_returns_unavailable_contract_when_api_is_down(monkeypatch) -> None:
    from lab_tracker.mcp_tools import write as write_tools

    class BrokenClient:
        def create_project(self, **_kwargs):
            raise mcp_server.LabTrackerAPIUnavailableError("HTTP 503 offline")

        def close(self) -> None:
            return None

    monkeypatch.setattr(write_tools, "client_from_env", lambda: BrokenClient())

    payload = write_tools.lab_tracker_create_project(name="Unavailable")

    assert payload["error"]["code"] == "lab_tracker_unavailable"
    assert payload["error"]["operation"] == "lab_tracker_create_project"
    assert payload["data"] is None
    assert payload["next_action"]["action"] == "proceed_without_graph_context"


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


def test_create_note_sends_targets_to_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/notes":
            body = json.loads(request.content.decode("utf-8"))
            assert body == {
                "project_id": "project-1",
                "raw_content": "claim source",
                "targets": [
                    {"entity_type": "claim", "entity_id": "claim-1"},
                    {"entity_type": "visualization", "entity_id": "viz-1"},
                ],
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
            raw_content="claim source",
            targets=[
                {"entity_type": "claim", "entity_id": "claim-1"},
                {"entity_type": "visualization", "entity_id": "viz-1"},
            ],
        )
    finally:
        client.close()

    assert payload == {"data": {"note_id": "note-1"}}
    assert [request.url.path for request in requests] == ["/notes"]


def test_client_create_evidence_tools_send_api_payloads() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content.decode("utf-8"))
        if request.url.path == "/datasets":
            assert body == {
                "project_id": "project-1",
                "primary_question_id": "question-1",
                "secondary_question_ids": ["question-2"],
                "commit_manifest": {"metadata": {"source": "paper"}},
                "commit_hash": "hash-1",
                "status": "staged",
            }
            return _json_response(201, {"data": {"dataset_id": "dataset-1"}})
        if request.url.path == "/analyses":
            assert body == {
                "project_id": "project-1",
                "dataset_ids": ["dataset-1"],
                "method_hash": "publication:method",
                "code_version": "published-pdf:v1",
                "environment_hash": "env-1",
                "status": "staged",
            }
            return _json_response(201, {"data": {"analysis_id": "analysis-1"}})
        if request.url.path == "/claims":
            if body["statement"] == "Interpretation only":
                assert body == {
                    "project_id": "project-1",
                    "statement": "Interpretation only",
                    "confidence": 55.0,
                    "status": "proposed",
                }
            else:
                assert body == {
                    "project_id": "project-1",
                    "statement": "Analysis supports the effect",
                    "confidence": 82.5,
                    "status": "supported",
                    "supported_by_analysis_ids": ["analysis-1"],
                }
            return _json_response(201, {"data": {"claim_id": "claim-1"}})
        if request.url.path == "/visualizations":
            assert body == {
                "analysis_id": "analysis-1",
                "viz_type": "figure",
                "file_path": "doi:10.1371/journal.pcbi.1011051#fig5",
                "caption": "Paper figure 5",
                "related_claim_ids": ["claim-1"],
            }
            return _json_response(201, {"data": {"viz_id": "viz-1"}})
        return _json_response(404, {"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        assert client.create_dataset(
            project_id="project-1",
            primary_question_id="question-1",
            secondary_question_ids=["question-2"],
            commit_manifest={"metadata": {"source": "paper"}},
            commit_hash="hash-1",
            status="STAGED",
        )["data"]["dataset_id"] == "dataset-1"
        assert client.create_analysis(
            project_id="project-1",
            dataset_ids=["dataset-1"],
            method_hash="publication:method",
            code_version="published-pdf:v1",
            environment_hash="env-1",
        )["data"]["analysis_id"] == "analysis-1"
        assert client.create_claim(
            project_id="project-1",
            statement="Interpretation only",
            confidence=55.0,
        )["data"]["claim_id"] == "claim-1"
        assert client.create_claim(
            project_id="project-1",
            statement="Analysis supports the effect",
            confidence=82.5,
            status="SUPPORTED",
            supported_by_analysis_ids=["analysis-1"],
        )["data"]["claim_id"] == "claim-1"
        assert client.create_visualization(
            analysis_id="analysis-1",
            viz_type="figure",
            file_path="doi:10.1371/journal.pcbi.1011051#fig5",
            caption="Paper figure 5",
            related_claim_ids=["claim-1"],
        )["data"]["viz_id"] == "viz-1"
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/datasets",
        "/analyses",
        "/claims",
        "/claims",
        "/visualizations",
    ]


def test_client_upload_visualization_file_posts_multipart(tmp_path) -> None:
    figure_path = tmp_path / "figure.png"
    figure_path.write_bytes(b"figure-bytes")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/visualizations/viz-1/file"
        assert request.headers["content-type"].startswith("multipart/form-data")
        assert b"figure.png" in request.content
        assert b"figure-bytes" in request.content
        return _json_response(
            201,
            {
                "data": {
                    "viz_id": "viz-1",
                    "asset": {"filename": "figure.png"},
                }
            },
        )

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )

    try:
        payload = client.upload_visualization_file(
            viz_id="viz-1",
            file_path=str(figure_path),
        )
    finally:
        client.close()

    assert payload["data"]["asset"]["filename"] == "figure.png"
    assert [request.url.path for request in seen] == ["/visualizations/viz-1/file"]


@pytest.mark.parametrize(
    ("method_name", "kwargs", "message"),
    [
        (
            "create_dataset",
            {
                "project_id": "project-1",
                "primary_question_id": "question-1",
                "status": "active",
            },
            "Allowed dataset statuses",
        ),
        (
            "create_analysis",
            {
                "project_id": "project-1",
                "dataset_ids": ["dataset-1"],
                "method_hash": "method-1",
                "code_version": "v1",
                "status": "active",
            },
            "Allowed analysis statuses",
        ),
        (
            "create_claim",
            {
                "project_id": "project-1",
                "statement": "claim",
                "confidence": 50.0,
                "status": "archived",
            },
            "Allowed claim statuses",
        ),
    ],
)
def test_create_evidence_tools_reject_invalid_status_before_api_request(
    method_name: str,
    kwargs: dict[str, object],
    message: str,
) -> None:
    requests: list[httpx.Request] = []
    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(
            lambda request: requests.append(request)
            or _json_response(500, {"error": {"message": "unexpected request"}})
        ),
    )

    try:
        with pytest.raises(mcp_server.LabTrackerAPIError, match=message):
            getattr(client, method_name)(**kwargs)
    finally:
        client.close()

    assert requests == []


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


def test_client_readiness_uses_service_credentials_when_configured() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/auth/login":
            return _json_response(200, {"data": {"access_token": "token-1"}})
        if request.url.path == "/readiness":
            return _json_response(200, {"status": "ok"})
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
        assert client.readiness() == {"status": "ok"}
    finally:
        client.close()

    assert seen == [
        ("POST", "/auth/login", None),
        ("GET", "/readiness", "Bearer token-1"),
    ]
