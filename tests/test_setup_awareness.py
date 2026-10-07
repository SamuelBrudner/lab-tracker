"""Tests for the agent-awareness loop: setup guide, skill install, doctor
drift semantics, status suggestions/--brief, and the SessionStart hook."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

import httpx
import pytest

from lab_tracker.cli import (
    InitResult,
    _doctor,
    init_consumer_repo,
    refresh_setup_skills,
    update_consumer_repo,
)
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
from lab_tracker.skill_bundle import skill_resources
from lab_tracker_client import cli as lt_cli
from lab_tracker_client import registry as repo_registry
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


def test_setup_guide_names_the_clients_that_setup_never_registers() -> None:
    guide = " ".join(setup_guide_markdown().split())
    # Claude Code is the only client whose config the scaffold writes; the rest
    # register in user-level settings, and the per-client steps live in the docs.
    assert "asks the person to approve the server on first run" in guide
    for client in ("Claude Desktop chat", "Codex in the ChatGPT desktop app", "Codex CLI"):
        assert client in guide, client
    assert "setup never writes" in guide
    assert "docs/agent-setup.md" in guide
    # A GUI client needs the absolute lt-mcp path, and only an in-client read
    # proves what that client launched.
    assert "--command <absolute path>" in guide
    assert "lab_tracker_list_projects" in guide


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
    assert skill_path in install_result.created
    assert (skill_path.parent.parent / "lab-tracker" / "SKILL.md") in install_result.created

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
    monkeypatch.setattr(setup_helpers, "probe_health_diagnostics", lambda _url: {"reachable": True})

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


def _default_skill_paths(agent_home: Path) -> dict[str, Path]:
    return {
        "claude": agent_home / ".claude" / "skills" / "lab-tracker-setup" / "SKILL.md",
        "codex": agent_home / ".agents" / "skills" / "lab-tracker-setup" / "SKILL.md",
    }


def _write_skill(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _suggestion_containing(suggestions: list[str], marker: str) -> str:
    return next(item for item in suggestions if marker in item)


def _assert_skills_only_suggestion(suggestion: str) -> None:
    assert "`lt update --skills-only`" in suggestion
    assert "leaves this repository's files alone" in suggestion
    # The pre-fix commands also scaffolded ten files into the working directory.
    assert "--install-skills" not in suggestion
    assert "lt setup init" not in suggestion


def test_skill_suggestions_name_the_skills_only_command(
    default_agent_home,
    monkeypatch,
) -> None:
    repo = default_agent_home.parent / "repo-skill-suggestions"
    repo.mkdir()
    monkeypatch.setattr(
        setup_helpers, "probe_health_diagnostics", lambda _url: {"reachable": True}
    )
    paths = _default_skill_paths(default_agent_home)
    _write_skill(paths["claude"], setup_skill_markdown())

    missing = _suggestion_containing(
        setup_helpers.setup_status(repo)["suggestions"], "missing from: codex"
    )
    _assert_skills_only_suggestion(missing)

    _write_skill(paths["codex"], "custom stale skill")
    stale_status = setup_helpers.setup_status(repo)
    stale = _suggestion_containing(stale_status["suggestions"], "skills are stale")
    _assert_skills_only_suggestion(stale)

    # Status payloads that predate the per-agent target list take the legacy branch.
    legacy_status = {**stale_status, "skills": {"installed": True, "up_to_date": False}}
    legacy = _suggestion_containing(
        setup_helpers._suggestions(legacy_status), "skill is stale"
    )
    _assert_skills_only_suggestion(legacy)


@pytest.mark.parametrize("state", ["stale", "missing"])
def test_suggested_skill_fix_command_runs_verbatim_and_leaves_cwd_untouched(
    default_agent_home,
    monkeypatch,
    state: str,
) -> None:
    scratch = default_agent_home.parent / "not-a-consumer-repo"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    monkeypatch.setattr(
        setup_helpers, "probe_health_diagnostics", lambda _url: {"reachable": True}
    )
    paths = _default_skill_paths(default_agent_home)
    if state == "stale":
        for name, path in paths.items():
            _write_skill(path, f"{name} customised skill")
        marker = "skills are stale"
    else:
        _write_skill(paths["claude"], setup_skill_markdown())
        marker = "missing from: codex"

    suggestion = _suggestion_containing(
        setup_helpers.setup_status(scratch)["suggestions"], marker
    )
    command = re.search(r"`(lt [^`]+)`", suggestion)
    assert command is not None, suggestion
    argv = shlex.split(command.group(1))
    assert argv[0] == "lt"

    lt_cli.main(argv[1:])

    for path in paths.values():
        assert path.read_text(encoding="utf-8") == setup_skill_markdown()
    if state == "stale":
        for name, path in paths.items():
            backup = path.with_name(path.name + ".bak-lt-update")
            assert backup.read_text(encoding="utf-8") == f"{name} customised skill"
    # The whole point of the fix: nothing lands in the directory it ran from.
    assert list(scratch.iterdir()) == []
    assert not repo_registry.registry_path().exists()
    healthy = setup_helpers.setup_status(scratch)
    assert healthy["skills"]["all_up_to_date"] is True
    assert not any(
        "lab-tracker-setup skill" in item for item in healthy["suggestions"]
    )


def test_refresh_setup_skills_touches_nothing_but_the_skill_homes(
    default_agent_home,
    monkeypatch,
) -> None:
    scratch = default_agent_home.parent / "scratch-cwd"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    paths = _default_skill_paths(default_agent_home)

    created = refresh_setup_skills()
    assert set(created.created) == {
        path.parent.parent / p for path in paths.values() for p in skill_resources()
    }
    for path in paths.values():
        assert path.read_text(encoding="utf-8") == setup_skill_markdown()

    # Each customised target gets its own refresh backup.
    for name, path in paths.items():
        path.write_text(f"{name} customised skill", encoding="utf-8")
    refreshed = refresh_setup_skills()
    assert set(refreshed.overwritten) == set(paths.values())
    for name, path in paths.items():
        backup = path.with_name(path.name + ".bak-lt-update")
        assert path.read_text(encoding="utf-8") == setup_skill_markdown()
        assert backup.read_text(encoding="utf-8") == f"{name} customised skill"
        assert refreshed.backups[path] == backup

    assert set(refresh_setup_skills().up_to_date) == {
        path.parent.parent / p for path in paths.values() for p in skill_resources()
    }
    assert set(refreshed.as_dict()) == set(InitResult().as_dict())
    assert refreshed.offers == []
    assert refreshed.warnings == []
    assert list(scratch.iterdir()) == []
    assert not repo_registry.registry_path().exists()
    assert sorted(item.name for item in default_agent_home.iterdir()) == [
        ".agents",
        ".claude",
    ]


def test_refresh_setup_skills_dry_run_writes_nothing(
    default_agent_home,
    monkeypatch,
) -> None:
    scratch = default_agent_home.parent / "scratch-cwd-dry"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    paths = _default_skill_paths(default_agent_home)
    _write_skill(paths["claude"], "stale claude skill")

    result = refresh_setup_skills(dry_run=True)

    assert set(result.diffs) == {
        path.parent.parent / p for path in paths.values() for p in skill_resources()
    }
    assert paths["claude"].read_text(encoding="utf-8") == "stale claude skill"
    assert not paths["claude"].with_name("SKILL.md.bak-lt-update").exists()
    assert paths["claude"] in result.backups
    assert not (default_agent_home / ".agents").exists()
    assert list(scratch.iterdir()) == []


def test_setup_guide_documents_skills_only() -> None:
    guide = " ".join(setup_guide_markdown().split())
    assert "`lt update --skills-only`" in guide
    assert "current directory" in guide


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


SERVER_REVISION = "b" * 40


@pytest.fixture
def broken_mcp_install(tmp_path, monkeypatch):
    """Reproduce GH #214: the resolved ``mcp`` release lacks a server module."""

    site = tmp_path / "broken-site"
    site.mkdir()
    (site / "broken_lt_mcp_server.py").write_text(
        "from mcp.server.fastmcp_removed_upstream import FastMCP  # noqa: F401\n",
        encoding="utf-8",
    )
    # The check imports in a child interpreter, which sees only PYTHONPATH.
    python_path = [str(site), *filter(None, [os.environ.get("PYTHONPATH")])]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(python_path))
    monkeypatch.setattr(setup_helpers, "_MCP_SERVER_MODULE", "broken_lt_mcp_server")


def _healthy_status_repo(isolated_homes, monkeypatch, name: str) -> Path:
    repo = isolated_homes / name
    init_consumer_repo(repo, yes=True, install_skills=True)
    (repo / "lt_ids.json").write_text(
        json.dumps({"project_id": "p-1", "project_name": "demo"}), encoding="utf-8"
    )
    monkeypatch.chdir(repo)
    lt_cli.main(["watch", "add", "results", "--config", str(repo / ".lab-tracker" / "watch.json")])
    # An env-pinned URL keeps the "record your server URL" suggestion quiet.
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")
    return repo


def _server_reports_release(monkeypatch, version: str, revision: str = SERVER_REVISION) -> None:
    monkeypatch.setattr(
        setup_helpers,
        "probe_health_diagnostics",
        lambda _url: {"reachable": True, "release": {"version": version, "revision": revision}},
    )


@pytest.mark.usefixtures("offline_server")
def test_doctor_reports_that_lt_mcp_can_start(tmp_path, capsys) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)

    lt_cli.main(["doctor", "--target", str(repo)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["lt_mcp"]["importable"] is True
    assert payload["lt_mcp"]["module"] == "lab_tracker.mcp_server"
    assert "error" not in payload["lt_mcp"]


@pytest.mark.usefixtures("offline_server")
def test_doctor_fails_loudly_when_lt_mcp_cannot_import(
    tmp_path, broken_mcp_install, capsys
) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)

    with pytest.raises(SystemExit) as excinfo:
        lt_cli.main(["doctor", "--target", str(repo)])
    payload = json.loads(capsys.readouterr().out)

    assert excinfo.value.code == 1
    lt_mcp = payload["lt_mcp"]
    assert lt_mcp["importable"] is False
    assert lt_mcp["error"].startswith("ModuleNotFoundError:")
    assert "mcp.server.fastmcp_removed_upstream" in lt_mcp["error"]
    assert "broken_lt_mcp_server.py" in lt_mcp["traceback"]
    assert "Agents page" in lt_mcp["next_step"]
    # Managed-block drift is unaffected: only the install is broken.
    assert not any(target["drifted"] for target in payload["targets"])

    # Prompt hooks stay silent, exactly as they do for drift.
    lt_cli.main(["doctor", "--target", str(repo), "--fail-silent"])
    assert capsys.readouterr().out == ""


@pytest.mark.usefixtures("offline_server")
def test_doctor_all_checks_the_install_once_per_sweep(broken_mcp_install, capsys) -> None:
    with pytest.raises(SystemExit):
        lt_cli.main(["doctor", "--all"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "doctor-all"
    assert payload["repos"] == []
    assert payload["lt_mcp"]["importable"] is False


def _installed_release(monkeypatch, version: str) -> None:
    monkeypatch.setattr(
        setup_helpers,
        "installed_release",
        lambda: setup_helpers.ReleaseIdentity(version=version, revision="a" * 40),
    )


def _doctor_payload(repo, capsys, *extra: str) -> dict:
    lt_cli.main(["doctor", "--target", str(repo), *extra])
    return json.loads(capsys.readouterr().out)


def test_doctor_warns_when_the_client_is_a_patch_release_behind(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)
    _installed_release(monkeypatch, "0.4.0")
    _server_reports_release(monkeypatch, "0.4.1")

    payload = _doctor_payload(repo, capsys)  # a warning must not raise SystemExit

    assert payload["server"]["reachable"] is True
    assert payload["client"]["status"] == "behind"
    assert payload["client"]["update_recommended"] is True
    [warning] = payload["warnings"]
    assert "(release 0.4.0) is behind its server (release 0.4.1)" in warning
    assert f"lab-tracker.git@{SERVER_REVISION}" in warning
    assert not any(target["drifted"] for target in payload["targets"])


def test_doctor_is_quiet_when_the_client_is_current(tmp_path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)
    _installed_release(monkeypatch, "0.4.0")
    _server_reports_release(monkeypatch, "0.4.0", revision="c" * 40)  # same release, new commit

    payload = _doctor_payload(repo, capsys)

    assert payload["client"]["status"] == "current"
    assert payload["warnings"] == []


def test_doctor_tries_the_server_but_only_warns_when_it_cannot_connect(
    tmp_path, offline_server, capsys
) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)

    payload = _doctor_payload(repo, capsys)  # no SystemExit: an unreachable server is soft

    assert payload["server"]["reachable"] is False
    assert payload["client"]["status"] == "unknown"
    assert payload["client"]["update_recommended"] is False
    [warning] = payload["warnings"]
    assert payload["server"]["base_url"] in warning
    assert "tcp_connection_failed" in warning
    assert "release check was skipped" in warning
    assert "Check the server address and port." in warning
    # The warning is data, so prompt hooks that keep drift output still get it.
    assert _doctor_payload(repo, capsys, "--fail-silent")["warnings"] == payload["warnings"]


def test_doctor_reports_an_http_error_from_the_server_as_a_warning(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)
    monkeypatch.setattr(
        setup_helpers,
        "probe_health_diagnostics",
        lambda _url: {
            "reachable": True,
            "diagnosis": "http_error",
            "status_code": 404,
            "detail": "HTTP connection succeeded; server returned HTTP 404.",
            "next_step": "Check the URL, access requirements, and application or proxy logs.",
        },
    )

    [warning] = _doctor_payload(repo, capsys)["warnings"]

    assert "http_error" in warning
    assert "server returned HTTP 404" in warning


def test_doctor_warns_when_the_releases_cannot_be_compared(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)
    _installed_release(monkeypatch, "0.4.0")
    monkeypatch.setattr(setup_helpers, "probe_health_diagnostics", lambda _url: {"reachable": True})

    payload = _doctor_payload(repo, capsys)

    assert payload["client"]["status"] == "unknown"
    [warning] = payload["warnings"]
    assert "release (0.4.0) with the server's (unknown)" in warning


def test_doctor_survives_a_malformed_server_address(tmp_path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "not a url")

    payload = _doctor_payload(repo, capsys)

    assert payload["server"]["base_url"] is None
    assert payload["server"]["diagnosis"] == "server_check_failed"
    [warning] = payload["warnings"]
    assert "server_check_failed" in warning
    assert "LAB_TRACKER_BASE_URL" in warning


def test_doctor_survives_a_probe_that_raises(tmp_path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)

    def explode(_url: str) -> dict:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(setup_helpers, "probe_health_diagnostics", explode)

    [warning] = _doctor_payload(repo, capsys)["warnings"]

    assert "server_check_failed" in warning
    assert "RuntimeError: probe exploded" in warning


def test_doctor_exit_code_ignores_warnings_but_not_a_broken_install(
    tmp_path, broken_mcp_install, offline_server, capsys
) -> None:
    repo = tmp_path / "repo"
    init_consumer_repo(repo, yes=True)

    with pytest.raises(SystemExit) as excinfo:
        lt_cli.main(["doctor", "--target", str(repo)])
    payload = json.loads(capsys.readouterr().out)

    assert excinfo.value.code == 1  # the install decides the exit code...
    assert payload["lt_mcp"]["importable"] is False
    assert payload["warnings"]  # ...and the soft warning is still reported


def test_doctor_all_checks_the_server_once_per_sweep(tmp_path, monkeypatch, capsys) -> None:
    repos = [tmp_path / "one", tmp_path / "two"]
    for repo in repos:
        init_consumer_repo(repo, yes=True)
    monkeypatch.setattr(
        repo_registry, "list_repos", lambda: [{"root": str(r), "actions": []} for r in repos]
    )
    _installed_release(monkeypatch, "0.4.0")
    probes: list[str] = []

    def probe(url: str) -> dict:
        probes.append(url)
        return {"reachable": True, "release": {"version": "0.4.2", "revision": SERVER_REVISION}}

    monkeypatch.setattr(setup_helpers, "probe_health_diagnostics", probe)

    lt_cli.main(["doctor", "--all"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "doctor-all"
    assert len(payload["repos"]) == 2
    assert len(probes) == 1
    assert payload["client"]["update_recommended"] is True
    assert len(payload["warnings"]) == 1


def test_status_suggests_reinstalling_a_broken_lt_mcp_first(
    isolated_homes, broken_mcp_install, monkeypatch, capsys
) -> None:
    repo = _healthy_status_repo(isolated_homes, monkeypatch, "consumer-broken-mcp")
    capsys.readouterr()

    lt_cli.main(["setup", "status", "--target", str(repo), "--brief"])
    brief = json.loads(capsys.readouterr().out)

    assert len(brief["suggestions"]) == 1
    assert brief["suggestions"][0].startswith("lt-mcp cannot start in this environment")
    assert "ModuleNotFoundError" in brief["brief"]


def test_status_names_a_client_behind_the_server_release(
    isolated_homes, monkeypatch, capsys
) -> None:
    repo = _healthy_status_repo(isolated_homes, monkeypatch, "consumer-behind")
    capsys.readouterr()
    _server_reports_release(monkeypatch, "99.0.0")

    payload = setup_helpers.setup_status(repo)

    assert payload["client"]["status"] == "behind"
    assert payload["client"]["client_behind_server"] is True
    assert payload["client"]["server"] == {"version": "99.0.0", "revision": SERVER_REVISION}
    [suggestion] = payload["suggestions"]
    assert suggestion.startswith("This lab-tracker client (release ")
    assert "is behind its server (release 99.0.0)" in suggestion
    assert f"lab-tracker.git@{SERVER_REVISION}" in suggestion
    assert "`lt update`" in suggestion

    lt_cli.main(["setup", "status", "--target", str(repo), "--brief"])
    brief = json.loads(capsys.readouterr().out)
    assert set(brief) == {"command", "brief", "suggestions"}
    assert "is behind its server" in brief["brief"]


@pytest.mark.parametrize("server_version", ["0.0.1", "0+unknown", None])
def test_status_stays_quiet_without_a_newer_server_release(
    isolated_homes, monkeypatch, server_version
) -> None:
    repo = _healthy_status_repo(isolated_homes, monkeypatch, "consumer-quiet")
    _server_reports_release(monkeypatch, server_version)

    payload = setup_helpers.setup_status(repo)

    assert payload["client"]["client_behind_server"] is False
    assert payload["suggestions"] == []


@pytest.mark.parametrize("unreadable_side", ["server", "client"])
def test_status_treats_an_oversized_release_as_unreadable(
    isolated_homes, monkeypatch, capsys, unreadable_side
) -> None:
    # A version past CPython's integer-string limit (4300 digits) made int()
    # raise, and --fail-silent then dropped the whole status.
    oversized = "9" * 5000
    repo = _healthy_status_repo(isolated_homes, monkeypatch, "consumer-oversized")
    client_version = oversized if unreadable_side == "client" else "0.4.0"
    monkeypatch.setattr(
        setup_helpers,
        "installed_release",
        lambda: setup_helpers.ReleaseIdentity(version=client_version, revision="a" * 40),
    )
    _server_reports_release(monkeypatch, oversized if unreadable_side == "server" else "99.0.0")
    capsys.readouterr()

    payload = setup_helpers.setup_status(repo)

    assert payload["client"]["status"] == "unknown"
    assert payload["client"]["client_behind_server"] is False
    assert payload["client"]["update_recommended"] is False
    assert payload["suggestions"] == []
    # --fail-silent turns any crash into empty output, so a printed line proves
    # the hook's status survived.
    lt_cli.main(["setup", "status", "--target", str(repo), "--brief", "--fail-silent"])
    brief = json.loads(capsys.readouterr().out)
    assert brief["brief"] == "lab-tracker: capture is configured; server reachable."


def test_status_suggests_the_update_for_a_patch_release_gap(isolated_homes, monkeypatch) -> None:
    # A PATCH release is where a fix for a broken install lands (docs/versioning.md).
    repo = _healthy_status_repo(isolated_homes, monkeypatch, "consumer-patch")
    monkeypatch.setattr(
        setup_helpers,
        "installed_release",
        lambda: setup_helpers.ReleaseIdentity(version="0.4.0", revision="a" * 40),
    )
    _server_reports_release(monkeypatch, "0.4.3")

    payload = setup_helpers.setup_status(repo)

    assert payload["client"]["status"] == "behind"
    assert payload["client"]["client_behind_server"] is True
    assert payload["client"]["update_recommended"] is True
    [suggestion] = payload["suggestions"]
    assert "(release 0.4.0) is behind its server (release 0.4.3)" in suggestion


def test_status_compares_the_release_the_health_probe_reads(
    isolated_homes, monkeypatch
) -> None:
    repo = _healthy_status_repo(isolated_homes, monkeypatch, "consumer-probe")
    monkeypatch.setattr(
        setup_helpers,
        "installed_release",
        lambda: setup_helpers.ReleaseIdentity(version="0.4.0", revision="a" * 40),
    )

    def send(_self, request, **_kwargs):
        return httpx.Response(
            200,
            json={"app": {"version": "0.5.0", "source_revision": SERVER_REVISION}},
            request=request,
        )

    monkeypatch.setattr(httpx.Client, "send", send)

    payload = setup_helpers.setup_status(repo)

    assert payload["client"]["status"] == "behind"
    assert payload["client"]["update_recommended"] is True
    assert payload["client"]["server"] == {"version": "0.5.0", "revision": SERVER_REVISION}
    [suggestion] = payload["suggestions"]
    assert f"lab-tracker.git@{SERVER_REVISION}" in suggestion


def test_status_reports_revision_drift_without_nagging(isolated_homes, monkeypatch) -> None:
    repo = _healthy_status_repo(isolated_homes, monkeypatch, "consumer-drift")
    monkeypatch.setattr(
        setup_helpers,
        "installed_release",
        lambda: setup_helpers.ReleaseIdentity(version="0.1.0", revision="a" * 40),
    )
    _server_reports_release(monkeypatch, "0.1.0")

    payload = setup_helpers.setup_status(repo)

    assert payload["client"]["status"] == "current"
    assert payload["client"]["same_revision"] is False
    assert payload["suggestions"] == []
