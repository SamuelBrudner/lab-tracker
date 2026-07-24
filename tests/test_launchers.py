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
