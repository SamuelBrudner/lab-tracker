"""Figure capture for plain ``python script.py`` runs (``lt setup autotrack --scripts``).

The setup verb writes ``lab_tracker_autotrack.pth`` into the site-packages of
the Python environment that runs it. Its one line runs at every interpreter
start of that environment and is nearly free: it checks the
``LAB_TRACKER_AUTOTRACK`` kill switch, then loads the stdlib-only
:mod:`lab_tracker_client._autotrack_pth` by path, which only watches for
matplotlib (see that module). Nothing else happens until a script, outside
IPython, first saves or shows a figure; then :func:`activate_script_autotrack`
installs:

- the autotrack ``savefig`` hook (saves to a path, or to an open real file),
  exactly as in IPython, and
- a ``pyplot.show()`` hook for scripts that never save: before showing, each
  open figure is rendered as PNG and captured, once per figure per run (the
  logical id carries a per-process run id; showing identical bytes again
  sends nothing, and a figure the run saved to a file is left to that save).

Both follow the bound-project rule: a figure is captured only when its
project comes from ``LAB_TRACKER_PROJECT_ID`` or the checkout binding
(``lt_ids.json``) of the saved file or, for a shown figure, of the script.
"""

from __future__ import annotations

import hashlib
import io
import sys
import sysconfig
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client import figure as _figure
from lab_tracker_client import figure_autotrack as _autotrack
from lab_tracker_client._autotrack_pth import MODULE_NAME as PTH_MODULE_NAME
from lab_tracker_client.capture_project import capture_checkout_root
from lab_tracker_client.client import LTValidationError

SCRIPTS_PTH_FILENAME = "lab_tracker_autotrack.pth"
SCRIPTS_PTH_MARKER = "# Lab Tracker scripts autotrack (managed by `lt setup autotrack --scripts`)"
_SHOW_HOOK_MARKER = "_lab_tracker_show_hook"
_STATE: dict[str, Any] = {"show_original": None, "run_id": None}
# Figure number -> hash of the bytes last captured for it in this run.
_SHOWN: dict[int, str] = {}


def activate_script_autotrack() -> bool:
    """Install the save hook and, once pyplot is loaded, the show hook (idempotent).

    Does nothing inside IPython, where the IPython startup file's autotrack
    (with inline display capture) applies instead, or with the kill switch set.
    """

    try:
        if not _autotrack.autotrack_env_enabled() or _in_ipython():
            return False
        installed = _autotrack.autotrack(displays=False)
        if installed:
            install_show_hook()
        return installed
    except Exception:  # noqa: BLE001 - never break the user's plotting call.
        return False


def install_show_hook() -> bool:
    """Wrap ``matplotlib.pyplot.show`` to capture the figures it shows."""

    pyplot = sys.modules.get("matplotlib.pyplot")
    current = getattr(pyplot, "show", None)
    if pyplot is None or current is None:
        return False
    if getattr(current, _SHOW_HOOK_MARKER, False):
        return True
    original = current

    def show(*args: Any, **kwargs: Any) -> Any:
        with suppress(Exception):
            capture_shown_figures()
        return original(*args, **kwargs)

    setattr(show, _SHOW_HOOK_MARKER, True)
    show.__wrapped__ = original  # type: ignore[attr-defined]
    show.__name__ = getattr(original, "__name__", "show")
    show.__doc__ = getattr(original, "__doc__", None)
    pyplot.show = show
    _STATE["show_original"] = original
    return True


def uninstall_show_hook() -> None:
    pyplot = sys.modules.get("matplotlib.pyplot")
    original = _STATE["show_original"]
    if pyplot is not None and original is not None:
        current = getattr(pyplot, "show", None)
        if getattr(current, _SHOW_HOOK_MARKER, False):
            pyplot.show = original
    _STATE["show_original"] = None


def capture_shown_figures() -> list[_figure.FigureCaptureResult]:
    """Capture every open pyplot figure as PNG, once per figure per run."""

    results: list[_figure.FigureCaptureResult] = []
    if not _autotrack.autotrack_env_enabled():
        return results
    helpers = sys.modules.get("matplotlib._pylab_helpers")
    gcf = getattr(helpers, "Gcf", None)
    managers = list(gcf.get_all_fig_managers()) if gcf is not None else []
    options = _autotrack.autotrack_options()
    for manager in managers:
        with suppress(Exception):
            fig = manager.canvas.figure
            # A figure this run saved to a file is captured through that save.
            if _autotrack.figure_saved_since(fig, 0):
                continue
            number = int(getattr(manager, "num", 0) or getattr(fig, "number", 0) or 0)
            payload = _render_png(fig)
            if not payload:
                continue
            digest = hashlib.sha256(payload).hexdigest()
            if _SHOWN.get(number) == digest:
                continue
            _SHOWN[number] = digest
            results.append(_capture_shown(fig, number, payload, options))
    return results


def _capture_shown(
    fig: Any, number: int, payload: bytes, options: dict[str, Any]
) -> _figure.FigureCaptureResult:
    script = _script_path()
    anchor = script.parent if script is not None else Path.cwd()
    label = _script_label(script)
    run_id = run_identifier()
    where = script if script is not None else Path.cwd().resolve()
    stem = script.stem if script is not None else "python"
    metadata: dict[str, NoteMetadataScalar] = {
        **dict(options.get("metadata") or {}),
        "figure_autotracked": True,
        "figure_show_captured": True,
        "figure_number": number,
        "script_path": label,
        "script_run_id": run_id,
    }
    return _figure.capture_figure_bytes(
        payload,
        filename=f"{stem}-figure{number}.png",
        anchor=anchor,
        source_uri=f"{where.as_uri()}#show=run-{run_id}/figure-{number}",
        logical_id=f"show/{label}/run-{run_id}/figure-{number}",
        fig=fig,
        client=options.get("client"),
        project_id=options.get("project_id"),
        metadata=metadata,
        require_bound_project=True,
    )


def _render_png(fig: Any) -> bytes:
    buffer = io.BytesIO()
    token = _figure._AUTOTRACK_SUPPRESSED.set(True)
    try:
        fig.savefig(buffer, format="png", bbox_inches="tight")
    finally:
        _figure._AUTOTRACK_SUPPRESSED.reset(token)
    return buffer.getvalue()


def run_identifier() -> str:
    """This process's run id: a start timestamp plus a short random suffix."""

    if _STATE["run_id"] is None:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        _STATE["run_id"] = f"{stamp}-{uuid.uuid4().hex[:6]}"
    return str(_STATE["run_id"])


def _script_path() -> Path | None:
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0 or argv0 in {"-c", "-m", "-"}:
        return None
    with suppress(OSError):
        path = Path(argv0).expanduser().resolve()
        if path.is_file():
            return path
    return None


def _script_label(script: Path | None) -> str:
    if script is None:
        return "python-c" if sys.argv and sys.argv[0] == "-c" else "python"
    with suppress(Exception):
        root = capture_checkout_root(script)
        if root is not None:
            return script.relative_to(root).as_posix()
    return script.name


def _in_ipython() -> bool:
    getter = getattr(sys.modules.get("IPython"), "get_ipython", None)
    try:
        return getter is not None and getter() is not None
    except Exception:  # noqa: BLE001 - no shell.
        return False


def _reset_script_capture_state_for_tests() -> None:
    uninstall_show_hook()
    _SHOWN.clear()
    _STATE["run_id"] = None


# --- `lt setup autotrack --scripts` ------------------------------------------


def scripts_site_dir() -> Path:
    """This environment's site-packages (``purelib``), where the .pth goes."""

    return Path(sysconfig.get_paths()["purelib"])


def scripts_pth_path(site_dir: str | Path | None = None) -> Path:
    return Path(site_dir or scripts_site_dir()) / SCRIPTS_PTH_FILENAME


def bootstrap_path() -> Path:
    """The installed stdlib-only bootstrap the .pth line loads by path."""

    return (Path(__file__).parent / "_autotrack_pth.py").resolve()


def scripts_pth_source(bootstrap: str | Path | None = None) -> str:
    """The managed .pth file: comments plus one guarded ``import`` line.

    The line runs at every interpreter start, so it only reads the kill
    switch and, when the bootstrap file exists, runs its cached bytecode
    (compiling the source only when the bytecode loader is unavailable)
    without importing ``lab_tracker_client``; any error is swallowed.
    """

    boot = str(bootstrap or bootstrap_path())
    code = (
        "try:\n"
        f" p = {boot!r}\n"
        " if os.environ.get('LAB_TRACKER_AUTOTRACK', '1').strip().lower() not in "
        "('0', 'false', 'no', 'off') and os.path.isfile(p):\n"
        f"  m = type(sys)({PTH_MODULE_NAME!r})\n"
        "  m.__file__ = p\n"
        "  try:\n"
        f"   c = sys.modules['_frozen_importlib_external'].SourceFileLoader({PTH_MODULE_NAME!r}, p)"
        f".get_code({PTH_MODULE_NAME!r})\n"
        "  except Exception:\n"
        "   with open(p, 'rb') as f:\n"
        "    c = compile(f.read(), p, 'exec')\n"
        "  exec(c, m.__dict__)\n"
        f"  sys.modules[{PTH_MODULE_NAME!r}] = m\n"
        "  m.install()\n"
        "except Exception:\n"
        " pass\n"
    )
    return (
        f"{SCRIPTS_PTH_MARKER}\n"
        "# Captures matplotlib figures that plain Python scripts save or show, only in a\n"
        "# checkout bound to a project. LAB_TRACKER_AUTOTRACK=0 disables it; remove it\n"
        "# with `lt setup autotrack --scripts --uninstall`.\n"
        f"import os, sys; exec({code!r})\n"
    )


def scripts_pth_status(site_dir: str | Path | None = None) -> dict[str, Any]:
    """Read-only state of the scripts .pth for ``lt setup status``."""

    path = scripts_pth_path(site_dir)
    installed = False
    up_to_date: bool | None = None
    if path.exists():
        content = ""
        with suppress(OSError, UnicodeDecodeError):
            content = path.read_text(encoding="utf-8")
        installed = SCRIPTS_PTH_MARKER in content
        up_to_date = content == scripts_pth_source() if installed else None
    return {
        "pth_file": str(path),
        "site_packages": str(path.parent),
        "python": sys.executable,
        "installed": installed,
        "up_to_date": up_to_date,
        "conflict": path.exists() and not installed,
        "kill_switch_set": not _autotrack.autotrack_env_enabled(),
    }


def install_scripts_pth(
    *,
    dry_run: bool = False,
    uninstall: bool = False,
    site_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Write (or remove) the .pth in this environment's site-packages.

    Only scripts run by this environment's interpreter are affected, so run
    it with the Python your analysis scripts use.
    """

    path = scripts_pth_path(site_dir)
    status = scripts_pth_status(site_dir)
    payload: dict[str, Any] = {
        "command": "setup-autotrack",
        "target": "scripts",
        "pth_file": str(path),
        "site_packages": str(path.parent),
        "python": sys.executable,
        "dry_run": dry_run,
    }
    if uninstall:
        if not status["installed"]:
            payload["action"] = "absent"
            return payload
        payload["action"] = "would-remove" if dry_run else "removed"
        if not dry_run:
            path.unlink()
        return payload
    if status["conflict"]:
        raise LTValidationError(
            f"{path} exists and was not written by Lab Tracker; it is left alone. "
            "Move it aside and run this again."
        )
    if status["installed"] and status["up_to_date"]:
        payload["action"] = "current"
        return payload
    payload["action"] = (
        ("would-update" if status["installed"] else "would-install")
        if dry_run
        else ("updated" if status["installed"] else "installed")
    )
    payload["content"] = scripts_pth_source()
    payload["note"] = (
        f"Applies to scripts run by {sys.executable} (environment {sys.prefix}); run this "
        "with the Python your analysis scripts use."
    )
    if not dry_run:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(scripts_pth_source(), encoding="utf-8")
        except OSError as exc:
            raise LTValidationError(
                f"Could not write {path} ({exc}); run this with a Python environment "
                "you can install into, such as the analysis project's virtualenv."
            ) from exc
    return payload


__all__ = [
    "SCRIPTS_PTH_FILENAME",
    "activate_script_autotrack",
    "capture_shown_figures",
    "install_scripts_pth",
    "install_show_hook",
    "run_identifier",
    "scripts_pth_path",
    "scripts_pth_source",
    "scripts_pth_status",
    "uninstall_show_hook",
]
