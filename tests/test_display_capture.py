"""Inline notebook figures: autotrack captures what an IPython kernel displays."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

import lab_tracker_client.display_capture as display_module
import lab_tracker_client.figure as figure_module
from lab_tracker_client import LabTracker, autotrack
from lab_tracker_client.figure import _reset_figure_capture_state_for_tests

PNG_A = b"\x89PNG\r\n\x1a\n" + b"figure-a"
PNG_B = b"\x89PNG\r\n\x1a\n" + b"figure-b"


class FakeFigure:
    """Stands in for matplotlib.figure.Figure: displays as PNG, saves to a path."""

    def __init__(self, png: bytes = PNG_A) -> None:
        self.png = png

    def savefig(self, fname: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        if isinstance(fname, (str, Path)):
            Path(fname).write_bytes(self.png)
        else:
            fname.write(self.png)


class FakeDisplayFormatter:
    """IPython's DisplayFormatter.format contract: (format_dict, metadata_dict)."""

    def format(self, obj: Any, include: Any = None, exclude: Any = None) -> Any:  # noqa: ARG002
        data: dict[str, Any] = {"text/plain": repr(obj)}
        if isinstance(obj, FakeFigure):
            data["image/png"] = base64.b64encode(obj.png).decode("ascii")
        return data, {}


class FakeEvents:
    """IPython's EventManager: ordered callbacks, triggered over a copy."""

    def __init__(self) -> None:
        self.callbacks: dict[str, list[Any]] = {
            name: [] for name in ("pre_execute", "pre_run_cell", "post_execute", "post_run_cell")
        }

    def register(self, event: str, function: Any) -> None:
        if function not in self.callbacks[event]:
            self.callbacks[event].append(function)

    def unregister(self, event: str, function: Any) -> None:
        self.callbacks[event].remove(function)

    def trigger(self, event: str, *args: Any) -> None:
        for callback in self.callbacks[event][:]:
            callback(*args)


class FakeShell:
    """Just enough of an InteractiveShell to run cells that display figures."""

    def __init__(self) -> None:
        self.display_formatter = FakeDisplayFormatter()
        self.events = FakeEvents()
        self.user_ns: dict[str, Any] = {}
        self.execution_count = 1
        self.shown: list[dict[str, Any]] = []

    def display(self, obj: Any) -> None:
        data, _metadata = self.display_formatter.format(obj)
        self.shown.append(data)

    def run_cell(self, source: str, body: Any = None, *, cell_id: str | None = None) -> Any:
        info = types.SimpleNamespace(raw_cell=source, cell_id=cell_id, silent=False)
        result = types.SimpleNamespace(execution_count=self.execution_count, info=info)
        self.execution_count += 1
        self.events.trigger("pre_execute")
        self.events.trigger("pre_run_cell", info)
        try:
            if body is not None:
                body(self)
        finally:
            self.events.trigger("post_execute")
            self.events.trigger("post_run_cell", result)
        return result


def _flush_figures_for(shell: FakeShell, figures: list[FakeFigure]) -> Any:
    """matplotlib-inline's flush_figures: display then forget the open figures."""

    def flush_figures() -> None:
        for fig in figures:
            shell.display(fig)
        figures.clear()

    return flush_figures


@pytest.fixture
def ipython(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A fake running IPython kernel plus a fake matplotlib.figure module."""

    shell = FakeShell()
    ipython_module = types.ModuleType("IPython")
    ipython_module.get_ipython = lambda: shell  # type: ignore[attr-defined]
    ipython_module.version_info = (9, 0, 0)  # type: ignore[attr-defined]
    matplotlib = types.ModuleType("matplotlib")
    figure = types.ModuleType("matplotlib.figure")
    figure.Figure = FakeFigure  # type: ignore[attr-defined]
    matplotlib.figure = figure  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "IPython", ipython_module)
    monkeypatch.setitem(sys.modules, "matplotlib", matplotlib)
    monkeypatch.setitem(sys.modules, "matplotlib.figure", figure)
    for key in (
        "LAB_TRACKER_AUTOTRACK",
        "LAB_TRACKER_SESSION_ID",
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_SESSION_CONTEXT",
        "JPY_SESSION_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-config"))
    _reset_figure_capture_state_for_tests()
    yield shell
    autotrack(False)
    _reset_figure_capture_state_for_tests()


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_capture(payload: bytes, **kwargs: Any) -> figure_module.FigureCaptureResult:
        calls.append({"payload": payload, **kwargs})
        return figure_module.FigureCaptureResult(
            action="imported",
            path=str(kwargs["filename"]),
            source_external_id="figure:x",
            source_uri=str(kwargs["source_uri"]),
            content_hash="0" * 64,
            metadata={},
            client_capture_id="figure:x",
        )

    monkeypatch.setattr(figure_module, "capture_figure_bytes", fake_capture)
    return calls


def _plotting_cell(figures: list[FakeFigure], *, also_display: FakeFigure | None = None) -> Any:
    def body(shell: FakeShell) -> None:
        # Like `import matplotlib.pyplot` under the inline backend: flush_figures
        # is registered on post_execute during the first plotting cell, after
        # autotrack's own hooks.
        shell.events.register("post_execute", _flush_figures_for(shell, figures))
        if also_display is not None:
            shell.display(also_display)

    return body


def test_displayed_figures_are_captured_once_per_cell_after_flush_figures(
    ipython: FakeShell, captured: list[dict[str, Any]], tmp_path: Path, monkeypatch
) -> None:
    notebook = tmp_path / "analysis.ipynb"
    notebook.write_text("{}")
    monkeypatch.setenv("JPY_SESSION_NAME", str(notebook))
    assert autotrack() is True
    assert display_module.is_capturing_displays() is True

    first, second = FakeFigure(PNG_A), FakeFigure(PNG_B)
    source = "plt.plot(x)\ndisplay(fig)"
    result = ipython.run_cell(
        source,
        _plotting_cell([first, second], also_display=first),
        cell_id="cell-uuid-1",
    )

    # The kernel displayed `first` twice (explicitly, then at cell end) and
    # `second` once; each figure is captured once, with its displayed bytes.
    assert len(ipython.shown) == 3
    assert [call["payload"] for call in captured] == [PNG_A, PNG_B]
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    for index, call in enumerate(captured, start=1):
        assert call["require_bound_project"] is True
        assert call["anchor"] == tmp_path
        assert call["logical_id"] == f"display/{notebook}/cell-cell-uuid-1/figure-{index}"
        assert call["filename"] == f"analysis-cell{result.execution_count}-figure{index}.png"
        assert call["source_uri"] == (
            f"{notebook.as_uri()}#display=cell-cell-uuid-1/figure-{index}"
        )
        assert call["metadata"] == {
            "figure_autotracked": True,
            "figure_display_captured": True,
            "figure_display_format": "png",
            "notebook_path": str(notebook),
            "notebook_path_source": "jpy_session_name",
            "notebook_cell_source_sha256": source_hash,
            "notebook_figure_index": index,
            "notebook_cell_id": "cell-uuid-1",
            "notebook_cell_execution_count": result.execution_count,
        }


def test_rerunning_a_cell_reuses_its_logical_ids(
    ipython: FakeShell, captured: list[dict[str, Any]]
) -> None:
    autotrack()
    for _ in range(2):
        ipython.run_cell("plot()", _plotting_cell([FakeFigure()]))
    first, second = captured
    # Same cell source, no cell id: the source hash keys the cell, so the
    # re-run lands on the same logical capture and the server coalesces it.
    assert first["logical_id"] == second["logical_id"]
    assert first["logical_id"].startswith("display/unknown")
    assert first["metadata"]["notebook_path_source"] == "unknown"


def test_a_figure_saved_in_the_cell_is_left_to_its_file_capture(
    ipython: FakeShell, captured: list[dict[str, Any]], tmp_path: Path, monkeypatch
) -> None:
    file_captures: list[Path] = []
    monkeypatch.setattr(
        figure_module,
        "_capture_saved_figure",
        lambda **kwargs: file_captures.append(kwargs["path"]),
    )
    autotrack()
    saved, shown = FakeFigure(PNG_A), FakeFigure(PNG_B)

    def body(shell: FakeShell) -> None:
        _plotting_cell([saved, shown])(shell)
        saved.savefig(tmp_path / "saved.png")

    ipython.run_cell("fig.savefig('saved.png')", body)
    assert file_captures == [tmp_path / "saved.png"]
    assert [call["payload"] for call in captured] == [PNG_B]
    assert captured[0]["metadata"]["notebook_figure_index"] == 2

    # The save only covers the cell it happened in.
    ipython.run_cell("display(fig)", lambda shell: shell.display(saved))
    assert [call["payload"] for call in captured] == [PNG_B, PNG_A]


def test_non_figure_and_non_image_displays_are_ignored(
    ipython: FakeShell, captured: list[dict[str, Any]], monkeypatch
) -> None:
    autotrack()

    def svg_only(self: Any, obj: Any, include: Any = None, exclude: Any = None) -> Any:  # noqa: ARG001
        return {"image/svg+xml": "<svg/>", "text/plain": "fig"}, {}

    ipython.run_cell("df", lambda shell: shell.display({"not": "a figure"}))
    autotrack(False)
    monkeypatch.setattr(FakeDisplayFormatter, "format", svg_only)
    autotrack()
    ipython.run_cell("fig", lambda shell: shell.display(FakeFigure()))
    assert captured == []


def test_raw_png_bytes_from_older_ipython_are_accepted() -> None:
    assert display_module._display_bytes(PNG_A, display_module.PNG_MAGIC) == PNG_A
    encoded = base64.b64encode(PNG_A)
    assert display_module._display_bytes(encoded, display_module.PNG_MAGIC) == PNG_A
    assert display_module._display_bytes(b"not-an-image", display_module.PNG_MAGIC) is None
    assert display_module._display_bytes("%%%", display_module.PNG_MAGIC) is None


def test_capture_failures_never_reach_the_notebook(ipython: FakeShell, monkeypatch) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(figure_module, "capture_figure_bytes", broken)
    autotrack()
    result = ipython.run_cell("plot()", _plotting_cell([FakeFigure()]))
    assert result.execution_count == 1
    assert len(ipython.shown) == 1


def test_kill_switch_and_uninstall_leave_the_formatter_untouched(
    ipython: FakeShell, captured: list[dict[str, Any]], monkeypatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_AUTOTRACK", "0")
    assert autotrack() is False
    assert "format" not in vars(ipython.display_formatter)
    ipython.run_cell("plot()", _plotting_cell([FakeFigure()]))
    assert captured == []

    monkeypatch.delenv("LAB_TRACKER_AUTOTRACK")
    assert autotrack() is True
    assert "format" in vars(ipython.display_formatter)
    assert autotrack(False) is False
    assert "format" not in vars(ipython.display_formatter)
    assert ipython.events.callbacks["pre_run_cell"] == []
    assert ipython.events.callbacks["post_run_cell"] == []
    ipython.run_cell("plot()", _plotting_cell([FakeFigure()]))
    assert captured == []

    # displays=False keeps the save hook but not the display capture.
    assert autotrack(displays=False) is True
    assert display_module.is_capturing_displays() is False


def test_notebook_discovery_order(tmp_path: Path, monkeypatch) -> None:
    shell = types.SimpleNamespace(user_ns={"__vsc_ipynb_file__": str(tmp_path / "vs.ipynb")})
    monkeypatch.delenv("JPY_SESSION_NAME", raising=False)
    assert display_module.discover_notebook(shell) == display_module.NotebookLocation(
        tmp_path / "vs.ipynb", str(tmp_path / "vs.ipynb"), "vscode"
    )
    assert display_module.discover_notebook(None).source == "unknown"

    monkeypatch.setenv("JPY_SESSION_NAME", str(tmp_path / "lab.ipynb"))
    assert display_module.discover_notebook(shell).path == tmp_path / "lab.ipynb"

    # A server-root-relative session name resolves when the kernel runs in
    # the notebook's folder, and is kept as a label otherwise.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JPY_SESSION_NAME", "notebooks/rel.ipynb")
    assert display_module.discover_notebook(shell) == display_module.NotebookLocation(
        None, "notebooks/rel.ipynb", "jpy_session_name"
    )
    (tmp_path / "rel.ipynb").write_text("{}")
    assert display_module.discover_notebook(shell).path == tmp_path / "rel.ipynb"


def _git_checkout(path: Path, project_id: str | None) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    if project_id:
        (path / "lt_ids.json").write_text(json.dumps({"project_id": project_id}))
    return path


def test_displays_upload_into_the_notebooks_bound_checkout_only(
    ipython: FakeShell, tmp_path: Path, monkeypatch, capsys
) -> None:
    bound = _git_checkout(tmp_path / "bound", "project-notebook")
    unbound = _git_checkout(tmp_path / "unbound", None)
    uploads: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        project = body.split(b'name="project_id"', 1)[1].split(b"\r\n\r\n", 1)[1]
        uploads.append((project.split(b"\r\n--", 1)[0].decode(), body))
        return httpx.Response(201, json={"data": {"note_id": "note-1", "metadata": {}}})

    with LabTracker(
        base_url="http://testserver",
        default_project_id="project-default",
        transport=httpx.MockTransport(handler),
    ) as lt:
        autotrack(client=lt)
        monkeypatch.setenv("JPY_SESSION_NAME", str(bound / "nb.ipynb"))
        ipython.run_cell("plot()", _plotting_cell([FakeFigure(PNG_A)]))
        monkeypatch.setenv("JPY_SESSION_NAME", str(unbound / "nb.ipynb"))
        ipython.run_cell("plot()", _plotting_cell([FakeFigure(PNG_B)]))

    assert [project for project, _body in uploads] == ["project-notebook"]
    assert PNG_A in uploads[0][1]
    assert "autotrack is not capturing saves" in capsys.readouterr().err
    assert sorted(item.name for item in bound.iterdir()) == [".git", "lt_ids.json"]


def test_real_matplotlib_figures_display_and_capture(
    ipython: FakeShell, captured: list[dict[str, Any]], monkeypatch
) -> None:
    pytest.importorskip("matplotlib")
    monkeypatch.delitem(sys.modules, "matplotlib", raising=False)
    monkeypatch.delitem(sys.modules, "matplotlib.figure", raising=False)
    from matplotlib.figure import Figure

    def ipython_like_format(self: Any, obj: Any, include: Any = None, exclude: Any = None):  # noqa: ARG001
        data: dict[str, Any] = {"text/plain": repr(obj)}
        if isinstance(obj, Figure):
            buffer = io.BytesIO()
            obj.savefig(buffer, format="png", bbox_inches="tight")
            data["image/png"] = base64.b64encode(buffer.getvalue()).decode("ascii")
        return data, {}

    monkeypatch.setattr(FakeDisplayFormatter, "format", ipython_like_format)
    assert autotrack() is True
    fig = Figure()
    fig.add_subplot().plot([1, 2, 3])
    try:
        ipython.run_cell("fig", lambda shell: shell.display(fig))
    finally:
        autotrack(False)
    assert len(captured) == 1
    assert captured[0]["payload"].startswith(display_module.PNG_MAGIC)
    assert captured[0]["fig"] is fig
