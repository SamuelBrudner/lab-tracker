"""OS-scheduler enrollment for recurring watch capture.

Registers a per-config scheduled job running ``lt watch run --fail-silent``
(scan + sync in one process), mirroring the daily-review installer pattern:
the recurrence lives in the OS scheduler (Task Scheduler on Windows, a
managed crontab line elsewhere) — the Lab Tracker server stays free of
background machinery. All scheduler interaction goes through an injectable
``runner`` so tests never touch the real scheduler.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lab_tracker_client.client import LTValidationError

JsonObject = dict[str, Any]

DEFAULT_INTERVAL_MINUTES = 15
_TASK_NAME_PREFIX = "LabTrackerWatch"
_CRON_MARKER_PREFIX = "# LAB-TRACKER-WATCH"
# schtasks /TR rejects commands longer than 261 characters.
_SCHTASKS_COMMAND_LIMIT = 261

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def task_name(config_path: Path) -> str:
    # normcase so install/uninstall agree on Windows even when the config no
    # longer exists on disk (resolve() preserves typed case for missing paths).
    identity = os.path.normcase(str(config_path))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{_TASK_NAME_PREFIX}-{digest}"


def watch_run_command(lt_path: str, config_path: Path) -> str:
    lt = lt_path.replace("\\", "/") if sys.platform != "win32" else lt_path
    return f'"{lt}" watch run --config "{config_path}" --fail-silent'


def install_schedule(
    *,
    config_path: str | Path,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    lt_path: str | None = None,
    dry_run: bool = False,
    platform: str | None = None,
    runner: Runner | None = None,
) -> JsonObject:
    if interval_minutes < 1:
        raise LTValidationError("--interval-minutes must be 1 or greater.")
    resolved_platform = platform or sys.platform
    if resolved_platform == "win32":
        if interval_minutes > 1439:
            raise LTValidationError(
                "--interval-minutes must be 1439 or less for Task Scheduler."
            )
    elif interval_minutes > 59:
        # cron's minute field cannot express step values above 59: Vixie cron
        # silently degrades */90 to hourly and stricter crons reject the line.
        raise LTValidationError(
            "--interval-minutes must be 59 or less for the crontab backend; "
            "an hourly-or-slower cadence needs a hand-written cron entry."
        )
    resolved_config = Path(config_path).expanduser().resolve()
    if not resolved_config.exists():
        raise LTValidationError(
            f"Watch config not found: {resolved_config}. "
            "A watch folder is registered with 'lt watch add' first."
        )
    resolved_runner = runner or subprocess.run
    resolved_lt = lt_path or _default_lt_path()
    command = watch_run_command(resolved_lt, resolved_config)
    name = task_name(resolved_config)
    payload: JsonObject = {
        "command": "setup-schedule",
        "action": "would-install" if dry_run else "installed",
        "scheduler": "schtasks" if resolved_platform == "win32" else "crontab",
        "task_name": name,
        "interval_minutes": interval_minutes,
        "config": str(resolved_config),
        "scheduled_command": command,
        "dry_run": dry_run,
    }
    if resolved_platform == "win32":
        if len(command) > _SCHTASKS_COMMAND_LIMIT:
            raise LTValidationError(
                "The scheduled command exceeds the Windows Task Scheduler "
                f"limit of {_SCHTASKS_COMMAND_LIMIT} characters; use shorter "
                "paths or pass --lt-path."
            )
        if dry_run:
            return payload
        completed = resolved_runner(  # noqa: S603 - fixed executable, no shell.
            [
                "schtasks",
                "/Create",
                "/F",
                "/SC",
                "MINUTE",
                "/MO",
                str(interval_minutes),
                "/TN",
                name,
                "/TR",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise LTValidationError(
                f"schtasks /Create failed: {(completed.stderr or completed.stdout).strip()}"
            )
        return payload
    cron_line = (
        f"*/{interval_minutes} * * * * {command} "
        f"{_CRON_MARKER_PREFIX} {name}"
    )
    payload["cron_line"] = cron_line
    if dry_run:
        return payload
    existing = _read_crontab(resolved_runner)
    kept = [
        line
        for line in existing.splitlines()
        if f"{_CRON_MARKER_PREFIX} {name}" not in line
    ]
    kept.append(cron_line)
    # Preserve the user's crontab lines verbatim — only our marked line is
    # managed; no whole-file strip.
    _write_crontab(resolved_runner, "\n".join(kept) + "\n")
    return payload


def uninstall_schedule(
    *,
    config_path: str | Path,
    dry_run: bool = False,
    platform: str | None = None,
    runner: Runner | None = None,
) -> JsonObject:
    resolved_config = Path(config_path).expanduser().resolve()
    resolved_platform = platform or sys.platform
    resolved_runner = runner or subprocess.run
    name = task_name(resolved_config)
    payload: JsonObject = {
        "command": "setup-schedule",
        "action": "would-remove" if dry_run else "removed",
        "scheduler": "schtasks" if resolved_platform == "win32" else "crontab",
        "task_name": name,
        "config": str(resolved_config),
        "dry_run": dry_run,
    }
    if dry_run:
        return payload
    if resolved_platform == "win32":
        completed = resolved_runner(  # noqa: S603 - fixed executable, no shell.
            ["schtasks", "/Delete", "/F", "/TN", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            detail = ((completed.stderr or "") + (completed.stdout or "")).lower()
            if "cannot find" in detail or "does not exist" in detail:
                payload["action"] = "absent"
                return payload
            # Access-denied etc.: the task still exists and keeps firing —
            # reporting 'absent' would hide that forever.
            raise LTValidationError(
                "schtasks /Delete failed: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )
        return payload
    existing = _read_crontab(resolved_runner)
    kept = [
        line
        for line in existing.splitlines()
        if f"{_CRON_MARKER_PREFIX} {name}" not in line
    ]
    if len(kept) == len(existing.splitlines()):
        payload["action"] = "absent"
        return payload
    _write_crontab(resolved_runner, ("\n".join(kept) + "\n") if kept else "")
    return payload


def _read_crontab(runner: Runner) -> str:
    completed = runner(  # noqa: S603 - fixed executable, no shell.
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode == 0:
        return completed.stdout
    # Only a definite "no crontab for this user" may be treated as empty:
    # writing back on any OTHER failure would replace the user's real
    # crontab with just our line.
    if "no crontab" in (completed.stderr or "").lower():
        return ""
    raise LTValidationError(
        f"crontab -l failed: {(completed.stderr or completed.stdout).strip()!r}; "
        "refusing to rewrite the crontab from an unknown state."
    )


def _write_crontab(runner: Runner, content: str) -> None:
    completed = runner(  # noqa: S603 - fixed executable, no shell.
        ["crontab", "-"],
        input=content,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise LTValidationError(
            f"crontab update failed: {(completed.stderr or completed.stdout).strip()}"
        )


def _default_lt_path() -> str:
    from lab_tracker_client.executables import resolve_executable

    path = resolve_executable("lt").path
    if path:
        return path
    raise LTValidationError(
        "Could not locate the lt executable for the scheduled command; pass --lt-path."
    )
