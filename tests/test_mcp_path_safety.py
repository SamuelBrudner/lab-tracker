"""Path-parameter safety for the API-backed MCP client and tools (review H7).

Every identifier an MCP tool interpolates into an API path is agent-supplied.
httpx collapses ``..`` dot segments and treats ``?``/``#`` as delimiters, so an
unvalidated id can retarget a request to any route the held credential may
call. These tests pin that no such id ever reaches the transport.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from lab_tracker.mcp_api_client import (
    LabTrackerAPIClient,
    LabTrackerAPIValidationError,
    MCPSettings,
)

VALID_ID = "3f2b8c1e-6a4d-4e2f-9b1a-7c5d8e9f0a12"
OTHER_ID = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"

HOSTILE_IDS = [
    "../auth/users",
    "..",
    ".",
    "",
    "../../auth/tokens",
    f"{VALID_ID}/../../auth/users",
    f"{VALID_ID}?x=1",
    f"{VALID_ID}#frag",
    "%2e%2e",
    f"{VALID_ID}%2F..",
    "not-a-uuid",
    f"{{{VALID_ID}}}",
    f"urn:uuid:{VALID_ID}",
    VALID_ID.replace("-", ""),
    f" {VALID_ID}",
    f"{VALID_ID}\n",
]


class _RecordingTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"data": {"ok": True}, "meta": {}})


@pytest.fixture
def recorder() -> Iterator[tuple[LabTrackerAPIClient, _RecordingTransport]]:
    transport = _RecordingTransport()
    client = LabTrackerAPIClient(
        MCPSettings(base_url="http://api.local", api_key="lpat_test"),
        transport=transport,
    )
    try:
        yield client, transport
    finally:
        client.close()


ClientCall = Callable[[LabTrackerAPIClient, str], Any]

# One entry per path-parameterised client method, keyed by the argument that
# is interpolated into the path.
CLIENT_CALLS: dict[str, ClientCall] = {
    "graph_overview.project_id": lambda c, v: c.graph_overview(v),
    "search_graph.project_id": lambda c, v: c.search_graph(v, "q"),
    "graph_neighborhood.project_id": lambda c, v: c.get_graph_neighborhood(
        v, "question", VALID_ID
    ),
    "graph_neighborhood.entity_id": lambda c, v: c.get_graph_neighborhood(
        VALID_ID, "question", v
    ),
    "get_visualization.visualization_id": lambda c, v: c.get_visualization(v),
    "list_goals.project_id": lambda c, v: c.list_goals(project_id=v),
    "get_goal.goal_id": lambda c, v: c.get_goal(v),
    "publication_readiness.project_id": lambda c, v: c.publication_readiness(v),
    "dataset_provenance.dataset_id": lambda c, v: c.get_dataset_provenance(v),
    "analysis_provenance.analysis_id": lambda c, v: c.get_analysis_provenance(v),
    "claim_provenance.claim_id": lambda c, v: c.get_claim_provenance(v),
    "export_goal_artifact.goal_id": lambda c, v: c.export_goal_artifact(v),
    "export_question_subtree.question_id": lambda c, v: c.export_question_subtree(v),
    "refactor_question.question_id": lambda c, v: c.refactor_question(
        question_id=v,
        replacement_text="t",
        replacement_question_type="other",
        replacement_status="staged",
        reason="r",
    ),
    "list_question_refactors.question_id": lambda c, v: c.list_question_refactors(
        question_id=v
    ),
    "create_claim_edge.claim_id": lambda c, v: c.create_claim_edge(
        claim_id=v, target_claim_id=OTHER_ID, relation="supports"
    ),
    "list_claim_edges.claim_id": lambda c, v: c.list_claim_edges(claim_id=v),
    "create_goal.project_id": lambda c, v: c.create_goal(
        project_id=v, goal_type="paper", title="t"
    ),
    "update_goal.goal_id": lambda c, v: c.update_goal(goal_id=v, title="t"),
    "link_node_to_goal.goal_id": lambda c, v: c.link_node_to_goal(
        goal_id=v, entity_type="question", entity_id=OTHER_ID, relation="advances"
    ),
    "list_node_goals.project_id": lambda c, v: c.list_node_goals(
        project_id=v, entity_type="question", entity_id=VALID_ID
    ),
    "list_node_goals.entity_id": lambda c, v: c.list_node_goals(
        project_id=VALID_ID, entity_type="question", entity_id=v
    ),
}


@pytest.mark.parametrize("call_name", sorted(CLIENT_CALLS))
@pytest.mark.parametrize("hostile_id", HOSTILE_IDS)
def test_client_rejects_non_uuid_path_ids_before_transport(
    recorder: tuple[LabTrackerAPIClient, _RecordingTransport],
    call_name: str,
    hostile_id: str,
) -> None:
    client, transport = recorder

    with pytest.raises(LabTrackerAPIValidationError) as excinfo:
        CLIENT_CALLS[call_name](client, hostile_id)

    assert excinfo.value.code == "validation_error"
    assert transport.requests == []


@pytest.mark.parametrize("call_name", sorted(CLIENT_CALLS))
def test_client_valid_uuid_path_ids_reach_the_intended_route(
    recorder: tuple[LabTrackerAPIClient, _RecordingTransport],
    call_name: str,
) -> None:
    client, transport = recorder

    CLIENT_CALLS[call_name](client, VALID_ID)

    assert len(transport.requests) == 1
    raw_path = transport.requests[0].url.raw_path.decode("ascii").split("?", 1)[0]
    assert f"/{VALID_ID}" in raw_path
    assert "/auth" not in raw_path
    assert ".." not in raw_path


def test_client_uppercase_uuid_is_accepted(
    recorder: tuple[LabTrackerAPIClient, _RecordingTransport],
) -> None:
    client, transport = recorder

    client.get_goal(VALID_ID.upper())

    assert transport.requests[0].url.path == f"/goals/{VALID_ID.upper()}"


@pytest.mark.parametrize(
    "call",
    [
        lambda c, v: c.get_graph_neighborhood(VALID_ID, v, OTHER_ID),
        lambda c, v: c.list_node_goals(project_id=VALID_ID, entity_type=v, entity_id=OTHER_ID),
    ],
    ids=["graph_neighborhood", "list_node_goals"],
)
@pytest.mark.parametrize(
    "entity_type",
    ["..", ".", "", "../../auth", "question/../..", "question?x=1", "batches", "Question"],
)
def test_client_rejects_unknown_path_entity_types_before_transport(
    recorder: tuple[LabTrackerAPIClient, _RecordingTransport],
    call: ClientCall,
    entity_type: str,
) -> None:
    client, transport = recorder

    with pytest.raises(LabTrackerAPIValidationError):
        call(client, entity_type)

    assert transport.requests == []


@pytest.mark.parametrize(
    "call",
    [
        lambda c, v: c.export_goal_artifact(VALID_ID, layer=v),
        lambda c, v: c.export_question_subtree(VALID_ID, layer=v),
    ],
    ids=["goal", "question"],
)
@pytest.mark.parametrize("layer", ["..", ".", "../../../auth/users", "logic/..", "unknown"])
def test_client_rejects_unknown_ara_layers_before_transport(
    recorder: tuple[LabTrackerAPIClient, _RecordingTransport],
    call: ClientCall,
    layer: str,
) -> None:
    client, transport = recorder

    with pytest.raises(LabTrackerAPIValidationError):
        call(client, layer)

    assert transport.requests == []


@pytest.mark.parametrize("layer", ["logic", "src", "trace", "evidence"])
def test_client_accepts_documented_ara_layers(
    recorder: tuple[LabTrackerAPIClient, _RecordingTransport],
    layer: str,
) -> None:
    client, transport = recorder

    client.export_goal_artifact(VALID_ID, layer=layer)

    assert transport.requests[0].url.path == f"/goals/{VALID_ID}/ara-artifact/{layer}"


def test_upload_rejects_hostile_viz_id_before_touching_the_file(
    recorder: tuple[LabTrackerAPIClient, _RecordingTransport],
    tmp_path: Path,
) -> None:
    client, transport = recorder
    missing = tmp_path / "does-not-exist.png"

    with pytest.raises(LabTrackerAPIValidationError) as excinfo:
        client.upload_visualization_file(viz_id="../auth/users", file_path=str(missing))

    # The id check runs first, so the error cannot double as a file-existence oracle.
    assert "does not exist" not in str(excinfo.value)
    assert transport.requests == []


# --- tool boundary: the normal structured error envelope, no transport call ---


def _install_recording_client(
    monkeypatch: pytest.MonkeyPatch, module: Any
) -> _RecordingTransport:
    transport = _RecordingTransport()

    def factory() -> LabTrackerAPIClient:
        return LabTrackerAPIClient(
            MCPSettings(base_url="http://api.local", api_key="lpat_test"),
            transport=transport,
        )

    monkeypatch.setattr(module, "client_from_env", factory)
    return transport


@pytest.mark.parametrize(
    "invoke",
    [
        lambda tools: tools.lab_tracker_get_goal(goal_id="../auth/users"),
        lambda tools: tools.lab_tracker_graph_overview(project_id="../graph-drafts"),
        lambda tools: tools.lab_tracker_list_node_goals(
            project_id="..", entity_type="batches", entity_id="runs"
        ),
        lambda tools: tools.lab_tracker_get_claim_provenance(claim_id="../../auth/tokens"),
        lambda tools: tools.lab_tracker_export_goal_artifact(
            goal_id=VALID_ID, layer="../../../auth/invitations"
        ),
        lambda tools: tools.lab_tracker_list_claim_edges(claim_id=f"{VALID_ID}?limit=1#"),
    ],
    ids=[
        "get_goal",
        "graph_overview",
        "list_node_goals",
        "claim_provenance",
        "export_goal_layer",
        "list_claim_edges",
    ],
)
def test_read_tools_return_validation_envelope_for_traversal_ids(
    monkeypatch: pytest.MonkeyPatch,
    invoke: Callable[[Any], dict[str, Any]],
) -> None:
    from lab_tracker.mcp_tools import read as read_tools

    read_tools.close_cached_read_client()
    transport = _install_recording_client(monkeypatch, read_tools)
    try:
        payload = invoke(read_tools)
    finally:
        read_tools.close_cached_read_client()

    assert payload["data"] is None
    assert payload["error"]["code"] == "validation_error"
    assert payload["next_action"]["action"] == "revise_request_or_credentials"
    assert transport.requests == []


@pytest.mark.parametrize(
    "invoke",
    [
        lambda tools: tools.lab_tracker_update_goal(goal_id="../questions/x", title="t"),
        lambda tools: tools.lab_tracker_link_node_to_goal(
            goal_id="../exploration-nodes",
            entity_type="question",
            entity_id=VALID_ID,
            relation="advances",
        ),
        lambda tools: tools.lab_tracker_create_claim_edge(
            claim_id="../../auth/tokens", target_claim_id=VALID_ID, relation="supports"
        ),
        lambda tools: tools.lab_tracker_refactor_question(
            question_id="..", replacement_text="t", reason="r"
        ),
        lambda tools: tools.lab_tracker_create_goal(
            project_id="../../auth/invitations", goal_type="paper", title="t"
        ),
        lambda tools: tools.lab_tracker_upload_visualization_file(
            viz_id="../notes", file_path="/etc/hostname"
        ),
    ],
    ids=[
        "update_goal",
        "link_node_to_goal",
        "create_claim_edge",
        "refactor_question",
        "create_goal",
        "upload_visualization_file",
    ],
)
def test_write_tools_return_validation_envelope_for_traversal_ids(
    monkeypatch: pytest.MonkeyPatch,
    invoke: Callable[[Any], dict[str, Any]],
) -> None:
    from lab_tracker.mcp_tools import write as write_tools

    transport = _install_recording_client(monkeypatch, write_tools)

    payload = invoke(write_tools)

    assert payload["data"] is None
    assert payload["error"]["code"] == "validation_error"
    assert transport.requests == []
