"""MCP tools fail loud on unknown arguments instead of silently dropping them.

FastMCP builds each tool's argument model with pydantic's default ``extra="ignore"``,
so a misspelled or misplaced argument (``project_id_typo``, or a top-level
``dataset_id`` next to ``lab_tracker_record_evidence_bundle``'s ``dataset`` object)
was dropped and the tool ran without it. Every registered tool, on stdio and on the
hosted surface, must reject the call naming the argument before it acquires an API
client.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent, Tool

from lab_tracker import mcp_server
from lab_tracker.mcp_tools import read as read_tools
from lab_tracker.mcp_tools import write as write_tools

INBOUND_TOKEN = "inbound-" + "a" * 32
UNKNOWN_ARGUMENT = "lab_tracker_unknown_argument"
# Path-bound ids must be canonical UUIDs; the client refuses anything else (H7).
PROJECT_ID = "11111111-1111-4111-8111-111111111111"
DATASET_ID = "22222222-2222-4222-8222-222222222222"
QUESTION_ID = "66666666-6666-4666-8666-666666666666"


def _hosted_settings(**overrides: Any) -> mcp_server.MCPServerRuntimeSettings:
    values: dict[str, Any] = {
        "transport": "streamable-http",
        "host": "127.0.0.1",
        "port": 9000,
        "path": "/mcp",
        "inbound_token": INBOUND_TOKEN,
    }
    values.update(overrides)
    return mcp_server.MCPServerRuntimeSettings(**values)


SERVERS: dict[str, FastMCP] = {
    "stdio": mcp_server.build_server(mcp_server.MCPServerRuntimeSettings()),
    "hosted": mcp_server.build_server(_hosted_settings()),
    "hosted_writes": mcp_server.build_server(_hosted_settings(allow_writes=True)),
}
# The surfaces that register lab_tracker_record_evidence_bundle.
BUNDLE_SURFACES = ["stdio", "hosted_writes"]


def _tools(server: FastMCP) -> dict[str, Tool]:
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


REGISTERED_TOOLS = [
    pytest.param(surface, name, id=f"{surface}-{name}")
    for surface, server in SERVERS.items()
    for name in _tools(server)
]


def _forbid_client_acquisition(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail any call that reaches an API client; return the acquisitions it made."""

    acquisitions: list[str] = []

    def unexpected_client() -> None:
        acquisitions.append("client_from_env")
        raise AssertionError("a call with an unrecognized argument acquired an API client")

    read_tools.close_cached_read_client()
    monkeypatch.setattr(read_tools, "client_from_env", unexpected_client)
    monkeypatch.setattr(write_tools, "client_from_env", unexpected_client)
    return acquisitions


class _ClientAcquired(Exception):
    """Raised by the stand-in client factory: the call passed argument validation."""


def _record_client_acquisition(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stop any call at its API client; return the acquisitions it made."""

    acquisitions: list[str] = []

    def acquired_client() -> None:
        acquisitions.append("client_from_env")
        raise _ClientAcquired("reached API client acquisition")

    read_tools.close_cached_read_client()
    monkeypatch.setattr(read_tools, "client_from_env", acquired_client)
    monkeypatch.setattr(write_tools, "client_from_env", acquired_client)
    return acquisitions


def _call_over_protocol(
    server: FastMCP, tool_name: str, arguments: dict[str, Any]
) -> CallToolResult:
    """Call a tool through the MCP request handler a connected client reaches.

    The low-level server bound its ``tools/call`` handler when the server was built,
    so this path ignores anything that only patches ``call_tool`` afterwards.
    """

    async def call() -> CallToolResult:
        async with create_connected_server_and_client_session(server) as session:
            return await session.call_tool(tool_name, arguments)

    return asyncio.run(call())


def _result_text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def _evidence_bundle_arguments(**dataset_placement: Any) -> dict[str, Any]:
    """The eval transcript's bundle call, with the reused dataset placed by the caller."""

    return {
        "project_id": PROJECT_ID,
        "primary_question_id": QUESTION_ID,
        **dataset_placement,
        "analysis": {
            "method_hash": "preview-latency-v2",
            "code_version": "fixture-v2",
            "dataset_ids": [DATASET_ID],
        },
        "dry_run": True,
    }


def _schema_value(schema: dict[str, Any]) -> Any:
    """A value the argument model accepts for one required property."""

    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        branches = [branch for branch in schema["anyOf"] if branch.get("type") != "null"]
        if not branches:
            pytest.fail(f"anyOf argument schema has no non-null branch: {schema!r}")
        return _schema_value(branches[0])
    schema_type = schema.get("type")
    if schema_type == "array":
        if "items" not in schema:
            pytest.fail(f"array argument schema has no items schema: {schema!r}")
        return [_schema_value(schema["items"])]
    samples = {
        "string": PROJECT_ID,
        "number": 0.5,
        "integer": 1,
        "boolean": False,
        "object": {},
    }
    if schema_type not in samples:
        pytest.fail(f"_schema_value has no sample for argument schema {schema!r}; extend it")
    return samples[schema_type]


def _required_arguments(tool: Tool) -> dict[str, Any]:
    """Valid values for every required argument, so the unknown key is the only fault.

    A call that omitted them would fail on the missing fields, and pydantic echoes
    the whole input dict (unknown key included) in that error.
    """

    schema = tool.inputSchema
    return {name: _schema_value(schema["properties"][name]) for name in schema.get("required", [])}


@pytest.mark.parametrize(("surface", "tool_name"), REGISTERED_TOOLS)
def test_every_tool_rejects_an_unknown_argument_before_client_acquisition(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    tool_name: str,
) -> None:
    server = SERVERS[surface]
    arguments = _required_arguments(_tools(server)[tool_name])
    acquisitions = _forbid_client_acquisition(monkeypatch)

    with pytest.raises(ToolError, match=UNKNOWN_ARGUMENT):
        asyncio.run(server.call_tool(tool_name, {**arguments, UNKNOWN_ARGUMENT: 1}))

    assert acquisitions == []


@pytest.mark.parametrize("surface", BUNDLE_SURFACES)
def test_evidence_bundle_rejects_top_level_component_id_before_client_acquisition(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    acquisitions = _forbid_client_acquisition(monkeypatch)

    # The most frequent failed call in docs/evals/2026-10-05-gpt-6-luna.jsonl: the
    # reused dataset's id sits beside the bundle instead of inside ``dataset``.
    with pytest.raises(ToolError, match=r"\bdataset_id\b"):
        asyncio.run(
            SERVERS[surface].call_tool(
                "lab_tracker_record_evidence_bundle",
                _evidence_bundle_arguments(dataset_id=DATASET_ID),
            )
        )

    assert acquisitions == []


@pytest.mark.parametrize("surface", list(SERVERS))
def test_protocol_call_rejects_an_unknown_argument_before_client_acquisition(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    acquisitions = _forbid_client_acquisition(monkeypatch)

    result = _call_over_protocol(SERVERS[surface], "lab_tracker_health", {UNKNOWN_ARGUMENT: 1})

    assert result.isError is True
    assert UNKNOWN_ARGUMENT in _result_text(result)
    assert acquisitions == []


@pytest.mark.parametrize("surface", BUNDLE_SURFACES)
def test_protocol_evidence_bundle_rejects_top_level_dataset_id_before_client_acquisition(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    acquisitions = _forbid_client_acquisition(monkeypatch)

    result = _call_over_protocol(
        SERVERS[surface],
        "lab_tracker_record_evidence_bundle",
        _evidence_bundle_arguments(dataset_id=DATASET_ID),
    )

    assert result.isError is True
    assert re.search(r"\bdataset_id\b", _result_text(result)), _result_text(result)
    assert acquisitions == []


@pytest.mark.parametrize("surface", BUNDLE_SURFACES)
def test_protocol_evidence_bundle_with_dataset_id_nested_reaches_the_client(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    # Guard: the correct reuse shape is not caught by the unknown-argument check.
    acquisitions = _record_client_acquisition(monkeypatch)

    result = _call_over_protocol(
        SERVERS[surface],
        "lab_tracker_record_evidence_bundle",
        _evidence_bundle_arguments(dataset={"dataset_id": DATASET_ID}),
    )

    assert "reached API client acquisition" in _result_text(result)
    assert acquisitions == ["client_from_env"]


@pytest.mark.parametrize("surface", list(SERVERS))
def test_protocol_call_with_a_declared_optional_argument_reaches_the_client(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    # Guard: only undeclared keys are refused, not optional ones the caller supplied.
    tool = _tools(SERVERS[surface])["lab_tracker_list_projects"]
    assert "limit" in tool.inputSchema["properties"]
    assert "limit" not in tool.inputSchema.get("required", [])
    acquisitions = _record_client_acquisition(monkeypatch)

    result = _call_over_protocol(SERVERS[surface], "lab_tracker_list_projects", {"limit": 5})

    assert "reached API client acquisition" in _result_text(result)
    assert acquisitions == ["client_from_env"]


@pytest.mark.parametrize("surface", BUNDLE_SURFACES)
def test_evidence_bundle_description_nests_reused_ids_in_their_component(
    surface: str,
) -> None:
    description = _tools(SERVERS[surface])["lab_tracker_record_evidence_bundle"].description or ""

    assert 'dataset={"dataset_id":' in description
    assert "Supply dataset_id, analysis_id" not in description
