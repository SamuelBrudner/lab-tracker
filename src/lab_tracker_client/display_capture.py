"""Capture the matplotlib figures an IPython or Jupyter kernel displays inline.

Most notebook figures are displayed inline and never saved: matplotlib-inline's
``flush_figures`` shows them when a cell ends, ``display(fig)`` shows one on
demand, and a figure that is a cell's last value is shown by the display
hook. Every one of those paths asks the shell's
``display_formatter.format`` for the figure's representations, so when
:func:`lab_tracker_client.autotrack` runs inside IPython this module wraps
that one call and keeps the exact PNG (or JPEG) bytes the kernel sent to the
frontend. Nothing is rendered a second time.

Displays are coalesced per cell. A figure displayed several times in one cell
is captured once, with the bytes of its last display, after the cell has
finished: IPython fires ``post_run_cell`` after every ``post_execute``
callback, including ``flush_figures`` (which closes the figures it shows), so
the end-of-cell displays are already recorded by then. A figure the cell also
saved to a file is left to the save's own capture.

Each figure is filed through :func:`lab_tracker_client.figure.capture_figure_bytes`
with the same bound-project rule and kill switch (``LAB_TRACKER_AUTOTRACK=0``)
as autotrack. Its logical id is built from the notebook, the cell (its Jupyter
cell id when the frontend sends one, else a hash of its source), and the
figure's order in the cell, so re-running a cell coalesces into the same
staged note instead of piling up new ones.
"""

from __future__ import annotations

import binascii
import hashlib
import os
import sys
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client import figure as _figure
from lab_tracker_client import figure_autotrack as _autotrack
from lab_tracker_client.capture_project import capture_checkout_root

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
# (MIME type, format label, filename suffix, magic bytes), most preferred first.
# SVG and PDF displays are not captured: the server refuses SVG uploads and a
# PDF display is rare enough that the PNG a notebook normally shows suffices.
DISPLAY_FORMATS = (
    ("image/png", "png", ".png", PNG_MAGIC),
    ("image/jpeg", "jpeg", ".jpg", JPEG_MAGIC),
)
NOTEBOOK_PATH_SOURCE_SESSION = "jpy_session_name"
NOTEBOOK_PATH_SOURCE_VSCODE = "vscode"
NOTEBOOK_PATH_UNKNOWN = "unknown"
_WRAPPER_MARKER = "_lab_tracker_display_capture"
_STATE: dict[str, Any] = {"capture": None}


@dataclass(frozen=True)
class NotebookLocation:
    """Where the running kernel's notebook lives, as far as the kernel can tell."""

    path: Path | None
    label: str
    source: str


@dataclass
class _Cell:
    source_sha256: str = ""
    cell_id: str = ""
    save_counter: int = 0


@dataclass
class _Displayed:
    fig: Any
    payload: bytes
    display_format: str
    suffix: str


def discover_notebook(shell: Any = None) -> NotebookLocation:
    """The kernel's notebook: ``JPY_SESSION_NAME``, VS Code's file, else unknown.

    Jupyter Server sets ``JPY_SESSION_NAME`` for the kernels it starts (an
    absolute path, or a path relative to the server root, which is resolved
    against the kernel's working folder when the notebook is there). VS Code
    records the notebook as ``__vsc_ipynb_file__`` in the user namespace.
    """

    session = (os.environ.get("JPY_SESSION_NAME") or "").strip()
    if session:
        candidate = Path(session).expanduser()
        if not candidate.is_absolute():
            local = Path.cwd() / candidate.name
            if local.is_file():
                candidate = local
        if candidate.is_absolute():
            return NotebookLocation(candidate, str(candidate), NOTEBOOK_PATH_SOURCE_SESSION)
        return NotebookLocation(None, session, NOTEBOOK_PATH_SOURCE_SESSION)
    namespace = getattr(shell, "user_ns", None)
    vscode = namespace.get("__vsc_ipynb_file__") if isinstance(namespace, Mapping) else None
    if isinstance(vscode, str) and vscode.strip():
        path = Path(vscode.strip()).expanduser()
        return NotebookLocation(path, str(path), NOTEBOOK_PATH_SOURCE_VSCODE)
    return NotebookLocation(None, NOTEBOOK_PATH_UNKNOWN, NOTEBOOK_PATH_UNKNOWN)


class DisplayCapture:
    """Per-shell display capture: the format wrapper, cell hooks, and pending displays."""

    def __init__(self, shell: Any) -> None:
        self.shell = shell
        self._formatter: Any = None
        self._wrapper: Any = None
        self._restore_to: Any = None
        self._owned_instance_attribute = False
        self._active = False
        self._local = threading.local()
        self._pending: dict[int, _Displayed] = {}
        self._cell = _Cell()
        self._execution_count: int | None = None

    def install(self) -> None:
        """Wrap the display formatter and register the cell hooks (idempotent).

        Another tool may wrap the formatter on top of this one; a wrapper of
        ours anywhere in its ``__wrapped__`` chain counts as installed. Each
        wrapper calls the function it wrapped, so no chain can loop back.
        """

        formatter = self.shell.display_formatter
        current = formatter.format
        if not _chain_has_wrapper(current, self):
            if self._formatter is not None and self._formatter is not formatter:
                self._restore_formatter()
            original = current

            def _format(obj: Any, *args: Any, **kwargs: Any) -> Any:
                return self._format(original, obj, *args, **kwargs)

            setattr(_format, _WRAPPER_MARKER, self)
            _format.__wrapped__ = original  # type: ignore[attr-defined]
            self._owned_instance_attribute = "format" not in vars(formatter)
            self._formatter = formatter
            self._wrapper = _format
            self._restore_to = original
            formatter.format = _format
        self._active = True
        events = self.shell.events
        events.register("pre_run_cell", self._pre_run_cell)
        events.register("post_run_cell", self._post_run_cell)

    def uninstall(self) -> None:
        """Restore the formatter and drop the cell hooks; pending displays are discarded.

        A wrapper another tool has wrapped cannot be unhooked; it stays in that
        chain as a pass-through.
        """

        self._active = False
        self._restore_formatter()
        for event, callback in (
            ("pre_run_cell", self._pre_run_cell),
            ("post_run_cell", self._post_run_cell),
        ):
            with suppress(Exception):
                self.shell.events.unregister(event, callback)
        self._pending = {}

    def _restore_formatter(self) -> None:
        formatter = self._formatter
        if formatter is None:
            return
        with suppress(Exception):
            if formatter.format is self._wrapper:
                if self._owned_instance_attribute:
                    del formatter.format
                else:
                    formatter.format = self._restore_to
        self._formatter = None
        self._wrapper = None
        self._restore_to = None

    def _format(self, original: Any, obj: Any, *args: Any, **kwargs: Any) -> Any:
        depth = getattr(self._local, "depth", 0)
        if not self._active or depth:
            return original(obj, *args, **kwargs)
        self._local.depth = depth + 1
        try:
            result = original(obj, *args, **kwargs)
        finally:
            self._local.depth = depth
        # Recording is best effort: a display must never fail because of it.
        with suppress(Exception):
            self._record(obj, result)
        return result

    def _record(self, obj: Any, result: Any) -> None:
        figure_class = getattr(sys.modules.get("matplotlib.figure"), "Figure", None)
        if figure_class is None or not isinstance(obj, figure_class):
            return
        format_dict = result[0] if isinstance(result, tuple) and result else None
        if not isinstance(format_dict, Mapping):
            return
        for mime_type, display_format, suffix, magic in DISPLAY_FORMATS:
            payload = _display_bytes(format_dict.get(mime_type), magic)
            if payload:
                # Re-displaying a figure keeps its first position in the cell
                # and replaces its bytes: the last display wins.
                self._pending[id(obj)] = _Displayed(obj, payload, display_format, suffix)
                return

    def _pre_run_cell(self, *args: Any) -> None:
        with suppress(Exception):
            if self._pending:
                # Displays made after the previous cell finished (a silent
                # execution, a background thread) belong to that cell.
                self.flush()
            info = args[0] if args else None
            raw_cell = getattr(info, "raw_cell", None)
            self._cell = _Cell(
                source_sha256=hashlib.sha256(str(raw_cell or "").encode("utf-8")).hexdigest(),
                cell_id=str(getattr(info, "cell_id", None) or ""),
                save_counter=_autotrack.save_counter(),
            )
            self._execution_count = None

    def _post_run_cell(self, *args: Any) -> None:
        with suppress(Exception):
            result = args[0] if args else None
            count = getattr(result, "execution_count", None)
            self.flush(count if isinstance(count, int) else None)

    def flush(self, execution_count: int | None = None) -> list[_figure.FigureCaptureResult]:
        """Capture the displays pending for the current cell; fail-soft."""

        pending, self._pending = self._pending, {}
        if execution_count is not None:
            self._execution_count = execution_count
        results: list[_figure.FigureCaptureResult] = []
        if not pending:
            return results
        cell = self._cell
        location = discover_notebook(self.shell)
        options = _autotrack.autotrack_options()
        for index, displayed in enumerate(pending.values(), start=1):
            if _autotrack.figure_saved_since(displayed.fig, cell.save_counter):
                continue
            with suppress(Exception):
                results.append(self._capture(displayed, index, cell, location, options))
        return results

    def _capture(
        self,
        displayed: _Displayed,
        index: int,
        cell: _Cell,
        location: NotebookLocation,
        options: Mapping[str, Any],
    ) -> _figure.FigureCaptureResult:
        anchor = location.path.parent if location.path is not None else Path.cwd()
        notebook_label = _notebook_label(location)
        cell_key = f"cell-{cell.cell_id}" if cell.cell_id else f"cell-{cell.source_sha256[:12]}"
        count = self._execution_count
        stem = location.path.stem if location.path is not None else "notebook"
        where = location.path if location.path is not None else Path.cwd().resolve()
        metadata: dict[str, NoteMetadataScalar] = {
            **dict(options.get("metadata") or {}),
            "figure_autotracked": True,
            "figure_display_captured": True,
            "figure_display_format": displayed.display_format,
            "notebook_path": notebook_label,
            "notebook_path_source": location.source,
            "notebook_cell_source_sha256": cell.source_sha256,
            "notebook_figure_index": index,
        }
        if cell.cell_id:
            metadata["notebook_cell_id"] = cell.cell_id
        if isinstance(count, int):
            metadata["notebook_cell_execution_count"] = count
        return _figure.capture_figure_bytes(
            displayed.payload,
            filename=f"{stem}-cell{count if isinstance(count, int) else 'x'}-figure{index}"
            f"{displayed.suffix}",
            anchor=anchor,
            source_uri=f"{where.as_uri()}#display={cell_key}/figure-{index}",
            logical_id=f"display/{notebook_label}/{cell_key}/figure-{index}",
            fig=displayed.fig,
            client=options.get("client"),
            project_id=options.get("project_id"),
            metadata=metadata,
            require_bound_project=True,
        )


def _chain_has_wrapper(func: Any, capture: DisplayCapture) -> bool:
    """Whether ``func`` is, or wraps (via ``__wrapped__``), one of ``capture``'s wrappers."""

    for _depth in range(32):
        if func is None:
            return False
        if getattr(func, _WRAPPER_MARKER, None) is capture:
            return True
        func = getattr(func, "__wrapped__", None)
    return False


def _notebook_label(location: NotebookLocation) -> str:
    """The notebook as recorded: checkout-relative when possible."""

    if location.path is None:
        if location.source != NOTEBOOK_PATH_UNKNOWN:
            return location.label
        with suppress(Exception):
            cwd = Path.cwd().resolve()
            root = capture_checkout_root(cwd / "_")
            if root is not None:
                relative = cwd.relative_to(root).as_posix()
                return f"{NOTEBOOK_PATH_UNKNOWN}@{relative}"
        return NOTEBOOK_PATH_UNKNOWN
    with suppress(Exception):
        root = capture_checkout_root(location.path)
        if root is not None:
            return location.path.resolve().relative_to(root).as_posix()
    return str(location.path)


def _display_bytes(value: Any, magic: bytes) -> bytes | None:
    """Raw image bytes from a display value (base64 text or raw bytes)."""

    data: bytes
    try:
        if isinstance(value, str):
            data = binascii.a2b_base64(value.encode("ascii"))
        elif isinstance(value, (bytes, bytearray)):
            data = bytes(value)
            if not data.startswith(magic):
                data = binascii.a2b_base64(data)
        else:
            return None
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return None
    return data if data.startswith(magic) else None


def active_ipython_shell() -> Any:
    """The running IPython shell, or ``None`` (never imports IPython itself)."""

    module = sys.modules.get("IPython")
    getter = getattr(module, "get_ipython", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001 - no shell is the answer, not an error.
        return None


def install_display_capture(shell: Any = None) -> bool:
    """Capture inline displays on ``shell`` (default: the running one); idempotent.

    Returns whether display capture is installed afterwards: ``False`` outside
    IPython or when the kill switch is set.
    """

    if not _autotrack.autotrack_env_enabled():
        return False
    shell = shell if shell is not None else active_ipython_shell()
    if (
        shell is None
        or getattr(shell, "display_formatter", None) is None
        or getattr(shell, "events", None) is None
    ):
        return False
    current = _STATE["capture"]
    if current is not None and current.shell is not shell:
        current.uninstall()
        current = None
    if current is None:
        current = DisplayCapture(shell)
        _STATE["capture"] = current
    current.install()
    return True


def uninstall_display_capture() -> None:
    """Stop capturing inline displays."""

    current = _STATE["capture"]
    _STATE["capture"] = None
    if current is not None:
        current.uninstall()


def is_capturing_displays() -> bool:
    return _STATE["capture"] is not None


__all__ = [
    "DISPLAY_FORMATS",
    "DisplayCapture",
    "NotebookLocation",
    "active_ipython_shell",
    "discover_notebook",
    "install_display_capture",
    "is_capturing_displays",
    "uninstall_display_capture",
]
