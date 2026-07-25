from __future__ import annotations

import asyncio
import json

import httpx

from lab_tracker import mcp_server
from lab_tracker.mcp_tools import READ_TOOLS, WRITE_TOOLS


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def test_mcp_registers_experiment_read_and_explicit_write_tools() -> None:
    registered = {
        tool.name: tool for tool in asyncio.run(mcp_server.server.list_tools())
    }
    read_names = {tool.__name__ for tool in READ_TOOLS}
    write_names = {tool.__name__ for tool in WRITE_TOOLS}

    assert {
        "lab_tracker_list_experiments",
        "lab_tracker_get_experiment",
        "lab_tracker_list_experiment_sessions",
        "lab_tracker_list_experiment_datasets",
    }.issubset(read_names)
    assert {
        "lab_tracker_create_experiment",
        "lab_tracker_update_experiment",
    }.issubset(write_names)
    assert registered["lab_tracker_list_experiments"].annotations.readOnlyHint
    assert (
        registered["lab_tracker_update_experiment"].annotations.destructiveHint
        is True
    )


def test_mcp_api_client_round_trips_experiment_crud_memberships_and_anchor() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/experiments" and request.method == "GET":
            assert request.url.params["search"] == "stability"
            assert request.url.params["session_id"] == "session-1"
            return _response({"data": [{"experiment_id": "experiment-1"}]})
        if path == "/experiments" and request.method == "POST":
            body = json.loads(request.content)
            assert body == {
                "project_id": "project-1",
                "name": "Stability run",
                "primary_question_id": "question-1",
            }
            return _response({"data": {"experiment_id": "experiment-1"}})
        if path == "/experiments/experiment-1" and request.method == "GET":
            return _response(
                {"data": {"experiment_id": "experiment-1", "status": "active"}}
            )
        if path == "/experiments/experiment-1" and request.method == "PATCH":
            assert json.loads(request.content) == {"status": "closed"}
            return _response(
                {"data": {"experiment_id": "experiment-1", "status": "closed"}}
            )
        if path == "/experiments/experiment-1/sessions":
            return _response({"data": [{"session_id": "session-1"}]})
        if path == "/experiments/experiment-1/datasets":
            return _response({"data": [{"dataset_id": "dataset-1"}]})
        if path == "/assistant/decision-context":
            assert json.loads(request.content) == {
                "task_kind": "experiment_plan",
                "query": "stability",
                "experiment_id": "experiment-1",
                "limit": 5,
            }
            return _response(
                {
                    "data": {
                        "scope": {
                            "anchors": [
                                {
                                    "entity_type": "experiment",
                                    "entity_id": "experiment-1",
                                }
                            ]
                        }
                    }
                }
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    client = mcp_server.LabTrackerAPIClient(
        mcp_server.MCPSettings(base_url="http://testserver"),
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.list_experiments(
            search="stability",
            session_id="session-1",
        )["data"][0]["experiment_id"] == "experiment-1"
        assert client.create_experiment(
            project_id="project-1",
            name="Stability run",
            primary_question_id="question-1",
        )["data"]["experiment_id"] == "experiment-1"
        assert client.get_experiment("experiment-1")["data"]["status"] == "active"
        assert client.update_experiment(
            experiment_id="experiment-1",
            status="closed",
        )["data"]["status"] == "closed"
        assert client.list_experiment_sessions("experiment-1")["data"][0][
            "session_id"
        ] == "session-1"
        assert client.list_experiment_datasets("experiment-1")["data"][0][
            "dataset_id"
        ] == "dataset-1"
        context = client.get_decision_context(
            task_kind="experiment_plan",
            query="stability",
            experiment_id="experiment-1",
            limit=5,
        )
        assert context["data"]["scope"]["anchors"][0][
            "entity_type"
        ] == "experiment"
    finally:
        client.close()

    assert [request.url.path for request in requests] == [
        "/experiments",
        "/experiments",
        "/experiments/experiment-1",
        "/experiments/experiment-1",
        "/experiments/experiment-1/sessions",
        "/experiments/experiment-1/datasets",
        "/assistant/decision-context",
    ]
