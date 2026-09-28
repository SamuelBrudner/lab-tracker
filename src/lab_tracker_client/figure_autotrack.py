"""Opt-in capture of matplotlib saves with an explicit, env, or ``lt_ids.json`` project.

``autotrack()`` wraps ``matplotlib.figure.Figure.savefig`` so a figure saved
to a file path is captured through the same fail-soft path as ``savefig``:
staged evidence with a content hash, run context, host identity, and the
active session, never a raised exception in the user's script. It is
strictly opt-in: nothing installs it unless the person calls it, runs
``lt setup autotrack`` to add it to their IPython startup, and the
``LAB_TRACKER_AUTOTRACK=0`` kill switch disables it everywhere.

Because the startup hook fires in every directory, it captures a save only
when its project comes from ``autotrack(project_id=...)``,
``LAB_TRACKER_PROJECT_ID``, or the saved file's checkout binding
(``lt_ids.json``). Any other save is skipped with a notice on stderr, named
once per unbound checkout (or per folder outside any checkout); nothing is
sent or queued, so a figure never lands in a default project it was not
meant for.

Explicit ``lab_tracker_client.savefig`` / ``capture_figures`` calls suppress
the hook while they save, so a figure is never captured twice.

Inside IPython or Jupyter, ``autotrack()`` also captures the figures a cell
displays inline (see :mod:`lab_tracker_client.display_capture`), under the
same bound-project rule and kill switch. A save to an open file object is
captured when the object is a real file with a filesystem ``name``; in-memory
buffers such as ``BytesIO`` are ignored.
"""

from __future__ import annotations

import io
import os
import stat
import sys
import weakref
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client import figure as _figure
from lab_tracker_client.client import LabTracker

AUTOTRACK_ENV = "LAB_TRACKER_AUTOTRACK"
IPYTHON_STARTUP_FILENAME = "50-lab-tracker-autotrack.py"
IPYTHON_STARTUP_BEGIN = "# --- BEGIN LAB TRACKER AUTOTRACK (managed by `lt setup autotrack`) ---"
IPYTHON_STARTUP_END = "# --- END LAB TRACKER AUTOTRACK ---"
_STATE: dict[str, Any] = {"original": None, "options": None}
# Figures this process saved to a file, with the save counter at that moment,
# so a display or show hook can leave a figure the file capture already took.
_SAVED_FIGURES: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()
_SAVE_COUNTER = [0]


def autotrack_env_enabled() -> bool:
    """False only when the kill switch is set (``0``, ``false``, ``no``, ``off``)."""

    return os.getenv(AUTOTRACK_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def is_autotracking() -> bool:
    return _STATE["original"] is not None


def autotrack(
    enabled: bool = True,
    *,
    patterns: Iterable[str] = _figure._DEFAULT_IMAGE_PATTERNS,
    client: LabTracker | None = None,
    project_id: str | None = None,
    metadata: dict[str, NoteMetadataScalar] | None = None,
    displays: bool = True,
) -> bool:
    """Install (``True``) or remove (``False``) the matplotlib save hook.

    Returns whether the hook is installed afterwards. Installing is a no-op
    that returns ``False`` when matplotlib is not importable or the
    ``LAB_TRACKER_AUTOTRACK`` kill switch is set; calling it twice keeps one
    hook. The hook captures only saves whose target is a filesystem path (or
    an open real file with that path as its ``name``) matching ``patterns``.
    Inside IPython, ``displays`` also captures figures a cell displays inline.
    """

    if not enabled:
        _uninstall()
        return False
    if not autotrack_env_enabled():
        return False
    if is_autotracking():
        _STATE["options"] = _options(patterns, client, project_id, metadata)
        _install_display_capture(displays)
        return True
    figure_module = _matplotlib_figure_module()
    if figure_module is None:
        return False
    original = figure_module.Figure.savefig
    _STATE["original"] = original
    _STATE["options"] = _options(patterns, client, project_id, metadata)

    def _tracked_savefig(self: Any, fname: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, fname, *args, **kwargs)
        path = _path_target(fname) or _file_object_target(fname)
        options = _STATE["options"] or {}
        matched = path is not None and _suffix_matches(path, options.get("patterns", ()))
        if matched:
            _note_figure_saved(self)
        if _figure._AUTOTRACK_SUPPRESSED.get():
            return result
        if path is not None and matched:
            # Same fail-soft capture as savefig(); a failure here is reported
            # once on stderr and never reaches the user's plotting code.
            _figure._capture_saved_figure(
                fig=self,
                path=path,
                client=options.get("client"),
                project_id=options.get("project_id"),
                logical_id=None,
                metadata={**(options.get("metadata") or {}), "figure_autotracked": True},
                preview_max_bytes=_figure.FIGURE_PREVIEW_MAX_BYTES,
                version_every_change=False,
                require_bound_project=True,
            )
        return result

    _tracked_savefig.__wrapped__ = original  # type: ignore[attr-defined]
    _tracked_savefig.__name__ = getattr(original, "__name__", "savefig")
    _tracked_savefig.__doc__ = getattr(original, "__doc__", None)
    figure_module.Figure.savefig = _tracked_savefig
    _install_display_capture(displays)
    return True


def _install_display_capture(displays: bool) -> None:
    """Capture inline figure displays when running inside IPython; fail-soft."""

    try:
        from lab_tracker_client import display_capture

        if displays:
            display_capture.install_display_capture()
        else:
            display_capture.uninstall_display_capture()
    except Exception:  # noqa: BLE001 - display capture must never break autotrack.
        return


def figure_saved_since(fig: Any, counter: int) -> bool:
    """Whether ``fig`` was saved to a matching file after save counter ``counter``."""

    try:
        return _SAVED_FIGURES.get(fig, -1) > counter
    except TypeError:
        return False


def save_counter() -> int:
    """The number of matching file saves this process has made so far."""

    return _SAVE_COUNTER[0]


def _note_figure_saved(fig: Any) -> None:
    _SAVE_COUNTER[0] += 1
    try:
        _SAVED_FIGURES[fig] = _SAVE_COUNTER[0]
    except TypeError:
        return


def autotrack_options() -> dict[str, Any]:
    """The installed hook's options (patterns, client, project, metadata)."""

    return dict(_STATE["options"] or {})


def _uninstall() -> None:
    _install_display_capture(False)
    original = _STATE["original"]
    if original is None:
        return
    figure_module = _matplotlib_figure_module()
    if figure_module is not None:
        figure_module.Figure.savefig = original
    _STATE["original"] = None
    _STATE["options"] = None


def _options(
    patterns: Iterable[str],
    client: LabTracker | None,
    project_id: str | None,
    metadata: dict[str, NoteMetadataScalar] | None,
) -> dict[str, Any]:
    return {
        "patterns": tuple(patterns),
        "client": client,
        "project_id": project_id,
        "metadata": dict(metadata or {}),
    }


def _matplotlib_figure_module() -> Any:
    try:
        import matplotlib.figure as figure_module
    except Exception:  # noqa: BLE001 - matplotlib absent or broken: no hook.
        return None
    return figure_module if hasattr(figure_module, "Figure") else None


def _path_target(fname: Any) -> Path | None:
    if isinstance(fname, (str, os.PathLike)):
        text = os.fspath(fname)
        if isinstance(text, str) and text.strip():
            return Path(text).expanduser()
    return None


def _file_object_target(fname: Any) -> Path | None:
    """The file an open real file object writes to, flushed; else ``None``.

    Only an object whose ``name`` is a filesystem path naming the very file
    its descriptor has open counts (``open(path, "wb")``, a named temporary
    file); ``BytesIO`` and other in-memory buffers have no such file.
    """

    name = getattr(fname, "name", None)
    if isinstance(name, bytes):
        name = os.fsdecode(name)
    if not isinstance(name, (str, os.PathLike)) or not os.fspath(name):
        return None
    try:
        descriptor_stat = os.fstat(fname.fileno())
        path_stat = os.stat(name)
        if not stat.S_ISREG(path_stat.st_mode) or not os.path.samestat(
            descriptor_stat, path_stat
        ):
            return None
        fname.flush()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return None
    # Absolute now: the name was just checked against the open file from this cwd.
    return Path(os.path.abspath(os.fspath(name)))


def _suffix_matches(path: Path, patterns: Iterable[str]) -> bool:
    return any(path.match(pattern) for pattern in patterns)


def ipython_startup_dir() -> Path:
    base = os.getenv("IPYTHONDIR")
    root = Path(base).expanduser() if base else Path.home() / ".ipython"
    return root / "profile_default" / "startup"


def ipython_startup_path() -> Path:
    return ipython_startup_dir() / IPYTHON_STARTUP_FILENAME


def ipython_startup_source() -> str:
    return (
        f"{IPYTHON_STARTUP_BEGIN}\n"
        "# Captures matplotlib figures saved to a path into Lab Tracker as staged\n"
        "# evidence, only inside a checkout bound to a project (lt_ids.json) or with\n"
        "# LAB_TRACKER_PROJECT_ID set. Set LAB_TRACKER_AUTOTRACK=0 to disable, or\n"
        "# remove this file with `lt setup autotrack --uninstall`. Fail-soft: never\n"
        "# raises into a notebook.\n"
        "try:\n"
        "    import lab_tracker_client\n"
        "\n"
        "    lab_tracker_client.autotrack()\n"
        "except Exception:  # noqa: BLE001 - a missing client must not break IPython.\n"
        "    pass\n"
        f"{IPYTHON_STARTUP_END}\n"
    )


def ipython_startup_status() -> dict[str, Any]:
    path = ipython_startup_path()
    installed = False
    current = False
    if path.exists():
        content = path.read_text(encoding="utf-8")
        installed = IPYTHON_STARTUP_BEGIN in content
        current = content == ipython_startup_source()
    return {
        "startup_file": str(path),
        "installed": installed,
        "up_to_date": current if installed else None,
        "kill_switch_set": not autotrack_env_enabled(),
    }


def install_ipython_startup(*, dry_run: bool = False, uninstall: bool = False) -> dict[str, Any]:
    """Write (or remove) the IPython startup file that enables autotrack.

    The file is the only thing touched; it is owned entirely by Lab Tracker,
    so uninstalling deletes it rather than editing around user content.
    """

    path = ipython_startup_path()
    status = ipython_startup_status()
    payload: dict[str, Any] = {
        "command": "setup-autotrack",
        "startup_file": str(path),
        "dry_run": dry_run,
        "python": sys.executable,
    }
    if uninstall:
        if not status["installed"]:
            payload["action"] = "absent"
            return payload
        payload["action"] = "would-remove" if dry_run else "removed"
        if not dry_run:
            path.unlink()
        return payload
    if status["installed"] and status["up_to_date"]:
        payload["action"] = "current"
        return payload
    payload["action"] = (
        ("would-update" if status["installed"] else "would-install")
        if dry_run
        else ("updated" if status["installed"] else "installed")
    )
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ipython_startup_source(), encoding="utf-8")
    return payload


__all__ = [
    "AUTOTRACK_ENV",
    "autotrack",
    "autotrack_env_enabled",
    "autotrack_options",
    "figure_saved_since",
    "install_ipython_startup",
    "ipython_startup_path",
    "ipython_startup_status",
    "is_autotracking",
    "save_counter",
]
