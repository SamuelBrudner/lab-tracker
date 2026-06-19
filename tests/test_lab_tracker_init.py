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
    _open_browser_when_ready,
    init_consumer_repo,
    serve_app,
)
from lab_tracker.cli import (
    main as lab_tracker_main,
)
from lab_tracker.demo_seed import DemoSeedResult
from lab_tracker_client.cli import main as lt_main


def test_init_creates_portable_consumer_files(tmp_path: Path) -> None:
    result = init_consumer_repo(tmp_path, project_name="Consumer Project")

    assert sorted(path.relative_to(tmp_path).as_posix() for path in result.created) == [
        ".claude/settings.json",
        ".mcp.json",
        "AGENTS.lt.md",
        "CLAUDE.md",
        "lt_ids.json",
        "scripts/lt.py",
    ]
    mcp_config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp_config["mcpServers"]["lab-tracker"]
    assert server["command"] == "lt-mcp"
    assert "python.exe" not in json.dumps(mcp_config).lower()
    assert "C:\\" not in json.dumps(mcp_config)

    shim = (tmp_path / "scripts" / "lt.py").read_text(encoding="utf-8")
    assert "from lab_tracker_client import *" in shim
    assert "from lab_tracker_client.cli import main" in shim

    ids = json.loads((tmp_path / "lt_ids.json").read_text(encoding="utf-8"))
    assert ids == {"project_id": "", "project_name": "Consumer Project"}
    assert "first non-blank line" in (tmp_path / "AGENTS.lt.md").read_text(encoding="utf-8")
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


def test_serve_app_runs_migrations_schedules_browser_and_starts_server(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

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

    assert calls[0] == ("upgrade", "head")
    assert calls[1] == (
        "schedule_browser",
        {
            "opener_is_fake": True,
            "readiness_url": "http://127.0.0.1:8123/health",
            "url": "http://127.0.0.1:8123/app",
        },
    )
    assert calls[2] == (
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
