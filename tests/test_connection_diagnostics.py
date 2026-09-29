"""Failure stages use observed connections, not assumptions about tailnets."""

import json
import socket
import ssl
import threading
from contextlib import contextmanager

import httpx
import pytest

from lab_tracker.mcp_api_client import LabTrackerAPIClient, MCPSettings
from lab_tracker.mcp_tools import read
from lab_tracker_client import LabTracker, LTAPIError, setup
from lab_tracker_client import cli as lt_cli
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
    assert ("`tailscale status`" in result["next_step"]) == (kind == "tls")
    assert ("Publishing Through Tailscale Funnel" in result["next_step"]) == (kind == "tls")
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

    def send(_self, request, **_kwargs):
        calls.append(str(request.url))
        return httpx.Response(status, request=request)

    monkeypatch.setattr(httpx.Client, "send", send)
    result = setup.probe_health_diagnostics("https://origin.ts.net")
    assert result["reachable"] is reachable
    assert calls == ["https://origin.ts.net/health"]
    if status >= 400:
        assert result["diagnosis"] == "http_error"
        assert result["status_code"] == status
        assert "Funnel" not in str(result)
    else:
        assert "diagnosis" not in result


def test_health_probe_reports_the_server_release(monkeypatch):
    def send(_self, request, **_kwargs):
        return httpx.Response(
            200,
            json={"status": "ok", "app": {"version": "0.9.0", "source_revision": "B" * 40}},
            request=request,
        )

    monkeypatch.setattr(httpx.Client, "send", send)

    result = setup.probe_health_diagnostics("https://origin.ts.net")

    assert result == {"reachable": True, "release": {"version": "0.9.0", "revision": "b" * 40}}


def test_health_probe_without_a_json_body_stays_reachable_without_a_release(monkeypatch):
    def send(_self, request, **_kwargs):
        return httpx.Response(200, text="<html>ok</html>", request=request)

    monkeypatch.setattr(httpx.Client, "send", send)

    result = setup.probe_health_diagnostics("https://origin.ts.net")

    assert result["reachable"] is True
    assert "release" not in result


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


def test_health_probe_stays_fail_soft_when_httpx_rejects_the_proxy_setting(monkeypatch):
    # httpx reads the proxy variables while building the client; a SOCKS proxy
    # without the optional socksio package raises ImportError there, and the
    # session-start status must still report instead of crashing.
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")

    result = setup.probe_health_diagnostics("http://127.0.0.1:9")

    assert result["reachable"] is False
    assert result["diagnosis"] in {"transport_error", "proxy_connection_failed"}


def test_malformed_health_url_stays_fail_soft():
    assert setup.probe_health_diagnostics("https://[invalid")["reachable"] is False


def test_read_timeout_is_not_reported_as_tls_stall():
    result = ConnectionTrace("https://origin.ts.net").diagnose(httpx.ReadTimeout(""))
    assert result["diagnosis"] == "http_response_timeout"
    assert "Funnel" not in str(result)


def _setup_connect_dry_run(monkeypatch, tmp_path, capsys, base_url):
    """Run ``lt setup connect --dry-run`` and return (payload, config dir)."""
    config_dir = tmp_path / "lt-home"
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(config_dir))
    lt_cli.main(["setup", "connect", "--base-url", base_url, "--dry-run"])
    return json.loads(capsys.readouterr().out), config_dir


def test_setup_connect_reports_actual_tls_stall(monkeypatch, tmp_path, capsys):
    # NO_PROXY keeps the loopback probe off any sandbox/CI proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setattr(setup, "_HEALTH_PROBE_TIMEOUT_SECONDS", 0.1)
    with stalled_tls_server() as url:
        payload, config_dir = _setup_connect_dry_run(monkeypatch, tmp_path, capsys, url)
    assert payload["command"] == "setup-connect"
    assert payload["server_reachable"] is False
    diagnostic = payload["server_diagnostic"]
    assert diagnostic["diagnosis"] == "tls_handshake_stalled"
    assert "TLS handshake did not complete" in diagnostic["detail"]
    assert diagnostic["next_step"]
    assert "status_code" not in diagnostic
    assert "Funnel" not in json.dumps(diagnostic)  # 127.0.0.1 is not a .ts.net name
    assert not config_dir.exists()


@pytest.mark.parametrize("status,reachable", [(200, True), (401, True), (503, False)])
def test_setup_connect_reports_health_http_status(
    monkeypatch, tmp_path, capsys, status, reachable
):
    monkeypatch.setattr(
        httpx.Client,
        "send",
        lambda _self, request, **_kwargs: httpx.Response(status, request=request),
    )
    payload, _ = _setup_connect_dry_run(monkeypatch, tmp_path, capsys, "https://origin.ts.net")
    assert payload["server_reachable"] is reachable
    if status < 400:
        assert "server_diagnostic" not in payload
    else:
        diagnostic = payload["server_diagnostic"]
        assert diagnostic["diagnosis"] == "http_error"
        assert diagnostic["status_code"] == status
        assert set(diagnostic) == {"diagnosis", "detail", "next_step", "status_code"}
        assert "Funnel" not in json.dumps(diagnostic)


@pytest.mark.parametrize("failed_event", ["start_tls", "connect_tcp"])
def test_setup_connect_funnel_guidance_only_for_an_observed_tls_stall(
    monkeypatch, tmp_path, capsys, failed_event
):
    def send(_self, request, **_kwargs):
        trace = request.extensions["trace"]
        if failed_event == "start_tls":
            trace("connection.start_tls.started", {"server_hostname": b"origin.ts.net"})
        trace(f"connection.{failed_event}.failed", {})
        raise httpx.ConnectTimeout("secret handshake detail")

    monkeypatch.setattr(httpx.Client, "send", send)
    payload, _ = _setup_connect_dry_run(monkeypatch, tmp_path, capsys, "https://origin.ts.net")
    assert payload["server_reachable"] is False
    diagnostic = payload["server_diagnostic"]
    assert "secret" not in json.dumps(payload)
    if failed_event == "start_tls":
        assert diagnostic["diagnosis"] == "tls_handshake_stalled"
        assert "cannot confirm" in diagnostic["detail"]
        assert "tailscale funnel status" in diagnostic["next_step"]
    else:
        assert diagnostic["diagnosis"] == "tcp_connection_failed"
        assert "Funnel" not in json.dumps(diagnostic)


def test_setup_connect_without_a_base_url_does_not_probe(monkeypatch, tmp_path, capsys):
    def send(_self, _request, **_kwargs):
        raise AssertionError("no --base-url, so no health probe")

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-home"))
    lt_cli.main(["setup", "connect", "--project", "p-1", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert "server_reachable" not in payload
    assert "server_diagnostic" not in payload


@pytest.mark.parametrize("cause", ["handshake timed out", "handshake timed out."])
def test_error_message_separates_the_cause_from_the_diagnostic(cause):
    def handler(request):
        trace = request.extensions["trace"]
        trace("connection.start_tls.started", {"server_hostname": b"origin.example"})
        trace("connection.start_tls.failed", {})
        raise httpx.ConnectTimeout(cause, request=request)

    with (
        LabTracker(base_url="https://origin.example", transport=httpx.MockTransport(handler)) as lt,
        pytest.raises(LTAPIError) as caught,
    ):
        lt.health()
    message = str(caught.value)
    assert "failed: handshake timed out. The TCP connection succeeded" in message
    assert ".." not in message
