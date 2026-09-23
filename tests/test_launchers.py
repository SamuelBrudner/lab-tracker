from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_macos_launcher_explains_uv_prerequisite_before_exiting() -> None:
    launcher = REPO_ROOT / "deploy" / "launchers" / "Start Lab Tracker.command"
    script = launcher.read_text(encoding="utf-8")

    assert 'cd "$(dirname "$0")/../.."' in script
    assert "command -v uv" in script
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in script
    assert "right-click it, choose Open" in script
    assert "exec python3 -m lab_tracker serve" not in script


def test_windows_launcher_resolves_the_repository_root() -> None:
    launcher = REPO_ROOT / "deploy" / "launchers" / "Start Lab Tracker.bat"
    script = launcher.read_text(encoding="utf-8")

    assert 'cd /d "%~dp0\\..\\.."' in script


def _batch_lines_inside_blocks(script: str) -> list[str]:
    """Return lines cmd.exe parses as part of an enclosing ``( ... )`` block."""
    inside: list[str] = []
    depth = 0
    for raw_line in script.splitlines():
        line = raw_line.strip()
        unquoted = "".join(line.split('"')[::2])
        if depth > 0:
            inside.append(line)
        depth += unquoted.count("(") - unquoted.count(")")
        assert depth >= 0, raw_line
    assert depth == 0
    return inside


def test_windows_launcher_reports_the_serve_exit_status() -> None:
    # cmd.exe expands %VAR% when it parses a whole ( ... ) block, before any
    # command in it runs, so an in-block `exit /b %ERRORLEVEL%` returns the
    # pre-serve value (0) and hides every failure.
    launcher = REPO_ROOT / "deploy" / "launchers" / "Start Lab Tracker.bat"
    script = launcher.read_text(encoding="utf-8")
    lines = [line.strip() for line in script.splitlines()]

    inside_blocks = _batch_lines_inside_blocks(script)
    assert not [line for line in inside_blocks if "%ERRORLEVEL%" in line.upper()]
    assert 'set "status=%ERRORLEVEL%"' in lines
    assert 'set "status=%ERRORLEVEL%"' not in inside_blocks
    assert lines[-1] == "exit /b %status%"


def test_windows_launcher_keeps_the_console_open_on_failure() -> None:
    launcher = REPO_ROOT / "deploy" / "launchers" / "Start Lab Tracker.bat"
    script = launcher.read_text(encoding="utf-8")

    inside_blocks = _batch_lines_inside_blocks(script)
    assert "pause" in inside_blocks
    assert 'if not "%status%"=="0" (' in script
    assert "https://docs.astral.sh/uv/" in script
