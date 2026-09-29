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
from lab_tracker_client.transport import HEALTH_PROBE_DEADLINE_SECONDS

SERVER_REVISION = "b" * 40
NOTICE = "UPDATE AVAILABLE: test notice"
# Past CPython's integer-string limit (4300 digits): ``int()`` raises on it.
OVERSIZED_VERSION = "9" * 5000
SOCKS_WITHOUT_SOCKSIO = (
    "Using SOCKS proxy, but the 'socksio' package is not installed. "
    "Make sure to install httpx using `pip install httpx[socks]`."
)


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
    seen_deadlines: list[float | None] = []

    def __init__(self, settings: MCPSettings, health: Any) -> None:
        self._health = health
        _HealthProbe.seen_timeouts.append(settings.timeout_seconds)

    def health(self, *, deadline_seconds: float | None = None) -> dict[str, Any]:
        _HealthProbe.seen_deadlines.append(deadline_seconds)
        if isinstance(self._health, Exception):
            raise self._health
        return self._health

    def close(self) -> None:
        return None


def _probe_with(
    monkeypatch: pytest.MonkeyPatch, health: Any, *, client_version: str = "0.1.0"
) -> str | None:
    monkeypatch.setattr(
        mcp_server,
        "LabTrackerAPIClient",
        lambda settings: _HealthProbe(settings, health),
    )
    monkeypatch.setattr(
        mcp_server,
        "installed_release",
        lambda: ReleaseIdentity(version=client_version, revision="a" * 40),
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
    # Bounded well below the API client's ordinary request timeout, and the
    # whole response by one shared wall-clock deadline.
    assert _HealthProbe.seen_timeouts[-1] == 2.0
    assert _HealthProbe.seen_deadlines[-1] == HEALTH_PROBE_DEADLINE_SECONDS


@pytest.mark.parametrize(
    "health",
    [
        # Same release, different commit: revision drift alone never nags.
        {"app": {"version": "0.1.0", "source_revision": SERVER_REVISION}},
        {"app": {"version": "0.0.9", "source_revision": SERVER_REVISION}},
        # A PATCH release is a backward-compatible fix (docs/versioning.md).
        {"app": {"version": "0.1.7", "source_revision": SERVER_REVISION}},
        # A server from before /health reported its release.
        {"app": {"source_revision": SERVER_REVISION}},
        # A version too long to be a release is unreadable, not an error.
        {"app": {"version": OVERSIZED_VERSION, "source_revision": SERVER_REVISION}},
        LabTrackerAPIUnavailableError("connection refused"),
        RuntimeError("unexpected"),
    ],
)
def test_probe_fails_open_without_a_newer_server_release(monkeypatch, health: Any) -> None:
    assert _probe_with(monkeypatch, health) is None


def test_probe_ignores_an_installed_release_it_cannot_read(monkeypatch) -> None:
    health = {"app": {"version": "0.2.0", "source_revision": SERVER_REVISION}}

    assert _probe_with(monkeypatch, health, client_version=OVERSIZED_VERSION) is None


def test_probe_fails_open_and_says_why_when_the_client_cannot_be_built(
    monkeypatch, capsys
) -> None:
    def unbuildable(_settings: MCPSettings) -> None:
        raise ImportError(SOCKS_WITHOUT_SOCKSIO)

    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", unbuildable)

    assert mcp_server.probe_client_update_notice(MCPSettings()) is None
    err = capsys.readouterr().err
    assert "could not check whether this Lab Tracker MCP client is behind its server" in err
    assert "ImportError" in err
    assert "socksio" in err


def test_probe_fails_open_under_a_proxy_setting_httpx_cannot_use(monkeypatch, capsys) -> None:
    # httpx reads the proxy variables when the client is constructed; a SOCKS
    # proxy without the optional socksio package raises ImportError right there.
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")

    assert mcp_server.probe_client_update_notice(MCPSettings(base_url="http://127.0.0.1:9")) is None
    assert "starting without the update check" in capsys.readouterr().err


def test_probe_reports_the_reason_on_one_bounded_stderr_line(monkeypatch, capsys) -> None:
    health = RuntimeError("first line\nsecond line Bearer sekrit-token " + "x" * 5000)

    assert _probe_with(monkeypatch, health) is None
    lines = capsys.readouterr().err.splitlines()

    assert len(lines) == 1
    assert "first line second line" in lines[0]
    assert "sekrit-token" not in lines[0]
    assert len(lines[0]) < 1000


def test_main_starts_the_server_when_the_update_check_cannot_run_for_a_loopback_target(
    monkeypatch, capsys
) -> None:
    # A loopback target skips the target-safety gate, which builds its own client
    # for any other target; only the update check's client is unbuildable here.
    events: list[tuple[str, ...]] = []

    class FakeServer:
        def run(self, *, transport: str) -> None:
            events.append(("run", transport))

    def fake_build(settings=None, *, client_update_notice=None):
        events.append(("build", settings.transport, str(client_update_notice)))
        return FakeServer()

    def unbuildable(_settings: MCPSettings) -> None:
        raise ImportError(SOCKS_WITHOUT_SOCKSIO)

    monkeypatch.setenv("LAB_TRACKER_MCP_TRANSPORT", "stdio")
    monkeypatch.delenv("LAB_TRACKER_MCP_BASE_URL", raising=False)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", unbuildable)
    monkeypatch.setattr(mcp_server, "build_server", fake_build)

    mcp_server.main()

    assert events == [("build", "stdio", "None"), ("run", "stdio")]
    assert "socksio" in capsys.readouterr().err


class _RecordingServer:
    def __init__(self, events: list[tuple[str, ...]]) -> None:
        self._events = events

    def run(self, *, transport: str) -> None:
        self._events.append(("run", transport))


def _stub_server_build(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    events: list[tuple[str, ...]] = []

    def fake_build(settings=None, *, client_update_notice=None):
        events.append(("build", settings.transport, str(client_update_notice)))
        return _RecordingServer(events)

    monkeypatch.setattr(mcp_server, "build_server", fake_build)
    return events


def _remote_stdio_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_MCP_TRANSPORT", "stdio")
    monkeypatch.delenv("LAB_TRACKER_MCP_BASE_URL", raising=False)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://lab.example.test")


def test_main_starts_the_server_when_no_client_can_be_built_for_a_remote_target(
    monkeypatch, capsys
) -> None:
    # A remote target runs the real target-safety gate, which builds its own
    # client before the update check does; both must stay advisory.
    def unbuildable(_settings: MCPSettings) -> None:
        raise ImportError(SOCKS_WITHOUT_SOCKSIO)

    _remote_stdio_env(monkeypatch)
    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", unbuildable)
    events = _stub_server_build(monkeypatch)

    mcp_server.main()

    assert events == [("build", "stdio", "None"), ("run", "stdio")]
    err = capsys.readouterr().err
    assert "startup safety probe could not run" in err
    assert "could not check whether this Lab Tracker MCP client is behind its server" in err


def test_main_starts_the_server_under_a_socks_proxy_setting(monkeypatch, capsys) -> None:
    # The real repro: with ALL_PROXY=socks5://... and no socksio installed, httpx
    # raised ImportError out of lt-mcp's startup for every remote API target.
    for name in ("NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    _remote_stdio_env(monkeypatch)
    events = _stub_server_build(monkeypatch)

    mcp_server.main()

    assert events == [("build", "stdio", "None"), ("run", "stdio")]
    assert "WARNING" in capsys.readouterr().err


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
