"""Failure stages use observed connections, not assumptions about tailnets."""

import socket
import ssl
import threading
from contextlib import contextmanager

import httpx
import pytest

from lab_tracker.mcp_api_client import LabTrackerAPIClient, MCPSettings
from lab_tracker.mcp_tools import read
from lab_tracker_client import LabTracker, LTAPIError, setup
from lab_tracker_client.connection_diagnostics import ConnectionTrace


@contextmanager
def stalled_tls_server():
    stop = threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(2)

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    stop.wait(3)
            except OSError:
                pass

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield f"https://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            stop.set()
            worker.join(timeout=3)


def test_real_tcp_accept_tls_stall_is_classified_without_extra_connection(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with stalled_tls_server() as url:
        with (
            LabTracker(base_url=url, timeout_seconds=0.1) as client,
            pytest.raises(LTAPIError) as caught,
        ):
            client.health()
        assert caught.value.connection_diagnostic["diagnosis"] == "tls_handshake_stalled"
        assert "TCP connection succeeded" in str(caught.value)
        assert "Funnel" not in str(caught.value)


def test_setup_reports_actual_tls_stall(monkeypatch, tmp_path):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setattr(setup, "_HEALTH_PROBE_TIMEOUT_SECONDS", 0.1)
    with stalled_tls_server() as url:
        monkeypatch.setattr(setup, "_resolve_base_url", lambda _profile: (url, "profile"))
        result = setup.setup_status(tmp_path)["server"]
    assert result["reachable"] is False
    assert result["diagnosis"] == "tls_handshake_stalled"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("tcp", "tcp_connection_failed"),
        ("tls", "tls_handshake_stalled"),
        ("proxy_tls", "proxy_connection_failed"),
    ],
)
def test_stage_and_conditional_funnel_guidance(kind, expected):
    trace = ConnectionTrace("https://origin.example.ts.net:8443")
    if kind != "tcp":
        trace(
            "connection.start_tls.started",
            {
                "server_hostname": b"proxy.example"
                if kind == "proxy_tls"
                else b"origin.example.ts.net",
            },
        )
    trace("connection.connect_tcp.failed" if kind == "tcp" else "connection.start_tls.failed", {})
    result = trace.diagnose(httpx.ConnectTimeout("secret"))
    assert result["diagnosis"] == expected
    assert "secret" not in str(result)
    assert ("tailscale funnel status" in result["next_step"]) == (kind == "tls")
    if kind == "tls":
        assert "If this host uses" in result["detail"]
        assert "cannot confirm" in result["detail"]
        assert "do not need to join" in result["next_step"]


@pytest.mark.parametrize(
    "cause,expected",
    [
        (ssl.SSLCertVerificationError("bad cert"), "tls_certificate_error"),
        (socket.gaierror("unknown host"), "dns_resolution_failed"),
    ],
)
def test_cause_chain_classification(cause, expected):
    exc = httpx.ConnectError("connection failed")
    exc.__cause__ = cause
    assert ConnectionTrace("https://host").diagnose(exc)["diagnosis"] == expected


def test_unobserved_timeout_does_not_claim_tls_or_funnel():
    result = ConnectionTrace("https://origin.ts.net").diagnose(httpx.ConnectTimeout(""))
    assert result["diagnosis"] == "transport_error"
    assert "Funnel" not in str(result)


@pytest.mark.parametrize("status,reachable", [(200, True), (401, True), (503, False)])
def test_health_http_status_preserves_reachability_contract(monkeypatch, status, reachable):
    calls = []

    def get(_self, url, **kwargs):
        calls.append(url)
        return httpx.Response(status)

    monkeypatch.setattr(httpx.Client, "get", get)
    result = setup.probe_health_diagnostics("https://origin.ts.net")
    assert result["reachable"] is reachable
    assert calls == ["https://origin.ts.net/health"]
    if status >= 400:
        assert result["diagnosis"] == "http_error"
        assert result["status_code"] == status
        assert "Funnel" not in str(result)
    else:
        assert "diagnosis" not in result


def test_mcp_health_keeps_fail_soft_and_exposes_diagnostic(monkeypatch):
    def handler(request):
        trace = request.extensions["trace"]
        trace("connection.start_tls.started", {"server_hostname": b"origin.ts.net"})
        trace("connection.start_tls.failed", {})
        raise httpx.ConnectTimeout("handshake timed out")

    client = LabTrackerAPIClient(
        MCPSettings(base_url="https://origin.ts.net"),
        transport=httpx.MockTransport(handler),
    )
    read.close_cached_read_client()
    monkeypatch.setattr(read, "client_from_env", lambda: client)
    try:
        result = read._read_tool("lab_tracker_health", lambda c: c.health(), hint={})
    finally:
        read.close_cached_read_client()
    assert result["error"]["code"] == "lab_tracker_unavailable"
    assert result["error"]["diagnosis"] == "tls_handshake_stalled"
    assert "tailscale funnel status" in result["error"]["next_step"]
    assert result["next_action"]["action"] == "proceed_without_graph_context"


def test_malformed_health_url_stays_fail_soft():
    assert setup.probe_health_diagnostics("https://[invalid")["reachable"] is False


def test_read_timeout_is_not_reported_as_tls_stall():
    result = ConnectionTrace("https://origin.ts.net").diagnose(httpx.ReadTimeout(""))
    assert result["diagnosis"] == "http_response_timeout"
    assert "Funnel" not in str(result)
