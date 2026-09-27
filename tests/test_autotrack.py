"""Opt-in matplotlib autotrack: hook install/remove, suppression, kill switch."""

from __future__ import annotations

import argparse
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


def _recording_client(uploads: list[str]):
    import httpx

    from lab_tracker_client import LabTracker

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        chunk = body.split(b'name="project_id"', 1)[1].split(b"\r\n\r\n", 1)[1]
        uploads.append(chunk.split(b"\r\n--", 1)[0].decode("utf-8"))
        return httpx.Response(201, json={"data": {"note_id": "note-1", "metadata": {}}})

    return LabTracker(
        base_url="http://testserver",
        default_project_id="project-default",
        transport=httpx.MockTransport(handler),
    )


def _git_repo(path: Path) -> Path:
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    return path


@pytest.fixture
def isolated_capture_env(monkeypatch, tmp_path: Path) -> Path:
    for key in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_CAPTURE_OUTBOX",
        "LAB_TRACKER_SESSION_CONTEXT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))
    outbox = tmp_path / "outbox"
    monkeypatch.setenv("LAB_TRACKER_WATCH_OUTBOX", str(outbox))
    return outbox


def test_autotrack_skips_saves_whose_project_would_only_be_a_default(
    fake_matplotlib, isolated_capture_env: Path, tmp_path: Path, capsys
) -> None:
    """A user-wide hook must never file a notebook's figures into the
    profile's default project: outside a bound checkout nothing is sent or
    queued, and the scientist is told why once."""

    uploads: list[str] = []
    with _recording_client(uploads) as lt:
        autotrack(client=lt)
        FakeFigure(b"one").savefig(tmp_path / "one.png")
        FakeFigure(b"two").savefig(tmp_path / "two.png")

    assert uploads == []
    assert not isolated_capture_env.exists()
    err = capsys.readouterr().err
    assert err.count("autotrack is not capturing saves") == 1
    assert "is not inside a git checkout" in err
    assert "Nothing was sent or queued" in err
    # Outside a checkout there is nothing for `lt project bind` to bind.
    assert "lt project bind" not in err
    assert "LAB_TRACKER_PROJECT_ID" in err


def test_autotrack_tells_an_unbound_checkout_to_bind_itself(
    fake_matplotlib, isolated_capture_env: Path, tmp_path: Path, capsys
) -> None:
    """Inside a git checkout without lt_ids.json, binding the checkout is the fix."""

    checkout = _git_repo(tmp_path / "analysis")
    uploads: list[str] = []
    with _recording_client(uploads) as lt:
        autotrack(client=lt)
        FakeFigure().savefig(checkout / "fig.png")

    assert uploads == []
    err = capsys.readouterr().err
    assert "not bound to a project (no lt_ids.json)" in err
    assert "lt project bind" in err


def test_autotrack_names_each_unbound_checkout_root_once(
    fake_matplotlib, isolated_capture_env: Path, tmp_path: Path, capsys
) -> None:
    """The notice is keyed on the checkout, not the reason: every unbound
    checkout (its root, not the save's subfolder) is named exactly once, and
    a save outside any repository names its own directory."""

    first = _git_repo(tmp_path / "first")
    second = _git_repo(tmp_path / "second")
    loose = tmp_path / "loose"
    for folder in (first / "figs" / "a", first / "figs" / "b", second / "out", loose):
        folder.mkdir(parents=True)
    uploads: list[str] = []
    with _recording_client(uploads) as lt:
        autotrack(client=lt)
        FakeFigure().savefig(first / "figs" / "a" / "one.png")
        FakeFigure().savefig(first / "figs" / "b" / "two.png")
        FakeFigure().savefig(second / "out" / "three.png")
        FakeFigure().savefig(loose / "four.png")
        FakeFigure().savefig(loose / "five.png")

    assert uploads == []
    err_lines = capsys.readouterr().err.splitlines()
    notices = [line for line in err_lines if "autotrack is not capturing" in line]
    assert len(notices) == 3
    assert f"saves in {first.resolve()}:" in notices[0]
    assert f"saves in {second.resolve()}:" in notices[1]
    assert f"saves in {loose.resolve()}:" in notices[2]


def test_autotrack_skips_a_checkout_bound_only_by_its_watch_config(
    fake_matplotlib, isolated_capture_env: Path, tmp_path: Path, capsys
) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / ".lab-tracker").mkdir()
    (repo / ".lab-tracker" / "watch.json").write_text(
        json.dumps({"version": 1, "project_id": "project-watch", "watches": []})
    )
    uploads: list[str] = []
    with _recording_client(uploads) as lt:
        autotrack(client=lt)
        FakeFigure().savefig(repo / "plot.png")

    assert uploads == []
    assert "only in its watch config" in capsys.readouterr().err


def test_autotrack_captures_into_the_checkout_binding_not_the_default(
    fake_matplotlib, isolated_capture_env: Path, tmp_path: Path
) -> None:
    repo = _git_repo(tmp_path / "repo")
    (repo / "lt_ids.json").write_text(json.dumps({"project_id": "project-checkout"}))
    uploads: list[str] = []
    with _recording_client(uploads) as lt:
        autotrack(client=lt)
        FakeFigure().savefig(repo / "plot.png")

    assert uploads == ["project-checkout"]


def test_autotrack_captures_into_an_explicit_or_environment_project(
    fake_matplotlib, isolated_capture_env: Path, tmp_path: Path, monkeypatch
) -> None:
    uploads: list[str] = []
    with _recording_client(uploads) as lt:
        autotrack(client=lt, project_id="project-explicit")
        FakeFigure(b"explicit").savefig(tmp_path / "explicit.png")
        monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", "project-env")
        autotrack(client=lt)
        FakeFigure(b"env").savefig(tmp_path / "env.png")

    assert uploads == ["project-explicit", "project-env"]


_AUTOTRACK_PROJECT_SOURCES = ("LAB_TRACKER_PROJECT_ID", "lt_ids.json")


def _subcommand_help(parser: argparse.ArgumentParser, name: str) -> str:
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    return next(str(choice.help) for choice in subparsers._choices_actions if choice.dest == name)


def _subcommand(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    return subparsers.choices[name]


def _setup_autotrack_help() -> str:
    return _subcommand_help(_subcommand(lt_cli._build_parser(), "setup"), "autotrack")


def test_setup_autotrack_help_names_the_only_saves_it_captures() -> None:
    """After the unbound-save fix the hook no longer captures every figure:
    the help and module summary say which saves it takes."""

    help_text = " ".join(_setup_autotrack_help().split())
    summary = (autotrack_module.__doc__ or "").splitlines()[0]
    for text in (help_text, summary):
        assert "every" not in text
    for source in _AUTOTRACK_PROJECT_SOURCES:
        assert source in help_text
    assert "lt_ids.json" in summary
