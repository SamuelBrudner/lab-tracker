"""MCP tools must not block the server event loop (review finding M38)."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import anyio
import pytest
from mcp.server.fastmcp import FastMCP

from lab_tracker import mcp_server
from lab_tracker.mcp_tools import read as read_tools
from lab_tracker.mcp_tools import (
    register_hosted_write_tools,
    register_read_tools,
    register_write_tools,
)

INBOUND_TOKEN = "inbound-" + "a" * 32
BLOCK_SECONDS = 5.0


def _hosted_settings() -> mcp_server.MCPServerRuntimeSettings:
    return mcp_server.MCPServerRuntimeSettings(
        transport="streamable-http",
        host="127.0.0.1",
        port=9000,
        path="/mcp",
        inbound_token=INBOUND_TOKEN,
    )


class _SlowClient:
    """A read client whose decision-context call blocks until health is called."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def get_decision_context(self, **_kwargs: Any) -> dict[str, Any]:
        released = self.release.wait(timeout=BLOCK_SECONDS)
        return {"data": {"released_by_concurrent_call": released}}

    def health(self) -> dict[str, Any]:
        self.release.set()
        return {"data": {"status": "ok"}}

    def close(self) -> None:
        return None


@pytest.mark.parametrize("hosted", [True, False])
def test_slow_tool_call_does_not_stall_concurrent_calls(
    monkeypatch: pytest.MonkeyPatch, hosted: bool
) -> None:
    slow_client = _SlowClient()
    read_tools.close_cached_read_client()
    monkeypatch.setattr(read_tools, "client_from_env", lambda: slow_client)
    server = mcp_server.build_server(
        _hosted_settings() if hosted else mcp_server.MCPServerRuntimeSettings()
    )
    results: dict[str, Any] = {}

    async def slow_call() -> None:
        _content, structured = await server.call_tool(
            "lab_tracker_get_decision_context",
            {"task_kind": "summary", "query": "state"},
        )
        results["slow"] = structured

    async def fast_call() -> None:
        await anyio.sleep(0.05)
        _content, structured = await server.call_tool("lab_tracker_health", {})
        results["fast"] = structured

    async def run_both() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(slow_call)
            group.start_soon(fast_call)

    started = time.monotonic()
    try:
        anyio.run(run_both)
    finally:
        read_tools.close_cached_read_client()
    elapsed = time.monotonic() - started

    slow_payload = results["slow"].get("result", results["slow"])
    assert slow_payload["data"]["released_by_concurrent_call"] is True
    assert elapsed < BLOCK_SECONDS
    fast_payload = results["fast"].get("result", results["fast"])
    assert fast_payload["data"] == {"status": "ok"}


def _tool_contract(server: FastMCP) -> dict[str, Any]:
    tools = asyncio.run(server.list_tools())
    return {
        tool.name: (
            tool.title,
            tool.description,
            tool.inputSchema,
            tool.outputSchema,
            tool.annotations,
        )
        for tool in tools
    }


@pytest.mark.parametrize("hosted", [True, False])
def test_offloaded_tools_keep_the_registered_contract(hosted: bool) -> None:
    reference = FastMCP("reference")
    register_read_tools(reference)
    if hosted:
        server = mcp_server.build_server(_hosted_settings())
    else:
        register_write_tools(reference)
        server = mcp_server.build_server(mcp_server.MCPServerRuntimeSettings())

    assert _tool_contract(server) == _tool_contract(reference)


def test_offloaded_hosted_write_tools_keep_the_registered_contract() -> None:
    from dataclasses import replace

    reference = FastMCP("reference")
    register_read_tools(reference)
    register_hosted_write_tools(reference)
    server = mcp_server.build_server(replace(_hosted_settings(), allow_writes=True))

    assert _tool_contract(server) == _tool_contract(reference)


class _ClosableClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_failed_call_never_closes_a_newer_cached_read_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _ClosableClient()
    current = _ClosableClient()
    read_tools.close_cached_read_client()
    monkeypatch.setattr(read_tools, "client_from_env", lambda: current)
    try:
        assert read_tools._read_client() is current

        # A concurrent call that used an older client fails after the cache moved on.
        read_tools._discard_read_client(stale)
        assert current.closed is False
        assert read_tools._read_client() is current

        read_tools._discard_read_client(current)
        assert current.closed is True
    finally:
        read_tools.close_cached_read_client()
