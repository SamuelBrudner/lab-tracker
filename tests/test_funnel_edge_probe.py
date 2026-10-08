"""Tests for the Funnel edge probe host script (``scripts/funnel_edge_probe.py``)."""

from __future__ import annotations

import ast
import gzip
import importlib.util
import json
import plistlib
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "funnel_edge_probe.py"
FAST = {"connect": 2.0, "tls": 0.5, "http": 0.5}


def _load_module():
    spec = importlib.util.spec_from_file_location("funnel_edge_probe", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_module()


@pytest.fixture
def silent_listener():
    """Completes TCP handshakes from its backlog but never reads or answers."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    yield listener.getsockname()[1]
    listener.close()


def test_script_stays_python39_compatible_for_launchd():
    # launchd runs the probe with macOS's /usr/bin/python3, which is 3.9.
    ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"), feature_version=(3, 9))


def test_accepted_connection_without_server_hello_is_a_tls_handshake_timeout(silent_listener):
    # The issue #211 signature: TCP connects, the ClientHello is sent, and no
    # ServerHello ever arrives.
    result = probe.check_address(
        "lab.example.ts.net",
        "127.0.0.1",
        silent_listener,
        "/health",
        timeouts=probe.Timeouts(**FAST),
    )

    assert result["outcome"] == "tls_handshake"
    assert result["error"] == "timeout"
    assert result["connect_ms"] >= 0
    assert result["failed_after_ms"] >= 400


def test_connection_closed_during_handshake_is_not_reported_as_a_timeout():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def accept_and_close() -> None:
        conn, _ = listener.accept()
        conn.close()

    thread = threading.Thread(target=accept_and_close, daemon=True)
    thread.start()
    try:
        result = probe.check_address(
            "lab.example.ts.net",
            "127.0.0.1",
            listener.getsockname()[1],
            "/health",
            timeouts=probe.Timeouts(connect=2.0, tls=5.0, http=1.0),
        )
    finally:
        thread.join(timeout=5)
        listener.close()

    assert result["outcome"] == "tls_handshake"
    assert result["error"] != "timeout"


def test_refused_connection_fails_at_tcp_connect():
    placeholder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    placeholder.bind(("127.0.0.1", 0))
    port = placeholder.getsockname()[1]
    placeholder.close()

    result = probe.check_address(
        "lab.example.ts.net", "127.0.0.1", port, "/health", timeouts=probe.Timeouts(**FAST)
    )

    assert result["outcome"] == "tcp_connect"
    assert result["error"] == "connection refused"


def test_http_response_names_the_instance_that_answered():
    client, server = socket.socketpair()
    body = json.dumps(
        {
            "status": "ok",
            "app": {
                "name": "marion-lab-tracker",
                "source_revision": "901c3aa3c61dd2218122e179d94ff40bf7f5d7ae",
            },
        }
    ).encode()
    server.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    try:
        status, payload = probe.read_http_response(client, "lab.example.ts.net", 8443, "/health")
        request = server.recv(4096)
    finally:
        client.close()
        server.close()

    assert status == 200
    assert b"GET /health HTTP/1.1\r\nHost: lab.example.ts.net:8443\r\n" in request
    assert probe.health_identity(payload) == {
        "app": "marion-lab-tracker",
        "revision": "901c3aa3c61d",
    }
    assert probe.health_identity(b"<html>") == {}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://lab.example.ts.net", ("lab.example.ts.net", 443, "/health")),
        ("https://lab.example.ts.net:8443/", ("lab.example.ts.net", 8443, "/health")),
        ("https://lab.example.ts.net/lab/health", ("lab.example.ts.net", 443, "/lab/health")),
    ],
)
def test_parse_target_probes_the_health_route(url, expected):
    assert probe.parse_target(url) == expected


def test_parse_target_rejects_non_https_urls():
    with pytest.raises(ValueError, match="https://"):
        probe.parse_target("http://lab.example.ts.net:8443")


def test_parse_dig_output_skips_cname_lines():
    output = "lab.example.ts.net.\n209.177.145.97\n2607:f740:f::67\n\n"
    assert probe.parse_dig_output(output) == ["209.177.145.97", "2607:f740:f::67"]


def test_serve_config_summary_marks_funnel_and_foreground_mappings():
    config = {
        "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
        "Web": {
            "lab.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}},
            "lab.example.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8100"}}},
        },
        "AllowFunnel": {"lab.example.ts.net:8443": True},
        "Foreground": {
            "session": {
                "Web": {
                    "lab.example.ts.net:10000": {"Handlers": {"/": {"Text": "hello"}}},
                },
                "AllowFunnel": {"lab.example.ts.net:10000": True},
            }
        },
    }

    serve = probe.summarize_serve_config(config)

    assert serve == {
        "lab.example.ts.net:443": {"funnel": False, "handlers": {"/": "http://127.0.0.1:8000"}},
        "lab.example.ts.net:8443": {"funnel": True, "handlers": {"/": "http://127.0.0.1:8100"}},
        "lab.example.ts.net:10000": {
            "funnel": True,
            "handlers": {"/": "Text"},
            "foreground": True,
        },
    }
    assert probe.loopback_backends(serve) == ["http://127.0.0.1:8000", "http://127.0.0.1:8100"]


def test_tailscale_status_summary_keeps_node_state_and_health():
    status = {
        "BackendState": "Running",
        "Version": "1.102.4",
        "Health": ["Tailscale could not connect to the 'nyc' relay server."],
        "Self": {"Online": True, "Relay": "nyc", "DNSName": "lab.example.ts.net."},
        "Peer": {"ignored": {}},
    }

    assert probe.summarize_tailscale_status(status) == {
        "backend_state": "Running",
        "online": True,
        "relay": "nyc",
        "dns_name": "lab.example.ts.net",
        "version": "1.102.4",
        "health": ["Tailscale could not connect to the 'nyc' relay server."],
    }


def test_probe_command_records_a_stall_and_report_names_it(tmp_path, monkeypatch, silent_listener):
    url = f"https://lab.example.ts.net:{silent_listener}"
    monkeypatch.setattr(
        probe,
        "resolve_public",
        lambda host, resolver, ipv6: {
            "resolver": resolver,
            "addresses": ["127.0.0.1"],
            "error": None,
        },
    )
    monkeypatch.setattr(probe, "resolve_system", lambda host, port: ["100.64.0.1"])
    monkeypatch.setattr(
        probe,
        "tailscale_snapshot",
        lambda: {"backend_state": "Running", "online": True, "health": [], "serve": {}},
    )
    log = tmp_path / "edge.jsonl"

    exit_code = probe.main(
        ["probe", "--url", url, "--log", str(log), "--tls-timeout", "0.5", "--http-timeout", "0.5"]
    )

    assert exit_code == 0
    record = json.loads(log.read_text(encoding="utf-8"))
    assert record["ok"] is False
    assert record["public"][0]["host_view"] == ["100.64.0.1"]
    [check] = record["public"][0]["checks"]
    assert (check["outcome"], check["error"]) == ("tls_handshake", "timeout")

    report = probe.build_report(probe.iter_records(log))
    assert "1 run" in report[1]
    assert url in report[1]
    assert "tls_handshake timeout (1/1 addresses)" in report[1]
    assert "tailscale Running" in report[1]


def _record(at: datetime, *, failing: bool = False, boot: str = "2026-09-29T00:25:53+00:00",
            state: str = "Running") -> dict:
    checks = [
        {"address": "209.177.145.97", "outcome": "ok", "error": None},
        {"address": "209.177.145.192", "outcome": "ok", "error": None},
    ]
    if failing:
        checks = [dict(check, outcome="tls_handshake", error="timeout") for check in checks]
    return {
        "schema": 1,
        "at": at.isoformat(timespec="seconds"),
        "host": {"boot_time": boot},
        "tailscale": {"backend_state": state, "online": True, "health": [], "serve": {}},
        "backends": [{"target": "http://127.0.0.1:8100", "status": 200}],
        "public": [
            {
                "url": "https://lab.example.ts.net:8443",
                "checks": checks,
                "ok": not failing,
            }
        ],
        "ok": not failing,
    }


def test_report_lists_incidents_gaps_reboots_and_state_changes():
    start = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    step = timedelta(minutes=5)
    records = [
        _record(start),
        _record(start + step, failing=True),
        _record(start + 2 * step, failing=True),
        _record(start + 3 * step),
        # Two hours with no runs, then a reboot that left Tailscale stopped.
        _record(
            start + 3 * step + timedelta(hours=2),
            boot="2026-10-08T14:10:00+00:00",
            state="Stopped",
        ),
    ]

    lines = probe.build_report(records)
    text = "\n".join(lines)

    assert lines[0].startswith("5 probe runs from ")
    incident = "(2 runs)  https://lab.example.ts.net:8443  tls_handshake timeout (2/2 addresses)"
    assert incident in text
    assert "no probe runs for 2h: host asleep or off, or the agent was not running" in text
    assert "host booted" in text
    assert "tailscale Running -> Stopped" in text


def test_report_on_a_quiet_log_says_so():
    start = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    lines = probe.build_report([_record(start), _record(start + timedelta(minutes=5))])
    assert lines[1] == "No failures, gaps, reboots, or Tailscale state changes."
    assert probe.build_report([]) == ["No probe records found."]


def test_log_rotates_into_numbered_gzip_files_and_report_reads_them_in_order(tmp_path):
    log = tmp_path / "edge.jsonl"
    start = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    for minute in range(4):
        probe.append_record(
            log, _record(start + timedelta(minutes=5 * minute)), max_bytes=1, keep=2
        )

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "edge.jsonl",
        "edge.jsonl.1.gz",
        "edge.jsonl.2.gz",
    ]
    with gzip.open(tmp_path / "edge.jsonl.1.gz", "rt", encoding="utf-8") as handle:
        assert json.loads(handle.read())["at"] == "2026-10-08T12:10:00+00:00"
    times = [record["at"] for record in probe.iter_records(log)]
    assert times == [
        "2026-10-08T12:05:00+00:00",
        "2026-10-08T12:10:00+00:00",
        "2026-10-08T12:15:00+00:00",
    ]


def test_launchd_plist_runs_the_copied_script_with_a_path_that_finds_tailscale(tmp_path):
    log = tmp_path / "Logs" / "funnel-edge-probe.jsonl"
    plist = plistlib.loads(
        probe.render_launchd_plist(
            python="/usr/bin/python3",
            script="/Users/me/Library/Application Support/lab-tracker/funnel_edge_probe.py",
            urls=["https://lab.example.ts.net", "https://lab.example.ts.net:8443"],
            log=log,
            interval_seconds=300,
            ipv6=False,
        )
    )

    assert plist["Label"] == "com.lab-tracker.funnel-edge-probe"
    assert plist["ProgramArguments"] == [
        "/usr/bin/python3",
        "/Users/me/Library/Application Support/lab-tracker/funnel_edge_probe.py",
        "probe",
        "--log",
        str(log),
        "--resolver",
        "1.1.1.1",
        "--no-ipv6",
        "--url",
        "https://lab.example.ts.net",
        "--url",
        "https://lab.example.ts.net:8443",
    ]
    assert plist["StartInterval"] == 300
    assert plist["RunAtLoad"] is True
    assert plist["ProcessType"] == "Interactive"
    assert "/usr/local/bin" in plist["EnvironmentVariables"]["PATH"]
    assert plist["StandardErrorPath"] == str(log.with_name("funnel-edge-probe.stderr.log"))
