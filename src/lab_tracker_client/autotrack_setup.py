"""``lt setup autotrack --jupyter`` / ``--scripts``: consent-gated hook installs.

Without these flags ``lt setup autotrack`` manages only the IPython startup
file (:func:`lab_tracker_client.figure_autotrack.install_ipython_startup`).
With them it manages only the named pieces:

- ``--jupyter``: the Jupyter Server config file that turns notebook saves
  into daily lab-notebook pages (:mod:`lab_tracker_client.notebook_capture`);
- ``--scripts``: the ``.pth`` file that lets plain Python scripts capture the
  figures they save or show (:mod:`lab_tracker_client.script_capture`).

Like every setup verb that writes outside the repository, a run needs
``--yes`` (apply) or ``--dry-run`` (preview); ``--uninstall`` removes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lab_tracker_client import notebook_capture, script_capture
from lab_tracker_client.client import LTValidationError

_WHAT_IS_WRITTEN = {
    "jupyter": "a Jupyter Server config file (jupyter_server_config.d/lab-tracker.json)",
    "scripts": "a .pth file into this Python environment's site-packages",
}


def setup_autotrack_targets(
    *,
    jupyter: bool,
    scripts: bool,
    yes: bool,
    dry_run: bool,
    uninstall: bool,
) -> dict[str, Any]:
    """Install, preview, or remove the requested hooks; one payload per target."""

    installers: list[tuple[str, Callable[..., dict[str, Any]]]] = []
    if jupyter:
        installers.append(("jupyter", notebook_capture.install_jupyter_hook))
    if scripts:
        installers.append(("scripts", script_capture.install_scripts_pth))
    if not (yes or dry_run):
        flags = " ".join(f"--{name}" for name, _install in installers)
        written = " and ".join(_WHAT_IS_WRITTEN[name] for name, _install in installers)
        raise SystemExit(
            f"lt setup autotrack {flags} writes {written}; "
            "pass --yes to consent or --dry-run to preview."
        )
    try:
        if not dry_run and not uninstall:
            # Refuse a conflicting target before writing any of them.
            for _name, install in installers:
                install(dry_run=True)
        results = [install(dry_run=dry_run, uninstall=uninstall) for _name, install in installers]
    except LTValidationError as exc:
        raise SystemExit(f"error: {exc}") from None
    if len(results) == 1:
        return results[0]
    return {"command": "setup-autotrack", "dry_run": dry_run, "targets": results}


def autotrack_hook_status() -> dict[str, Any]:
    """``lt setup status`` view of the Jupyter save hook and the scripts .pth."""

    status: dict[str, Any] = {}
    for name, read in (
        ("jupyter", notebook_capture.jupyter_hook_status),
        ("scripts", script_capture.scripts_pth_status),
    ):
        try:
            status[name] = read()
        except Exception as exc:  # noqa: BLE001 - status is read-only and best effort.
            status[name] = {"installed": False, "error": str(exc)}
    return status


__all__ = ["autotrack_hook_status", "setup_autotrack_targets"]
