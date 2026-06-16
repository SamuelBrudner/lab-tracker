from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import tomllib

from lab_tracker import mcp_server


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
    assert mcp_server.server.instructions is not None
    assert "CALL THIS FIRST before research-facing decisions" in (
        mcp_server.server.instructions
    )
    assert "what to plot" in mcp_server.server.instructions


def test_fastmcp_registers_agent_consultation_policy_resource() -> None:
    resources = asyncio.run(mcp_server.server.list_resources())

    uris = {str(resource.uri) for resource in resources}
    assert "lab-tracker://agent-consultation-policy" in uris


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


def test_read_tool_returns_unavailable_contract_when_api_is_down(monkeypatch) -> None:
    from lab_tracker.mcp_tools import read as read_tools

    class BrokenClient:
        def list_projects(self, **_kwargs):
            raise mcp_server.LabTrackerAPIError("HTTP 502 bad gateway")

        def close(self) -> None:
            return None

    monkeypatch.setattr(read_tools, "client_from_env", lambda: BrokenClient())

    payload = read_tools.lab_tracker_list_projects()

    assert payload["error"]["code"] == "lab_tracker_unavailable"
    assert payload["error"]["operation"] == "lab_tracker_list_projects"
    assert payload["data"] is None
    assert payload["next_action"]["action"] == "proceed_without_graph_context"


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
