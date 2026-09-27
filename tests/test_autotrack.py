"""Opt-in matplotlib autotrack: hook install/remove, suppression, kill switch."""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest

import lab_tracker_client.figure as figure_module
import lab_tracker_client.figure_autotrack as autotrack_module
from lab_tracker_client import autotrack, capture_figures, is_autotracking, savefig
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.figure import _reset_figure_capture_state_for_tests


class FakeFigure:
    """Stands in for matplotlib.figure.Figure; savefig writes the payload."""

    def __init__(self, payload: bytes = b"figure-bytes") -> None:
        self.payload = payload

    def savefig(self, fname, *args, **kwargs):  # noqa: ANN001, ARG002
        if isinstance(fname, (str, Path)):
            Path(fname).write_bytes(self.payload)
        else:
            fname.write(self.payload)
        return "saved"


@pytest.fixture
def fake_matplotlib(monkeypatch):
    """Inject a minimal matplotlib.figure module so the hook can patch it."""

    matplotlib = types.ModuleType("matplotlib")
    figure = types.ModuleType("matplotlib.figure")
    figure.Figure = FakeFigure
    matplotlib.figure = figure
    monkeypatch.setitem(sys.modules, "matplotlib", matplotlib)
    monkeypatch.setitem(sys.modules, "matplotlib.figure", figure)
    monkeypatch.delenv("LAB_TRACKER_AUTOTRACK", raising=False)
    monkeypatch.delenv("LAB_TRACKER_SESSION_ID", raising=False)
    _reset_figure_capture_state_for_tests()
    yield figure
    autotrack(False)
    _reset_figure_capture_state_for_tests()


@pytest.fixture
def captured(monkeypatch):
    """Record captures instead of talking to a server."""

    calls: list[dict] = []

    def fake_capture(**kwargs):
        calls.append(kwargs)
        return figure_module.FigureCaptureResult(
            action="imported",
            path=str(kwargs["path"]),
            source_external_id="figure:x",
            source_uri="file:///x",
            content_hash="0" * 64,
            metadata={},
            client_capture_id="figure:x",
        )

    monkeypatch.setattr(figure_module, "_capture_saved_figure", fake_capture)
    return calls


def test_autotrack_captures_path_saves_once_and_can_be_removed(
    fake_matplotlib, captured, tmp_path: Path
) -> None:
    assert is_autotracking() is False
    assert autotrack() is True
    assert is_autotracking() is True
    assert autotrack() is True  # idempotent: still one hook

    fig = FakeFigure()
    assert fig.savefig(tmp_path / "plot.png") == "saved"
    assert (tmp_path / "plot.png").read_bytes() == b"figure-bytes"
    assert len(captured) == 1
    assert captured[0]["path"] == tmp_path / "plot.png"
    assert captured[0]["metadata"] == {"figure_autotracked": True}

    # Saves to file objects and to non-figure suffixes are left alone.
    fig.savefig(io.BytesIO())
    fig.savefig(tmp_path / "table.csv")
    assert len(captured) == 1

    assert autotrack(False) is False
    assert is_autotracking() is False
    fig.savefig(tmp_path / "later.png")
    assert len(captured) == 1
    assert fake_matplotlib.Figure.savefig is FakeFigure.savefig


def test_explicit_helpers_suppress_the_hook_so_nothing_is_captured_twice(
    fake_matplotlib, captured, tmp_path: Path
) -> None:
    autotrack()
    savefig(FakeFigure(b"explicit"), tmp_path / "explicit.png")
    assert len(captured) == 1  # the explicit call's own capture only

    with capture_figures(tmp_path):
        FakeFigure(b"inside").savefig(tmp_path / "inside.png")
    # capture_figures reports the new file itself; the hook stayed quiet.
    assert [Path(call["path"]).name for call in captured] == ["explicit.png", "inside.png"]


def test_kill_switch_and_missing_matplotlib_leave_no_hook(
    fake_matplotlib, captured, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LAB_TRACKER_AUTOTRACK", "0")
    assert autotrack() is False
    assert is_autotracking() is False
    FakeFigure().savefig(tmp_path / "plot.png")
    assert captured == []

    monkeypatch.delenv("LAB_TRACKER_AUTOTRACK")
    monkeypatch.setitem(sys.modules, "matplotlib.figure", None)
    assert autotrack() is False


def test_hook_options_reach_the_capture(fake_matplotlib, captured, tmp_path: Path) -> None:
    autotrack(patterns=("*.pdf",), project_id="project-7", metadata={"rig": "2"})
    FakeFigure().savefig(tmp_path / "ignored.png")
    FakeFigure().savefig(tmp_path / "kept.pdf")
    assert len(captured) == 1
    assert captured[0]["project_id"] == "project-7"
    assert captured[0]["metadata"] == {"rig": "2", "figure_autotracked": True}


def test_setup_autotrack_manages_the_ipython_startup_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("IPYTHONDIR", str(tmp_path / "ipython"))
    monkeypatch.delenv("LAB_TRACKER_AUTOTRACK", raising=False)
    startup = autotrack_module.ipython_startup_path()

    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["setup", "autotrack"])
    lt_cli.main(["setup", "autotrack", "--dry-run"])
    preview = json.loads(capsys.readouterr().out)
    assert preview["action"] == "would-install"
    assert not startup.exists()

    lt_cli.main(["setup", "autotrack", "--yes"])
    installed = json.loads(capsys.readouterr().out)
    assert installed["action"] == "installed"
    assert Path(installed["startup_file"]) == startup
    content = startup.read_text(encoding="utf-8")
    assert "lab_tracker_client.autotrack()" in content
    assert autotrack_module.IPYTHON_STARTUP_BEGIN in content
    assert autotrack_module.ipython_startup_status()["installed"] is True

    lt_cli.main(["setup", "autotrack", "--yes"])
    assert json.loads(capsys.readouterr().out)["action"] == "current"

    lt_cli.main(["setup", "autotrack", "--yes", "--uninstall"])
    assert json.loads(capsys.readouterr().out)["action"] == "removed"
    assert not startup.exists()
    lt_cli.main(["setup", "autotrack", "--yes", "--uninstall"])
    assert json.loads(capsys.readouterr().out)["action"] == "absent"
