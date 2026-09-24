"""lt-mcp tells the connected agent when its client is behind the server (GH #239)."""

from __future__ import annotations

import asyncio
from typing import Any

import anyio
import pytest
from mcp.server.fastmcp import FastMCP

from lab_tracker import mcp_server
from lab_tracker.client_release import ReleaseIdentity
from lab_tracker.decision_context_constants import MCP_SERVER_INSTRUCTIONS
from lab_tracker.mcp_api_client import LabTrackerAPIUnavailableError, MCPSettings
from lab_tracker.mcp_tools import read as read_tools
from lab_tracker.mcp_tools import register_read_tools, register_write_tools

SERVER_REVISION = "b" * 40
NOTICE = "UPDATE AVAILABLE: test notice"


class _ReadClient:
    def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    def list_projects(self, **_kwargs: Any) -> dict[str, Any]:
        return {"data": [], "meta": {"total": 0}}

    def close(self) -> None:
        return None


def _call(server: FastMCP, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def call() -> dict[str, Any]:
        _content, structured = await server.call_tool(name, arguments)
        return structured.get("result", structured)

    return anyio.run(call)


@pytest.fixture
def read_client(monkeypatch: pytest.MonkeyPatch):
    read_tools.close_cached_read_client()
    monkeypatch.setattr(read_tools, "client_from_env", _ReadClient)
    yield
    read_tools.close_cached_read_client()


def test_current_client_behaves_exactly_as_before(read_client) -> None:
    server = mcp_server.build_server(mcp_server.MCPServerRuntimeSettings())

    assert server.instructions == MCP_SERVER_INSTRUCTIONS
    assert mcp_server.UPDATE_NOTICE_KEY not in _call(server, "lab_tracker_health", {})


def test_stale_client_notice_reaches_instructions_and_every_tool_result(read_client) -> None:
    server = mcp_server.build_server(
        mcp_server.MCPServerRuntimeSettings(), client_update_notice=NOTICE
    )

    assert server.instructions == f"{NOTICE}\n\n{MCP_SERVER_INSTRUCTIONS}"
    health = _call(server, "lab_tracker_health", {})
    projects = _call(server, "lab_tracker_list_projects", {})
    assert health[mcp_server.UPDATE_NOTICE_KEY] == NOTICE
    assert health["status"] == "ok"
    assert projects[mcp_server.UPDATE_NOTICE_KEY] == NOTICE
    assert projects["data"] == []


def test_update_notice_keeps_the_registered_tool_contract() -> None:
    reference = FastMCP("reference")
    register_read_tools(reference)
    register_write_tools(reference)
    server = mcp_server.build_server(
        mcp_server.MCPServerRuntimeSettings(), client_update_notice=NOTICE
    )

    def contract(mcp: FastMCP) -> dict[str, Any]:
        return {
            tool.name: (
                tool.title,
                tool.description,
                tool.inputSchema,
                tool.outputSchema,
                tool.annotations,
            )
            for tool in asyncio.run(mcp.list_tools())
        }

    assert contract(server) == contract(reference)


class _HealthProbe:
    seen_timeouts: list[float] = []

    def __init__(self, settings: MCPSettings, health: Any) -> None:
        self._health = health
        _HealthProbe.seen_timeouts.append(settings.timeout_seconds)

    def health(self) -> dict[str, Any]:
        if isinstance(self._health, Exception):
            raise self._health
        return self._health

    def close(self) -> None:
        return None


def _probe_with(monkeypatch: pytest.MonkeyPatch, health: Any) -> str | None:
    monkeypatch.setattr(
        mcp_server,
        "LabTrackerAPIClient",
        lambda settings: _HealthProbe(settings, health),
    )
    monkeypatch.setattr(
        mcp_server,
        "installed_release",
        lambda: ReleaseIdentity(version="0.1.0", revision="a" * 40),
    )
    return mcp_server.probe_client_update_notice(MCPSettings(timeout_seconds=30.0))


def test_probe_names_both_releases_and_the_pinned_update(monkeypatch) -> None:
    notice = _probe_with(
        monkeypatch,
        {"status": "ok", "app": {"version": "0.2.0", "source_revision": SERVER_REVISION}},
    )

    assert notice is not None
    assert notice.startswith("UPDATE AVAILABLE: this Lab Tracker MCP client runs release 0.1.0")
    assert "server runs release 0.2.0" in notice
    assert "Tell the person" in notice
    assert f"lab-tracker.git@{SERVER_REVISION}" in notice
    # Bounded well below the API client's ordinary request timeout.
    assert _HealthProbe.seen_timeouts[-1] == 2.0


@pytest.mark.parametrize(
    "health",
    [
        # Same release, different commit: revision drift alone never nags.
        {"app": {"version": "0.1.0", "source_revision": SERVER_REVISION}},
        {"app": {"version": "0.0.9", "source_revision": SERVER_REVISION}},
        # A server from before /health reported its release.
        {"app": {"source_revision": SERVER_REVISION}},
        LabTrackerAPIUnavailableError("connection refused"),
        RuntimeError("unexpected"),
    ],
)
def test_probe_fails_open_without_a_newer_server_release(monkeypatch, health: Any) -> None:
    assert _probe_with(monkeypatch, health) is None


def test_main_passes_the_stdio_notice_to_the_server_and_stderr(monkeypatch, capsys) -> None:
    events: list[tuple[str, ...]] = []

    class FakeServer:
        def run(self, *, transport: str) -> None:
            events.append(("run", transport))

    def fake_build(settings=None, *, client_update_notice=None):
        events.append(("build", settings.transport, client_update_notice))
        return FakeServer()

    monkeypatch.setenv("LAB_TRACKER_MCP_TRANSPORT", "stdio")
    monkeypatch.setattr(mcp_server, "_ensure_mcp_target_safe", lambda _s, *, hosted: None)
    monkeypatch.setattr(mcp_server, "probe_client_update_notice", lambda _settings: NOTICE)
    monkeypatch.setattr(mcp_server, "build_server", fake_build)

    mcp_server.main()

    assert events == [("build", "stdio", NOTICE), ("run", "stdio")]
    assert f"NOTICE: {NOTICE}" in capsys.readouterr().err
