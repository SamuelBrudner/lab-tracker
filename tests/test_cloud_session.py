"""Cloud agent sessions: detection, unreachable addresses, and ``lt setup status``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab_tracker.cli import _agents_fragment, init_consumer_repo
from lab_tracker.client_release import client_install_command
from lab_tracker.decision_context_constants import (
    CLOUD_SESSION_POLICY,
    MCP_SERVER_INSTRUCTIONS,
    managed_agent_activation_block,
)
from lab_tracker.mcp_api_client import _NO_CREDENTIALS_MESSAGE
from lab_tracker.setup_guide import setup_guide_markdown, setup_skill_markdown
from lab_tracker_client import setup as setup_helpers
from lab_tracker_client.cloud_session import (
    CLAUDE_CODE_REMOTE_ENV,
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


@pytest.mark.parametrize(
    ("environ", "detected", "signal"),
    [
        ({}, False, None),
        ({CLAUDE_CODE_REMOTE_ENV: "true"}, True, CLAUDE_CODE_REMOTE_ENV),
        ({CLAUDE_CODE_REMOTE_ENV: "TRUE "}, True, CLAUDE_CODE_REMOTE_ENV),
        ({CLAUDE_CODE_REMOTE_ENV: "false"}, False, None),
        ({CLAUDE_CODE_REMOTE_ENV: "1"}, False, None),
        ({CLOUD_SESSION_ENV: "1"}, True, CLOUD_SESSION_ENV),
        ({CLOUD_SESSION_ENV: "codex"}, True, CLOUD_SESSION_ENV),
        ({CLOUD_SESSION_ENV: "off", CLAUDE_CODE_REMOTE_ENV: "true"}, False, CLOUD_SESSION_ENV),
        ({CLOUD_SESSION_ENV: "  ", CLAUDE_CODE_REMOTE_ENV: "true"}, True, CLAUDE_CODE_REMOTE_ENV),
    ],
)
def test_cloud_session_detection(
    environ: dict[str, str], detected: bool, signal: str | None
) -> None:
    assert cloud_session(environ) == {"detected": detected, "signal": signal}


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
        (_PUBLIC_URL, None),
        ("https://lab.example.org.", None),
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

    payload = setup_helpers.setup_status(cloud_repo)

    assert payload["cloud_session"] == {"detected": False, "signal": None}
    assert any("lab-tracker serve" in item for item in payload["suggestions"])


def test_cloud_status_names_the_unreachable_default_address_first(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _offline(monkeypatch)

    payload = setup_helpers.setup_status(cloud_repo)

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

    suggestions = setup_helpers.setup_status(cloud_repo)["suggestions"]

    address = [item for item in suggestions if "100.101.102.103" in item]
    assert len(address) == 1
    assert "from LAB_TRACKER_BASE_URL" in address[0]
    assert "a cloud VM is not on that network" in address[0]


def test_cloud_status_explains_a_public_server_that_does_not_answer(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _offline(monkeypatch, diagnosis="proxy_connection_failed")

    suggestions = setup_helpers.setup_status(cloud_repo)["suggestions"]

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

    payload = setup_helpers.setup_status(cloud_repo)

    assert payload["cloud_session"]["mcp_json"] == {
        "command": str(missing),
        "command_found": False,
        "base_url": "http://127.0.0.1:8000",
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


def test_cloud_status_points_to_the_setup_page_without_a_server_revision(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _probe(monkeypatch, {"reachable": True})
    missing = cloud_repo / "nowhere" / "lt-mcp"
    _write_mcp_json(cloud_repo, command=str(missing), base_url=_PUBLIC_URL)

    install = setup_helpers.setup_status(cloud_repo)["suggestions"][0]

    assert "web app's Setup page" in install
    assert "uv tool install --force" not in install


def test_cloud_status_moves_a_client_update_into_the_setup_script(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch, version="999.0.0")

    suggestions = setup_helpers.setup_status(cloud_repo)["suggestions"]

    behind = [item for item in suggestions if "behind its server" in item]
    assert len(behind) == 1
    assert f"`{client_install_command(_SERVER_REVISION)}`" in behind[0]
    assert "setup script" in behind[0]
    assert "lt maintain" not in behind[0]


def test_cloud_status_puts_skills_in_the_setup_script(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", _PUBLIC_URL)
    _online(monkeypatch)

    suggestions = setup_helpers.setup_status(cloud_repo)["suggestions"]

    skills = [item for item in suggestions if "skills" in item]
    assert len(skills) == 1
    assert "`lt update --skills-only` in the cloud environment's setup script" in skills[0]


@pytest.mark.parametrize(
    ("tokens", "note"),
    [
        ({}, "no Lab Tracker token is in this VM"),
        (
            {"LAB_TRACKER_ACCESS_TOKEN": "lpat_x", "LAB_TRACKER_MCP_API_KEY": "lpat_x"},
            "this VM's environment supplies the Lab Tracker token",
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

    brief = setup_helpers.setup_status(cloud_repo, brief=True)

    assert brief["suggestions"] == []
    assert brief["brief"].startswith("lab-tracker (cloud session): server reachable; ")
    assert note in brief["brief"]


def test_a_cloud_brief_line_with_suggestions_leads_with_the_first(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _offline(monkeypatch)

    brief = setup_helpers.setup_status(cloud_repo, brief=True)

    assert brief["brief"].startswith("lab-tracker (cloud session): ")
    assert brief["brief"].endswith(brief["suggestions"][0])


def test_an_explicit_off_switch_restores_the_computer_report(
    cloud_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CLOUD_SESSION_ENV, "0")
    _offline(monkeypatch)

    payload = setup_helpers.setup_status(cloud_repo, brief=True)

    assert payload["brief"].startswith("lab-tracker: ")
    assert any("lab-tracker serve" in item for item in payload["suggestions"])


def test_cloud_session_policy_reaches_every_agent_instruction_surface() -> None:
    for text in (MCP_SERVER_INSTRUCTIONS, managed_agent_activation_block(), _agents_fragment()):
        assert CLOUD_SESSION_POLICY in " ".join(text.split())
    for phrase in (
        "CLAUDE_CODE_REMOTE=true",
        "do not start `lab-tracker serve` in the VM",
        "never ask for a token in the chat",
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

    payload = setup_helpers.setup_status(cloud_repo)

    assert payload["cloud_session"]["mcp_json"] is None
    assert not any(".mcp.json` starts" in item for item in payload["suggestions"])
