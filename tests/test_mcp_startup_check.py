"""The lt-mcp smoke check runs in a child interpreter, so it is bounded and crash-proof.

``lt doctor`` and the SessionStart hook's ``lt setup status --brief --fail-silent``
run this check. It imports the server module, and an import can hang, call
``sys.exit``, or crash the interpreter; none of that may hang or kill ``lt``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

from lab_tracker.cli import init_consumer_repo
from lab_tracker_client import cli as lt_cli
from lab_tracker_client import setup as setup_helpers

FAKE_MODULE = "fake_lt_mcp_server"
# Short enough to keep a hang test fast; only the hang tests wait for it.
HANG_TIMEOUT_SECONDS = 0.5
# A hung import must give up near its timeout, never wait for the module.
HANG_ELAPSED_LIMIT_SECONDS = 20.0


def _use_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> Path:
    """Make ``FAKE_MODULE`` the server module, importable by a child interpreter.

    A child interpreter never sees this process's ``sys.path`` edits, so the
    module is exposed the way a real environment exposes packages: PYTHONPATH.
    """

    site = tmp_path / "fake-site"
    site.mkdir()
    (site / f"{FAKE_MODULE}.py").write_text(source, encoding="utf-8")
    python_path = [str(site), *filter(None, [os.environ.get("PYTHONPATH")])]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(python_path))
    monkeypatch.setattr(setup_helpers, "_MCP_SERVER_MODULE", FAKE_MODULE)
    return site


def _isolate_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep ``setup_status`` off the real machine profile, skill homes, and network."""

    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-home"))
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", str(tmp_path / "skills-home"))
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(setup_helpers, "probe_health_diagnostics", lambda _url: {"reachable": True})


def test_an_importable_server_module_is_reported_importable(monkeypatch, tmp_path) -> None:
    _use_module(monkeypatch, tmp_path, "VALUE = 1\n")

    payload = setup_helpers.mcp_startup_check()

    assert payload == {"module": FAKE_MODULE, "python": sys.executable, "importable": True}


def test_the_real_server_module_is_importable_from_a_child_interpreter() -> None:
    payload = setup_helpers.mcp_startup_check()

    assert payload["importable"] is True, payload
    assert payload["module"] == "lab_tracker.mcp_server"
    assert "error" not in payload


def test_an_import_error_is_reported_with_its_traceback_and_next_step(
    monkeypatch, tmp_path
) -> None:
    _use_module(monkeypatch, tmp_path, "from mcp.server.fastmcp_removed_upstream import FastMCP\n")

    payload = setup_helpers.mcp_startup_check()

    assert payload["importable"] is False
    assert payload["error"].startswith("ModuleNotFoundError:")
    assert "mcp.server.fastmcp_removed_upstream" in payload["error"]
    assert f"{FAKE_MODULE}.py" in payload["traceback"]
    assert "Agents page" in payload["next_step"]
    assert payload["module"] == FAKE_MODULE
    assert payload["python"] == sys.executable


def test_a_multi_line_import_error_stays_one_line(monkeypatch, tmp_path) -> None:
    _use_module(
        monkeypatch,
        tmp_path,
        "raise ImportError('C-extensions failed.\\n\\nRebuild them.\\n  See the docs.')\n",
    )

    payload = setup_helpers.mcp_startup_check()

    assert payload["error"] == "ImportError: C-extensions failed. Rebuild them. See the docs."


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("raise SystemExit('refusing to start')\n", "SystemExit: refusing to start"),
        ("import sys\nsys.exit(0)\n", "SystemExit: 0"),
        ("raise KeyboardInterrupt\n", "KeyboardInterrupt:"),
    ],
)
def test_an_exit_during_import_is_a_failure_not_an_exit_of_lt(
    monkeypatch, tmp_path, source: str, error: str
) -> None:
    # In-process, SystemExit escaped the check and ended lt with the module's code.
    _use_module(monkeypatch, tmp_path, source)

    payload = setup_helpers.mcp_startup_check()

    assert payload["importable"] is False
    assert payload["error"] == error
    assert "Traceback" in payload["traceback"]


def test_a_hung_import_times_out_as_not_importable(monkeypatch, tmp_path) -> None:
    pid_file = tmp_path / "child.pid"
    _use_module(
        monkeypatch,
        tmp_path,
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "print('loading the heavy dependency', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n",
    )
    monkeypatch.setattr(setup_helpers, "_MCP_IMPORT_TIMEOUT_SECONDS", HANG_TIMEOUT_SECONDS)

    started = time.monotonic()
    payload = setup_helpers.mcp_startup_check()
    elapsed = time.monotonic() - started

    assert elapsed < HANG_ELAPSED_LIMIT_SECONDS
    assert payload["importable"] is False
    assert payload["error"] == (
        f"TimeoutError: importing {FAKE_MODULE} did not finish within "
        f"{HANG_TIMEOUT_SECONDS:g} seconds"
    )
    # The last output before the hang shows where it stopped.
    assert "loading the heavy dependency" in payload["traceback"]
    assert "did not finish importing" in payload["next_step"]
    assert "Agents page" in payload["next_step"]
    if sys.platform != "win32":
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text(encoding="utf-8")), 0)


def test_the_timeout_is_a_named_short_bound() -> None:
    assert 0 < setup_helpers._MCP_IMPORT_TIMEOUT_SECONDS <= 30


def test_an_exit_status_from_the_import_process_is_reported(monkeypatch, tmp_path) -> None:
    _use_module(monkeypatch, tmp_path, "import os\nos._exit(7)\n")

    payload = setup_helpers.mcp_startup_check()

    assert payload["importable"] is False
    assert payload["error"] == f"the interpreter importing {FAKE_MODULE} exited with status 7"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX reports a signal, not a status")
def test_a_hard_crash_during_import_is_reported_as_the_signal(monkeypatch, tmp_path) -> None:
    _use_module(monkeypatch, tmp_path, "import os\nos.abort()\n")

    payload = setup_helpers.mcp_startup_check()

    assert payload["importable"] is False
    assert payload["error"] == (
        f"the interpreter importing {FAKE_MODULE} was killed by SIGABRT"
    )
    assert "Agents page" in payload["next_step"]


def test_an_interpreter_that_cannot_start_is_reported(monkeypatch, tmp_path) -> None:
    _use_module(monkeypatch, tmp_path, "VALUE = 1\n")
    missing = tmp_path / "no-such-python"
    monkeypatch.setattr(sys, "executable", str(missing))

    payload = setup_helpers.mcp_startup_check()

    assert payload["importable"] is False
    assert "Error:" in payload["error"]
    assert payload["python"] == str(missing)


def test_the_working_directory_cannot_shadow_the_server_module(monkeypatch, tmp_path) -> None:
    # ``python -c`` would put the cwd first on sys.path; a console script does not.
    site = _use_module(monkeypatch, tmp_path, "VALUE = 1\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / f"{FAKE_MODULE}.py").write_text("raise RuntimeError('shadowed')\n", encoding="utf-8")
    monkeypatch.chdir(cwd)

    assert setup_helpers.mcp_startup_check()["importable"] is True

    # And the cwd is not searched at all: with the installed copy gone, the
    # cwd copy is not picked up either.
    (site / f"{FAKE_MODULE}.py").unlink()
    payload = setup_helpers.mcp_startup_check()
    assert payload["importable"] is False
    assert payload["error"].startswith("ModuleNotFoundError:"), payload
    assert "shadowed" not in payload["traceback"]


def test_the_check_leaves_this_process_alone(monkeypatch, tmp_path) -> None:
    # FastMCP's constructor calls logging.basicConfig at import time; in this
    # process it would leak every later INFO log (alembic, httpx) onto lt's stderr.
    _use_module(
        monkeypatch,
        tmp_path,
        "import logging\nlogging.basicConfig(level=logging.DEBUG, force=True)\n",
    )
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level

    assert setup_helpers.mcp_startup_check()["importable"] is True

    assert root.handlers == handlers
    assert root.level == level
    assert FAKE_MODULE not in sys.modules


def test_import_output_stays_out_of_the_lt_json_output(
    monkeypatch, tmp_path, capsys
) -> None:
    _use_module(
        monkeypatch,
        tmp_path,
        "import sys\nprint('noise on stdout')\nprint('noise on stderr', file=sys.stderr)\n",
    )
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)
    capsys.readouterr()

    lt_cli.main(["doctor", "--target", str(repo)])
    captured = capsys.readouterr()

    assert json.loads(captured.out)["lt_mcp"]["importable"] is True
    assert "noise" not in captured.out
    assert "noise" not in captured.err


def test_the_traceback_tail_is_bounded_and_redacted(monkeypatch, tmp_path) -> None:
    _use_module(
        monkeypatch,
        tmp_path,
        "import sys\n"
        "print('early ' + 'x' * 20000, file=sys.stderr)\n"
        "raise RuntimeError('proxy http://alice:hunter2@proxy.example:3128 refused')\n",
    )

    payload = setup_helpers.mcp_startup_check()

    assert len(payload["traceback"]) <= setup_helpers._MCP_IMPORT_TRACEBACK_LIMIT
    assert "RuntimeError: proxy" in payload["traceback"]
    assert "hunter2" not in payload["traceback"]
    assert "hunter2" not in payload["error"]
    assert "early" not in payload["traceback"]


def test_doctor_and_status_survive_a_hung_import(monkeypatch, tmp_path, capsys) -> None:
    _use_module(monkeypatch, tmp_path, "import time\ntime.sleep(30)\n")
    monkeypatch.setattr(setup_helpers, "_MCP_IMPORT_TIMEOUT_SECONDS", HANG_TIMEOUT_SECONDS)
    _isolate_status(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)
    capsys.readouterr()

    with pytest.raises(SystemExit) as excinfo:
        lt_cli.main(["doctor", "--target", str(repo)])
    doctor = json.loads(capsys.readouterr().out)
    assert excinfo.value.code == 1
    assert doctor["lt_mcp"]["importable"] is False
    assert doctor["lt_mcp"]["error"].startswith("TimeoutError:")

    lt_cli.main(["setup", "status", "--target", str(repo), "--brief", "--fail-silent"])
    brief = json.loads(capsys.readouterr().out)
    assert brief["suggestions"][0].startswith("lt-mcp cannot start in this environment")
    assert "TimeoutError" in brief["brief"]


def test_brief_line_keeps_the_error_to_one_short_line(monkeypatch, tmp_path) -> None:
    long_error = "ImportError: first line\n  second line\n" + "x" * 2000
    monkeypatch.setattr(
        setup_helpers,
        "mcp_startup_check",
        lambda: {"importable": False, "error": long_error, "module": FAKE_MODULE},
    )
    _isolate_status(monkeypatch, tmp_path)

    brief = setup_helpers.setup_status(tmp_path, brief=True)
    full = setup_helpers.setup_status(tmp_path)

    suggestion = full["suggestions"][0]
    assert "\n" not in brief["brief"]
    assert "\n" not in suggestion
    assert "ImportError: first line second line" in suggestion
    assert "x" * 2000 not in suggestion
    # The fixed wording around the error is a couple of hundred characters.
    assert len(suggestion) <= setup_helpers._BRIEF_ERROR_LIMIT + 300
    # The full payload keeps the error as the check reported it.
    assert full["lt_mcp"]["error"] == long_error


def test_brief_line_for_a_short_one_line_error_is_unchanged(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        setup_helpers,
        "mcp_startup_check",
        lambda: {"importable": False, "error": "ModuleNotFoundError: No module named 'x'"},
    )
    _isolate_status(monkeypatch, tmp_path)

    suggestion = setup_helpers.setup_status(tmp_path)["suggestions"][0]

    assert suggestion.startswith(
        "lt-mcp cannot start in this environment "
        "(ModuleNotFoundError: No module named 'x'); the install command"
    )
