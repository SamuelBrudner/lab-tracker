from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python 3.10 CI
    import tomli as tomllib

from lab_tracker.cli import (
    ManagedBlockMarkerError,
    _doctor,
    _doctor_exit_code,
    _extract_managed_body,
    _extract_version_line,
    _open_browser_when_ready,
    _upsert_managed_block,
    init_consumer_repo,
    serve_app,
    update_consumer_repo,
)
from lab_tracker.cli import (
    main as lab_tracker_main,
)
from lab_tracker.decision_context_constants import (
    AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN,
    AGENTS_CODE_CONVENTIONS_BLOCK_END,
    CLAUDE_BLOCK_BEGIN,
    CLAUDE_BLOCK_END,
    CODE_CONVENTIONS_BLOCK_BEGIN,
    CODE_CONVENTIONS_BLOCK_END,
    COMMIT_CAPTURE_RECOVERY_POLICY,
    code_conventions_version_line,
    code_facing_idioms,
    cursor_rules_mdc,
    managed_agent_activation_block,
    managed_code_conventions_block,
)
from lab_tracker.demo_seed import DemoSeedResult
from lab_tracker.setup_guide import setup_skill_markdown
from lab_tracker.skill_bundle import skill_resources
from lab_tracker_client import registry as repo_registry
from lab_tracker_client.cli import main as lt_main


def test_init_creates_portable_consumer_files(tmp_path: Path) -> None:
    result = init_consumer_repo(tmp_path, project_name="Consumer Project")

    assert sorted(path.relative_to(tmp_path).as_posix() for path in result.created) == [
        ".claude/settings.json",
        ".cursor/mcp.json",
        ".gemini/settings.json",
        ".mcp.json",
        "AGENTS.lt.md",
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
        "lt_ids.json",
        "scripts/lt.py",
    ]
    mcp_config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp_config["mcpServers"]["lab-tracker"]
    assert server["command"] == "lt-mcp"
    assert "python.exe" not in json.dumps(mcp_config).lower()
    assert "C:\\" not in json.dumps(mcp_config)

    # Cursor reads .cursor/mcp.json (top-level mcpServers shape, not Copilot's
    # `servers` schema). It must stay portable and in lockstep with .mcp.json.
    cursor_config = json.loads(
        (tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8")
    )
    cursor_server = cursor_config["mcpServers"]["lab-tracker"]
    assert cursor_server["command"] == "lt-mcp"
    assert "servers" not in cursor_config
    assert "python.exe" not in json.dumps(cursor_config).lower()
    assert "C:\\" not in json.dumps(cursor_config)
    assert cursor_config == mcp_config

    # Gemini CLI reads .gemini/settings.json with the same mcpServers shape;
    # it stays in lockstep with .mcp.json so every vendor launches lt-mcp.
    gemini_config = json.loads(
        (tmp_path / ".gemini" / "settings.json").read_text(encoding="utf-8")
    )
    assert gemini_config == mcp_config

    shim = (tmp_path / "scripts" / "lt.py").read_text(encoding="utf-8")
    assert "from lab_tracker_client import *" in shim
    assert "from lab_tracker_client.cli import main" in shim

    ids = json.loads((tmp_path / "lt_ids.json").read_text(encoding="utf-8"))
    assert ids == {"project_id": "", "project_name": "Consumer Project"}
    agents_fragment = (tmp_path / "AGENTS.lt.md").read_text(encoding="utf-8")
    assert "first non-blank line" in agents_fragment
    assert "Managed code-facing conventions" in agents_fragment
    assert "never accept" in agents_fragment  # proposal workflow is human-gated
    assert ".gemini/settings.json" in agents_fragment
    assert "~/.codex/config.toml" in agents_fragment
    assert COMMIT_CAPTURE_RECOVERY_POLICY in agents_fragment
    # Every agent instruction file gets the same activation block; the
    # code-conventions blocks stay behind --yes consent.
    agents_md = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    gemini_md = (tmp_path / "GEMINI.md").read_text(encoding="utf-8")
    assert "BEGIN LAB TRACKER MCP ACTIVATION" in agents_md
    assert "BEGIN LAB TRACKER MCP ACTIVATION" in gemini_md
    assert AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN not in agents_md
    assert not (tmp_path / ".cursor" / "rules" / "lab-tracker.mdc").exists()
    claude_settings = json.loads(
        (tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    hook_command = claude_settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert hook_command == "lt prime --if-research-facing --fail-silent --limit 5"
    claude_md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "BEGIN LAB TRACKER MCP ACTIVATION" in claude_md
    assert "lab_tracker_get_decision_context" in claude_md
    for prompt_name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        prompt = (tmp_path / prompt_name).read_text(encoding="utf-8")
        assert COMMIT_CAPTURE_RECOVERY_POLICY in prompt
        assert prompt.index("lt outbox sync") < prompt.index("lt outbox status")


def test_init_skips_existing_files_without_force(tmp_path: Path) -> None:
    custom = tmp_path / ".mcp.json"
    custom.write_text('{"custom": true}\n', encoding="utf-8")

    result = init_consumer_repo(tmp_path)

    assert custom in result.skipped
    assert custom.read_text(encoding="utf-8") == '{"custom": true}\n'


def test_init_injects_managed_claude_block_idempotently(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Existing instructions\n", encoding="utf-8")

    first = init_consumer_repo(tmp_path)
    content = claude.read_text(encoding="utf-8")
    second = init_consumer_repo(tmp_path)

    assert claude in first.overwritten
    assert claude in second.skipped
    assert content == claude.read_text(encoding="utf-8")
    assert content.count("BEGIN LAB TRACKER MCP ACTIVATION") == 1


def test_init_force_overwrites_existing_files(tmp_path: Path) -> None:
    custom = tmp_path / ".mcp.json"
    custom.write_text('{"custom": true}\n', encoding="utf-8")

    result = init_consumer_repo(tmp_path, force=True)

    assert custom in result.overwritten
    assert json.loads(custom.read_text(encoding="utf-8"))["mcpServers"]["lab-tracker"][
        "command"
    ] == "lt-mcp"


def test_init_warns_when_hook_interpreter_cannot_resolve_lt(
    tmp_path: Path, monkeypatch
) -> None:
    # Simulate the GH #76 environment: the client lives in a venv that is not on
    # the ambient PATH/interpreter, so the generated hooks are dead-on-arrival.
    monkeypatch.setattr("lab_tracker.cli.shutil.which", lambda _name: None)
    monkeypatch.setattr("lab_tracker.cli.importlib.util.find_spec", lambda _name: None)

    result = init_consumer_repo(tmp_path)

    joined = "\n".join(result.warnings)
    assert "no `lt` executable is on the current PATH" in joined
    assert "not importable" in joined
    # Warnings ride in the machine-readable payload too.
    assert result.as_dict()["warnings"] == result.warnings


def test_init_has_no_env_warnings_when_lt_and_client_resolve(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("lab_tracker.cli.shutil.which", lambda _name: "/venv/bin/lt")
    monkeypatch.setattr("lab_tracker.cli.importlib.util.find_spec", lambda _name: object())

    result = init_consumer_repo(tmp_path)

    assert result.warnings == []


def test_init_cli_prints_hook_env_warning_to_stderr(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("lab_tracker.cli.shutil.which", lambda _name: None)
    monkeypatch.setattr("lab_tracker.cli.importlib.util.find_spec", lambda _name: object())

    lab_tracker_main(["init", "--target", str(tmp_path)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # stdout stays clean JSON
    assert payload["warnings"]
    assert "warning: " in captured.err
    assert "no `lt` executable" in captured.err


def test_init_yes_writes_managed_code_conventions_blocks(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "# Existing Agents\n\n<!-- BEGIN BEADS -->\nbeads block\n<!-- END BEADS -->\n",
        encoding="utf-8",
    )

    init_consumer_repo(tmp_path, yes=True)

    expected_body = code_facing_idioms()
    expected_version = code_conventions_version_line(expected_body)
    claude_content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    agents_content = agents.read_text(encoding="utf-8")
    cursor_content = (tmp_path / ".cursor" / "rules" / "lab-tracker.mdc").read_text(
        encoding="utf-8"
    )

    assert CLAUDE_BLOCK_BEGIN in claude_content
    assert claude_content.count(CODE_CONVENTIONS_BLOCK_BEGIN) == 1
    assert agents_content.count(AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN) == 1
    assert "<!-- BEGIN BEADS -->" in agents_content
    assert cursor_content.startswith("---\ndescription: Lab Tracker code-facing conventions")
    assert (
        _extract_managed_body(
            claude_content,
            begin_marker=CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=CODE_CONVENTIONS_BLOCK_END,
        )
        == expected_body
    )
    assert (
        _extract_managed_body(
            agents_content,
            begin_marker=AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=AGENTS_CODE_CONVENTIONS_BLOCK_END,
        )
        == expected_body
    )
    assert (
        _extract_managed_body(
            cursor_content,
            begin_marker=CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=CODE_CONVENTIONS_BLOCK_END,
        )
        == expected_body
    )
    assert _extract_version_line(claude_content, end_marker=CODE_CONVENTIONS_BLOCK_END) == (
        expected_version
    )


def test_init_dry_run_returns_diffs_without_writing(tmp_path: Path) -> None:
    result = init_consumer_repo(tmp_path, yes=True, dry_run=True)

    assert list(tmp_path.iterdir()) == []
    assert result.created
    assert result.diffs
    assert str(tmp_path / "AGENTS.md") in result.as_dict()["diffs"]


def test_init_cli_dry_run_writes_nothing(tmp_path: Path, capsys) -> None:
    lab_tracker_main(["init", "--target", str(tmp_path), "--yes", "--dry-run"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["diffs"]
    assert str(tmp_path / "AGENTS.md") in payload["diffs"]
    assert list(tmp_path.iterdir()) == []


def test_init_uninstall_strips_managed_blocks_preserving_surroundings(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Existing Agents\n", encoding="utf-8")
    init_consumer_repo(tmp_path, yes=True)

    result = init_consumer_repo(tmp_path, uninstall=True)

    claude_content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    agents_content = agents.read_text(encoding="utf-8")
    cursor_content = (tmp_path / ".cursor" / "rules" / "lab-tracker.mdc").read_text(
        encoding="utf-8"
    )
    assert tmp_path / "CLAUDE.md" in result.stripped
    assert CLAUDE_BLOCK_BEGIN not in claude_content
    assert CODE_CONVENTIONS_BLOCK_BEGIN not in claude_content
    assert CLAUDE_BLOCK_BEGIN not in agents_content
    assert AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN not in agents_content
    assert "# Existing Agents" in agents_content
    gemini_content = (tmp_path / "GEMINI.md").read_text(encoding="utf-8")
    assert CLAUDE_BLOCK_BEGIN not in gemini_content
    assert CODE_CONVENTIONS_BLOCK_BEGIN not in cursor_content


def test_init_cli_yes_then_uninstall(tmp_path: Path, capsys) -> None:
    lab_tracker_main(["init", "--target", str(tmp_path), "--yes"])
    capsys.readouterr()

    lab_tracker_main(["init", "--target", str(tmp_path), "--uninstall"])

    payload = json.loads(capsys.readouterr().out)
    assert str(tmp_path / "CLAUDE.md") in payload["stripped"]
    assert CODE_CONVENTIONS_BLOCK_BEGIN not in (
        tmp_path / "CLAUDE.md"
    ).read_text(encoding="utf-8")


def test_upsert_managed_block_replaces_single_marker_corruption() -> None:
    block = managed_code_conventions_block()
    existing = f"# Existing\n\n{CODE_CONVENTIONS_BLOCK_BEGIN}\nstale body\n"

    content = _upsert_managed_block(
        existing,
        block,
        begin_marker=CODE_CONVENTIONS_BLOCK_BEGIN,
        end_marker=CODE_CONVENTIONS_BLOCK_END,
    )

    assert content.count(CODE_CONVENTIONS_BLOCK_BEGIN) == 1
    assert content.count(CODE_CONVENTIONS_BLOCK_END) == 1
    assert "stale body" not in content
    assert "# Existing" in content
    assert block in content


@pytest.mark.usefixtures("offline_server")
def test_doctor_reports_code_conventions_drift(tmp_path: Path, capsys) -> None:
    init_consumer_repo(tmp_path, yes=True)

    healthy = _doctor(tmp_path)
    assert all(target["present"] and target["in_sync"] for target in healthy["targets"])
    assert code_facing_idioms() in healthy["code_facing_idioms"]

    lab_tracker_main(["doctor", "--target", str(tmp_path)])
    assert json.loads(capsys.readouterr().out)["command"] == "doctor"

    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace("EntityRef", "EntityRefDrift", 1),
        encoding="utf-8",
    )

    drifted = _doctor(tmp_path)
    assert any(not target["in_sync"] for target in drifted["targets"])
    with pytest.raises(SystemExit):
        lab_tracker_main(["check-idioms", "--target", str(tmp_path)])


def test_doctor_treats_safe_default_absent_blocks_as_not_installed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_consumer_repo(tmp_path)

    payload = _doctor(tmp_path)
    assert any(not target["present"] for target in payload["targets"])
    assert not any(target["drifted"] for target in payload["targets"])

    lab_tracker_main(["check-idioms", "--target", str(tmp_path)])
    assert json.loads(capsys.readouterr().out)["command"] == "doctor"

    lt_main(["check-idioms", "--target", str(tmp_path)])
    assert json.loads(capsys.readouterr().out)["command"] == "doctor"


@pytest.mark.usefixtures("offline_server")
def test_lt_doctor_delegates_and_honors_fail_silent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_consumer_repo(tmp_path, yes=True)

    lt_main(["doctor", "--target", str(tmp_path)])
    assert json.loads(capsys.readouterr().out)["command"] == "doctor"

    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace("EntityRef", "EntityRefDrift", 1),
        encoding="utf-8",
    )

    lt_main(["doctor", "--target", str(tmp_path), "--fail-silent"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_update_refreshes_stale_scaffold_and_managed_blocks(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path, yes=True)
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text('{"hooks": {"OldHookShape": []}}\n', encoding="utf-8")
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace("EntityRef", "EntityRefDrift", 1),
        encoding="utf-8",
    )
    assert _doctor_exit_code(_doctor(tmp_path)) == 1

    result = update_consumer_repo(tmp_path)

    # Stale hook config refreshed, with the old content preserved as a backup.
    assert "lt prime" in settings.read_text(encoding="utf-8")
    backup = settings.with_name(settings.name + ".bak-lt-update")
    assert result.backups[settings] == backup
    assert "OldHookShape" in backup.read_text(encoding="utf-8")
    # Drifted conventions block re-rendered: doctor is clean again.
    assert _doctor_exit_code(_doctor(tmp_path)) == 0


def test_update_preserves_conventions_consent(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path)  # no --yes: no conventions blocks installed

    without_consent = update_consumer_repo(tmp_path)
    claude_text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    assert "BEGIN LAB TRACKER CODE CONVENTIONS" not in claude_text
    assert not (tmp_path / ".cursor" / "rules" / "lab-tracker.mdc").exists()
    assert any("--yes" in offer for offer in without_consent.offers)

    update_consumer_repo(tmp_path, yes=True)

    assert "BEGIN LAB TRACKER CODE CONVENTIONS" in (tmp_path / "CLAUDE.md").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / ".cursor" / "rules" / "lab-tracker.mdc").exists()


def test_update_refreshes_commit_capture_recovery_policy(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path)
    prompt_paths = [
        tmp_path / "CLAUDE.md",
        tmp_path / "AGENTS.md",
        tmp_path / "GEMINI.md",
        tmp_path / "AGENTS.lt.md",
    ]
    for path in prompt_paths:
        stale = path.read_text(encoding="utf-8").replace(
            COMMIT_CAPTURE_RECOVERY_POLICY,
            "Stale commit-capture recovery policy.",
        )
        path.write_text(stale, encoding="utf-8")

    update_consumer_repo(tmp_path)

    for path in prompt_paths:
        refreshed = path.read_text(encoding="utf-8")
        assert COMMIT_CAPTURE_RECOVERY_POLICY in refreshed
        assert "Stale commit-capture recovery policy." not in refreshed
        assert refreshed.index("lt outbox sync") < refreshed.index("lt outbox status")


def test_update_never_rewrites_lt_ids(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path)
    ids = tmp_path / "lt_ids.json"
    ids.write_text('{"project_id": "abc-123", "project_name": "Josh thesis"}\n', encoding="utf-8")

    result = update_consumer_repo(tmp_path)

    assert ids in result.skipped
    assert json.loads(ids.read_text(encoding="utf-8"))["project_id"] == "abc-123"


def test_update_scaffold_files_are_stable_between_runs(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path, yes=True)

    update_consumer_repo(tmp_path)
    second = update_consumer_repo(tmp_path)

    scaffold = {
        tmp_path / ".mcp.json",
        tmp_path / ".cursor" / "mcp.json",
        tmp_path / ".gemini" / "settings.json",
        tmp_path / ".claude" / "settings.json",
        tmp_path / "scripts" / "lt.py",
        tmp_path / "AGENTS.lt.md",
    }
    assert scaffold <= set(second.up_to_date)
    assert not second.backups


def test_update_keeps_cursor_rule_byte_stable_with_one_front_matter_header(
    tmp_path: Path,
) -> None:
    init_consumer_repo(tmp_path, yes=True)
    cursor_rule = tmp_path / ".cursor" / "rules" / "lab-tracker.mdc"
    initial = cursor_rule.read_text(encoding="utf-8")

    first = update_consumer_repo(tmp_path)
    second = update_consumer_repo(tmp_path)

    assert initial == cursor_rules_mdc()
    assert cursor_rule.read_text(encoding="utf-8") == initial
    assert initial.count("description: Lab Tracker code-facing conventions") == 1
    # Unchanged managed files are up to date for update, like scaffold files.
    assert cursor_rule in first.up_to_date
    assert cursor_rule in second.up_to_date


def test_update_repairs_duplicate_cursor_front_matter_and_dry_run_previews_it(
    tmp_path: Path,
) -> None:
    init_consumer_repo(tmp_path, yes=True)
    cursor_rule = tmp_path / ".cursor" / "rules" / "lab-tracker.mdc"
    front_matter, _separator, _body = cursor_rules_mdc().partition("\n\n")
    duplicated = f"{front_matter}\n\n{cursor_rules_mdc()}"
    cursor_rule.write_text(duplicated, encoding="utf-8")

    preview = update_consumer_repo(tmp_path, dry_run=True)

    assert cursor_rule.read_text(encoding="utf-8") == duplicated
    assert cursor_rule in preview.diffs
    assert preview._preview_contents[cursor_rule] == cursor_rules_mdc()

    result = update_consumer_repo(tmp_path)

    repaired = cursor_rule.read_text(encoding="utf-8")
    assert repaired == cursor_rules_mdc()
    assert repaired.count("description: Lab Tracker code-facing conventions") == 1
    assert cursor_rule in result.overwritten


def test_update_dry_run_writes_nothing(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path, yes=True)
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text('{"hooks": {}}\n', encoding="utf-8")

    result = update_consumer_repo(tmp_path, dry_run=True)

    assert settings.read_text(encoding="utf-8") == '{"hooks": {}}\n'
    assert not settings.with_name(settings.name + ".bak-lt-update").exists()
    assert settings in result.diffs and "lt prime" in result.diffs[settings]


def test_lt_update_cli_delegates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    init_consumer_repo(tmp_path, yes=True)
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text('{"hooks": {"OldHookShape": []}}\n', encoding="utf-8")

    lt_main(["update", "--target", str(tmp_path)])

    payload = json.loads(capsys.readouterr().out)
    assert str(settings) in payload["overwritten"]
    assert str(settings) in payload["backups"]
    assert "lt prime" in settings.read_text(encoding="utf-8")


_BOTH_UPDATE_CLIS = pytest.mark.parametrize(
    "main",
    [lt_main, lab_tracker_main],
    ids=["lt", "lab_tracker"],
)


@pytest.fixture
def skill_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "skills-home"
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", str(home))
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))
    return home


@pytest.fixture
def scratch_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory that is not a consumer repo, used as the cwd."""

    scratch = tmp_path / "scratch-cwd"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    return scratch


def _seed_stale_skill(skill_home: Path) -> Path:
    skill_path = skill_home / "lab-tracker-setup" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("customised skill", encoding="utf-8")
    return skill_path


def _backup_of(path: Path) -> Path:
    return path.with_name(path.name + ".bak-lt-update")


@_BOTH_UPDATE_CLIS
def test_update_skills_only_cli_never_scaffolds(
    main, skill_home: Path, scratch_cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skill_path = _seed_stale_skill(skill_home)

    main(["update", "--skills-only"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert skill_path.read_text(encoding="utf-8") == setup_skill_markdown()
    assert _backup_of(skill_path).read_text(encoding="utf-8") == "customised skill"
    assert payload["overwritten"] == [str(skill_path)]
    assert payload["backups"] == {str(skill_path): str(_backup_of(skill_path))}
    assert set(payload["created"]) == {
        str(skill_home / p) for p in skill_resources() if p != "lab-tracker-setup/SKILL.md"
    }
    assert payload["offers"] == []
    assert payload["warnings"] == []
    assert captured.err == ""
    assert list(scratch_cwd.iterdir()) == []
    assert not repo_registry.registry_path().exists()


@_BOTH_UPDATE_CLIS
@pytest.mark.parametrize(
    "flags",
    [["--skills-only"], ["--install-skills", "--skills-only"]],
    ids=["skills-only", "install-skills-and-skills-only"],
)
def test_update_skills_only_spellings_are_identical(
    main,
    flags: list[str],
    skill_home: Path,
    scratch_cwd: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["update", *flags])

    payload = json.loads(capsys.readouterr().out)
    skill_path = skill_home / "lab-tracker-setup" / "SKILL.md"
    assert set(payload["created"]) == {str(skill_home / p) for p in skill_resources()}
    assert skill_path.read_text(encoding="utf-8") == setup_skill_markdown()
    assert sorted(item.name for item in skill_home.iterdir()) == [
        "lab-tracker", "lab-tracker-setup", "scientific-visualization"
    ]
    assert list(scratch_cwd.iterdir()) == []


@_BOTH_UPDATE_CLIS
@pytest.mark.parametrize(
    ("extra_flags", "named_flag"),
    [
        (["--yes"], "--yes"),
        (["--target", "{target}"], "--target"),
        (["--target", "."], "--target"),
        (["--yes", "--target", "{target}"], "--yes and --target"),
    ],
    ids=["yes", "target", "current-directory-target", "both"],
)
def test_update_skills_only_rejects_contradictory_flags(
    main,
    extra_flags: list[str],
    named_flag: str,
    tmp_path: Path,
    skill_home: Path,
    scratch_cwd: Path,
) -> None:
    skill_path = _seed_stale_skill(skill_home)
    never_created = tmp_path / "never-created"
    argv = [flag.format(target=never_created) for flag in extra_flags]

    with pytest.raises(SystemExit) as excinfo:
        main(["update", "--skills-only", *argv])

    # A string SystemExit code is what makes the process exit 1 with the message.
    assert isinstance(excinfo.value.code, str)
    assert "--skills-only" in excinfo.value.code
    assert named_flag in excinfo.value.code
    assert skill_path.read_text(encoding="utf-8") == "customised skill"
    assert not _backup_of(skill_path).exists()
    assert not never_created.exists()
    assert list(scratch_cwd.iterdir()) == []


@pytest.mark.parametrize(
    ("main", "command"),
    [(lt_main, "lt"), (lab_tracker_main, "lab-tracker")],
    ids=["lt", "lab-tracker"],
)
def test_update_skills_only_conflict_names_the_command_the_docs_use(
    main, command: str, skill_home: Path, scratch_cwd: Path
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["update", "--skills-only", "--yes"])

    assert str(excinfo.value.code).startswith(f"{command} update --skills-only refreshes only")


@_BOTH_UPDATE_CLIS
def test_update_skills_only_creates_only_the_skill_directories(
    main, skill_home: Path, scratch_cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The skill home does not exist yet: installing creates it (and the skill
    # directory in it), and nothing at all under the current directory.
    assert not skill_home.exists()

    main(["update", "--skills-only"])

    capsys.readouterr()
    installed_files = {
        path.relative_to(skill_home).as_posix()
        for path in skill_home.rglob("*")
        if path.is_file()
    }
    assert installed_files == set(skill_resources())
    assert list(scratch_cwd.iterdir()) == []


@_BOTH_UPDATE_CLIS
def test_update_skills_only_resolves_a_relative_skills_home_against_the_cwd(
    main,
    monkeypatch: pytest.MonkeyPatch,
    scratch_cwd: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Why the docs say to use an absolute LAB_TRACKER_SKILLS_HOME: a relative one
    # is taken relative to the current directory, which then gets the skill.
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", "relative-skills")
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))

    main(["update", "--skills-only"])

    payload = json.loads(capsys.readouterr().out)
    skill_path = scratch_cwd / "relative-skills" / "lab-tracker-setup" / "SKILL.md"
    assert skill_path.is_file()
    assert Path(payload["created"][0]).resolve() == skill_path.resolve()


@_BOTH_UPDATE_CLIS
def test_update_skills_only_dry_run_writes_nothing(
    main, skill_home: Path, scratch_cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    skill_path = _seed_stale_skill(skill_home)

    main(["update", "--skills-only", "--dry-run"])

    payload = json.loads(capsys.readouterr().out)
    assert set(payload["diffs"]) == {str(skill_home / p) for p in skill_resources()}
    assert payload["overwritten"] == [str(skill_path)]
    assert skill_path.read_text(encoding="utf-8") == "customised skill"
    assert not _backup_of(skill_path).exists()
    assert list(scratch_cwd.iterdir()) == []
    assert not repo_registry.registry_path().exists()


@_BOTH_UPDATE_CLIS
def test_update_skills_only_ignores_malformed_base_url_env(
    main,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_home: Path,
    scratch_cwd: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "not a url")

    # Skills-only never resolves the MCP URL, so a bad one cannot break it...
    main(["update", "--skills-only"])
    assert json.loads(capsys.readouterr().out)["created"]

    # ...while a repo update, which bakes the URL into MCP configs, still fails.
    with pytest.raises(ValueError):
        main(["update", "--target", str(tmp_path / "repo")])


@_BOTH_UPDATE_CLIS
def test_update_install_skills_without_skills_only_still_refreshes_repo(
    main,
    tmp_path: Path,
    skill_home: Path,
    scratch_cwd: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "consumer"
    init_consumer_repo(repo, yes=True)
    mcp_json = repo / ".mcp.json"
    mcp_json.write_text("{}\n", encoding="utf-8")
    skill_path = _seed_stale_skill(skill_home)

    main(["update", "--install-skills", "--target", str(repo)])

    payload = json.loads(capsys.readouterr().out)
    assert str(mcp_json) in payload["overwritten"]
    assert str(mcp_json) in payload["backups"]
    assert str(skill_path) in payload["overwritten"]
    assert skill_path.read_text(encoding="utf-8") == setup_skill_markdown()
    assert list(scratch_cwd.iterdir()) == []


@_BOTH_UPDATE_CLIS
def test_update_without_target_still_defaults_to_the_current_directory(
    main, skill_home: Path, scratch_cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["update"])

    payload = json.loads(capsys.readouterr().out)
    assert str(scratch_cwd / ".mcp.json") in payload["created"]
    assert (scratch_cwd / ".mcp.json").is_file()
    assert not skill_home.exists()


def test_generated_scripts_lt_help_runs(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path)
    repo_src = Path(__file__).resolve().parent.parent / "src"

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.lt", "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(repo_src)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "Lab Tracker consumer CLI" in completed.stdout


def test_lab_tracker_init_console_entrypoints_are_packaged() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]

    assert scripts["lab_tracker"] == "lab_tracker.cli:main"
    assert scripts["lab-tracker"] == "lab_tracker.cli:main"
    assert scripts["lt"] == "lab_tracker_client.cli:main"


def _fake_seed_demo_database(project_id, calls: list[dict[str, bool]]):
    def fake_seed_demo_database(
        *,
        run_migrations: bool,
        allow_duplicates: bool,
        allow_non_local: bool,
        with_review: bool,
    ):
        calls.append(
            {
                "run_migrations": run_migrations,
                "allow_duplicates": allow_duplicates,
                "allow_non_local": allow_non_local,
                "with_review": with_review,
            }
        )
        return DemoSeedResult(
            created=True,
            project_id=project_id,
            project_name="Demo",
            question_count=1,
            dataset_count=1,
            note_count=1,
            analysis_count=1,
            claim_count=1,
            visualization_count=1,
        )

    return fake_seed_demo_database


def test_seed_demo_cli_prints_json_summary(monkeypatch, capsys) -> None:
    project_id = uuid4()
    calls: list[dict[str, bool]] = []
    monkeypatch.setattr(
        "lab_tracker.cli.seed_demo_database", _fake_seed_demo_database(project_id, calls)
    )

    lab_tracker_main(
        ["seed-demo", "--skip-migrations", "--allow-duplicates", "--allow-non-local"]
    )

    assert calls == [
        {
            "run_migrations": False,
            "allow_duplicates": True,
            "allow_non_local": True,
            "with_review": False,
        }
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is True
    assert payload["project_id"] == str(project_id)
    assert payload["question_count"] == 1
    assert payload["staged_note_count"] == 0
    assert payload["review_change_set_id"] is None


def test_seed_demo_cli_forwards_with_review(monkeypatch, capsys) -> None:
    calls: list[dict[str, bool]] = []
    monkeypatch.setattr(
        "lab_tracker.cli.seed_demo_database", _fake_seed_demo_database(uuid4(), calls)
    )

    lab_tracker_main(["seed-demo", "--skip-migrations", "--with-review"])

    assert calls == [
        {
            "run_migrations": False,
            "allow_duplicates": False,
            "allow_non_local": False,
            "with_review": True,
        }
    ]
    assert json.loads(capsys.readouterr().out)["created"] is True


def test_lt_prime_non_research_prompt_emits_nothing(capsys) -> None:
    lt_main(
        [
            "prime",
            "--if-research-facing",
            "--fail-silent",
            "--prompt",
            "please fix the import ordering",
        ]
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_serve_app_runs_migrations_schedules_browser_and_starts_server(
    monkeypatch, tmp_path: Path
) -> None:
    # Keep this hermetic: serve_app() resolves Settings from the current working
    # directory's .env plus the process environment. Run from an empty temp dir
    # (no .env) and drop any inherited overrides so the asserted defaults hold
    # regardless of a developer's local .env contents.
    monkeypatch.chdir(tmp_path)
    for var in (
        "LAB_TRACKER_DATABASE_URL",
        "LAB_TRACKER_BACKUP_PATH",
        "LAB_TRACKER_BACKUP_KEEP",
    ):
        monkeypatch.delenv(var, raising=False)

    calls: list[tuple[str, object]] = []

    def fake_backup(database_url: str, **kwargs):
        calls.append(("backup", (database_url, kwargs)))

    def fake_upgrade(config, revision):
        script_location = Path(config.get_main_option("script_location"))
        assert script_location.name == "alembic"
        assert (script_location / "env.py").exists()
        calls.append(("upgrade", revision))

    def fake_open(url: str):
        calls.append(("open", url))
        return True

    def fake_start_browser_when_ready(url: str, **kwargs):
        calls.append(
            (
                "schedule_browser",
                {
                    "opener_is_fake": kwargs["opener"] is fake_open,
                    "readiness_url": kwargs["readiness_url"],
                    "url": url,
                },
            )
        )
        return None

    def fake_runner(app_path: str, **kwargs):
        calls.append(("runner", (app_path, kwargs)))

    monkeypatch.setattr("lab_tracker.cli.create_sqlite_backup", fake_backup)
    monkeypatch.setattr("lab_tracker.cli.command.upgrade", fake_upgrade)
    monkeypatch.setattr(
        "lab_tracker.cli._start_browser_when_ready",
        fake_start_browser_when_ready,
    )

    serve_app(
        browser_opener=fake_open,
        port=8123,
        reload=True,
        server_runner=fake_runner,
    )

    assert calls[0] == (
        "backup",
        (
            "sqlite+pysqlite:///./lab_tracker.db",
            {
                "backup_dir": "~/.lab-tracker/backups",
                "keep": 10,
                "skip_missing": True,
                "skip_unsupported": True,
            },
        ),
    )
    assert calls[1] == ("upgrade", "head")
    assert calls[2] == (
        "schedule_browser",
        {
            "opener_is_fake": True,
            "readiness_url": "http://127.0.0.1:8123/health",
            "url": "http://127.0.0.1:8123/app",
        },
    )
    assert calls[3] == (
        "runner",
        ("lab_tracker.asgi:app", {"host": "127.0.0.1", "port": 8123, "reload": True}),
    )


def test_browser_opener_waits_until_readiness_probe_passes() -> None:
    probe_results = iter([False, False, True])
    opened: list[str] = []
    sleeps: list[float] = []

    result = _open_browser_when_ready(
        "http://127.0.0.1:8123/app",
        readiness_url="http://127.0.0.1:8123/health",
        opener=opened.append,
        readiness_probe=lambda _url: next(probe_results),
        poll_interval_seconds=0.1,
        max_attempts=3,
        sleep=sleeps.append,
    )

    assert result is True
    assert opened == ["http://127.0.0.1:8123/app"]
    assert sleeps == [0.1, 0.1]


def test_serve_app_can_skip_migrations_and_browser() -> None:
    calls: list[tuple[str, object]] = []

    def fake_runner(app_path: str, **kwargs):
        calls.append(("runner", (app_path, kwargs)))

    serve_app(
        host="0.0.0.0",
        insecure_allow_lan=True,
        open_browser=False,
        run_migrations=False,
        server_runner=fake_runner,
    )

    assert calls == [
        (
            "runner",
            ("lab_tracker.asgi:app", {"host": "0.0.0.0", "port": 8000, "reload": False}),
        )
    ]


def test_serve_app_refuses_lan_bind_when_auth_is_disabled(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")

    def fake_runner(app_path: str, **kwargs):
        calls.append(("runner", (app_path, kwargs)))

    with pytest.raises(SystemExit, match="Refusing to bind Lab Tracker"):
        serve_app(
            host="0.0.0.0",
            open_browser=False,
            run_migrations=False,
            server_runner=fake_runner,
        )

    assert calls == []


class _UpgradeReached(Exception):
    pass


_STRONG_TEST_SECRET = "seed-demo-guard-test-secret-0123456789abcdef"


def _record_upgrade(calls: list[str]):
    def fake_upgrade(_config, revision: str) -> None:
        calls.append(revision)
        raise _UpgradeReached

    return fake_upgrade


@pytest.mark.parametrize(
    ("environment", "auth_enabled"),
    [("production", None), ("local", "true")],
)
def test_seed_demo_refuses_non_local_or_auth_enabled_database(
    monkeypatch, environment: str, auth_enabled: str | None
) -> None:
    from lab_tracker.cli import seed_demo_database

    upgrades: list[str] = []
    monkeypatch.setattr("lab_tracker.cli.command.upgrade", _record_upgrade(upgrades))
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", environment)
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", _STRONG_TEST_SECRET)
    if auth_enabled is None:
        monkeypatch.delenv("LAB_TRACKER_AUTH_ENABLED", raising=False)
    else:
        monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", auth_enabled)

    with pytest.raises(SystemExit, match="Refusing to seed demo data"):
        seed_demo_database()

    assert upgrades == []


def test_seed_demo_allow_non_local_opts_past_the_guard(monkeypatch) -> None:
    from lab_tracker.cli import seed_demo_database

    upgrades: list[str] = []
    monkeypatch.setattr("lab_tracker.cli.command.upgrade", _record_upgrade(upgrades))
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", _STRONG_TEST_SECRET)
    monkeypatch.delenv("LAB_TRACKER_AUTH_ENABLED", raising=False)

    with pytest.raises(_UpgradeReached):
        seed_demo_database(allow_non_local=True)

    assert upgrades == ["head"]


def test_seed_demo_local_auth_disabled_database_passes_the_guard(monkeypatch) -> None:
    from lab_tracker.cli import seed_demo_database

    upgrades: list[str] = []
    monkeypatch.setattr("lab_tracker.cli.command.upgrade", _record_upgrade(upgrades))
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")

    with pytest.raises(_UpgradeReached):
        seed_demo_database()

    assert upgrades == ["head"]


def test_seed_demo_cli_refusal_exits_without_traceback(monkeypatch, capsys) -> None:
    upgrades: list[str] = []
    monkeypatch.setattr("lab_tracker.cli.command.upgrade", _record_upgrade(upgrades))
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "production")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", _STRONG_TEST_SECRET)
    monkeypatch.delenv("LAB_TRACKER_AUTH_ENABLED", raising=False)

    with pytest.raises(SystemExit, match="--allow-non-local"):
        lab_tracker_main(["seed-demo"])

    assert upgrades == []


def test_init_backs_up_file_before_repairing_unpaired_begin_marker(tmp_path: Path) -> None:
    from lab_tracker.decision_context_constants import CLAUDE_BLOCK_END

    claude = tmp_path / "CLAUDE.md"
    original = (
        f"# Mine\n\n{CLAUDE_BLOCK_BEGIN}\nstale activation\n\n"
        "## My notes after the marker\nirreplaceable user content\n"
    )
    claude.write_text(original, encoding="utf-8")

    result = init_consumer_repo(tmp_path)

    repaired = claude.read_text(encoding="utf-8")
    assert repaired.count(CLAUDE_BLOCK_BEGIN) == 1
    assert repaired.count(CLAUDE_BLOCK_END) == 1
    backup = tmp_path / "CLAUDE.md.bak-lt-update"
    assert result.backups[claude] == backup
    assert backup.read_text(encoding="utf-8") == original
    assert any(
        "unpaired" in warning and str(claude) in warning and str(backup) in warning
        for warning in result.warnings
    )


def test_uninstall_backs_up_file_before_stripping_unpaired_end_marker(tmp_path: Path) -> None:
    from lab_tracker.decision_context_constants import CLAUDE_BLOCK_END

    claude = tmp_path / "CLAUDE.md"
    original = f"# Mine\nirreplaceable user content\n{CLAUDE_BLOCK_END}\n# After\n"
    claude.write_text(original, encoding="utf-8")

    result = init_consumer_repo(tmp_path, uninstall=True)

    assert CLAUDE_BLOCK_END not in claude.read_text(encoding="utf-8")
    backup = tmp_path / "CLAUDE.md.bak-lt-update"
    assert result.backups[claude] == backup
    assert backup.read_text(encoding="utf-8") == original
    assert any("unpaired" in warning for warning in result.warnings)


def test_dry_run_reports_unpaired_marker_backup_without_writing(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    original = f"# Mine\n\n{CLAUDE_BLOCK_BEGIN}\nuser content after marker\n"
    claude.write_text(original, encoding="utf-8")

    result = init_consumer_repo(tmp_path, dry_run=True)

    backup = tmp_path / "CLAUDE.md.bak-lt-update"
    assert result.backups[claude] == backup
    assert not backup.exists()
    assert claude.read_text(encoding="utf-8") == original


def test_second_unpaired_repair_keeps_the_first_backup_of_the_original(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    original = (
        f"# Mine\n\n{CODE_CONVENTIONS_BLOCK_BEGIN}\nstale conventions\n"
        f"{CLAUDE_BLOCK_BEGIN}\nstale activation\nuser tail\n"
    )
    claude.write_text(original, encoding="utf-8")

    result = init_consumer_repo(tmp_path, yes=True)

    backup = tmp_path / "CLAUDE.md.bak-lt-update"
    assert result.backups[claude] == backup
    assert backup.read_text(encoding="utf-8") == original


# --- `lt update` converges on its own output (issue #292) ---------------------

_MANAGED_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursor/rules/lab-tracker.mdc")
_BUCKETS = ("created", "skipped", "overwritten", "up_to_date")
_OLD_VERSION_LINE = "<!-- lab-tracker-code-conventions version=0.0.1 sha256=000000000000 -->"


def _buckets(result: object, path: Path) -> list[str]:
    """Every bucket ``path`` appears in, once per appearance."""

    payload = result.as_dict()  # type: ignore[attr-defined]
    return [name for name in _BUCKETS for item in payload[name] if item == str(path)]


def _stale_activation() -> str:
    return managed_agent_activation_block().replace(
        "## Lab Tracker Knowledge Graph Consultation", "## Old consultation heading"
    )


def _stale_conventions() -> str:
    return (
        managed_code_conventions_block()
        .replace("# Lab Tracker Code-Facing Idioms", "# Old idioms heading")
        .replace(code_conventions_version_line(), _OLD_VERSION_LINE)
    )


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "apply"])
def test_update_reports_current_managed_files_up_to_date_once(
    tmp_path: Path, dry_run: bool
) -> None:
    init_consumer_repo(tmp_path, yes=True)
    before = {name: (tmp_path / name).read_text(encoding="utf-8") for name in _MANAGED_FILES}

    result = update_consumer_repo(tmp_path, dry_run=dry_run)

    for name in _MANAGED_FILES:
        assert _buckets(result, tmp_path / name) == ["up_to_date"], name
        assert (tmp_path / name).read_text(encoding="utf-8") == before[name]
    assert not result.diffs


@pytest.mark.parametrize("after_gap", ["", "\n", "\n\n"], ids=["glued", "blank", "two-blank"])
@pytest.mark.parametrize("between_gap", ["", "\n", "\n\n"], ids=["glued", "blank", "two-blank"])
@pytest.mark.parametrize("before_gap", ["", "\n"], ids=["one-blank", "two-blank"])
def test_update_replaces_only_the_managed_regions(
    tmp_path: Path, before_gap: str, between_gap: str, after_gap: str
) -> None:
    def document(activation: str, conventions: str) -> str:
        return (
            f"# My project\n\nIntro.\n{before_gap}\n{activation}{between_gap}"
            f"{conventions}{after_gap}## My notes\n\nKeep this.\n"
        )

    claude = tmp_path / "CLAUDE.md"
    claude.write_text(document(_stale_activation(), _stale_conventions()), encoding="utf-8")

    first = update_consumer_repo(tmp_path)
    refreshed = claude.read_text(encoding="utf-8")
    second = update_consumer_repo(tmp_path, dry_run=True)

    # Both blocks are refreshed; every byte around them is the user's.
    assert refreshed == document(
        managed_agent_activation_block(), managed_code_conventions_block()
    )
    assert _buckets(first, claude) == ["overwritten"]
    assert _buckets(second, claude) == ["up_to_date"]
    assert claude not in second.diffs


def test_update_writes_a_stale_file_once_and_reports_what_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_consumer_repo(tmp_path, yes=True)
    claude = tmp_path / "CLAUDE.md"
    current = claude.read_text(encoding="utf-8")
    claude.write_text(
        current.replace(managed_agent_activation_block(), _stale_activation()).replace(
            managed_code_conventions_block(), _stale_conventions()
        ),
        encoding="utf-8",
    )
    with monkeypatch.context() as patch:
        writes = _write_count(patch)
        result = update_consumer_repo(tmp_path)

    assert writes.count(claude.resolve()) == 1
    assert _buckets(result, claude) == ["overwritten"]
    assert claude.read_text(encoding="utf-8") == current
    # The applied run shows what it wrote, both blocks in one diff.
    assert "-## Old consultation heading" in result.diffs[claude]
    assert "+## Lab Tracker Knowledge Graph Consultation" in result.diffs[claude]
    assert f"-{_OLD_VERSION_LINE}" in result.diffs[claude]


def test_init_lists_a_new_file_with_both_blocks_once_as_created(tmp_path: Path) -> None:
    result = init_consumer_repo(tmp_path, yes=True, dry_run=True)

    for name in ("CLAUDE.md", "AGENTS.md"):
        assert _buckets(result, tmp_path / name) == ["created"], name


def test_applied_update_reports_how_existing_files_changed(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path, yes=True)
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text('{"hooks": {}}\n', encoding="utf-8")

    result = update_consumer_repo(tmp_path)

    assert settings in result.diffs and "lt prime" in result.diffs[settings]


def test_applied_init_reports_no_diff_for_files_it_created(tmp_path: Path) -> None:
    result = init_consumer_repo(tmp_path, yes=True)

    assert result.created
    assert not result.diffs


def test_repair_backup_is_the_original_text_not_a_planned_one(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    original = (
        f"# Mine\n\n{_stale_activation()}\n"
        f"{CODE_CONVENTIONS_BLOCK_BEGIN}\nstale conventions\nuser tail\n"
    )
    claude.write_text(original, encoding="utf-8")

    result = init_consumer_repo(tmp_path, yes=True)

    # The activation refresh is only planned when the conventions repair
    # backs the file up, so the backup is what was on disk.
    backup = tmp_path / "CLAUDE.md.bak-lt-update"
    assert result.backups[claude] == backup
    assert backup.read_text(encoding="utf-8") == original


def test_out_of_order_markers_stop_the_run_naming_the_file(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    original = f"# Mine\n{CLAUDE_BLOCK_END}\nuser text\n{CLAUDE_BLOCK_BEGIN}\nstale\n"
    claude.write_text(original, encoding="utf-8")

    with pytest.raises(ManagedBlockMarkerError, match="comes before"):
        _upsert_managed_block(original, managed_agent_activation_block())
    with pytest.raises(SystemExit, match="CLAUDE.md"):
        update_consumer_repo(tmp_path)
    with pytest.raises(SystemExit, match="CLAUDE.md"):
        init_consumer_repo(tmp_path, uninstall=True)
    assert claude.read_text(encoding="utf-8") == original
    assert not (tmp_path / "CLAUDE.md.bak-lt-update").exists()


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        # A current block is left exactly as it is.
        ("# A\n\n{block}\n## B\n", "# A\n\n{block}\n## B\n"),
        # A region that ends the file without a newline gains the block's newline.
        ("# A\n\n{stale_body}", "# A\n\n{block}"),
        # Text on END's own line is moved to the next line, not lost.
        ("{stale_body}trailing", "{block}trailing"),
    ],
    ids=["current", "end-of-file", "same-line-text"],
)
def test_upsert_splices_only_between_the_markers(existing: str, expected: str) -> None:
    block = managed_agent_activation_block()
    stale_body = _stale_activation().rstrip("\n")
    values = {"block": block, "stale_body": stale_body}

    assert _upsert_managed_block(existing.format(**values), block) == expected.format(**values)


# --- Review follow-ups: one file behind two paths, glued text, marker order ----


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target.name)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows without rights
        pytest.skip(f"cannot create symlinks here: {exc}")


def _hardlink(link: Path, target: Path) -> None:
    try:
        os.link(target, link)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - no hard links here
        pytest.skip(f"cannot create hard links here: {exc}")


def _write_count(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    writes: list[Path] = []
    write_text = Path.write_text

    def counting_write_text(self: Path, *args: object, **kwargs: object) -> int:
        writes.append(self.resolve())
        return write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", counting_write_text)
    return writes


def test_a_claude_md_symlinked_to_agents_md_keeps_every_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Team notes\n", encoding="utf-8")
    _symlink(tmp_path / "CLAUDE.md", agents)

    # A scoped patch, so conftest's isolation of the home stays in force.
    with monkeypatch.context() as patch:
        writes = _write_count(patch)
        first = init_consumer_repo(tmp_path, yes=True)
    second = update_consumer_repo(tmp_path, dry_run=True)

    content = agents.read_text(encoding="utf-8")
    assert content.startswith("# Team notes\n")
    assert content.count(CLAUDE_BLOCK_BEGIN) == 1
    assert content.count(CODE_CONVENTIONS_BLOCK_BEGIN) == 1
    assert content.count(AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN) == 1
    assert writes.count(agents.resolve()) == 1
    for name in ("CLAUDE.md", "AGENTS.md"):
        assert _buckets(first, tmp_path / name) == ["overwritten"], name
        assert _buckets(second, tmp_path / name) == ["up_to_date"], name
    assert (tmp_path / "CLAUDE.md").is_symlink()


def test_a_gemini_md_symlinked_to_agents_md_keeps_the_agents_conventions(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Team notes\n", encoding="utf-8")
    _symlink(tmp_path / "GEMINI.md", agents)

    init_consumer_repo(tmp_path, yes=True)

    content = agents.read_text(encoding="utf-8")
    assert content.count(CLAUDE_BLOCK_BEGIN) == 1
    assert content.count(AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN) == 1


def test_stale_blocks_behind_a_symlink_are_all_refreshed(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    stale_agents_conventions = (
        managed_code_conventions_block(
            begin_marker=AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN,
            end_marker=AGENTS_CODE_CONVENTIONS_BLOCK_END,
        )
        .replace("# Lab Tracker Code-Facing Idioms", "# Old idioms heading")
        .replace(code_conventions_version_line(), _OLD_VERSION_LINE)
    )
    agents.write_text(
        f"# Team notes\n\n{managed_agent_activation_block()}\n"
        f"{_stale_conventions()}\n{stale_agents_conventions}",
        encoding="utf-8",
    )
    _symlink(tmp_path / "CLAUDE.md", agents)

    update_consumer_repo(tmp_path)

    content = agents.read_text(encoding="utf-8")
    assert "# Old idioms heading" not in content
    assert _OLD_VERSION_LINE not in content
    assert content.count(CODE_CONVENTIONS_BLOCK_BEGIN) == 1
    assert content.count(AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN) == 1
    assert _doctor_exit_code(_doctor(tmp_path)) == 0


def test_text_glued_after_the_version_comment_stays_the_users(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    glued = (
        "# My project\n\nUser intro.\n\n"
        + managed_code_conventions_block().rstrip("\n")
        + managed_agent_activation_block()
        + "\n## My notes\n"
    )
    claude.write_text(glued, encoding="utf-8")

    first = update_consumer_repo(tmp_path)
    after_first = claude.read_text(encoding="utf-8")
    second = update_consumer_repo(tmp_path)

    # The activation block that followed the comment is moved to its own line,
    # never swallowed into the conventions region.
    assert after_first.count(CLAUDE_BLOCK_BEGIN) == 1
    assert after_first.count(CLAUDE_BLOCK_END) == 1
    assert "User intro." in after_first
    assert not first.backups
    assert claude.read_text(encoding="utf-8") == after_first
    assert _buckets(second, claude) == ["up_to_date"]


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "apply"])
def test_out_of_order_conventions_markers_stop_update_before_any_write(
    tmp_path: Path, dry_run: bool
) -> None:
    init_consumer_repo(tmp_path, yes=True)
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text('{"hooks": {}}\n', encoding="utf-8")
    agents = tmp_path / "AGENTS.md"
    broken = (
        f"# Mine\n{AGENTS_CODE_CONVENTIONS_BLOCK_END}\nuser text\n"
        f"{AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN}\nstale\n"
    )
    agents.write_text(broken, encoding="utf-8")

    with pytest.raises(SystemExit, match="AGENTS.md"):
        update_consumer_repo(tmp_path, dry_run=dry_run)

    # Checked before the first write: the customised scaffold file is untouched.
    assert settings.read_text(encoding="utf-8") == '{"hooks": {}}\n'
    assert not settings.with_name(settings.name + ".bak-lt-update").exists()
    assert agents.read_text(encoding="utf-8") == broken


def test_doctor_reports_out_of_order_markers_instead_of_crashing(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path, yes=True)
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(
        f"# Mine\n{CODE_CONVENTIONS_BLOCK_END}\nx\n{CODE_CONVENTIONS_BLOCK_BEGIN}\n",
        encoding="utf-8",
    )

    payload = _doctor(tmp_path)

    target = next(item for item in payload["targets"] if item["name"] == "CLAUDE.md")
    assert target["drifted"] is True
    assert str(claude) in target["error"] and "comes before" in target["error"]
    assert _doctor_exit_code(payload) == 1


def test_applied_uninstall_and_forced_init_report_what_they_changed(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path, yes=True)
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text('{"hooks": {}}\n', encoding="utf-8")

    forced = init_consumer_repo(tmp_path, force=True)
    stripped = init_consumer_repo(tmp_path, uninstall=True)

    assert "lt prime" in forced.diffs[settings]
    claude = tmp_path / "CLAUDE.md"
    assert f"-{CLAUDE_BLOCK_BEGIN}" in stripped.diffs[claude]
    assert _buckets(stripped, claude).count("overwritten") == 1


# --- Second review: plan everything before writing, one rule per comment -----


def _tree(root: Path) -> dict[str, str]:
    """Every file under ``root`` with its text."""

    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "apply"])
def test_markers_a_repair_puts_out_of_order_stop_the_run_before_any_write(
    tmp_path: Path, dry_run: bool
) -> None:
    init_consumer_repo(tmp_path, yes=True)
    (tmp_path / ".claude" / "settings.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    # In order on disk, but the repair of the lone activation BEGIN drops the
    # END that closes the conventions block, so the stray END comes first.
    (tmp_path / "CLAUDE.md").write_text(
        f"# Mine\n{CODE_CONVENTIONS_BLOCK_END}\nnote\n{CODE_CONVENTIONS_BLOCK_BEGIN}\nbody\n"
        f"{CLAUDE_BLOCK_BEGIN}\nold activation\n{CODE_CONVENTIONS_BLOCK_END}\n",
        encoding="utf-8",
    )
    before = _tree(tmp_path)

    with pytest.raises(SystemExit, match="CLAUDE.md.*comes before"):
        update_consumer_repo(tmp_path, dry_run=dry_run)

    # No scaffold refresh and no backup: nothing is written until every
    # block is planned.
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("linked", [False, True], ids=["own-file", "claude-md-linked"])
def test_a_lone_marker_repair_keeps_the_blocks_planned_before_it(
    tmp_path: Path, linked: bool
) -> None:
    agents = tmp_path / "AGENTS.md"
    original = f"# Team\n\n{AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN}\nleftover user notes\n"
    agents.write_text(original, encoding="utf-8")
    if linked:
        _symlink(tmp_path / "CLAUDE.md", agents)

    first = update_consumer_repo(tmp_path, yes=True)
    second = update_consumer_repo(tmp_path, yes=True, dry_run=True)

    # The repair keeps only the text before the lone BEGIN, where the
    # activation block (and, through the link, CLAUDE.md's conventions) went.
    content = agents.read_text(encoding="utf-8")
    assert content.count(CLAUDE_BLOCK_BEGIN) == 1
    assert content.count(AGENTS_CODE_CONVENTIONS_BLOCK_BEGIN) == 1
    assert content.count(CODE_CONVENTIONS_BLOCK_BEGIN) == int(linked)
    assert first.backups == {agents: tmp_path / "AGENTS.md.bak-lt-update"}
    assert (tmp_path / "AGENTS.md.bak-lt-update").read_text(encoding="utf-8") == original
    for name in ("CLAUDE.md", "AGENTS.md"):
        assert _buckets(second, tmp_path / name) == ["up_to_date"], name


def test_a_lone_end_repair_keeps_a_block_glued_after_the_version_comment(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "CLAUDE.md"
    lone_end = managed_code_conventions_block().replace(f"{CODE_CONVENTIONS_BLOCK_BEGIN}\n", "")
    original = (
        "# Mine\n\n"
        + lone_end.rstrip("\n")
        + managed_agent_activation_block()
        + "\n## My notes\n"
    )
    claude.write_text(original, encoding="utf-8")

    first = update_consumer_repo(tmp_path, yes=True)
    second = update_consumer_repo(tmp_path, yes=True)

    # The repair keeps what follows the comment's `-->`, so the activation
    # block stays where it was, after the conventions block and before the
    # notes; swallowing its BEGIN would move the blocks around.
    assert claude.read_text(encoding="utf-8") == (
        managed_code_conventions_block() + managed_agent_activation_block() + "\n## My notes\n"
    )
    # One repair, so the backup keeps the original text the repair dropped.
    backup = tmp_path / "CLAUDE.md.bak-lt-update"
    assert first.backups == {claude: backup}
    assert backup.read_text(encoding="utf-8") == original
    assert not second.backups and not second.warnings
    assert _buckets(second, claude) == ["up_to_date"]


def test_doctor_reads_a_version_comment_with_a_block_glued_after_it(tmp_path: Path) -> None:
    glued = managed_code_conventions_block().rstrip("\n") + managed_agent_activation_block()

    assert _extract_version_line(glued, end_marker=CODE_CONVENTIONS_BLOCK_END) == (
        code_conventions_version_line()
    )
    (tmp_path / "CLAUDE.md").write_text(f"# Mine\n\n{glued}", encoding="utf-8")
    target = next(item for item in _doctor(tmp_path)["targets"] if item["name"] == "CLAUDE.md")
    assert target["version_in_sync"] is True


@pytest.mark.parametrize("link", ["symlink", "hard-link"])
def test_uninstall_strips_a_file_behind_three_paths_as_its_preview_shows(
    tmp_path: Path, link: str
) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Team\n", encoding="utf-8")
    for name in ("CLAUDE.md", "GEMINI.md"):
        if link == "symlink":
            _symlink(tmp_path / name, agents)
        else:
            _hardlink(tmp_path / name, agents)
    init_consumer_repo(tmp_path, yes=True)

    preview = init_consumer_repo(tmp_path, uninstall=True, dry_run=True)
    applied = init_consumer_repo(tmp_path, uninstall=True)

    assert agents.read_text(encoding="utf-8") == "# Team\n"
    for name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        path = tmp_path / name
        # Every path to the file shows the whole change, and the preview is
        # what was applied.
        assert applied.diffs[path] == preview.diffs[path], name
        for marker in (CLAUDE_BLOCK_BEGIN, CODE_CONVENTIONS_BLOCK_BEGIN):
            assert f"-{marker}" in applied.diffs[path], name
        for result in (preview, applied):
            assert _buckets(result, path) == ["overwritten"], name
            assert result.stripped.count(path) == 1, name


def test_uninstall_skips_a_missing_file_once(tmp_path: Path) -> None:
    result = init_consumer_repo(tmp_path, uninstall=True)

    for name in _MANAGED_FILES:
        assert _buckets(result, tmp_path / name) == ["skipped"], name
    assert not result.stripped


def test_a_symlink_loop_stops_init_and_update_before_any_write(tmp_path: Path) -> None:
    _symlink(tmp_path / "CLAUDE.md", tmp_path / "GEMINI.md")
    _symlink(tmp_path / "GEMINI.md", tmp_path / "CLAUDE.md")

    for run in (init_consumer_repo, update_consumer_repo):
        with pytest.raises(SystemExit, match="CLAUDE.md"):
            run(tmp_path)
    uninstalled = init_consumer_repo(tmp_path, uninstall=True)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["CLAUDE.md", "GEMINI.md"]
    assert tmp_path / "CLAUDE.md" in uninstalled.skipped


def test_a_version_comment_after_the_activation_block_is_left_alone(tmp_path: Path) -> None:
    # Only the conventions blocks write a version comment. Taking one after
    # the activation END as part of the block would drop one per pass, so a
    # run of them would keep the plan moving.
    claude = tmp_path / "CLAUDE.md"
    strays = "\n".join([_OLD_VERSION_LINE] * 12)
    original = f"# Mine\n\n{managed_agent_activation_block()}{strays}\n"
    claude.write_text(original, encoding="utf-8")

    result = update_consumer_repo(tmp_path)

    assert _buckets(result, claude) == ["up_to_date"]
    assert claude.read_text(encoding="utf-8") == original


_HIDDEN_BY_A_REPAIR = (
    # The conventions markers are out of order, but repairing the lone
    # activation BEGIN before them would drop both.
    f"# Mine\n{CLAUDE_BLOCK_BEGIN}\nold activation\n"
    f"{CODE_CONVENTIONS_BLOCK_END}\nnote\n{CODE_CONVENTIONS_BLOCK_BEGIN}\nbody\n"
)


@pytest.mark.parametrize(
    "run",
    [
        lambda root: init_consumer_repo(root, yes=True),
        lambda root: update_consumer_repo(root, yes=True),
        lambda root: update_consumer_repo(root, yes=True, install_skills=True),
        lambda root: init_consumer_repo(root, uninstall=True),
    ],
    ids=["init-yes", "update-yes", "update-skills", "uninstall"],
)
def test_markers_out_of_order_on_disk_stop_every_run_a_repair_could_hide_them_from(
    tmp_path: Path, run: object
) -> None:
    (tmp_path / "CLAUDE.md").write_text(_HIDDEN_BY_A_REPAIR, encoding="utf-8")
    skills_home = Path(os.environ["LAB_TRACKER_SKILLS_HOME"])
    before = _tree(tmp_path)

    with pytest.raises(SystemExit, match="CLAUDE.md.*comes before"):
        run(tmp_path)  # type: ignore[operator]

    # In step with `lt doctor`, which reports this file: nothing is written.
    assert _tree(tmp_path) == before
    assert not skills_home.exists() or not any(skills_home.iterdir())
    assert any(error["name"] == "CLAUDE.md" for error in _doctor(tmp_path)["errors"])


@pytest.mark.usefixtures("offline_server")
def test_doctor_reports_activation_markers_lt_update_refuses(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path, yes=True)
    gemini = tmp_path / "GEMINI.md"
    gemini.write_text(f"# Mine\n{CLAUDE_BLOCK_END}\nx\n{CLAUDE_BLOCK_BEGIN}\n", encoding="utf-8")

    payload = _doctor(tmp_path)

    with pytest.raises(SystemExit, match="GEMINI.md"):
        update_consumer_repo(tmp_path, dry_run=True)
    [error] = payload["errors"]
    assert error["name"] == "GEMINI.md" and str(gemini) in error["error"]
    assert "by hand" in payload["suggestion"] and "GEMINI.md" in payload["suggestion"]
    # The conventions blocks are in sync; the refusal alone fails the check.
    assert not any(target["drifted"] for target in payload["targets"])
    assert _doctor_exit_code(payload) == 1
    with pytest.raises(SystemExit) as lt_doctor:
        lt_main(["doctor", "--target", str(tmp_path)])
    assert lt_doctor.value.code == 1


@pytest.mark.parametrize("link", ["symlink", "hard-link", "dangling", "reverse-symlink"])
def test_a_managed_file_that_is_a_scaffold_file_stops_the_run(tmp_path: Path, link: str) -> None:
    agents = tmp_path / "AGENTS.md"
    sidecar = tmp_path / "AGENTS.lt.md"
    if link == "dangling":
        _symlink(agents, sidecar)  # before init writes the sidecar
    else:
        init_consumer_repo(tmp_path)
        if link == "reverse-symlink":
            sidecar.unlink()
            _symlink(sidecar, agents)
        elif link == "symlink":
            agents.unlink()
            _symlink(agents, sidecar)
        else:
            agents.unlink()
            _hardlink(agents, sidecar)
    before = _tree(tmp_path)

    # Refreshing AGENTS.lt.md would drop the activation block that adding it
    # back puts in AGENTS.lt.md, so no run could leave both current.
    for run in (
        init_consumer_repo,
        lambda root: init_consumer_repo(root, force=True, yes=True),
        update_consumer_repo,
    ):
        with pytest.raises(SystemExit, match="AGENTS.md: the same file as .*AGENTS.lt.md"):
            run(tmp_path)

    assert _tree(tmp_path) == before
    [error] = _doctor(tmp_path)["errors"]
    assert error["name"] == "AGENTS.md"


def test_the_scaffold_paths_checked_are_the_ones_init_writes(tmp_path: Path) -> None:
    from lab_tracker.cli import _SCAFFOLD_PATHS

    result = init_consumer_repo(tmp_path)

    created = {path.relative_to(tmp_path).as_posix() for path in result.created}
    assert created - set(_MANAGED_FILES) == set(_SCAFFOLD_PATHS)


def test_alternating_stray_end_markers_settle_in_one_run(tmp_path: Path) -> None:
    # Each lone-END repair keeps only what follows the END, dropping the block
    # the other repair just placed, so every pair of strays costs a pass.
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(
        "# Mine\n\n"
        + "".join(
            f"{CLAUDE_BLOCK_END}\nnote {i}\n{CODE_CONVENTIONS_BLOCK_END}\nmore {i}\n"
            for i in range(12)
        ),
        encoding="utf-8",
    )

    first = update_consumer_repo(tmp_path, yes=True)
    second = update_consumer_repo(tmp_path, yes=True, dry_run=True)

    content = claude.read_text(encoding="utf-8")
    assert content.count(CLAUDE_BLOCK_BEGIN) == content.count(CLAUDE_BLOCK_END) == 1
    assert content.count(CODE_CONVENTIONS_BLOCK_BEGIN) == 1
    assert claude in first.backups
    assert _buckets(second, claude) == ["up_to_date"]


def test_planning_that_never_settles_stops_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import itertools

    import lab_tracker.cli as cli

    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Mine\n", encoding="utf-8")
    plan = cli._plan_managed_block
    ticks = itertools.count()

    def restless(block: object, *, result: object, dry_run: bool) -> None:
        plan(block, result=result, dry_run=dry_run)  # type: ignore[arg-type]
        result._preview_contents[claude] += f"<!-- {next(ticks)} -->"  # type: ignore[attr-defined]

    monkeypatch.setattr(cli, "_plan_managed_block", restless)
    before = _tree(tmp_path)

    with pytest.raises(SystemExit, match="CLAUDE.md: its Lab Tracker markers did not settle"):
        update_consumer_repo(tmp_path)

    assert _tree(tmp_path) == before


def test_doctor_reports_what_only_planning_lt_update_finds(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path, yes=True)
    claude = tmp_path / "CLAUDE.md"
    # Each pair is in order on disk, but repairing the lone activation BEGIN
    # drops the END that closes the conventions block.
    claude.write_text(
        f"# Mine\n{CODE_CONVENTIONS_BLOCK_END}\nnote\n{CODE_CONVENTIONS_BLOCK_BEGIN}\nbody\n"
        f"{CLAUDE_BLOCK_BEGIN}\nold activation\n{CODE_CONVENTIONS_BLOCK_END}\n",
        encoding="utf-8",
    )

    payload = _doctor(tmp_path)

    with pytest.raises(SystemExit) as refused:
        update_consumer_repo(tmp_path, dry_run=True)
    [error] = payload["errors"]
    assert error == {"name": "CLAUDE.md", "error": str(refused.value.code)}
    assert "by hand" in payload["suggestion"] and "refreshes them" not in payload["suggestion"]
    assert _doctor_exit_code(payload) == 1


def test_uninstall_removes_every_copy_of_a_block_in_one_run(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    block = managed_agent_activation_block()
    claude.write_text(f"# Mine\n\n{block}\nmiddle\n\n{block}\n## Notes\n", encoding="utf-8")

    first = init_consumer_repo(tmp_path, uninstall=True)
    second = init_consumer_repo(tmp_path, uninstall=True)

    content = claude.read_text(encoding="utf-8")
    assert CLAUDE_BLOCK_BEGIN not in content and CLAUDE_BLOCK_END not in content
    assert "middle" in content and "## Notes" in content
    assert _buckets(first, claude) == ["overwritten"]
    assert _buckets(second, claude) == ["skipped"]


def test_a_refused_uninstall_leaves_the_installed_skills_alone(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path, install_skills=True)
    skills_home = Path(os.environ["LAB_TRACKER_SKILLS_HOME"])
    (tmp_path / "CLAUDE.md").write_text(_HIDDEN_BY_A_REPAIR, encoding="utf-8")
    skills = _tree(skills_home)
    repo = _tree(tmp_path)
    assert skills

    with pytest.raises(SystemExit, match="CLAUDE.md.*comes before"):
        init_consumer_repo(tmp_path, uninstall=True, install_skills=True)

    assert _tree(skills_home) == skills
    assert _tree(tmp_path) == repo


def test_uninstall_lists_only_the_files_it_strips(tmp_path: Path) -> None:
    init_consumer_repo(tmp_path)
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Mine, with no Lab Tracker blocks\n", encoding="utf-8")

    result = init_consumer_repo(tmp_path, uninstall=True)

    assert claude not in result.stripped
    assert _buckets(result, claude) == ["skipped"]
    assert result.stripped == [tmp_path / "AGENTS.md", tmp_path / "GEMINI.md"]


def test_a_lone_end_repair_keeps_text_glued_after_the_version_comment(tmp_path: Path) -> None:
    claude = tmp_path / "CLAUDE.md"
    lone_end = managed_code_conventions_block().replace(f"{CODE_CONVENTIONS_BLOCK_BEGIN}\n", "")
    claude.write_text(
        "# Mine\n\n" + lone_end.rstrip("\n") + " my note\n## More\n", encoding="utf-8"
    )

    update_consumer_repo(tmp_path, yes=True)

    content = claude.read_text(encoding="utf-8")
    assert content.startswith(managed_code_conventions_block() + "my note\n## More\n")


def test_plain_init_refuses_a_repair_that_would_strand_conventions_markers(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "CLAUDE.md"
    # Init without --yes plans no conventions block, but repairing the lone
    # activation BEGIN would drop the END that closes it.
    claude.write_text(
        f"# Mine\n{CODE_CONVENTIONS_BLOCK_END}\nnote\n{CODE_CONVENTIONS_BLOCK_BEGIN}\nbody\n"
        f"{CLAUDE_BLOCK_BEGIN}\nold activation\n{CODE_CONVENTIONS_BLOCK_END}\n",
        encoding="utf-8",
    )
    before = _tree(tmp_path)

    with pytest.raises(SystemExit, match="CLAUDE.md.*comes before"):
        init_consumer_repo(tmp_path)

    assert _tree(tmp_path) == before


def test_plain_init_leaves_conventions_markers_it_does_not_manage_alone(
    tmp_path: Path,
) -> None:
    claude = tmp_path / "CLAUDE.md"
    misordered = f"# Mine\n{CODE_CONVENTIONS_BLOCK_END}\nx\n{CODE_CONVENTIONS_BLOCK_BEGIN}\n"
    claude.write_text(misordered, encoding="utf-8")

    init_consumer_repo(tmp_path)

    assert claude.read_text(encoding="utf-8").startswith(misordered)
    assert CLAUDE_BLOCK_BEGIN in claude.read_text(encoding="utf-8")
    assert [error["name"] for error in _doctor(tmp_path)["errors"]] == ["CLAUDE.md"]


@pytest.mark.usefixtures("offline_server")
def test_a_file_that_is_not_utf8_is_named_not_a_traceback(tmp_path: Path) -> None:
    from lab_tracker_client import setup as setup_helpers

    init_consumer_repo(tmp_path, yes=True)
    gemini = tmp_path / "GEMINI.md"
    gemini.write_bytes(b"# caf\xe9\n")

    payload = _doctor(tmp_path)
    status = setup_helpers.setup_status(tmp_path)

    with pytest.raises(SystemExit, match="GEMINI.md: cannot be read as UTF-8 text"):
        update_consumer_repo(tmp_path, dry_run=True)
    [error] = payload["errors"]
    assert error["name"] == "GEMINI.md" and "UTF-8" in str(error["error"])
    assert _doctor_exit_code(payload) == 1
    # Setup status keeps its conventions report and names the refusal.
    assert len(status["repo"]["conventions"]) == 3
    assert status["repo"]["update_refusals"] == payload["errors"]
