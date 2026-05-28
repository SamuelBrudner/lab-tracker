from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import tomllib

from lab_tracker.cli import init_consumer_repo


def test_init_creates_portable_consumer_files(tmp_path: Path) -> None:
    result = init_consumer_repo(tmp_path, project_name="Consumer Project")

    assert sorted(path.relative_to(tmp_path).as_posix() for path in result.created) == [
        ".mcp.json",
        "AGENTS.lt.md",
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


def test_init_skips_existing_files_without_force(tmp_path: Path) -> None:
    custom = tmp_path / ".mcp.json"
    custom.write_text('{"custom": true}\n', encoding="utf-8")

    result = init_consumer_repo(tmp_path)

    assert custom in result.skipped
    assert custom.read_text(encoding="utf-8") == '{"custom": true}\n'


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
