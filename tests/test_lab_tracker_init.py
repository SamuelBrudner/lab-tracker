from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import tomllib

from lab_tracker.cli import (
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
    CODE_CONVENTIONS_BLOCK_BEGIN,
    CODE_CONVENTIONS_BLOCK_END,
    code_conventions_version_line,
    code_facing_idioms,
    managed_code_conventions_block,
)
from lab_tracker.demo_seed import DemoSeedResult
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


def test_seed_demo_cli_prints_json_summary(monkeypatch, capsys) -> None:
    project_id = uuid4()
    calls: list[dict[str, bool]] = []

    def fake_seed_demo_database(*, run_migrations: bool, allow_duplicates: bool):
        calls.append(
            {
                "run_migrations": run_migrations,
                "allow_duplicates": allow_duplicates,
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

    monkeypatch.setattr("lab_tracker.cli.seed_demo_database", fake_seed_demo_database)

    lab_tracker_main(["seed-demo", "--skip-migrations", "--allow-duplicates"])

    assert calls == [{"run_migrations": False, "allow_duplicates": True}]
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is True
    assert payload["project_id"] == str(project_id)
    assert payload["question_count"] == 1


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
