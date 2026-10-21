"""Cloud agent sessions: detection, unreachable addresses, and ``lt setup status``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lab_tracker.cli import _agents_fragment, _doctor, init_consumer_repo
from lab_tracker.client_release import client_install_command
from lab_tracker.decision_context_constants import (
    CLOUD_SESSION_POLICY,
    MCP_SERVER_INSTRUCTIONS,
    managed_agent_activation_block,
)
from lab_tracker.mcp_api_client import _NO_CREDENTIALS_MESSAGE, MCPSettings
from lab_tracker.mcp_tools.resources import lab_tracker_quickstart
from lab_tracker.setup_guide import setup_guide_markdown, setup_skill_markdown
from lab_tracker_client import figure
from lab_tracker_client import setup as setup_helpers
from lab_tracker_client.client import LabTracker, LTAPIError, connection_profile_path
from lab_tracker_client.cloud_session import (
    CLAUDE_CODE_REMOTE_ENV,
    CLOUD_AGENTS_DOC,
    CLOUD_SESSION_ENV,
    LOOPBACK,
    PRIVATE_NETWORK,
    cloud_session,
    unreachable_from_cloud,
)

_PUBLIC_URL = "https://lab.example.org"
_SERVER_REVISION = "a" * 40
# Phrases from suggestions that only make sense on a person's own computer.
_COMPUTER_ONLY_PHRASES = (
    "lab-tracker serve",
    "lt setup connect",
    "lt watch add",
    "lt hooks install",
    "lt setup agent-hooks",
)


_REMOTE = CLAUDE_CODE_REMOTE_ENV
_DECLARED = CLOUD_SESSION_ENV


@pytest.mark.parametrize(
    ("environ", "detected", "signal", "host"),
    [
        ({}, False, None, None),
        ({_REMOTE: "true"}, True, _REMOTE, "claude-code"),
        ({_REMOTE: "TRUE "}, True, _REMOTE, "claude-code"),
        ({_REMOTE: "false"}, False, None, None),
        ({_REMOTE: "1"}, False, None, None),
        ({_DECLARED: "1"}, True, _DECLARED, None),
        ({_DECLARED: "codex"}, True, _DECLARED, "codex"),
        ({_DECLARED: "Codex "}, True, _DECLARED, "codex"),
        # Codex's harness marks the commands its agent runs; OpenAI documents no
        # cloud marker, so these name the agent only once a cloud session is declared.
        ({_DECLARED: "1", "CODEX_THREAD_ID": "t-1"}, True, _DECLARED, "codex"),
        ({_DECLARED: "1", "CODEX_CI": "1"}, True, _DECLARED, "codex"),
        ({"CODEX_THREAD_ID": "t-1", "CODEX_CI": "1"}, False, None, None),
        ({_DECLARED: "1", _REMOTE: "true"}, True, _DECLARED, "claude-code"),
        ({_DECLARED: "off", _REMOTE: "true"}, False, _DECLARED, None),
        ({_DECLARED: "  ", _REMOTE: "true"}, True, _REMOTE, "claude-code"),
    ],
)
def test_cloud_session_detection(
    environ: dict[str, str], detected: bool, signal: str | None, host: str | None
) -> None:
    assert cloud_session(environ) == {"detected": detected, "signal": signal, "host": host}


@pytest.mark.parametrize(
    ("base_url", "reason"),
    [
        ("http://127.0.0.1:8000", LOOPBACK),
        ("http://127.8.9.10", LOOPBACK),
        ("http://localhost:8000", LOOPBACK),
        ("http://lab.localhost", LOOPBACK),
        ("http://[::1]:8000", LOOPBACK),
        ("http://10.0.0.5:8000", PRIVATE_NETWORK),
        ("http://192.168.1.20", PRIVATE_NETWORK),
        ("http://172.16.4.2", PRIVATE_NETWORK),
        # Tailscale's address range is shared address space, not public.
        ("http://100.101.102.103:8000", PRIVATE_NETWORK),
        ("http://[fd7a:115c:a1e0::1]", PRIVATE_NETWORK),
        ("http://169.254.1.1", PRIVATE_NETWORK),
        ("http://labserver:8000", PRIVATE_NETWORK),
        ("http://labserver.local", PRIVATE_NETWORK),
        ("http://0.0.0.0:8000", LOOPBACK),
        ("http://[::]:8000", LOOPBACK),
        ("http://box.internal", PRIVATE_NETWORK),
        ("http://lab.home.arpa", PRIVATE_NETWORK),
        ("http://router.lan", PRIVATE_NETWORK),
        ("http://box.localdomain:8000", PRIVATE_NETWORK),
        (_PUBLIC_URL, None),
        ("https://lab.example.org.", None),
        # No host to judge: left to the probe rather than called private.
        ("", None),
        ("lab.example.org", None),
        ("${LAB_TRACKER_BASE_URL}", None),
        ("http://[::1", None),
        ("https://lab.tail1234.ts.net", None),
        ("https://8.8.8.8", None),
    ],
)
def test_unreachable_from_cloud_judges_only_the_address(base_url: str, reason: str | None) -> None:
    assert unreachable_from_cloud(base_url) == reason


@pytest.fixture
def cloud_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A cloned repository inside a Claude Code cloud VM, with no network I/O."""

    for name in (
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "LAB_TRACKER_MCP_API_KEY",
        "LAB_TRACKER_MCP_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(CLAUDE_CODE_REMOTE_ENV, "true")
    monkeypatch.setattr(
        setup_helpers,
        "mcp_startup_check",
        lambda: {"module": "lab_tracker.mcp_server", "importable": True},
    )
    repo = tmp_path / "analysis"
    repo.mkdir()
    return repo


def _status(repo: Path, *, brief: bool = False) -> dict[str, object]:
    """``setup_status``, checking that a cloud report offers nothing computer-only."""

    payload = setup_helpers.setup_status(repo, brief=brief)
    cloud = (
        str(payload["brief"]).startswith("lab-tracker (cloud session)")
        if brief
        else payload["cloud_session"]["detected"]
    )
    if cloud:
        for item in payload["suggestions"]:
            for phrase in _COMPUTER_ONLY_PHRASES:
                assert phrase not in item, (phrase, item)
    return payload


def _probe(monkeypatch: pytest.MonkeyPatch, result: dict[str, object]) -> list[str]:
    probed: list[str] = []

    def fake_probe(url: str) -> dict[str, object]:
        probed.append(url)
        return dict(result)

    monkeypatch.setattr(setup_helpers, "probe_health_diagnostics", fake_probe)
    return probed


def _online(monkeypatch: pytest.MonkeyPatch, *, version: str = "0.0.1") -> None:
    _probe(
        monkeypatch,
        {"reachable": True, "release": {"version": version, "revision": _SERVER_REVISION}},
    )


def _offline(monkeypatch: pytest.MonkeyPatch, diagnosis: str = "tcp_connection_failed") -> None:
    _probe(monkeypatch, {"reachable": False, "diagnosis": diagnosis})


def _write_mcp_json(repo: Path, *, command: str, base_url: str) -> None:
    entry = {"command": command, "env": {"LAB_TRACKER_BASE_URL": base_url}}
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lab-tracker": entry}}), encoding="utf-8"
    )


def _bound_scaffold(repo: Path, base_url: str) -> None:
    """A fully scaffolded, bound checkout whose ``.mcp.json`` names ``base_url``."""

    init_consumer_repo(repo, mcp_base_url=base_url, yes=True, install_skills=True)
    (repo / "lt_ids.json").write_text(
        json.dumps({"project_id": "p-1", "project_name": "demo"}), encoding="utf-8"
    )


def test_off_a_cloud_session_status_reports_only_the_detection(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CLAUDE_CODE_REMOTE_ENV)
    _offline(monkeypatch)

    payload = _status(cloud_repo)

    assert payload["cloud_session"] == {"detected": False, "signal": None, "host": None}
    assert any("lab-tracker serve" in item for item in payload["suggestions"])


def test_cloud_status_names_the_unreachable_default_address_first(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _offline(monkeypatch)

    payload = _status(cloud_repo)

    cloud = payload["cloud_session"]
    assert cloud["detected"] is True
    assert cloud["signal"] == CLAUDE_CODE_REMOTE_ENV
    assert cloud["base_url_unreachable"] == LOOPBACK
    assert cloud["mcp_json"] is None
    assert cloud["token_in_vm"] == {"lt": False, "lt_mcp": False}
    first = payload["suggestions"][0]
    assert "http://127.0.0.1:8000" in first
    assert "nothing in this VM sets one" in first
    assert "the VM itself" in first
    assert "LAB_TRACKER_BASE_URL" in first
    assert "docs/cloud-agents.md" in first
    for item in payload["suggestions"]:
        for phrase in _COMPUTER_ONLY_PHRASES:
            assert phrase not in item, (phrase, item)
    # Repository files still carry over to every session, so they stay suggested.
    assert any("lt setup init" in item for item in payload["suggestions"])
    assert any("lt project bind" in item for item in payload["suggestions"])


def test_cloud_status_flags_a_private_address_from_the_environment(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://100.101.102.103:8000")
    _offline(monkeypatch)

    suggestions = _status(cloud_repo)["suggestions"]

    address = [item for item in suggestions if "100.101.102.103" in item]
    assert len(address) == 1
    assert "from LAB_TRACKER_BASE_URL" in address[0]
    assert "Anthropic-hosted Claude Code environment is on no lab network" in address[0]


def test_cloud_status_explains_a_public_server_that_does_not_answer(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _offline(monkeypatch, diagnosis="proxy_connection_failed")

    suggestions = _status(cloud_repo)["suggestions"]

    answer = [item for item in suggestions if _PUBLIC_URL in item]
    assert len(answer) == 1
    assert "proxy_connection_failed" in answer[0]
    assert "lab.example.org" in answer[0]
    assert "network secret" in answer[0]
    assert "allowed domains" in answer[0]


def test_cloud_status_reports_the_mcp_json_launch_this_vm_cannot_run(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)
    missing = cloud_repo / "missing-bin" / "lt-mcp"
    _write_mcp_json(cloud_repo, command=str(missing), base_url="http://127.0.0.1:8000")

    payload = _status(cloud_repo)

    assert payload["cloud_session"]["mcp_json"] == {
        "command": str(missing),
        "command_found": False,
        "env": {"LAB_TRACKER_BASE_URL": "http://127.0.0.1:8000"},
        "base_url": "http://127.0.0.1:8000",
        "base_url_key": "LAB_TRACKER_BASE_URL",
        "base_url_from_mcp_json": True,
        "base_url_unreachable": LOOPBACK,
    }
    install, address = payload["suggestions"][:2]
    assert "is not installed in this VM" in install
    # The server reported its full revision, so the setup-script line is exact.
    assert f"`{client_install_command(_SERVER_REVISION)}`" in install
    assert "setup script" in install
    assert "LAB_TRACKER_BASE_URL=http://127.0.0.1:8000" in address
    assert "overrides the cloud environment's variable" in address
    assert "`lt update --dry-run`" in address
    assert "`lt update` applies it" in address
    assert "step 5" in address


def test_cloud_status_points_to_the_setup_page_without_a_server_revision(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _probe(monkeypatch, {"reachable": True})
    missing = cloud_repo / "nowhere" / "lt-mcp"
    _write_mcp_json(cloud_repo, command=str(missing), base_url=_PUBLIC_URL)

    install = _status(cloud_repo)["suggestions"][0]

    assert "web app's Setup page" in install
    assert "uv tool install --force" not in install


def test_cloud_status_moves_a_client_update_into_the_setup_script(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch, version="999.0.0")

    suggestions = _status(cloud_repo)["suggestions"]

    behind = [item for item in suggestions if "behind its server" in item]
    assert len(behind) == 1
    assert f"`{client_install_command(_SERVER_REVISION)}`" in behind[0]
    assert "setup script" in behind[0]
    assert "lt maintain --all" not in behind[0]


def test_cloud_status_puts_skills_in_the_setup_script(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)

    suggestions = _status(cloud_repo)["suggestions"]

    skills = [item for item in suggestions if "skills" in item]
    assert len(skills) == 1
    assert "`lt update --skills-only` in the cloud environment's setup script" in skills[0]


@pytest.mark.parametrize(
    ("tokens", "note"),
    [
        ({}, "no Lab Tracker token is in this VM"),
        (
            {"LAB_TRACKER_ACCESS_TOKEN": "lpat_x", "LAB_TRACKER_MCP_API_KEY": "lpat_x"},
            "a Lab Tracker token is in this VM for both `lt` and `lt-mcp`",
        ),
        ({"LAB_TRACKER_ACCESS_TOKEN": "lpat_x"}, "LAB_TRACKER_MCP_API_KEY is unset"),
        ({"LAB_TRACKER_MCP_TOKEN": "lpat_x"}, "LAB_TRACKER_ACCESS_TOKEN is unset"),
    ],
)
def test_a_healthy_cloud_brief_line_says_where_the_token_comes_from(
    cloud_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tokens: dict[str, str],
    note: str,
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    for name, value in tokens.items():
        monkeypatch.setenv(name, value)
    _online(monkeypatch)
    _bound_scaffold(cloud_repo, _PUBLIC_URL)
    _write_mcp_json(cloud_repo, command=str(Path(__file__)), base_url=_PUBLIC_URL)

    brief = _status(cloud_repo, brief=True)

    assert brief["suggestions"] == []
    assert brief["brief"].startswith("lab-tracker (cloud session): server reachable; ")
    assert note in brief["brief"]


def test_a_cloud_brief_line_with_suggestions_leads_with_the_first(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _offline(monkeypatch)

    brief = _status(cloud_repo, brief=True)

    assert brief["brief"].startswith("lab-tracker (cloud session): ")
    assert brief["brief"].endswith(brief["suggestions"][0])


def test_an_explicit_off_switch_restores_the_computer_report(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CLOUD_SESSION_ENV, "0")
    _offline(monkeypatch)

    payload = _status(cloud_repo, brief=True)

    assert payload["brief"].startswith("lab-tracker: ")
    assert any("lab-tracker serve" in item for item in payload["suggestions"])


def test_cloud_session_policy_reaches_every_agent_instruction_surface() -> None:
    for text in (MCP_SERVER_INSTRUCTIONS, managed_agent_activation_block(), _agents_fragment()):
        assert CLOUD_SESSION_POLICY in " ".join(text.split())
    for phrase in (
        "CLAUDE_CODE_REMOTE=true",
        "do not start `lab-tracker serve` in the VM",
        "never ask for a token in the chat",
        "personal agent hooks",
        "refuses a request for lack of a token",
        "belongs in the environment's setup or install script rather than `lt maintain`",
        "a Codex cloud task",
        "`lt prime` lists the project's open questions",
        "`lt outbox sync`, then `lt outbox status`",
        "`docs/cloud-agents.md`",
    ):
        assert phrase in CLOUD_SESSION_POLICY, phrase


def test_setup_guide_and_skill_describe_cloud_sessions() -> None:
    guide = setup_guide_markdown()
    assert "## Cloud sessions\n" in guide
    section = " ".join(guide.split("## Cloud sessions\n", 1)[1].split("\n## ", 1)[0].split())
    for phrase in (
        "`CLAUDE_CODE_REMOTE=true`",
        "`LAB_TRACKER_CLOUD_SESSION`",
        "`LAB_TRACKER_ACCESS_TOKEN` and `LAB_TRACKER_MCP_API_KEY`",
        "network secret",
        "`lt update --skills-only`",
        "`lt outbox sync`",
        "`docs/cloud-agents.md`",
    ):
        assert phrase in section, phrase
    assert "## Cloud sessions\n" in setup_skill_markdown()


def test_missing_mcp_credentials_error_keeps_a_cloud_token_out_of_the_chat() -> None:
    assert "network secret" in _NO_CREDENTIALS_MESSAGE
    assert "never in the chat" in _NO_CREDENTIALS_MESSAGE
    assert "host must match this server's host exactly" in _NO_CREDENTIALS_MESSAGE
    assert CLOUD_AGENTS_DOC in _NO_CREDENTIALS_MESSAGE


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps({"mcpServers": {"lab-tracker": "lt-mcp"}}), json.dumps([1])],
)
def test_an_unreadable_mcp_json_entry_is_reported_as_absent(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)
    (cloud_repo / ".mcp.json").write_text(content, encoding="utf-8")

    payload = _status(cloud_repo)

    assert payload["cloud_session"]["mcp_json"] is None
    assert not any(".mcp.json` starts" in item for item in payload["suggestions"])



def _write_entry(repo: Path, entry: dict[str, object]) -> None:
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lab-tracker": entry}}), encoding="utf-8"
    )


def _save_profile(base_url: str, token: str = "lpat_saved") -> None:
    path = connection_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base_url": base_url, "access_token": token}), encoding="utf-8")


def _mcp_json_suggestions(suggestions: list[str]) -> list[str]:
    return [item for item in suggestions if "`.mcp.json` starts" in item]


def test_mcp_json_variables_expand_as_claude_code_expands_them(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    bin_dir = cloud_repo / "bin"
    bin_dir.mkdir()
    (bin_dir / "lt-mcp").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("LT_BIN", str(bin_dir))
    _online(monkeypatch)
    _write_entry(
        cloud_repo,
        {"command": "${LT_BIN}/lt-mcp", "env": {"LAB_TRACKER_BASE_URL": "${LAB_TRACKER_BASE_URL}"}},
    )

    payload = _status(cloud_repo)

    launch = payload["cloud_session"]["mcp_json"]
    assert launch["command"] == f"{bin_dir}/lt-mcp"
    assert launch["command_found"] is True
    assert launch["base_url"] == _PUBLIC_URL
    assert launch["base_url_unreachable"] is None
    assert _mcp_json_suggestions(payload["suggestions"]) == []


def test_an_unset_mcp_json_variable_takes_its_default(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _offline(monkeypatch)
    _write_entry(
        cloud_repo,
        {
            "command": str(Path(__file__)),
            "env": {"LAB_TRACKER_BASE_URL": "${LAB_TRACKER_BASE_URL:-http://127.0.0.1:8000}"},
        },
    )

    payload = _status(cloud_repo)

    assert payload["cloud_session"]["mcp_json"]["base_url"] == "http://127.0.0.1:8000"
    assert len(_mcp_json_suggestions(payload["suggestions"])) == 1


def test_a_legacy_mcp_json_address_yields_to_the_environment(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # lt-mcp resolves LAB_TRACKER_BASE_URL before the legacy name, and Claude Code
    # merges the entry's env over the session's, so the session's address wins.
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)
    _write_entry(
        cloud_repo,
        {
            "command": str(Path(__file__)),
            "env": {"LAB_TRACKER_MCP_BASE_URL": "http://127.0.0.1:8000"},
        },
    )

    payload = _status(cloud_repo)

    launch = payload["cloud_session"]["mcp_json"]
    assert launch["base_url"] == _PUBLIC_URL
    assert launch["base_url_from_mcp_json"] is False
    assert _mcp_json_suggestions(payload["suggestions"]) == []


def test_a_legacy_mcp_json_address_alone_needs_only_the_environment_variable(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Once the environment sets LAB_TRACKER_BASE_URL, lt-mcp reads it before the
    # legacy key, so the entry needs no rewrite: the address suggestion suffices.
    _offline(monkeypatch)
    _write_entry(
        cloud_repo,
        {
            "command": str(Path(__file__)),
            "env": {"LAB_TRACKER_MCP_BASE_URL": "http://127.0.0.1:8000"},
        },
    )

    payload = _status(cloud_repo)

    assert payload["cloud_session"]["mcp_json"]["base_url_key"] == "LAB_TRACKER_MCP_BASE_URL"
    assert _mcp_json_suggestions(payload["suggestions"]) == []
    assert any("LAB_TRACKER_BASE_URL" in item for item in payload["suggestions"])


def test_a_mcp_json_passthrough_of_the_environment_is_not_an_override(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")
    _offline(monkeypatch)
    passthrough = {"LAB_TRACKER_BASE_URL": "${LAB_TRACKER_BASE_URL}"}
    _write_entry(cloud_repo, {"command": str(Path(__file__)), "env": passthrough})

    payload = _status(cloud_repo)

    assert payload["cloud_session"]["mcp_json"]["base_url_from_mcp_json"] is False
    assert _mcp_json_suggestions(payload["suggestions"]) == []
    assert any("from LAB_TRACKER_BASE_URL" in item for item in payload["suggestions"])


def test_an_http_mcp_json_entry_needs_no_local_executable(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)
    _write_entry(cloud_repo, {"type": "http", "url": "https://lab.example.org/mcp"})

    payload = _status(cloud_repo)

    assert payload["cloud_session"]["mcp_json"]["command_found"] is None
    assert not any("is not installed in this VM" in item for item in payload["suggestions"])


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX executable bit stands in for PATHEXT")
def test_lt_mcp_only_in_the_uv_run_environment_counts_as_missing(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `uv run lt` puts the project's .venv/bin first on PATH; Claude Code starts the
    # .mcp.json server with the session's PATH, which does not have it.
    venv = cloud_repo / ".venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    executable = bin_dir / "lt-mcp-cloud-test"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path(sys.executable).parent}")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)
    _write_mcp_json(cloud_repo, command=executable.name, base_url=_PUBLIC_URL)

    payload = _status(cloud_repo)

    assert payload["cloud_session"]["mcp_json"]["command_found"] is False
    assert any("is not installed in this VM" in item for item in payload["suggestions"])
    monkeypatch.delenv("VIRTUAL_ENV")
    assert _status(cloud_repo)["cloud_session"]["mcp_json"]["command_found"]


def test_malformed_mcp_json_values_do_not_break_the_report(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)
    _write_entry(
        cloud_repo,
        {"command": "~nosuchuser/bin/lt-mcp", "env": {"LAB_TRACKER_BASE_URL": "http://[::1"}},
    )

    brief = _status(cloud_repo, brief=True)

    assert brief["brief"].startswith("lab-tracker (cloud session): ")
    assert any("is not installed in this VM" in item for item in brief["suggestions"])


@pytest.mark.parametrize("cloud", [True, False])
def test_an_address_that_does_not_parse_is_a_suggestion_not_a_crash(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch, cloud: bool
) -> None:
    if not cloud:
        monkeypatch.delenv(CLAUDE_CODE_REMOTE_ENV)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "lab.example.org")
    probed = _probe(monkeypatch, {"reachable": True})

    payload = _status(cloud_repo)

    assert probed == []
    assert payload["server"]["diagnosis"] == "server_check_failed"
    address = [item for item in payload["suggestions"] if "absolute http://" in item]
    assert len(address) == 1
    assert ("cloud environment's settings" in address[0]) is cloud


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"LAB_TRACKER_BASE_URL": "http://127.0.0.1:8000"},
        {"LAB_TRACKER_BASE_URL": _PUBLIC_URL},
        {"LAB_TRACKER_MCP_BASE_URL": _PUBLIC_URL},
        {"LAB_TRACKER_USERNAME": "ada"},
        {"LAB_TRACKER_MCP_USERNAME": "ada"},
        {"LAB_TRACKER_MCP_TOKEN": "lpat_env"},
    ],
)
def test_token_in_vm_follows_each_clients_profile_token_rules(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    _save_profile("http://127.0.0.1:8000")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _offline(monkeypatch)

    tokens = _status(cloud_repo)["cloud_session"]["token_in_vm"]

    lt_client = LabTracker.from_env()
    try:
        lt_has_token = bool(lt_client.access_token)
    finally:
        lt_client.close()
    assert tokens == {"lt": lt_has_token, "lt_mcp": bool(MCPSettings.from_env().api_key)}


def test_a_profile_token_for_another_address_is_not_counted(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_profile("http://127.0.0.1:8000")
    _offline(monkeypatch)
    assert _status(cloud_repo)["cloud_session"]["token_in_vm"] == {
        "lt": True,
        "lt_mcp": True,
    }

    # The committed .mcp.json names the public address, so lt-mcp drops the token.
    _write_mcp_json(cloud_repo, command=str(Path(__file__)), base_url=_PUBLIC_URL)
    assert _status(cloud_repo)["cloud_session"]["token_in_vm"] == {
        "lt": True,
        "lt_mcp": False,
    }
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    assert _status(cloud_repo)["cloud_session"]["token_in_vm"] == {
        "lt": False,
        "lt_mcp": False,
    }


def test_doctor_offers_no_personal_agent_hooks_in_a_cloud_vm(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _doctor(cloud_repo)["suggestions"] == []

    monkeypatch.delenv(CLAUDE_CODE_REMOTE_ENV)
    assert any("lt setup agent-hooks" in item for item in _doctor(cloud_repo)["suggestions"])


def test_figure_capture_tries_the_upload_without_a_local_token_in_a_cloud_vm(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A network secret adds the token outside the VM, so no local token is no reason to skip.
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)

    client, project_id, close_client = figure._resolve_capture_client(client=None, project_id="p-1")
    try:
        assert client is not None
        assert (project_id, close_client) == ("p-1", True)
    finally:
        if client is not None:
            client.close()

    monkeypatch.delenv(CLAUDE_CODE_REMOTE_ENV)
    assert figure._resolve_capture_client(client=None, project_id="p-1") == (None, None, False)


def test_quickstart_resource_keeps_a_cloud_agent_off_a_local_server() -> None:
    text = " ".join(lab_tracker_quickstart().split())
    assert "In a cloud session (`CLAUDE_CODE_REMOTE=true`), do not start the app in the VM" in text
    assert CLOUD_AGENTS_DOC in text
    assert "only through Tailscale Funnel" in text


def test_lt_without_a_token_names_an_access_token_and_keeps_it_out_of_the_chat() -> None:
    client = LabTracker(base_url=_PUBLIC_URL)
    try:
        with pytest.raises(LTAPIError) as excinfo:
            client._bearer_token(required=True)
    finally:
        client.close()
    message = str(excinfo.value)
    assert "LAB_TRACKER_ACCESS_TOKEN" in message
    assert "lt setup connect --save-token" in " ".join(message.split())
    assert "never in the chat" in message
    assert CLOUD_AGENTS_DOC in message


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX executable bit stands in for PATHEXT")
def test_a_virtualenv_outside_the_checkout_is_the_session_s_own(
    cloud_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An image that sets VIRTUAL_ENV and PATH for the whole VM starts lt-mcp from it.
    venv = tmp_path / "opt" / "venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    executable = bin_dir / "lt-mcp-cloud-test"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path(sys.executable).parent}")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)
    _write_mcp_json(cloud_repo, command=executable.name, base_url=_PUBLIC_URL)

    payload = _status(cloud_repo)

    assert payload["cloud_session"]["mcp_json"]["command_found"] is True
    assert not any("is not installed in this VM" in item for item in payload["suggestions"])


def test_an_overlong_command_path_does_not_break_the_report(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)
    _write_mcp_json(cloud_repo, command=f"/{'x' * 300}/lt-mcp", base_url=_PUBLIC_URL)

    brief = _status(cloud_repo, brief=True)

    assert any("is not installed in this VM" in item for item in brief["suggestions"])


def test_a_refused_request_without_a_token_is_not_an_outage() -> None:
    # The no-token advice names `lt setup connect`; only an error the transport
    # wrapped (which carries a connection diagnostic) may count as offline.
    client = LabTracker(base_url=_PUBLIC_URL)
    try:
        with pytest.raises(LTAPIError) as excinfo:
            client._bearer_token(required=True)
    finally:
        client.close()
    assert "connect" in str(excinfo.value)
    assert figure._is_transport_failure(excinfo.value) is False

    offline = LTAPIError("Lab Tracker request GET /health failed: connection refused")
    diagnostic = {"diagnosis": "tcp_connection_failed"}
    offline.connection_diagnostic = diagnostic  # type: ignore[attr-defined]
    assert figure._is_transport_failure(offline) is True



@pytest.fixture
def codex_repo(cloud_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository inside a Codex cloud task, declared by its environment."""

    monkeypatch.delenv(CLAUDE_CODE_REMOTE_ENV)
    monkeypatch.setenv(CLOUD_SESSION_ENV, "codex")
    return cloud_repo


def test_a_codex_task_skips_the_claude_code_mcp_json_checks(
    codex_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex does not start servers from .mcp.json, so its launch is not judged.
    _offline(monkeypatch)
    missing = str(codex_repo / "nowhere" / "lt-mcp")
    _write_mcp_json(codex_repo, command=missing, base_url="http://127.0.0.1:8000")

    payload = _status(codex_repo)

    cloud = payload["cloud_session"]
    assert (cloud["host"], cloud["mcp_json"]) == ("codex", None)
    assert not any(".mcp.json" in item for item in payload["suggestions"])
    assert any("nothing in this VM sets one" in item for item in payload["suggestions"])


def test_a_codex_task_names_its_install_script(
    codex_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch, version="999.0.0")

    suggestions = _status(codex_repo)["suggestions"]

    behind = [item for item in suggestions if "behind its server" in item]
    skills = [item for item in suggestions if "skills" in item]
    assert len(behind) == len(skills) == 1
    for item in (behind[0], skills[0]):
        assert "install script (setup script in Codex Cloud (Legacy))" in item


def test_a_codex_task_can_reach_a_private_address_through_its_vpn(
    codex_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://100.101.102.103:8000")
    _online(monkeypatch)
    assert _status(codex_repo)["cloud_session"]["base_url_unreachable"] is None

    _offline(monkeypatch)
    payload = _status(codex_repo)

    assert payload["cloud_session"]["base_url_unreachable"] == PRIVATE_NETWORK
    address = [item for item in payload["suggestions"] if "100.101.102.103" in item]
    assert len(address) == 1
    assert "Tailscale VPN setting" in address[0]


def test_a_codex_task_explains_internet_access_that_is_off_by_default(
    codex_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "https://lab.tail1234.ts.net")
    _offline(monkeypatch, diagnosis="proxy_connection_failed")

    suggestions = _status(codex_repo)["suggestions"]

    answer = [item for item in suggestions if "No Lab Tracker API answered" in item]
    assert len(answer) == 1
    assert "internet access, which is off unless turned on" in answer[0]
    assert "saving a network secret for it" in answer[0]
    assert "through the Codex environment's Tailscale VPN" in answer[0]


def test_a_loopback_address_is_the_vm_itself_even_when_something_answers(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lab-tracker serve started in the VM answers, but its database goes with the VM.
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:8000")
    _online(monkeypatch)

    assert _status(cloud_repo)["cloud_session"]["base_url_unreachable"] == LOOPBACK


@pytest.mark.parametrize("cloud", [True, False])
def test_codex_without_network_is_named_before_any_address_advice(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch, cloud: bool
) -> None:
    if cloud:
        monkeypatch.delenv(CLAUDE_CODE_REMOTE_ENV)
        monkeypatch.setenv(CLOUD_SESSION_ENV, "codex")
    else:
        monkeypatch.delenv(CLAUDE_CODE_REMOTE_ENV)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    _offline(monkeypatch)

    suggestions = _status(cloud_repo)["suggestions"]

    assert suggestions[0].startswith("Codex runs this command with network access off")

    _online(monkeypatch)
    assert not any("network access off" in item for item in _status(cloud_repo)["suggestions"])


def test_a_codex_task_without_a_token_variable_is_told_where_one_goes(
    codex_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)
    _bound_scaffold(codex_repo, _PUBLIC_URL)

    suggestions = _status(codex_repo)["suggestions"]

    token = [item for item in suggestions if "No Lab Tracker token is in this VM" in item]
    assert len(token) == 1
    assert "network secret with the key LAB_TRACKER_ACCESS_TOKEN" in token[0]
    assert "never in the chat" in token[0]

    # A network secret's placeholder is enough: the proxy swaps in the real token.
    monkeypatch.setenv("LAB_TRACKER_ACCESS_TOKEN", "placeholder-from-codex")
    brief = _status(codex_repo, brief=True)
    assert brief["suggestions"] == []
    assert brief["brief"] == (
        "lab-tracker (cloud session): server reachable; "
        "a Lab Tracker token is available to `lt` in this VM."
    )
