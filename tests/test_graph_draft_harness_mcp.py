"""Integration tests for the loopback streamable-HTTP harness MCP surface.

These prove that an *external* MCP client (standing in for the vendor CLI) can
reach the in-process scoped executor over 127.0.0.1 with a per-run bearer token,
call a scoped read tool, and submit a patch captured server-side — the core of
the functional external-harness wiring.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from lab_tracker.services.graph_draft_harness_mcp import (
    HarnessGraphDraftMCPServer,
    HarnessMCPLoopbackServer,
)


class _FakeScopedExecutor:
    """Stand-in for ScopedGraphDraftReadToolExecutor: one allowlisted read tool."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def mcp_tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "search",
                "description": "Search the scoped graph.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"tool": name, "results": [{"id": "note-scoped-1"}]}


def _valid_patch() -> dict[str, Any]:
    return {
        "summary": "Proposed one question.",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [],
    }


async def _drive_client(url: str, token: str, mcp: HarnessGraphDraftMCPServer) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        streamablehttp_client(url, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert "search" in names
        assert "submit_graph_patch" in names
        # A live scoped read flows through the executor (server-side).
        await session.call_tool("search", {"query": "gel"})
        # The propose-only terminal tool captures the patch server-side.
        await session.call_tool("submit_graph_patch", {"graph_patch": _valid_patch()})


def test_harness_mcp_loopback_serves_scoped_tools_and_captures_patch() -> None:
    executor = _FakeScopedExecutor()
    mcp = HarnessGraphDraftMCPServer(executor=executor, max_tool_calls=4)
    with HarnessMCPLoopbackServer(mcp) as loopback:
        assert loopback.url.startswith("http://127.0.0.1:")
        asyncio.run(_drive_client(loopback.url, loopback.token, mcp))
    # The read went through the scoped executor, and the patch was captured
    # server-side (never trusted from stdout).
    assert executor.calls and executor.calls[0][0] == "search"
    assert mcp.graph_patch == _valid_patch()
    assert mcp.tool_trace["tool_call_count"] == 1
    assert mcp.tool_trace["submit_graph_patch_calls"] == 1


async def _expect_unauthorized(url: str) -> None:
    async with streamablehttp_client(url, headers={"Authorization": "Bearer wrong"}) as (
        read,
        write,
        _,
    ), ClientSession(read, write) as session:
        await session.initialize()


def test_harness_mcp_loopback_rejects_wrong_token() -> None:
    executor = _FakeScopedExecutor()
    mcp = HarnessGraphDraftMCPServer(executor=executor, max_tool_calls=4)
    with (
        HarnessMCPLoopbackServer(mcp) as loopback,
        pytest.raises(Exception),  # noqa: B017 - any failure means access denied
    ):
        asyncio.run(_expect_unauthorized(loopback.url))
    # No read tool ever executed for the unauthorized client.
    assert executor.calls == []
