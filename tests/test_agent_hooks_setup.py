"""`lt setup agent-hooks`: consent-gated Claude Code hook entries for agent capture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lab_tracker_client.agent_hooks as agent_hooks
from lab_tracker.cli import init_consumer_repo, update_consumer_repo
from lab_tracker_client import cli as lt_cli
from lab_tracker_client import setup as setup_helpers

USER_POST_TOOL_GROUP = {
    "matcher": "Write",
    "hooks": [{"type": "command", "command": 'prettier --write "$FILE"'}],
}
USER_SESSION_END_GROUP = {"hooks": [{"type": "command", "command": "./scripts/notify.sh"}]}


def _settings(repo: Path, name: str = "settings.json") -> dict:
    return json.loads((repo / ".claude" / name).read_text(encoding="utf-8"))


def _managed_commands(settings: dict, event: str) -> list[str]:
    return [
        hook["command"]
        for group in settings.get("hooks", {}).get(event, [])
        for hook in group.get("hooks", [])
        if agent_hooks.is_managed_hook(hook)
    ]


def _run(capsys, *args: str) -> dict:
    lt_cli.main(["setup", "agent-hooks", *args])
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def scaffolded(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    init_consumer_repo(repo)
    return repo


def test_applying_without_consent_hard_fails(scaffolded: Path) -> None:
    before = (scaffolded / ".claude" / "settings.json").read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["setup", "agent-hooks", "--target", str(scaffolded)])
    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["setup", "agent-hooks", "--target", str(scaffolded), "--uninstall"])
    assert (scaffolded / ".claude" / "settings.json").read_text(encoding="utf-8") == before


def test_setup_init_never_installs_agent_hooks(scaffolded: Path) -> None:
    settings = _settings(scaffolded)
    assert "SessionEnd" not in settings["hooks"]
    assert "PostToolUse" not in settings["hooks"]
    assert agent_hooks.agent_hooks_status(scaffolded)["installed"] is False


def test_dry_run_shows_the_diff_and_writes_nothing(scaffolded: Path, capsys) -> None:
    path = scaffolded / ".claude" / "settings.json"
    before = path.read_text(encoding="utf-8")

    preview = _run(capsys, "--target", str(scaffolded), "--dry-run")

    assert preview["action"] == "would-install"
    assert "+" in preview["diff"] and "lt agent session-end --fail-silent" in preview["diff"]
    assert "lt watch touch --fail-silent" in preview["diff"]
    assert path.read_text(encoding="utf-8") == before


def test_install_preserves_scaffold_and_user_hooks_and_is_idempotent(
    scaffolded: Path, capsys
) -> None:
    path = scaffolded / ".claude" / "settings.json"
    original = _settings(scaffolded)
    stale_sibling_group = {
        "matcher": "Edit",
        "hooks": [
            {"type": "command", "command": "lt watch touch"},
            {"type": "command", "command": "echo edited"},
        ],
    }
    seeded = json.loads(json.dumps(original))
    seeded["permissions"] = {"allow": ["Bash(lt setup status:*)"]}
    seeded["hooks"]["PostToolUse"] = [USER_POST_TOOL_GROUP, stale_sibling_group]
    seeded["hooks"]["SessionEnd"] = [USER_SESSION_END_GROUP]
    path.write_text(json.dumps(seeded, indent=2) + "\n", encoding="utf-8")

    installed = _run(capsys, "--target", str(scaffolded), "--yes")

    # A stale managed command counts as present, so this is an update.
    assert installed["action"] == "updated"
    settings = _settings(scaffolded)
    assert settings["permissions"] == seeded["permissions"]
    assert settings["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]
    assert settings["hooks"]["UserPromptSubmit"] == original["hooks"]["UserPromptSubmit"]
    assert settings["hooks"]["PostToolUse"][0] == USER_POST_TOOL_GROUP
    # The stale managed variant is replaced; its user sibling survives.
    assert settings["hooks"]["PostToolUse"][1] == {
        "matcher": "Edit",
        "hooks": [{"type": "command", "command": "echo edited"}],
    }
    assert settings["hooks"]["PostToolUse"][2] == agent_hooks.WATCH_TOUCH_HOOK.group()
    assert settings["hooks"]["SessionEnd"] == [
        USER_SESSION_END_GROUP,
        agent_hooks.SESSION_END_HOOK.group(),
    ]
    assert _managed_commands(settings, "SessionEnd") == ["lt agent session-end --fail-silent"]
    assert _managed_commands(settings, "PostToolUse") == ["lt watch touch --fail-silent"]
    session_end = settings["hooks"]["SessionEnd"][1]["hooks"][0]
    # Claude Code's SessionEnd hooks share a 1.5 s budget unless a timeout raises it.
    assert session_end["timeout"] == 60
    assert settings["hooks"]["PostToolUse"][2]["hooks"][0]["async"] is True
    assert settings["hooks"]["PostToolUse"][2]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"

    written = path.read_text(encoding="utf-8")
    again = _run(capsys, "--target", str(scaffolded), "--yes")
    assert again["action"] == "current"
    assert again["diff"] == ""
    assert path.read_text(encoding="utf-8") == written


def test_uninstall_removes_only_the_managed_entries(scaffolded: Path, capsys) -> None:
    path = scaffolded / ".claude" / "settings.json"
    seeded = _settings(scaffolded)
    seeded["hooks"]["PostToolUse"] = [USER_POST_TOOL_GROUP]
    path.write_text(json.dumps(seeded, indent=2) + "\n", encoding="utf-8")
    _run(capsys, "--target", str(scaffolded), "--yes")
    installed_text = path.read_text(encoding="utf-8")

    preview = _run(capsys, "--target", str(scaffolded), "--uninstall", "--dry-run")
    assert preview["action"] == "would-remove"
    assert "-" in preview["diff"]
    assert path.read_text(encoding="utf-8") == installed_text

    removed = _run(capsys, "--target", str(scaffolded), "--uninstall", "--yes")
    assert removed["action"] == "removed"
    assert _settings(scaffolded) == seeded

    absent = _run(capsys, "--target", str(scaffolded), "--uninstall", "--yes")
    assert absent["action"] == "absent"


def test_install_refuses_a_settings_file_it_cannot_edit_safely(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    path = repo / ".claude" / "settings.json"
    for broken in ("{not json", "[]", '{"hooks": []}', '{"hooks": {"SessionEnd": {}}}'):
        path.write_text(broken, encoding="utf-8")
        with pytest.raises(SystemExit, match="lt setup agent-hooks"):
            lt_cli.main(["setup", "agent-hooks", "--target", str(repo), "--yes"])
        assert path.read_text(encoding="utf-8") == broken


def test_local_scope_edits_settings_local_json_and_status_reports_it(
    tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    installed = _run(capsys, "--target", str(repo), "--local", "--yes")

    assert installed["action"] == "installed"
    assert installed["scope"] == "local"
    assert not (repo / ".claude" / "settings.json").exists()
    settings = _settings(repo, "settings.local.json")
    assert _managed_commands(settings, "SessionEnd") == ["lt agent session-end --fail-silent"]
    # The shared-file warning is only for the committed settings.json.
    assert not any("--local" in warning for warning in installed["warnings"])
    status = agent_hooks.agent_hooks_status(repo)
    assert status["installed"] is True
    assert [item["scope"] for item in status["files"] if item.get("hooks")] == ["local"]


def test_project_scope_warns_that_the_settings_file_is_shared(scaffolded: Path, capsys) -> None:
    preview = _run(capsys, "--target", str(scaffolded), "--dry-run")
    assert any("--local" in warning for warning in preview["warnings"])
    assert any("lt watch add" in warning for warning in preview["warnings"])


def test_lt_update_carries_the_agent_hooks_forward(scaffolded: Path, capsys) -> None:
    path = scaffolded / ".claude" / "settings.json"
    _run(capsys, "--target", str(scaffolded), "--yes")
    installed_text = path.read_text(encoding="utf-8")

    unchanged = update_consumer_repo(scaffolded)
    assert path in unchanged.up_to_date
    assert not unchanged.backups
    assert path.read_text(encoding="utf-8") == installed_text

    drifted = json.loads(installed_text)
    drifted["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "lt setup status"
    path.write_text(json.dumps(drifted, indent=2) + "\n", encoding="utf-8")

    refreshed = update_consumer_repo(scaffolded)

    assert path in refreshed.overwritten
    assert path.read_text(encoding="utf-8") == installed_text
    backup = path.with_name(path.name + ".bak-lt-update")
    assert '"lt setup status"' in backup.read_text(encoding="utf-8")

    forced = init_consumer_repo(scaffolded, force=True)
    assert path in forced.overwritten
    assert path.read_text(encoding="utf-8") == installed_text


def test_setup_status_reports_agent_hooks_without_suggesting_them(
    scaffolded: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(setup_helpers, "probe_health_diagnostics", lambda _url: {"reachable": True})

    before = setup_helpers.setup_status(scaffolded)
    assert before["agent_hooks"]["installed"] is False
    assert not any("agent-hooks" in item for item in before["suggestions"])

    _run(capsys, "--target", str(scaffolded), "--yes")
    after = setup_helpers.setup_status(scaffolded)
    assert after["agent_hooks"]["installed"] is True
    assert after["agent_hooks"]["session_end"] is True
    assert after["agent_hooks"]["watch_touch"] is True


def test_managed_hook_identity_is_by_command() -> None:
    assert agent_hooks.is_managed_hook({"type": "command", "command": "lt watch touch"})
    assert agent_hooks.is_managed_hook(
        {"type": "command", "command": "/opt/venv/bin/lt agent session-end --fail-silent"}
    )
    assert not agent_hooks.is_managed_hook({"type": "command", "command": "lt watch run"})
    assert not agent_hooks.is_managed_hook({"type": "command", "command": "echo lt watch touch"})
    assert not agent_hooks.is_managed_hook({"type": "prompt", "prompt": "lt watch touch"})
