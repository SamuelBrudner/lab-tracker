"""Typed API errors are operational failures, not CLI crashes (GH #220)."""

import json
import os
import subprocess
import sys

import httpx
import pytest

from lab_tracker_client import LabTracker, LTAPIError, cli


@pytest.mark.parametrize(
    "command", [["health"], ["readiness"], ["project", "bind", "--name", "demo", "--dry-run"]]
)
def test_api_failure_prints_stderr_and_closes_client(command, monkeypatch, capsys):
    def fail(request):
        raise httpx.ConnectTimeout("TLS handshake timed out", request=request)

    client = LabTracker(base_url="https://test.invalid", transport=httpx.MockTransport(fail))
    monkeypatch.delenv("LAB_TRACKER_DEBUG", raising=False)
    monkeypatch.setattr(cli.LabTracker, "from_env", lambda: client)
    with pytest.raises(SystemExit) as error:
        cli.main(command)
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: Lab Tracker request GET /")
    assert "TLS handshake timed out" in captured.err
    assert "Traceback" not in captured.err
    assert client._client.is_closed


def test_client_initialization_api_failure_is_also_handled(monkeypatch, capsys):
    def fail():
        raise LTAPIError("Cannot initialize API client")

    monkeypatch.delenv("LAB_TRACKER_DEBUG", raising=False)
    monkeypatch.setattr(cli.LabTracker, "from_env", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(["health"])
    assert error.value.code == 1
    assert capsys.readouterr().err == "error: Cannot initialize API client\n"


@pytest.mark.parametrize(
    "debug_args,debug_env,traceback",
    [
        ([], "", False),
        ([], "0", False),
        (["--debug"], "", True),
        ([], "1", True),
    ],
)
def test_process_exit_and_debug_traceback(debug_args, debug_env, traceback):
    # Deterministic failure through the actual CLI entrypoint, no network needed.
    program = """
import sys
import httpx
from lab_tracker_client import cli, LabTracker

def fail(request):
    raise httpx.ConnectTimeout('TLS handshake timed out', request=request)
client = LabTracker(base_url='https://test.invalid', transport=httpx.MockTransport(fail))
cli.LabTracker.from_env = lambda: client
cli.main(sys.argv[1:])
"""
    result = subprocess.run(
        [sys.executable, "-c", program, *debug_args, "health"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LAB_TRACKER_DEBUG": debug_env},
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert ("Traceback (most recent call last)" in result.stderr) is traceback
    assert "TLS handshake timed out" in result.stderr
    if not traceback:
        assert result.stderr.startswith("error: ")
        assert len(result.stderr.splitlines()) == 1


@pytest.mark.parametrize("debug", [False, True])
def test_fail_silent_hook_remains_silent(debug, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise LTAPIError("API unavailable")

    monkeypatch.setattr(cli.setup_helpers, "setup_status", fail)
    cli.main((["--debug"] if debug else []) + ["setup", "status", "--fail-silent"])
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_unexpected_errors_are_not_masked(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("programming defect")

    monkeypatch.setattr(cli.setup_helpers, "setup_status", fail)
    with pytest.raises(RuntimeError, match="programming defect"):
        cli.main(["setup", "status"])


def test_invalid_cli_arguments_keep_exit_two(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["health", "--invalid-option"])
    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_success_keeps_json_output(monkeypatch, capsys):
    client = LabTracker(
        base_url="https://test.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "ok"})),
    )
    monkeypatch.setattr(cli.LabTracker, "from_env", lambda: client)
    cli.main(["health"])
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "ok"}
    assert captured.err == ""
    assert client._client.is_closed
