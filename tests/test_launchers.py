from __future__ import annotations

from pathlib import Path


def test_macos_launcher_explains_uv_prerequisite_before_exiting() -> None:
    launcher = (
        Path(__file__).resolve().parent.parent
        / "launchers"
        / "Start Lab Tracker.command"
    )
    script = launcher.read_text(encoding="utf-8")

    assert "command -v uv" in script
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in script
    assert "right-click it, choose Open" in script
    assert "exec python3 -m lab_tracker serve" not in script
