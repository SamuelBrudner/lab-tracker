"""Hosted (streamable-http) MCP surface bounds: review findings M34, M35, M36.

* M35: the hosted server registers only read tools and resources unless writes
  are explicitly enabled, never registers tools that read local host files, and
  refuses to start unless the server-held API credential is proven read-only.
* M36: the hosted startup open-ADMIN guard fails closed when it cannot confirm
  that the API enforces authentication; stdio still boots but warns loudly.
* M34: Host/Origin (DNS-rebinding) validation never rejects requests through a
  Host-preserving reverse proxy by default, whatever the bind address, and is
  enabled by an operator-configured allowlist.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from lab_tracker import mcp_server
from lab_tracker.auth import utc_now
from lab_tracker.mcp_api_client import LabTrackerAPIClient as RealAPIClient
from lab_tracker.mcp_tools import READ_TOOLS, WRITE_TOOLS

INBOUND_TOKEN = "inbound-" + "a" * 32
READ_TOOL_NAMES = {tool.__name__ for tool in READ_TOOLS}
WRITE_TOOL_NAMES = {tool.__name__ for tool in WRITE_TOOLS}
LOCAL_FILE_TOOL_NAMES = {"lab_tracker_upload_visualization_file"}


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


def _tool_names(server: Any) -> set[str]:
    return {tool.name for tool in asyncio.run(server.list_tools())}


def _resource_uris(server: Any) -> set[str]:
    return {str(resource.uri) for resource in asyncio.run(server.list_resources())}


def _hosted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("LAB_TRACKER_MCP_INBOUND_TOKEN", INBOUND_TOKEN)


# --- M35: hosted tool registration ------------------------------------------


def test_stdio_server_keeps_registering_every_read_and_write_tool() -> None:
    names = _tool_names(mcp_server.build_server(mcp_server.MCPServerRuntimeSettings()))

    assert names >= READ_TOOL_NAMES | WRITE_TOOL_NAMES


def test_hosted_server_registers_only_read_tools_and_resources_by_default() -> None:
    server = mcp_server.build_server(_hosted_settings())
    stdio_server = mcp_server.build_server(mcp_server.MCPServerRuntimeSettings())

    assert _tool_names(server) == READ_TOOL_NAMES
    assert _resource_uris(server) == _resource_uris(stdio_server)


def test_hosted_server_with_write_opt_in_never_registers_local_file_tools() -> None:
    names = _tool_names(mcp_server.build_server(_hosted_settings(allow_writes=True)))

    assert names == READ_TOOL_NAMES | (WRITE_TOOL_NAMES - LOCAL_FILE_TOOL_NAMES)
    assert "lab_tracker_record_evidence_bundle" in names
    assert "lab_tracker_request_graph_draft" in names
    assert not names & LOCAL_FILE_TOOL_NAMES


@pytest.mark.parametrize(
    "visualization",
    [
        {"analysis_id": "a", "viz_type": "figure", "upload_file": True},
        {
            "analysis_id": "a",
            "viz_type": "figure",
            "upload_file": True,
            "upload_file_path": "/etc/hostname",
        },
        {"analysis_id": "a", "viz_type": "figure", "upload_file_path": "/etc/hostname"},
    ],
)
def test_hosted_evidence_bundle_refuses_local_file_uploads_before_touching_disk(
    monkeypatch: pytest.MonkeyPatch,
    visualization: dict[str, Any],
) -> None:
    from lab_tracker import mcp_evidence_bundle
    from lab_tracker.mcp_tools import write as write_tools

    class UnreachableClient:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"hosted upload refusal reached the API client: {name}")

        def close(self) -> None:
            return None

    def forbid_snapshot(visualization: object) -> None:
        raise AssertionError(f"hosted evidence bundle read a local file: {visualization}")

    monkeypatch.setattr(write_tools, "client_from_env", lambda: UnreachableClient())
    monkeypatch.setattr(mcp_evidence_bundle, "_snapshot_upload", forbid_snapshot)
    server = mcp_server.build_server(_hosted_settings(allow_writes=True))

    _content, structured = asyncio.run(
        server.call_tool(
            "lab_tracker_record_evidence_bundle",
            {
                "project_id": "3f2b8c1e-6a4d-4e2f-9b1a-7c5d8e9f0a12",
                "visualization": visualization,
                "dry_run": True,
            },
        )
    )

    payload = structured.get("result", structured)
    assert payload["data"] is None
    assert payload["error"]["code"] == "validation_error"
    assert "local file" in payload["error"]["message"]


def test_runtime_settings_parse_write_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _hosted_env(monkeypatch)
    assert mcp_server.MCPServerRuntimeSettings.from_env().allow_writes is False

    for raw in ("true", "1", "YES", " on "):
        monkeypatch.setenv("LAB_TRACKER_MCP_ALLOW_WRITES", raw)
        assert mcp_server.MCPServerRuntimeSettings.from_env().allow_writes is True
    for raw in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("LAB_TRACKER_MCP_ALLOW_WRITES", raw)
        assert mcp_server.MCPServerRuntimeSettings.from_env().allow_writes is False

    monkeypatch.setenv("LAB_TRACKER_MCP_ALLOW_WRITES", "sometimes")
    with pytest.raises(SystemExit, match="LAB_TRACKER_MCP_ALLOW_WRITES"):
        mcp_server.MCPServerRuntimeSettings.from_env()


# --- M35: startup proof that the hosted API credential is read-only ----------


def _bridged_settings(
    client: TestClient, api_key: str | None, **overrides: Any
) -> tuple[mcp_server.MCPSettings, list[httpx.Request], Callable[..., Any]]:
    """Return settings plus a client factory that forwards to the real app."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        forwarded_headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in {"host", "content-length"}
        }
        response = client.request(
            request.method,
            request.url.path,
            params=list(request.url.params.multi_items()),
            content=request.content,
            headers=forwarded_headers,
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
        )

    settings = mcp_server.MCPSettings(
        base_url="http://api.internal:8000", api_key=api_key, **overrides
    )

    def factory(_settings: mcp_server.MCPSettings) -> mcp_server.LabTrackerAPIClient:
        return RealAPIClient(_settings, transport=httpx.MockTransport(handler))

    return settings, requests, factory


def _mint_token(
    client: TestClient,
    headers: dict[str, str],
    *,
    role: str,
    read_only: bool,
    scope: str = "all",
) -> str:
    response = client.post(
        "/auth/tokens",
        json={
            "label": f"hosted mcp {role} read_only={read_only}",
            "role": role,
            "read_only": read_only,
            "scope": scope,
            "expires_at": (utc_now() + timedelta(days=7)).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["secret"])


def test_hosted_read_only_check_accepts_a_read_only_lpat(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _mint_token(client, admin_auth_headers, role="editor", read_only=True)
    settings, requests, factory = _bridged_settings(client, token)
    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", factory)
    project_count = len(client.get("/projects", headers=admin_auth_headers).json()["data"])

    mcp_server._ensure_hosted_api_credential_is_read_only(settings)

    assert [request.method for request in requests] == ["POST"]
    # The probe can never reach a handler, so it cannot create anything.
    assert len(client.get("/projects", headers=admin_auth_headers).json()["data"]) == (
        project_count
    )


def _note_count(client: TestClient, headers: dict[str, str]) -> int:
    return len(client.get("/notes", headers=headers).json()["data"])


def test_hosted_read_only_check_refuses_a_stage_evidence_lpat(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The probe targets the real capture route, so a token whose only write
    # grant is the stage_evidence scope is reported write-capable.
    token = _mint_token(
        client, admin_auth_headers, role="editor", read_only=False, scope="stage_evidence"
    )
    settings, requests, factory = _bridged_settings(client, token)
    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", factory)
    note_count = _note_count(client, admin_auth_headers)

    with pytest.raises(SystemExit, match="can write"):
        mcp_server._ensure_hosted_api_credential_is_read_only(settings)

    assert [(request.method, request.url.path) for request in requests] == [("POST", "/notes")]
    # Request validation rejects the empty body before any handler runs.
    assert _note_count(client, admin_auth_headers) == note_count


def test_hosted_read_only_check_accepts_a_read_only_stage_evidence_lpat(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _mint_token(
        client, admin_auth_headers, role="editor", read_only=True, scope="stage_evidence"
    )
    settings, requests, factory = _bridged_settings(client, token)
    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", factory)
    note_count = _note_count(client, admin_auth_headers)

    mcp_server._ensure_hosted_api_credential_is_read_only(settings)

    assert [(request.method, request.url.path) for request in requests] == [("POST", "/notes")]
    assert _note_count(client, admin_auth_headers) == note_count


@pytest.mark.parametrize("role", ["editor", "admin"])
def test_hosted_read_only_check_refuses_a_write_capable_lpat(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    token = _mint_token(client, admin_auth_headers, role=role, read_only=False)
    settings, _requests, factory = _bridged_settings(client, token)
    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", factory)
    project_count = len(client.get("/projects", headers=admin_auth_headers).json()["data"])

    with pytest.raises(SystemExit, match="can write") as excinfo:
        mcp_server._ensure_hosted_api_credential_is_read_only(settings)

    assert token not in str(excinfo.value)
    assert "LAB_TRACKER_MCP_ALLOW_WRITES" in str(excinfo.value)
    assert len(client.get("/projects", headers=admin_auth_headers).json()["data"]) == (
        project_count
    )


def test_hosted_read_only_check_refuses_a_rejected_lpat(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _requests, factory = _bridged_settings(client, "lpat_" + "x" * 43)
    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", factory)

    with pytest.raises(SystemExit, match="could not verify"):
        mcp_server._ensure_hosted_api_credential_is_read_only(settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": None},
        {"api_key": "   "},
        {"api_key": None, "username": "svc", "password": "pw"},
        {"api_key": "eyJhbGciOiJIUzI1NiJ9.session.jwt"},
    ],
    ids=["none", "blank", "username-password", "session-jwt"],
)
def test_hosted_read_only_check_requires_an_lpat_without_probing(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
) -> None:
    def fail_client(_settings: mcp_server.MCPSettings) -> None:
        raise AssertionError("a non-LPAT credential must be refused before probing")

    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", fail_client)

    with pytest.raises(SystemExit, match="read-only personal access token"):
        mcp_server._ensure_hosted_api_credential_is_read_only(
            mcp_server.MCPSettings(base_url="http://api.internal:8000", **overrides)
        )


@pytest.mark.parametrize(
    "response",
    [
        # The API's answer to the probe's empty body once the token policy
        # admitted the write: validation stops it before any handler runs.
        httpx.Response(
            422,
            json={
                "error": {
                    "code": "request_validation_error",
                    "message": "Request validation failed.",
                    "issues": [],
                }
            },
        ),
        httpx.Response(200, json={"data": {}}),
        httpx.Response(201, json={"data": {}}),
    ],
    ids=["422", "200", "201"],
)
def test_hosted_read_only_check_treats_an_unrefused_write_as_write_capable(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    def factory(settings: mcp_server.MCPSettings) -> mcp_server.LabTrackerAPIClient:
        return RealAPIClient(
            settings, transport=httpx.MockTransport(lambda _request: response)
        )

    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", factory)

    with pytest.raises(SystemExit, match="can write"):
        mcp_server._ensure_hosted_api_credential_is_read_only(
            mcp_server.MCPSettings(base_url="http://api.internal:8000", api_key="lpat_abc")
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={"error": {"code": "forbidden", "message": "nope"}}),
        httpx.Response(502, text="bad gateway"),
        # The probe route exists; a missing route or method means the probe hit
        # something other than the Lab Tracker API.
        httpx.Response(404, json={"detail": "Not Found"}),
        httpx.Response(405, json={"detail": "Method Not Allowed"}),
    ],
    ids=["other-403", "502", "404", "405"],
)
def test_hosted_read_only_check_fails_closed_on_indeterminate_answers(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
) -> None:
    def factory(settings: mcp_server.MCPSettings) -> mcp_server.LabTrackerAPIClient:
        return RealAPIClient(
            settings, transport=httpx.MockTransport(lambda _request: response)
        )

    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", factory)

    with pytest.raises(SystemExit):
        mcp_server._ensure_hosted_api_credential_is_read_only(
            mcp_server.MCPSettings(base_url="http://api.internal:8000", api_key="lpat_abc")
        )


def test_hosted_read_only_check_fails_closed_when_api_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    def factory(settings: mcp_server.MCPSettings) -> mcp_server.LabTrackerAPIClient:
        return RealAPIClient(settings, transport=httpx.MockTransport(refuse))

    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", factory)

    with pytest.raises(SystemExit, match="could not verify"):
        mcp_server._ensure_hosted_api_credential_is_read_only(
            mcp_server.MCPSettings(base_url="http://api.internal:8000", api_key="lpat_abc")
        )


def _record_main_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, object]], list[mcp_server.MCPServerRuntimeSettings]]:
    calls: list[tuple[str, object]] = []
    built: list[mcp_server.MCPServerRuntimeSettings] = []

    def fake_target_guard(_settings: mcp_server.MCPSettings, *, hosted: bool) -> None:
        calls.append(("target", hosted))

    def fake_read_only(_settings: mcp_server.MCPSettings) -> None:
        calls.append(("read_only", None))

    def fake_build(settings: mcp_server.MCPServerRuntimeSettings) -> object:
        built.append(settings)
        return object()

    monkeypatch.setattr(mcp_server, "_ensure_mcp_target_safe", fake_target_guard)
    monkeypatch.setattr(mcp_server, "_ensure_hosted_api_credential_is_read_only", fake_read_only)
    monkeypatch.setattr(mcp_server, "build_server", fake_build)
    monkeypatch.setattr(mcp_server, "_run_streamable_http", lambda _server, _settings: None)
    return calls, built


def test_main_verifies_read_only_credential_for_default_hosted_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hosted_env(monkeypatch)
    calls, built = _record_main_calls(monkeypatch)

    mcp_server.main()

    assert calls == [("target", True), ("read_only", None)]
    assert built[0].allow_writes is False


def test_main_skips_read_only_proof_only_with_explicit_write_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _hosted_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_MCP_ALLOW_WRITES", "true")
    calls, built = _record_main_calls(monkeypatch)

    mcp_server.main()

    assert calls == [("target", True)]
    assert built[0].allow_writes is True
    assert "LAB_TRACKER_MCP_ALLOW_WRITES" in capsys.readouterr().err


# --- M36: the open-ADMIN guard fails closed for the hosted transport ---------


class _ProbeClient:
    """Fake API client whose readiness probe returns or raises a fixed value."""

    outcome: object = None

    def __init__(self, _settings: mcp_server.MCPSettings) -> None:
        return None

    def readiness(self) -> dict[str, Any]:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        assert isinstance(self.outcome, dict)
        return self.outcome

    def close(self) -> None:
        return None


def _install_probe(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    probe = type("Probe", (_ProbeClient,), {"outcome": outcome})
    monkeypatch.setattr(mcp_server, "LabTrackerAPIClient", probe)


PROBE_FAILURES = [
    mcp_server.LabTrackerAPIUnavailableError("connection refused", code="lab_tracker_unavailable"),
    mcp_server.LabTrackerAPIUnavailableError("Bad gateway", status_code=502),
    mcp_server.LabTrackerAPIError("Not Found", status_code=404),
    mcp_server.LabTrackerAPIAuthError("Invalid personal access token.", status_code=401),
]


@pytest.mark.parametrize("failure", PROBE_FAILURES, ids=["unreachable", "502", "404", "401"])
@pytest.mark.parametrize(
    "base_url", ["http://app:8000", "http://127.0.0.1:8000"], ids=["remote", "loopback"]
)
def test_hosted_target_guard_fails_closed_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    base_url: str,
) -> None:
    _install_probe(monkeypatch, failure)

    with pytest.raises(SystemExit, match="could not confirm") as excinfo:
        mcp_server._ensure_mcp_target_safe(
            mcp_server.MCPSettings(base_url=base_url), hosted=True
        )

    assert base_url in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [{"status": "ok"}, {"status": "ok", "auth": None}, {"status": "ok", "auth": {}}],
    ids=["missing", "null", "empty"],
)
def test_hosted_target_guard_requires_readiness_to_report_auth_enabled(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    _install_probe(monkeypatch, payload)

    with pytest.raises(SystemExit, match="auth.enabled"):
        mcp_server._ensure_mcp_target_safe(
            mcp_server.MCPSettings(base_url="http://app:8000"), hosted=True
        )


def test_hosted_target_guard_refuses_auth_disabled_loopback_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_probe(monkeypatch, {"status": "ok", "auth": {"enabled": False}})

    with pytest.raises(SystemExit, match="auth-disabled"):
        mcp_server._ensure_mcp_target_safe(
            mcp_server.MCPSettings(base_url="http://127.0.0.1:8000"), hosted=True
        )


def test_hosted_target_guard_accepts_auth_enabled_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_probe(monkeypatch, {"status": "ok", "auth": {"enabled": True}})

    mcp_server._ensure_mcp_target_safe(
        mcp_server.MCPSettings(base_url="http://app:8000"), hosted=True
    )


@pytest.mark.parametrize("failure", PROBE_FAILURES[:3], ids=["unreachable", "502", "404"])
def test_stdio_target_guard_warns_loudly_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    _install_probe(monkeypatch, failure)

    mcp_server._ensure_mcp_target_safe(mcp_server.MCPSettings(base_url="http://lab.example.test"))

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "http://lab.example.test" in err
    assert "could not confirm" in err


# --- M34: configurable DNS-rebinding Host/Origin allowlist -------------------


def test_runtime_settings_default_to_no_host_or_origin_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hosted_env(monkeypatch)

    settings = mcp_server.MCPServerRuntimeSettings.from_env()

    assert settings.allowed_hosts == ()
    assert settings.allowed_origins == ()


def test_runtime_settings_refuse_origin_allowlist_without_host_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hosted_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_MCP_ALLOWED_ORIGINS", "https://github.com")

    with pytest.raises(SystemExit, match="LAB_TRACKER_MCP_ALLOWED_HOSTS"):
        mcp_server.MCPServerRuntimeSettings.from_env()


def test_runtime_settings_parse_allowed_hosts_and_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hosted_env(monkeypatch)
    monkeypatch.setenv("LAB_TRACKER_MCP_ALLOWED_HOSTS", " mcp.lab.internal , 127.0.0.1:* ")
    monkeypatch.setenv(
        "LAB_TRACKER_MCP_ALLOWED_ORIGINS", "https://github.com,https://x.githubcopilot.com"
    )

    settings = mcp_server.MCPServerRuntimeSettings.from_env()

    assert settings.allowed_hosts == ("mcp.lab.internal", "127.0.0.1:*")
    assert settings.allowed_origins == ("https://github.com", "https://x.githubcopilot.com")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LAB_TRACKER_MCP_ALLOWED_HOSTS", ","),
        ("LAB_TRACKER_MCP_ALLOWED_HOSTS", "*"),
        ("LAB_TRACKER_MCP_ALLOWED_HOSTS", "https://mcp.lab.internal"),
        ("LAB_TRACKER_MCP_ALLOWED_HOSTS", "mcp lab"),
        ("LAB_TRACKER_MCP_ALLOWED_ORIGINS", "*"),
        ("LAB_TRACKER_MCP_ALLOWED_ORIGINS", "github.com"),
        ("LAB_TRACKER_MCP_ALLOWED_ORIGINS", "https://github.com/path"),
    ],
)
def test_runtime_settings_reject_malformed_allowlists(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _hosted_env(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit, match=name):
        mcp_server.MCPServerRuntimeSettings.from_env()


@pytest.mark.parametrize("bind_host", ["127.0.0.1", "0.0.0.0"])
def test_build_server_enables_host_validation_with_configured_allowlist(
    bind_host: str,
) -> None:
    server = mcp_server.build_server(
        _hosted_settings(
            host=bind_host,
            allowed_hosts=("mcp.lab.internal",),
            allowed_origins=("https://github.com",),
        )
    )

    security = server.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["mcp.lab.internal"]
    assert security.allowed_origins == ["https://github.com"]


@pytest.mark.parametrize("bind_host", ["127.0.0.1", "0.0.0.0"])
def test_build_server_disables_host_validation_without_allowlist(bind_host: str) -> None:
    # FastMCP would otherwise auto-enable a loopback-only allowlist for a
    # loopback bind; the inbound bearer already defeats DNS rebinding.
    server = mcp_server.build_server(_hosted_settings(host=bind_host))

    security = server.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is False


def _initialize_status(
    settings: mcp_server.MCPServerRuntimeSettings,
    *,
    host_header: str,
    origin: str | None = None,
) -> int:
    runtime_server = mcp_server.build_server(settings)
    fastmcp_app = runtime_server.streamable_http_app()
    app = mcp_server.MCPInboundBearerAuthMiddleware(fastmcp_app, INBOUND_TOKEN)
    headers = {
        "Authorization": f"Bearer {INBOUND_TOKEN}",
        "Host": host_header,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if origin is not None:
        headers["Origin"] = origin
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }

    async def exercise() -> int:
        async with (
            fastmcp_app.router.lifespan_context(fastmcp_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9000"
            ) as client,
        ):
            response = await client.post("/mcp", content=json.dumps(body), headers=headers)
            return response.status_code

    return asyncio.run(exercise())


def test_proxied_host_is_accepted_when_configured() -> None:
    settings = _hosted_settings(
        allowed_hosts=("mcp.lab.internal",),
        allowed_origins=("https://github.com",),
    )

    assert _initialize_status(settings, host_header="mcp.lab.internal") == 200
    assert (
        _initialize_status(
            settings, host_header="mcp.lab.internal", origin="https://github.com"
        )
        == 200
    )


def test_unexpected_host_and_origin_are_rejected() -> None:
    configured = _hosted_settings(
        allowed_hosts=("mcp.lab.internal",),
        allowed_origins=("https://github.com",),
    )

    assert _initialize_status(configured, host_header="evil.example") == 421
    assert _initialize_status(configured, host_header="127.0.0.1:9000") == 421
    assert (
        _initialize_status(
            configured, host_header="mcp.lab.internal", origin="https://evil.example"
        )
        == 403
    )


@pytest.mark.parametrize("bind_host", ["127.0.0.1", "0.0.0.0"])
@pytest.mark.parametrize(
    "host_header",
    # Direct loopback, a Host-preserving `tailscale serve` / nginx / Traefik
    # front, the checked-in Caddyfile's public name, and a compose service name.
    ["127.0.0.1:9000", "labhost.tail1234.ts.net", "mcp.lab.internal", "mcp:8000"],
)
def test_host_preserving_proxy_is_accepted_without_allowlist(
    bind_host: str, host_header: str
) -> None:
    settings = _hosted_settings(host=bind_host)

    assert _initialize_status(settings, host_header=host_header) == 200
    assert (
        _initialize_status(settings, host_header=host_header, origin="https://github.com")
        == 200
    )


def test_host_allowlist_without_origins_rejects_every_origin() -> None:
    settings = _hosted_settings(allowed_hosts=("mcp.lab.internal",))

    assert _initialize_status(settings, host_header="mcp.lab.internal") == 200
    assert (
        _initialize_status(
            settings, host_header="mcp.lab.internal", origin="https://github.com"
        )
        == 403
    )


def test_hosted_initialize_through_proxied_host_completes_session() -> None:
    settings = _hosted_settings(allowed_hosts=("mcp.lab.internal",))
    runtime_server = mcp_server.build_server(settings)
    fastmcp_app = runtime_server.streamable_http_app()
    app = mcp_server.MCPInboundBearerAuthMiddleware(fastmcp_app, INBOUND_TOKEN)

    async def exercise() -> list[str]:
        async with (
            fastmcp_app.router.lifespan_context(fastmcp_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://mcp.lab.internal",
                headers={"Authorization": f"Bearer {INBOUND_TOKEN}"},
            ) as client,
            streamable_http_client("http://mcp.lab.internal/mcp", http_client=client) as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
        return [tool.name for tool in tools.tools]

    assert set(asyncio.run(exercise())) == READ_TOOL_NAMES


def test_caddyfile_preserves_public_host_and_names_the_allowlist() -> None:
    caddy = Path("deploy/mcp/Caddyfile").read_text(encoding="utf-8")

    # Caddy owns the public Host/Origin policy (421/403 above) and forwards the
    # client's Host unchanged; an operator who also wants lt-mcp to check it
    # lists that same public name.
    assert "header_up Host" not in caddy
    assert "header_up -Origin" not in caddy
    assert "LAB_TRACKER_MCP_ALLOWED_HOSTS=mcp.lab.internal" in caddy


def test_quickstart_does_not_promise_write_tools_a_hosted_server_lacks() -> None:
    text = mcp_server.lab_tracker_quickstart()

    assert "Read and write tools call" not in text
    assert "LAB_TRACKER_MCP_ALLOW_WRITES" in text
