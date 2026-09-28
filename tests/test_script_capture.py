"""Plain `python script.py` capture: the scripts .pth, lazy hooks, and plt.show()."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

import lab_tracker_client.figure as figure_module
import lab_tracker_client.script_capture as script_module
from lab_tracker_client import LabTracker, autotrack
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.figure import _reset_figure_capture_state_for_tests

PNG = b"\x89PNG\r\n\x1a\n" + b"shown"

FAKE_MATPLOTLIB = {
    "__init__.py": "",
    "figure.py": """
        class Figure:
            def __init__(self, payload=b"\\x89PNG\\r\\n\\x1a\\nfake"):
                self.payload = payload

            def savefig(self, fname, *args, **kwargs):
                if hasattr(fname, "write"):
                    fname.write(self.payload)
                else:
                    with open(fname, "wb") as handle:
                        handle.write(self.payload)
    """,
    "_pylab_helpers.py": """
        class Gcf:
            managers = []

            @classmethod
            def get_all_fig_managers(cls):
                return list(cls.managers)
    """,
    "pyplot.py": """
        import types

        from matplotlib._pylab_helpers import Gcf
        from matplotlib.figure import Figure

        SHOWN = []

        def figure(num, payload=b"\\x89PNG\\r\\n\\x1a\\nfake"):
            fig = Figure(payload)
            fig.number = num
            canvas = types.SimpleNamespace(figure=fig)
            Gcf.managers.append(types.SimpleNamespace(num=num, canvas=canvas))
            return fig

        def show(*args, **kwargs):
            SHOWN.append(len(Gcf.managers))
    """,
}


def _fake_matplotlib(root: Path) -> Path:
    package = root / "matplotlib"
    package.mkdir(parents=True)
    for name, source in FAKE_MATPLOTLIB.items():
        (package / name).write_text(textwrap.dedent(source), encoding="utf-8")
    return root


def _git_checkout(path: Path, project_id: str | None = None) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    if project_id:
        (path / "lt_ids.json").write_text(json.dumps({"project_id": project_id}))
    return path


def _child_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LAB_TRACKER_") and key not in {"PYTHONPATH", "MPLBACKEND"}
    }
    env["LAB_TRACKER_CONFIG_DIR"] = str(tmp_path / "lt-config")
    env["MPLBACKEND"] = "Agg"
    env.update(extra)
    return env


def _run_child(
    script: str, *, cwd: Path, env: dict[str, str], args: list[str]
) -> subprocess.CompletedProcess[str]:
    path = cwd / "analysis.py"
    path.write_text(textwrap.dedent(script), encoding="utf-8")
    return subprocess.run(  # noqa: S603 - the test's own interpreter.
        [sys.executable, str(path), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def site_dir(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    site.mkdir()
    (site / script_module.SCRIPTS_PTH_FILENAME).write_text(
        script_module.scripts_pth_source(), encoding="utf-8"
    )
    return site


CHILD_PRELUDE = """
    import json
    import site
    import sys

    fake_root, site_dir, out = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, fake_root)
    state = {}
    site.addsitedir(site_dir)  # processes the .pth exactly as interpreter start does
    state["bootstrap_loaded"] = "_lab_tracker_autotrack_pth" in sys.modules
    state["heavy_at_start"] = sorted(
        name for name in ("httpx", "lab_tracker_client", "lab_tracker") if name in sys.modules
    )
"""


def test_scripts_pth_waits_for_matplotlib_then_captures_saves_and_shows(
    tmp_path: Path, site_dir: Path
) -> None:
    fake_root = _fake_matplotlib(tmp_path / "fakes")
    checkout = _git_checkout(tmp_path / "analysis", "project-script")
    script = (
        CHILD_PRELUDE
        + """
    import matplotlib.pyplot as plt

    state["heavy_after_import"] = "lab_tracker_client" in sys.modules
    state["lazy_show"] = getattr(plt.show, "_lab_tracker_lazy", False)
    state["watcher_left"] = any(
        getattr(finder, "_lab_tracker_watcher", False) for finder in sys.meta_path
    )

    import lab_tracker_client.figure as figure_module

    calls = []
    figure_module._capture_saved_figure = lambda **kw: calls.append(
        {"kind": "save", "path": str(kw["path"]), "metadata": kw["metadata"]}
    )
    figure_module.capture_figure_bytes = lambda payload, **kw: calls.append(
        {
            "kind": "show",
            "logical_id": kw["logical_id"],
            "anchor": str(kw["anchor"]),
            "metadata": kw["metadata"],
            "bound": kw["require_bound_project"],
            "payload": payload.decode("latin-1"),
        }
    )
    saved = plt.figure(1)
    saved.savefig("saved.png")
    plt.figure(2, payload=b"\\x89PNG\\r\\n\\x1a\\nshown")
    plt.show()
    plt.show()  # the same bytes again: nothing new
    state["shown"] = plt.SHOWN
    state["calls"] = calls
    with open(out, "w") as handle:
        json.dump(state, handle)
    """
    )
    out = tmp_path / "state.json"
    result = _run_child(
        script,
        cwd=checkout,
        env=_child_env(tmp_path),
        args=[str(fake_root), str(site_dir), str(out)],
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    state = json.loads(out.read_text())
    assert state["bootstrap_loaded"] is True
    assert state["heavy_at_start"] == []
    # Importing matplotlib only swaps in lazy stand-ins; the client stays unloaded.
    assert state["heavy_after_import"] is False
    assert state["lazy_show"] is True
    assert state["watcher_left"] is False
    assert state["shown"] == [2, 2]
    save, show = state["calls"]
    assert save["kind"] == "save"
    assert Path(save["path"]).name == "saved.png"
    assert save["metadata"] == {"figure_autotracked": True}
    # Figure 1 was saved, so only figure 2 is captured from the show, once.
    assert show["kind"] == "show"
    assert show["bound"] is True
    assert show["anchor"] == str(checkout.resolve())
    assert show["payload"].encode("latin-1") == b"\x89PNG\r\n\x1a\nshown"
    assert show["logical_id"].startswith("show/analysis.py/run-")
    assert show["logical_id"].endswith("/figure-2")
    assert show["metadata"]["figure_show_captured"] is True
    assert show["metadata"]["figure_number"] == 2
    assert show["metadata"]["script_path"] == "analysis.py"
    assert show["logical_id"] == (
        f"show/analysis.py/run-{show['metadata']['script_run_id']}/figure-2"
    )


def test_scripts_pth_capture_follows_the_bound_project_rule(tmp_path: Path, site_dir: Path) -> None:
    """End to end with no patches: an unbound folder is skipped with one
    notice and nothing is written; a bound checkout without a configured
    server reports that once. The script itself always succeeds."""

    fake_root = _fake_matplotlib(tmp_path / "fakes")
    loose = tmp_path / "loose"
    loose.mkdir()
    script = (
        CHILD_PRELUDE
        + """
    import matplotlib.pyplot as plt

    plt.figure(1).savefig("one.png")
    plt.figure(2).savefig("two.png")
    plt.show()
    print("done")
    """
    )
    result = _run_child(
        script,
        cwd=loose,
        env=_child_env(tmp_path),
        args=[str(fake_root), str(site_dir), str(tmp_path / "unused.json")],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "done"
    assert result.stderr.count("autotrack is not capturing saves") == 1
    assert sorted(item.name for item in loose.iterdir()) == ["analysis.py", "one.png", "two.png"]

    bound = _git_checkout(tmp_path / "bound", "project-script")
    result = _run_child(
        script,
        cwd=bound,
        env=_child_env(tmp_path),
        args=[str(fake_root), str(site_dir), str(tmp_path / "unused.json")],
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("figure capture is unconfigured") == 1


def test_scripts_pth_honours_the_kill_switch_and_never_raises(
    tmp_path: Path, site_dir: Path
) -> None:
    fake_root = _fake_matplotlib(tmp_path / "fakes")
    script = (
        CHILD_PRELUDE
        + """
    import matplotlib.pyplot as plt

    state["lazy_show"] = getattr(plt.show, "_lab_tracker_lazy", False)
    with open(out, "w") as handle:
        json.dump(state, handle)
    """
    )
    out = tmp_path / "state.json"
    args = [str(fake_root), str(site_dir), str(out)]
    killed = _run_child(
        script, cwd=tmp_path, env=_child_env(tmp_path, LAB_TRACKER_AUTOTRACK="0"), args=args
    )
    assert killed.returncode == 0, killed.stderr
    assert json.loads(out.read_text()) == {
        "bootstrap_loaded": False,
        "heavy_at_start": [],
        "lazy_show": False,
    }

    # A bootstrap that is gone or broken must stay silent at startup.
    (site_dir / script_module.SCRIPTS_PTH_FILENAME).write_text(
        script_module.scripts_pth_source(tmp_path / "missing.py"), encoding="utf-8"
    )
    missing = _run_child(script, cwd=tmp_path, env=_child_env(tmp_path), args=args)
    assert (missing.returncode, missing.stderr) == (0, "")
    assert json.loads(out.read_text())["bootstrap_loaded"] is False

    broken = tmp_path / "broken.py"
    broken.write_text("def install(:\n", encoding="utf-8")
    (site_dir / script_module.SCRIPTS_PTH_FILENAME).write_text(
        script_module.scripts_pth_source(broken), encoding="utf-8"
    )
    failed = _run_child(script, cwd=tmp_path, env=_child_env(tmp_path), args=args)
    assert (failed.returncode, failed.stderr) == (0, "")


def test_scripts_pth_steps_aside_inside_ipython(tmp_path: Path, site_dir: Path) -> None:
    fake_root = _fake_matplotlib(tmp_path / "fakes")
    script = (
        CHILD_PRELUDE
        + """
    import types

    ipython = types.ModuleType("IPython")
    ipython.get_ipython = lambda: object()
    sys.modules["IPython"] = ipython
    import matplotlib.pyplot as plt

    state["lazy_show"] = getattr(plt.show, "_lab_tracker_lazy", False)
    state["watcher_left"] = any(
        getattr(finder, "_lab_tracker_watcher", False) for finder in sys.meta_path
    )
    with open(out, "w") as handle:
        json.dump(state, handle)
    """
    )
    out = tmp_path / "state.json"
    result = _run_child(
        script,
        cwd=tmp_path,
        env=_child_env(tmp_path),
        args=[str(fake_root), str(site_dir), str(out)],
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(out.read_text())
    assert state["bootstrap_loaded"] is True
    assert (state["lazy_show"], state["watcher_left"]) == (False, False)


def test_scripts_pth_with_real_matplotlib(tmp_path: Path, site_dir: Path) -> None:
    pytest.importorskip("matplotlib")
    checkout = _git_checkout(tmp_path / "analysis", "project-script")
    script = (
        CHILD_PRELUDE
        + """
    import matplotlib.pyplot as plt
    import lab_tracker_client.figure as figure_module

    calls = []
    figure_module._capture_saved_figure = lambda **kw: calls.append(["save", str(kw["path"])])
    figure_module.capture_figure_bytes = lambda payload, **kw: calls.append(
        ["show", payload[:8].hex(), kw["logical_id"]]
    )
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3])
    with open("through-handle.png", "wb") as handle:
        fig.savefig(handle)
    other, other_ax = plt.subplots()
    other_ax.plot([3, 2, 1])
    plt.show()
    state["calls"] = calls
    with open(out, "w") as handle:
        json.dump(state, handle)
    """
    )
    out = tmp_path / "state.json"
    result = _run_child(
        script,
        cwd=checkout,
        env=_child_env(tmp_path),
        args=[str(tmp_path / "no-fakes"), str(site_dir), str(out)],
    )
    assert result.returncode == 0, result.stderr
    calls = json.loads(out.read_text())["calls"]
    assert calls[0] == ["save", str(checkout.resolve() / "through-handle.png")]
    assert len(calls) == 2
    assert calls[1][0] == "show"
    assert calls[1][1] == b"\x89PNG\r\n\x1a\n".hex()
    assert calls[1][2].endswith("/figure-2")


# --- in-process: the show hook --------------------------------------------------


class FakeFigure:
    def __init__(self, payload: bytes = PNG) -> None:
        self.payload = payload

    def savefig(self, fname: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        if hasattr(fname, "write"):
            fname.write(self.payload)
        else:
            Path(fname).write_bytes(self.payload)


@pytest.fixture
def fake_pyplot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    managers: list[Any] = []
    shown: list[int] = []
    matplotlib = types.ModuleType("matplotlib")
    figure = types.ModuleType("matplotlib.figure")
    figure.Figure = FakeFigure  # type: ignore[attr-defined]
    helpers = types.ModuleType("matplotlib._pylab_helpers")
    helpers.Gcf = types.SimpleNamespace(get_all_fig_managers=lambda: list(managers))  # type: ignore[attr-defined]
    pyplot = types.ModuleType("matplotlib.pyplot")
    pyplot.show = lambda *args, **kwargs: shown.append(len(managers))  # type: ignore[attr-defined]
    for name, module in (
        ("matplotlib", matplotlib),
        ("matplotlib.figure", figure),
        ("matplotlib._pylab_helpers", helpers),
        ("matplotlib.pyplot", pyplot),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "IPython", raising=False)
    for key in (
        "LAB_TRACKER_AUTOTRACK",
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_SESSION_ID",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_SESSION_CONTEXT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))
    _reset_figure_capture_state_for_tests()
    script_module._reset_script_capture_state_for_tests()

    def new_figure(num: int, payload: bytes = PNG) -> FakeFigure:
        fig = FakeFigure(payload)
        managers.append(types.SimpleNamespace(num=num, canvas=types.SimpleNamespace(figure=fig)))
        return fig

    yield types.SimpleNamespace(module=pyplot, figure=new_figure, shown=shown)
    script_module._reset_script_capture_state_for_tests()
    autotrack(False)
    _reset_figure_capture_state_for_tests()


def test_show_captures_each_figure_once_per_run_into_the_scripts_checkout(
    fake_pyplot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _git_checkout(tmp_path / "analysis", "project-script")
    script = checkout / "src" / "run.py"
    script.parent.mkdir()
    script.write_text("print('hi')\n")
    monkeypatch.setattr(sys, "argv", [str(script)])
    uploads: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        fields = {}
        for name in ("project_id", "client_capture_id", "metadata"):
            chunk = body.split(f'name="{name}"'.encode(), 1)[1].split(b"\r\n\r\n", 1)[1]
            fields[name] = chunk.split(b"\r\n--", 1)[0].decode()
        uploads.append(fields)
        return httpx.Response(201, json={"data": {"note_id": "note-1", "metadata": {}}})

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-default",
        transport=httpx.MockTransport(handler),
    ) as lt:
        autotrack(client=lt, displays=False)
        assert script_module.install_show_hook() is True
        assert script_module.install_show_hook() is True  # one hook
        fake_pyplot.figure(1)
        fake_pyplot.module.show()
        fake_pyplot.module.show()  # unchanged bytes: nothing new
        fake_pyplot.figure(2, PNG + b"-second")
        fake_pyplot.module.show()

    assert fake_pyplot.shown == [1, 1, 2]
    assert [upload["project_id"] for upload in uploads] == ["project-script"] * 2
    run_id = script_module.run_identifier()
    assert [upload["client_capture_id"] for upload in uploads] == [
        f"figure:show/src/run.py/run-{run_id}/figure-1",
        f"figure:show/src/run.py/run-{run_id}/figure-2",
    ]
    metadata = json.loads(uploads[0]["metadata"])
    assert metadata["figure_show_captured"] is True
    assert metadata["script_path"] == "src/run.py"
    assert metadata["evidence_source_uri"] == f"{script.as_uri()}#show=run-{run_id}/figure-1"


def test_show_hook_is_fail_soft_and_removable(
    fake_pyplot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def broken(*_args: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs["logical_id"])
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(figure_module, "capture_figure_bytes", broken)
    original = fake_pyplot.module.show
    autotrack(displays=False)
    script_module.install_show_hook()
    fake_pyplot.figure(1)
    fake_pyplot.module.show()
    assert fake_pyplot.shown == [1]
    assert len(attempts) == 1
    script_module.uninstall_show_hook()
    assert fake_pyplot.module.show is original


def test_activation_is_a_no_op_inside_ipython_or_with_the_kill_switch(
    fake_pyplot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_AUTOTRACK", "0")
    assert script_module.activate_script_autotrack() is False
    monkeypatch.delenv("LAB_TRACKER_AUTOTRACK")
    ipython = types.ModuleType("IPython")
    ipython.get_ipython = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "IPython", ipython)
    assert script_module.activate_script_autotrack() is False
    assert not getattr(fake_pyplot.module.show, "_lab_tracker_show_hook", False)
    monkeypatch.delitem(sys.modules, "IPython")
    assert script_module.activate_script_autotrack() is True
    assert getattr(fake_pyplot.module.show, "_lab_tracker_show_hook", False) is True


# --- `lt setup autotrack --scripts` ----------------------------------------------


def test_setup_autotrack_scripts_manages_the_pth_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    site = tmp_path / "site-packages"
    site.mkdir()
    monkeypatch.setattr(script_module, "scripts_site_dir", lambda: site)
    monkeypatch.setenv("IPYTHONDIR", str(tmp_path / "ipython"))
    pth = site / script_module.SCRIPTS_PTH_FILENAME

    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["setup", "autotrack", "--scripts"])
    lt_cli.main(["setup", "autotrack", "--scripts", "--dry-run"])
    preview = json.loads(capsys.readouterr().out)
    assert preview["action"] == "would-install"
    assert preview["target"] == "scripts"
    assert preview["content"] == script_module.scripts_pth_source()
    assert not pth.exists()

    lt_cli.main(["setup", "autotrack", "--scripts", "--yes"])
    assert json.loads(capsys.readouterr().out)["action"] == "installed"
    content = pth.read_text(encoding="utf-8")
    import_lines = [line for line in content.splitlines() if not line.startswith("#")]
    assert len(import_lines) == 1 and import_lines[0].startswith("import os, sys; exec(")
    assert "lab_tracker_client" not in import_lines[0].split("exec(", 1)[0]
    # The IPython startup file is a separate piece and was not touched.
    assert not (tmp_path / "ipython").exists()

    lt_cli.main(["setup", "autotrack", "--scripts", "--yes"])
    assert json.loads(capsys.readouterr().out)["action"] == "current"
    assert script_module.scripts_pth_status()["installed"] is True

    lt_cli.main(["setup", "autotrack", "--scripts", "--yes", "--uninstall"])
    assert json.loads(capsys.readouterr().out)["action"] == "removed"
    assert not pth.exists()
    lt_cli.main(["setup", "autotrack", "--scripts", "--yes", "--uninstall"])
    assert json.loads(capsys.readouterr().out)["action"] == "absent"

    pth.write_text("import something_else\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="not written by Lab Tracker"):
        lt_cli.main(["setup", "autotrack", "--scripts", "--yes"])
    assert pth.read_text(encoding="utf-8") == "import something_else\n"


def test_lazy_stand_ins_install_the_real_hooks_once_and_never_loop(
    fake_pyplot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lab_tracker_client import _autotrack_pth as boot

    monkeypatch.setattr(boot, "_LAZY", [])
    monkeypatch.setattr(FakeFigure, "savefig", FakeFigure.savefig)  # restored afterwards
    original_savefig = FakeFigure.savefig
    saves: list[dict[str, Any]] = []
    shows: list[str] = []
    monkeypatch.setattr(figure_module, "_capture_saved_figure", lambda **kw: saves.append(kw))
    monkeypatch.setattr(
        figure_module,
        "capture_figure_bytes",
        lambda payload, **kw: shows.append(kw["logical_id"]),
    )

    boot._lazy_patch(FakeFigure, "savefig")
    boot._lazy_patch(fake_pyplot.module, "show")
    imported_show = fake_pyplot.module.show  # like `from matplotlib.pyplot import show`
    FakeFigure().savefig(tmp_path / "first.png")
    assert [kw["path"] for kw in saves] == [tmp_path / "first.png"]
    assert FakeFigure.savefig.__wrapped__ is original_savefig
    fake_pyplot.figure(1)
    imported_show()  # the early reference still reaches the installed show hook
    assert fake_pyplot.shown == [1]
    assert len(shows) == 1

    # Removing autotrack sticks: a stand-in never reinstalls it.
    autotrack(False)
    fake_pyplot.figure(2, PNG + b"-new")
    imported_show()
    FakeFigure().savefig(tmp_path / "after.png")
    assert (len(saves), len(shows)) == (1, 1)

    # autotrack() installed on top of a stand-in keeps its options and never loops.
    boot._lazy_patch(FakeFigure, "savefig")
    autotrack(project_id="project-explicit", displays=False)
    FakeFigure().savefig(tmp_path / "explicit.png")
    FakeFigure().savefig(tmp_path / "again.png")
    assert [kw["path"].name for kw in saves[1:]] == ["explicit.png", "again.png"]
    assert {kw["project_id"] for kw in saves[1:]} == {"project-explicit"}
