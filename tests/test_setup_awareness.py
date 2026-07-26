"""Tests for the agent-awareness loop: setup guide, skill install, doctor
drift semantics, status suggestions/--brief, and the SessionStart hook."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab_tracker.cli import _doctor, init_consumer_repo, update_consumer_repo
from lab_tracker.decision_context_constants import (
    MCP_SERVER_INSTRUCTIONS,
    code_conventions_version_line,
    managed_code_conventions_block,
)
from lab_tracker.mcp_api_client import lab_tracker_unavailable
from lab_tracker.mcp_tools.resources import lab_tracker_setup_guide
from lab_tracker.setup_guide import (
    SETUP_GUIDE_BEGIN_MARKER,
    SETUP_GUIDE_END_MARKER,
    setup_guide_markdown,
    setup_skill_markdown,
)
from lab_tracker_client import cli as lt_cli
from lab_tracker_client import setup as setup_helpers


@pytest.fixture
def isolated_homes(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-home"))
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", str(tmp_path / "skills-home"))
    for name in ("LAB_TRACKER_BASE_URL", "LAB_TRACKER_MCP_BASE_URL", "LAB_TRACKER_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def default_agent_home(tmp_path, monkeypatch):
    """Isolate the default Claude+Codex homes without repurposing HOME."""

    agent_home = tmp_path / "agent-home"
    monkeypatch.delenv("LAB_TRACKER_SKILLS_HOME", raising=False)
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-home"))
    monkeypatch.setattr(
        Path,
        "home",
        classmethod(lambda _path_cls: agent_home),
    )
    for name in ("LAB_TRACKER_BASE_URL", "LAB_TRACKER_MCP_BASE_URL", "LAB_TRACKER_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return agent_home


def test_repo_owned_setup_skill_matches_generator() -> None:
    skill_path = Path("skills/lab-tracker-setup/SKILL.md")
    assert skill_path.read_text(encoding="utf-8") == setup_skill_markdown()


def test_setup_skill_embeds_guide_between_markers() -> None:
    skill = setup_skill_markdown()
    assert SETUP_GUIDE_BEGIN_MARKER in skill
    assert SETUP_GUIDE_END_MARKER in skill
    section = skill.split(SETUP_GUIDE_BEGIN_MARKER, 1)[1].split(SETUP_GUIDE_END_MARKER, 1)[0]
    assert section.strip() == setup_guide_markdown().strip()


def test_setup_skill_allows_only_read_only_commands() -> None:
    skill = setup_skill_markdown()
    allowed = next(
        line for line in skill.splitlines() if line.startswith("allowed-tools:")
    )
    # Pre-approving all of `lt` would silently delete the harness permission
    # prompt — the second consent gate — for mutating commands.
    assert "Bash(lt:*)" not in allowed
    assert "lt setup status" in allowed
    assert "--yes" not in allowed


def test_setup_guide_states_consent_rules_non_imperatively() -> None:
    guide = setup_guide_markdown()
    assert "read-only" in guide
    assert "`--dry-run`" in guide
    assert "`--yes`" in guide
    assert "never relayed through an agent" in guide
    lowered = guide.lower()
    for forbidden in ("pip install", "subprocess", "curl "):
        assert forbidden not in lowered


def test_mcp_surface_points_at_setup_guide() -> None:
    assert lab_tracker_setup_guide() == setup_guide_markdown()
    assert "lab-tracker://setup-guide" in MCP_SERVER_INSTRUCTIONS
    envelope = lab_tracker_unavailable("lab_tracker_get_decision_context")
    assert envelope["next_action"]["action"] == "proceed_without_graph_context"
    assert "lab-tracker://setup-guide" in envelope["next_action"]["reason"]


def test_install_skills_renders_refreshes_and_uninstalls(isolated_homes) -> None:
    repo = isolated_homes / "repo"
    skill_path = isolated_homes / "skills-home" / "lab-tracker-setup" / "SKILL.md"

    result = init_consumer_repo(repo)
    assert not skill_path.exists()
    assert any("--install-skills" in offer for offer in result.offers)

    install_result = init_consumer_repo(repo, install_skills=True)
    assert skill_path.read_text(encoding="utf-8") == setup_skill_markdown()
    assert [path for path in install_result.created if path.name == "SKILL.md"] == [
        skill_path
    ]

    # Refresh path: a stale copy is rewritten with the original backed up.
    skill_path.write_text("stale text", encoding="utf-8")
    result = update_consumer_repo(repo, install_skills=True)
    assert skill_path.read_text(encoding="utf-8") == setup_skill_markdown()
    backup = skill_path.with_name(skill_path.name + ".bak-lt-update")
    assert backup.read_text(encoding="utf-8") == "stale text"

    result = init_consumer_repo(repo, uninstall=True, install_skills=True)
    assert not skill_path.exists()
    assert any("SKILL.md" in str(path) for path in result.stripped)


def test_default_install_skills_manages_claude_and_codex_targets(
    default_agent_home,
) -> None:
    repo = default_agent_home.parent / "repo"
    skill_paths = {
        "claude": (
            default_agent_home
            / ".claude"
            / "skills"
            / "lab-tracker-setup"
            / "SKILL.md"
        ),
        "codex": (
            default_agent_home
            / ".agents"
            / "skills"
            / "lab-tracker-setup"
            / "SKILL.md"
        ),
    }

    installed = init_consumer_repo(repo, install_skills=True)
    assert set(skill_paths.values()).issubset(set(installed.created))
    for path in skill_paths.values():
        assert path.read_text(encoding="utf-8") == setup_skill_markdown()

    # Each customized target gets its own refresh backup.
    for name, path in skill_paths.items():
        path.write_text(f"{name} customized skill", encoding="utf-8")
    refreshed = update_consumer_repo(repo, install_skills=True)
    for name, path in skill_paths.items():
        backup = path.with_name(path.name + ".bak-lt-update")
        assert path.read_text(encoding="utf-8") == setup_skill_markdown()
        assert backup.read_text(encoding="utf-8") == f"{name} customized skill"
        assert refreshed.backups[path] == backup

    preview = init_consumer_repo(
        repo,
        uninstall=True,
        install_skills=True,
        dry_run=True,
    )
    for path in skill_paths.values():
        assert path.exists()
        assert path in preview.stripped
        assert path in preview.diffs

    removed = init_consumer_repo(repo, uninstall=True, install_skills=True)
    for path in skill_paths.values():
        backup = path.with_name(path.name + ".bak-lt-update")
        assert not path.exists()
        assert not backup.exists()
        assert not path.parent.exists()
        assert path in removed.stripped


def test_version_only_skill_difference_is_not_stale_and_never_churns_backups(
    isolated_homes, monkeypatch, capsys
) -> None:
    repo = isolated_homes / "repo-version"
    skill_path = isolated_homes / "skills-home" / "lab-tracker-setup" / "SKILL.md"
    init_consumer_repo(repo, install_skills=True)

    # Simulate a package bump with unchanged text: only the version token in
    # the trailing line differs.
    content = skill_path.read_text(encoding="utf-8")
    import re as _re

    bumped = _re.sub(r"version=[^ ]+", "version=999.0.0", content)
    assert bumped != content
    skill_path.write_text(bumped, encoding="utf-8", newline="\n")

    # Status must not cry wolf (mirrors doctor's content-only drift).
    monkeypatch.chdir(repo)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")
    lt_cli.main(["setup", "status", "--target", str(repo)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["skills"]["up_to_date"] is True
    assert payload["skills"]["version_in_sync"] is False
    assert [target["name"] for target in payload["skills"]["targets"]] == ["custom"]
    assert payload["skills"]["all_installed"] is True
    assert payload["skills"]["all_up_to_date"] is True
    assert payload["skills"]["all_version_in_sync"] is False
    assert not any("skill is stale" in item for item in payload["suggestions"])

    # A meaningful customization is preserved in the backup slot...
    skill_path.write_text("my customized copy", encoding="utf-8")
    update_consumer_repo(repo, install_skills=True)
    backup = skill_path.with_name(skill_path.name + ".bak-lt-update")
    assert backup.read_text(encoding="utf-8") == "my customized copy"

    # ...and a later version-only refresh must NOT clobber that backup.
    content = skill_path.read_text(encoding="utf-8")
    skill_path.write_text(
        _re.sub(r"version=[^ ]+", "version=999.1.0", content),
        encoding="utf-8",
        newline="\n",
    )
    update_consumer_repo(repo, install_skills=True)
    assert backup.read_text(encoding="utf-8") == "my customized copy"
    assert skill_path.read_text(encoding="utf-8") == setup_skill_markdown()


def test_uninstall_removes_skill_backup_too(isolated_homes) -> None:
    repo = isolated_homes / "repo-uninstall"
    skill_path = isolated_homes / "skills-home" / "lab-tracker-setup" / "SKILL.md"
    init_consumer_repo(repo, install_skills=True)
    skill_path.write_text("customized", encoding="utf-8")
    update_consumer_repo(repo, install_skills=True)
    backup = skill_path.with_name(skill_path.name + ".bak-lt-update")
    assert backup.exists()

    init_consumer_repo(repo, uninstall=True, install_skills=True)
    assert not skill_path.exists()
    assert not backup.exists()
    assert not skill_path.parent.exists()


def test_missing_lt_ids_still_suggests_project_bind(
    isolated_homes, monkeypatch, capsys
) -> None:
    repo = isolated_homes / "repo-noids"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")
    lt_cli.main(["setup", "status", "--target", str(repo)])
    payload = json.loads(capsys.readouterr().out)
    assert any("lt project bind" in item for item in payload["suggestions"])


def test_install_skills_dry_run_writes_nothing(isolated_homes) -> None:
    repo = isolated_homes / "repo-dry"
    skill_path = isolated_homes / "skills-home" / "lab-tracker-setup" / "SKILL.md"
    result = init_consumer_repo(repo, install_skills=True, dry_run=True)
    assert not skill_path.exists()
    assert any("SKILL.md" in str(path) for path in result.diffs)


def test_default_install_skills_dry_run_reports_both_targets(
    default_agent_home,
) -> None:
    repo = default_agent_home.parent / "repo-dry-default"
    expected = {
        default_agent_home / ".claude" / "skills" / "lab-tracker-setup" / "SKILL.md",
        default_agent_home / ".agents" / "skills" / "lab-tracker-setup" / "SKILL.md",
    }

    result = init_consumer_repo(repo, install_skills=True, dry_run=True)

    assert expected.issubset(set(result.created))
    assert expected.issubset(set(result.diffs))
    assert all(not path.exists() for path in expected)


def test_setup_status_exposes_and_checks_each_default_skill_target(
    default_agent_home,
    monkeypatch,
) -> None:
    repo = default_agent_home.parent / "repo-status-targets"
    repo.mkdir()
    claude_path = (
        default_agent_home / ".claude" / "skills" / "lab-tracker-setup" / "SKILL.md"
    )
    codex_path = (
        default_agent_home / ".agents" / "skills" / "lab-tracker-setup" / "SKILL.md"
    )
    claude_path.parent.mkdir(parents=True)
    claude_path.write_text(setup_skill_markdown(), encoding="utf-8")
    monkeypatch.setattr(setup_helpers, "probe_health", lambda _url: True)

    partial = setup_helpers.setup_status(repo)
    skills = partial["skills"]
    assert skills["path"] == str(claude_path)
    assert skills["installed"] is True
    assert skills["up_to_date"] is True
    assert [target["name"] for target in skills["targets"]] == ["claude", "codex"]
    assert skills["targets"][1] == {
        "name": "codex",
        "path": str(codex_path),
        "installed": False,
    }
    assert skills["all_installed"] is False
    assert skills["all_up_to_date"] is False
    assert any(
        "missing from: codex" in suggestion
        for suggestion in partial["suggestions"]
    )

    codex_path.parent.mkdir(parents=True)
    codex_path.write_text("custom stale skill", encoding="utf-8")
    stale = setup_helpers.setup_status(repo)
    assert stale["skills"]["all_installed"] is True
    assert stale["skills"]["all_up_to_date"] is False
    assert stale["skills"]["targets"][1]["up_to_date"] is False
    assert any(
        "skills are stale" in suggestion
        for suggestion in stale["suggestions"]
    )

    update_consumer_repo(repo, install_skills=True)
    healthy = setup_helpers.setup_status(repo)
    assert healthy["skills"]["all_installed"] is True
    assert healthy["skills"]["all_up_to_date"] is True
    assert healthy["skills"]["all_version_in_sync"] is True
    assert not any(
        "lab-tracker-setup skill" in suggestion
        for suggestion in healthy["suggestions"]
    )


def test_doctor_content_only_drift(tmp_path) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)

    # A version-line-only difference (package bump, same text) must be clean.
    claude = repo / "CLAUDE.md"
    content = claude.read_text(encoding="utf-8")
    current_line = code_conventions_version_line()
    stale_line = current_line.replace("version=", "version=0.0.0-stale-")
    claude.write_text(content.replace(current_line, stale_line), encoding="utf-8")
    payload = _doctor(repo)
    target = next(item for item in payload["targets"] if item["name"] == "CLAUDE.md")
    assert target["version_in_sync"] is False
    assert target["drifted"] is False
    assert "suggestion" not in payload

    # Changed body text IS drift, and the suggestion names lt update.
    block = managed_code_conventions_block()
    claude.write_text(
        claude.read_text(encoding="utf-8").replace(
            "first-line", "first-line (edited)"
        ),
        encoding="utf-8",
    )
    assert "first-line (edited)" in claude.read_text(encoding="utf-8")
    payload = _doctor(repo)
    target = next(item for item in payload["targets"] if item["name"] == "CLAUDE.md")
    assert target["drifted"] is True
    assert "lt update" in payload["suggestion"]
    assert block  # silence unused warning paths


def test_scaffolded_settings_include_session_start_status_hook(tmp_path) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo)
    settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    session_start = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert session_start == "lt setup status --brief --fail-silent"
    prompt_submit = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert prompt_submit == "lt prime --if-research-facing --fail-silent --limit 5"


def test_status_suggestions_and_brief(isolated_homes, monkeypatch, capsys) -> None:
    repo = isolated_homes / "consumer"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")

    lt_cli.main(["setup", "status", "--target", str(repo)])
    payload = json.loads(capsys.readouterr().out)
    suggestions = payload["suggestions"]
    assert any("lt setup init" in item for item in suggestions)
    assert any("lt watch add" in item for item in suggestions)
    assert payload["skills"]["installed"] is False

    lt_cli.main(["setup", "status", "--target", str(repo), "--brief"])
    brief = json.loads(capsys.readouterr().out)
    assert set(brief) == {"command", "brief", "suggestions"}
    assert brief["brief"].startswith("lab-tracker:")
    assert "suggestion(s)" in brief["brief"]


def test_status_brief_healthy_is_one_line(isolated_homes, monkeypatch, capsys) -> None:
    repo = isolated_homes / "consumer-healthy"
    init_consumer_repo(repo, yes=True, install_skills=True)
    (repo / "lt_ids.json").write_text(
        json.dumps({"project_id": "p-1", "project_name": "demo"}), encoding="utf-8"
    )
    config_path = repo / ".lab-tracker" / "watch.json"
    monkeypatch.chdir(repo)
    lt_cli.main(["watch", "add", "results", "--config", str(config_path)])
    capsys.readouterr()
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")

    lt_cli.main(["setup", "status", "--target", str(repo), "--brief"])
    brief = json.loads(capsys.readouterr().out)
    # Not a git repo and no hook: no hook suggestions; everything else is
    # configured, so brief reports a healthy line.
    assert brief["suggestions"] == []
    assert brief["brief"].startswith("lab-tracker: capture is configured")
