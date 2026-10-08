"""`lt setup agent-hooks`: consent-gated Claude Code hook entries for agent capture."""

from __future__ import annotations

import json
import subprocess
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
SHARED = "settings.json"
LOCAL = "settings.local.json"


def _path(repo: Path, name: str) -> Path:
    return repo / ".claude" / name


def _settings(repo: Path, name: str) -> dict:
    return json.loads(_path(repo, name).read_text(encoding="utf-8"))


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
    before = _path(scaffolded, SHARED).read_text(encoding="utf-8")
    for extra in ([], ["--uninstall"], ["--shared"]):
        with pytest.raises(SystemExit, match="--yes"):
            lt_cli.main(["setup", "agent-hooks", "--target", str(scaffolded), *extra])
    assert _path(scaffolded, SHARED).read_text(encoding="utf-8") == before
    assert not _path(scaffolded, LOCAL).exists()


def test_setup_init_never_installs_agent_hooks(scaffolded: Path) -> None:
    settings = _settings(scaffolded, SHARED)
    assert "SessionEnd" not in settings["hooks"]
    assert "PostToolUse" not in settings["hooks"]
    assert not _path(scaffolded, LOCAL).exists()
    assert agent_hooks.agent_hooks_status(scaffolded)["installed"] is False


def test_default_dry_run_previews_the_personal_file_and_writes_nothing(
    scaffolded: Path, capsys
) -> None:
    shared_before = _path(scaffolded, SHARED).read_text(encoding="utf-8")

    lt_cli.main(["setup", "agent-hooks", "--target", str(scaffolded), "--dry-run"])
    captured = capsys.readouterr()
    preview = json.loads(captured.out)

    assert preview["action"] == "would-install"
    assert preview["scope"] == "local"
    assert preview["settings_path"] == str(_path(scaffolded, LOCAL))
    assert "settings.local.json (proposed)" in preview["diff"]
    assert "lt agent session-end --fail-silent" in preview["diff"]
    assert "lt watch touch --fail-silent" in preview["diff"]
    assert "EVERYONE" not in captured.err
    assert not any("EVERYONE" in warning for warning in preview["warnings"])
    assert not _path(scaffolded, LOCAL).exists()
    assert _path(scaffolded, SHARED).read_text(encoding="utf-8") == shared_before


def test_default_install_writes_only_the_personal_file(scaffolded: Path, capsys) -> None:
    shared_before = _path(scaffolded, SHARED).read_text(encoding="utf-8")

    installed = _run(capsys, "--target", str(scaffolded), "--yes")

    assert installed["action"] == "installed"
    assert installed["scope"] == "local"
    assert _path(scaffolded, SHARED).read_text(encoding="utf-8") == shared_before
    local = _settings(scaffolded, LOCAL)
    # The personal file carries only the managed hooks; Claude Code merges it
    # with the shared file's SessionStart/UserPromptSubmit hooks.
    assert local == {
        "hooks": {
            "SessionEnd": [agent_hooks.SESSION_END_HOOK.group()],
            "PostToolUse": [agent_hooks.WATCH_TOUCH_HOOK.group()],
        }
    }
    assert _run(capsys, "--target", str(scaffolded), "--yes")["action"] == "current"
    status = agent_hooks.agent_hooks_status(scaffolded)
    assert status["installed"] is True
    assert status["scopes"] == ["local"]


def test_the_local_flag_is_gone_because_local_is_the_default(scaffolded: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        lt_cli.main(["setup", "agent-hooks", "--target", str(scaffolded), "--local", "--yes"])
    assert excinfo.value.code == 2
    assert not _path(scaffolded, LOCAL).exists()


def test_shared_install_warns_preserves_hooks_and_is_idempotent(scaffolded: Path, capsys) -> None:
    path = _path(scaffolded, SHARED)
    original = _settings(scaffolded, SHARED)
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

    lt_cli.main(["setup", "agent-hooks", "--target", str(scaffolded), "--shared", "--yes"])
    captured = capsys.readouterr()
    installed = json.loads(captured.out)

    assert "EVERYONE who clones this repository" in captured.err
    assert agent_hooks.SHARED_SCOPE_WARNING in installed["warnings"]
    assert installed["scope"] == "shared"
    # A stale managed command counts as present, so this is an update.
    assert installed["action"] == "updated"
    assert not _path(scaffolded, LOCAL).exists()
    settings = _settings(scaffolded, SHARED)
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
    again = _run(capsys, "--target", str(scaffolded), "--shared", "--yes")
    assert again["action"] == "current"
    assert again["diff"] == ""
    assert path.read_text(encoding="utf-8") == written


def test_uninstall_removes_only_managed_entries_from_the_named_scope(
    scaffolded: Path, capsys
) -> None:
    local_path = _path(scaffolded, LOCAL)
    local_path.parent.mkdir(exist_ok=True)
    local_seed = {"permissions": {"allow": ["Bash(ls:*)"]}, "hooks": {"PostToolUse": []}}
    local_seed["hooks"]["PostToolUse"] = [USER_POST_TOOL_GROUP]
    local_path.write_text(json.dumps(local_seed, indent=2) + "\n", encoding="utf-8")
    shared_seed = _settings(scaffolded, SHARED)
    _run(capsys, "--target", str(scaffolded), "--yes")
    _run(capsys, "--target", str(scaffolded), "--shared", "--yes")
    installed_local = local_path.read_text(encoding="utf-8")
    assert agent_hooks.agent_hooks_status(scaffolded)["scopes"] == ["local", "shared"]

    preview = _run(capsys, "--target", str(scaffolded), "--uninstall", "--dry-run")
    assert preview["action"] == "would-remove"
    assert preview["scope"] == "local"
    assert local_path.read_text(encoding="utf-8") == installed_local

    removed = _run(capsys, "--target", str(scaffolded), "--uninstall", "--yes")
    assert removed["action"] == "removed"
    assert _settings(scaffolded, LOCAL) == local_seed
    # The shared file is left alone, and the payload says how to clear it too.
    assert any(
        "settings.json still has the agent hooks" in warning and "--shared --uninstall" in warning
        for warning in removed["warnings"]
    )
    assert agent_hooks.agent_hooks_status(scaffolded)["scopes"] == ["shared"]

    lt_cli.main(
        ["setup", "agent-hooks", "--target", str(scaffolded), "--shared", "--uninstall", "--yes"]
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["action"] == "removed"
    # Removing capture is not the enrolling direction: no shared-scope warning.
    assert "EVERYONE" not in captured.err
    assert _settings(scaffolded, SHARED) == shared_seed

    absent = _run(capsys, "--target", str(scaffolded), "--uninstall", "--yes")
    assert absent["action"] == "absent"


@pytest.mark.parametrize("extra", [[], ["--shared"]])
def test_install_refuses_a_settings_file_it_cannot_edit_safely(
    tmp_path: Path, extra: list[str]
) -> None:
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    path = _path(repo, SHARED if extra else LOCAL)
    for broken in ("{not json", "[]", '{"hooks": []}', '{"hooks": {"SessionEnd": {}}}'):
        path.write_text(broken, encoding="utf-8")
        with pytest.raises(SystemExit, match="lt setup agent-hooks"):
            lt_cli.main(["setup", "agent-hooks", "--target", str(repo), "--yes", *extra])
        assert path.read_text(encoding="utf-8") == broken


@pytest.mark.parametrize(
    "seed",
    [
        {"hooks": None, "env": {"A": "1"}},
        {"hooks": {"SessionEnd": None, "PostToolUse": None}, "env": {"A": "1"}},
    ],
)
@pytest.mark.parametrize("extra", [[], ["--shared"]])
def test_null_hooks_count_as_empty(tmp_path: Path, capsys, seed: dict, extra: list[str]) -> None:
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    path = _path(repo, SHARED if extra else LOCAL)
    path.write_text(json.dumps(seed), encoding="utf-8")

    absent = _run(capsys, "--target", str(repo), "--uninstall", "--yes", *extra)
    assert absent["action"] == "absent"
    assert json.loads(path.read_text(encoding="utf-8")) == seed

    installed = _run(capsys, "--target", str(repo), "--yes", *extra)

    assert installed["action"] == "installed"
    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["env"] == {"A": "1"}
    assert settings["hooks"]["SessionEnd"] == [agent_hooks.SESSION_END_HOOK.group()]
    assert settings["hooks"]["PostToolUse"] == [agent_hooks.WATCH_TOUCH_HOOK.group()]

    _run(capsys, "--target", str(repo), "--uninstall", "--yes", *extra)
    assert json.loads(path.read_text(encoding="utf-8")) == {"env": {"A": "1"}}


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    # The developer's global ignore may already hide settings.local.json.
    subprocess.run(  # noqa: S603, S607
        ["git", "-C", str(path), "config", "core.excludesFile", str(path / "no-global-ignore")],
        check=True,
    )
    return path


def test_warns_when_git_would_not_ignore_the_personal_file(tmp_path: Path, capsys) -> None:
    repo = _git_repo(tmp_path / "repo")

    exposed = _run(capsys, "--target", str(repo), "--dry-run")
    assert any(
        ".gitignore" in warning and "settings.local.json" in warning
        for warning in exposed["warnings"]
    )

    (repo / ".gitignore").write_text(".claude/settings.local.json\n", encoding="utf-8")
    ignored = _run(capsys, "--target", str(repo), "--dry-run")
    assert not any(".gitignore" in warning for warning in ignored["warnings"])

    # Outside a checkout there is no git to commit it.
    loose = tmp_path / "loose"
    loose.mkdir()
    outside = _run(capsys, "--target", str(loose), "--dry-run")
    assert not any(".gitignore" in warning for warning in outside["warnings"])


def test_install_warns_when_there_is_no_watch_config(scaffolded: Path, capsys) -> None:
    preview = _run(capsys, "--target", str(scaffolded), "--dry-run")
    assert any("lt watch add" in warning for warning in preview["warnings"])


def test_lt_update_carries_shared_hooks_forward_and_never_touches_the_personal_file(
    scaffolded: Path, capsys
) -> None:
    path = _path(scaffolded, SHARED)
    _run(capsys, "--target", str(scaffolded), "--shared", "--yes")
    installed_text = path.read_text(encoding="utf-8")
    local_path = _path(scaffolded, LOCAL)
    local_text = '{"hooks": {"SessionEnd": []}, "env": {"X": "1"}}\n'
    local_path.write_text(local_text, encoding="utf-8")

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
    assert local_path.read_text(encoding="utf-8") == local_text


def test_lt_update_leaves_a_personal_install_out_of_the_shared_file(
    scaffolded: Path, capsys
) -> None:
    _run(capsys, "--target", str(scaffolded), "--yes")
    local_text = _path(scaffolded, LOCAL).read_text(encoding="utf-8")

    update_consumer_repo(scaffolded)
    init_consumer_repo(scaffolded, force=True)

    assert _managed_commands(_settings(scaffolded, SHARED), "SessionEnd") == []
    assert _path(scaffolded, LOCAL).read_text(encoding="utf-8") == local_text


@pytest.mark.parametrize(("extra", "scope"), [([], "local"), (["--shared"], "shared")])
def test_setup_status_guides_optional_capture_and_stays_quiet_after_install(
    scaffolded: Path, monkeypatch, capsys, extra: list[str], scope: str
) -> None:
    monkeypatch.setattr(setup_helpers, "probe_health_diagnostics", lambda _url: {"reachable": True})

    before = setup_helpers.setup_status(scaffolded)
    assert before["agent_hooks"]["installed"] is False
    assert before["agent_hooks"]["scopes"] == []
    [notice] = [item for item in before["suggestions"] if "agent-hooks" in item]
    assert "Optional Claude Code" in notice
    assert "lt setup agent-hooks --dry-run" in notice
    assert "lt setup agent-hooks --yes" in notice
    assert "settings.local.json" in notice
    assert "Read + stage evidence" in notice
    assert not _path(scaffolded, LOCAL).exists()
    lt_cli.main(["setup", "status", "--target", str(scaffolded), "--brief"])
    assert notice in json.loads(capsys.readouterr().out)["suggestions"]

    _run(capsys, "--target", str(scaffolded), "--yes", *extra)
    after = setup_helpers.setup_status(scaffolded)
    assert after["agent_hooks"]["installed"] is True
    assert after["agent_hooks"]["session_end"] is True
    assert after["agent_hooks"]["watch_touch"] is True
    assert after["agent_hooks"]["scopes"] == [scope]
    assert not any("agent-hooks" in item for item in after["suggestions"])


@pytest.mark.usefixtures("offline_server")
@pytest.mark.parametrize("existing", [None, "SessionEnd", "PostToolUse"])
def test_doctor_guides_missing_or_partial_capture_without_writing_or_failing(
    scaffolded: Path, capsys, existing: str | None
) -> None:
    if existing:
        managed = (
            agent_hooks.SESSION_END_HOOK
            if existing == "SessionEnd"
            else agent_hooks.WATCH_TOUCH_HOOK
        )
        _path(scaffolded, LOCAL).write_text(json.dumps({"hooks": {existing: [managed.group()]}}))
    before = {path.name: path.read_bytes() for path in (scaffolded / ".claude").iterdir()}

    lt_cli.main(["doctor", "--target", str(scaffolded)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["agent_hooks"]["installed"] is False
    [notice] = payload["suggestions"]
    assert "lt setup agent-hooks --dry-run" in notice
    assert "lt setup agent-hooks --yes" in notice
    assert "full transcript stays local" in notice
    if existing:
        assert "capture is missing" in notice
    else:
        assert "not installed" in notice
    assert {path.name: path.read_bytes() for path in (scaffolded / ".claude").iterdir()} == before


@pytest.mark.usefixtures("offline_server")
def test_doctor_all_preserves_capture_guidance_per_repository(
    scaffolded: Path, tmp_path, capsys
) -> None:
    installed = tmp_path / "installed"
    init_consumer_repo(installed)
    _run(capsys, "--target", str(installed), "--yes")

    lt_cli.main(["doctor", "--all"])
    payload = json.loads(capsys.readouterr().out)
    by_root = {Path(item["root"]): item for item in payload["repos"]}

    assert by_root[scaffolded]["agent_hooks"]["installed"] is False
    assert "lt setup agent-hooks --dry-run" in by_root[scaffolded]["suggestions"][0]
    assert by_root[installed]["agent_hooks"]["installed"] is True
    assert by_root[installed]["suggestions"] == []
    assert not _path(scaffolded, LOCAL).exists()


@pytest.mark.usefixtures("offline_server")
def test_doctor_reports_disabled_capture_instead_of_reinstallation(
    scaffolded: Path, monkeypatch, capsys
) -> None:
    _run(capsys, "--target", str(scaffolded), "--yes")
    monkeypatch.setenv("LAB_TRACKER_AGENT_HOOKS", "0")

    lt_cli.main(["doctor", "--target", str(scaffolded)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["agent_hooks"]["installed"] is True
    assert payload["agent_hooks"]["enabled"] is False
    [notice] = payload["suggestions"]
    assert "disabled by LAB_TRACKER_AGENT_HOOKS" in notice
    assert "agent-hooks --yes" not in notice


@pytest.mark.usefixtures("offline_server")
def test_doctor_reports_unreadable_capture_settings_before_installation(
    scaffolded: Path, capsys
) -> None:
    broken = "{not json"
    _path(scaffolded, LOCAL).write_text(broken)

    lt_cli.main(["doctor", "--target", str(scaffolded)])
    payload = json.loads(capsys.readouterr().out)

    assert any(item.get("error") for item in payload["agent_hooks"]["files"])
    assert "repair the settings errors" in payload["suggestions"][0]
    assert _path(scaffolded, LOCAL).read_text() == broken


def test_managed_hook_identity_is_by_command() -> None:
    assert agent_hooks.is_managed_hook({"type": "command", "command": "lt watch touch"})
    assert agent_hooks.is_managed_hook(
        {"type": "command", "command": "/opt/venv/bin/lt agent session-end --fail-silent"}
    )
    assert not agent_hooks.is_managed_hook({"type": "command", "command": "lt watch run"})
    assert not agent_hooks.is_managed_hook({"type": "command", "command": "echo lt watch touch"})
    assert not agent_hooks.is_managed_hook({"type": "prompt", "prompt": "lt watch touch"})
