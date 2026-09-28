"""`lt setup autotrack --r`: the managed ``.Rprofile`` block for R figure autotrack.

The R side lives in the ``labtracker`` R package (``r/labtracker`` in the
repository). Its implementation is one file, and this Python package ships an
identical copy as package data (``r_labtracker/labtracker.R``), so R users get
autotrack from the same ``uv tool install`` / ``pip install`` that gives them
``lt``, without CRAN. The managed block this module writes into the user's R
profile:

* is skipped entirely when ``LAB_TRACKER_AUTOTRACK`` turns autotrack off;
* waits until ``grDevices`` is attached (default packages attach after the
  profile runs), then calls ``labtracker::autotrack()`` when the R package is
  installed, and otherwise ``sys.source()``-s the shipped copy into a private
  environment and attaches its four public functions as ``tools:labtracker``;
* records the ``lt`` that installed it, which the R side uses after
  ``LAB_TRACKER_LT`` and before ``lt`` on PATH (an R GUI often starts with a
  shorter PATH than the shell);
* never raises into R: a load failure is swallowed, and a missing shipped
  copy (the Python package moved) prints one message pointing back here.

Like every setup verb that writes outside the repository, this is consent
gated at the CLI layer (``--dry-run`` shows a diff, ``--yes`` applies,
``--uninstall`` removes the block and leaves the rest of the file untouched).
"""

from __future__ import annotations

import difflib
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from lab_tracker_client.client import LTValidationError

R_PROFILE_USER_ENV = "R_PROFILE_USER"
RPROFILE_BEGIN = "# --- BEGIN LAB TRACKER AUTOTRACK (managed by `lt setup autotrack --r`) ---"
RPROFILE_END = "# --- END LAB TRACKER AUTOTRACK ---"
SHIPPED_R_SOURCE = Path(__file__).resolve().parent / "r_labtracker" / "labtracker.R"
R_TOOLS_ENV_NAME = "tools:labtracker"
R_PUBLIC_FUNCTIONS = ("autotrack", "autotrack_status", "capture_file", "is_autotracking")

JsonObject = dict[str, Any]


def shipped_r_source() -> Path:
    """The R implementation shipped with this Python package."""

    return SHIPPED_R_SOURCE


def rprofile_path() -> Path:
    """The user R profile R reads: ``R_PROFILE_USER``, else ``~/.Rprofile``.

    ``~`` is R's home: on Windows that is ``R_USER`` or ``HOME`` when set,
    else the Documents folder. (A ``.Rprofile`` in the directory R starts in
    replaces this file for that session; see docs/lab-tracker-r.md.)
    """

    override = os.getenv(R_PROFILE_USER_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _r_home() / ".Rprofile"


def _r_home() -> Path:
    if sys.platform == "win32":
        for key in ("R_USER", "HOME"):
            value = os.getenv(key, "").strip()
            if value:
                return Path(value).expanduser()
        return Path.home() / "Documents"
    return Path.home()


def recorded_lt_path() -> str:
    """The ``lt`` next to this interpreter, else ``lt`` on PATH, else ``""``."""

    sibling = Path(sys.executable).parent / ("lt.exe" if sys.platform == "win32" else "lt")
    if sibling.exists():
        return str(sibling)
    return shutil.which("lt") or ""


def rprofile_block(*, r_source: Path | None = None, lt_path: str | None = None) -> str:
    """The managed block text, ending in a newline."""

    source = (r_source or shipped_r_source()).as_posix()
    lt = (lt_path if lt_path is not None else recorded_lt_path()).replace("\\", "/")
    public = ", ".join(_r_string(name) for name in R_PUBLIC_FUNCTIONS)
    lines = [
        RPROFILE_BEGIN,
        "# Captures figures saved with ggplot2::ggsave() or the png/jpeg/tiff/bmp/pdf",
        "# file devices into Lab Tracker as staged evidence, only inside a checkout",
        "# bound to a project (lt_ids.json) or with LAB_TRACKER_PROJECT_ID set. Set",
        "# LAB_TRACKER_AUTOTRACK=0 to disable, or remove this block with",
        "# `lt setup autotrack --r --uninstall --yes`. Fail-soft: never errors into R.",
        "local({",
        '  if (!(tolower(trimws(Sys.getenv("LAB_TRACKER_AUTOTRACK", "1"))) %in%',
        '        c("0", "false", "no", "off"))) {',
        f"    lt_source <- {_r_string(source)}",
        f"    lt_path <- {_r_string(lt)}",
        "    start <- function(...) try({",
        '      if (requireNamespace("labtracker", quietly = TRUE)) {',
        "        labtracker::autotrack(lt = lt_path)",
        "      } else if (file.exists(lt_source)) {",
        "        env <- new.env(parent = baseenv())",
        "        sys.source(lt_source, envir = env)",
        f"        if ({_r_string(R_TOOLS_ENV_NAME)} %in% search()) "
        f"detach({_r_string(R_TOOLS_ENV_NAME)})",
        f"        attach(mget(c({public}), envir = env),",
        f"               name = {_r_string(R_TOOLS_ENV_NAME)}, warn.conflicts = FALSE)",
        "        env$autotrack(lt = lt_path)",
        "      } else {",
        '        message("Lab Tracker autotrack: ", lt_source, " is missing; run ",',
        '                "`lt setup autotrack --r --yes` again to repoint ~/.Rprofile.")',
        "      }",
        "    }, silent = TRUE)",
        '    if ("package:grDevices" %in% search()) start() else',
        '      setHook(packageEvent("grDevices", "attach"), start)',
        "  }",
        "})",
        RPROFILE_END,
    ]
    return "\n".join(lines) + "\n"


def _r_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _block_span(content: str, path: Path) -> tuple[int, int] | None:
    """``(start, end)`` of the managed block including its final newline, or ``None``.

    Unpaired or repeated markers are refused rather than guessed around: the
    file belongs to the person, so only a block this command can recognise
    exactly is ever rewritten.
    """

    begins = content.count(RPROFILE_BEGIN)
    ends = content.count(RPROFILE_END)
    if begins == 0 and ends == 0:
        return None
    start = content.find(RPROFILE_BEGIN)
    end_marker = content.find(RPROFILE_END)
    if begins != 1 or ends != 1 or end_marker < start:
        raise LTValidationError(
            f"{path} has unpaired or repeated Lab Tracker autotrack markers. "
            "Repair or remove them by hand before re-running `lt setup autotrack --r`."
        )
    end = end_marker + len(RPROFILE_END)
    if content.startswith("\r\n", end):
        end += 2
    elif content.startswith("\n", end):
        end += 1
    return start, end


def rprofile_status() -> JsonObject:
    """Read-only state of the managed block (for ``lt setup status``)."""

    path = rprofile_path()
    installed = False
    current: bool | None = None
    with_error: str | None = None
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
            span = _block_span(content, path)
        except (OSError, UnicodeDecodeError, LTValidationError) as exc:
            with_error = str(exc)
            span = None
        else:
            installed = span is not None
            if span is not None:
                current = content[span[0] : span[1]] == rprofile_block()
    status: JsonObject = {
        "rprofile": str(path),
        "installed": installed,
        "up_to_date": current,
        "r_source": str(shipped_r_source()),
        "r_source_present": shipped_r_source().exists(),
        "rscript": shutil.which("Rscript"),
        "kill_switch_set": not _autotrack_env_enabled(),
    }
    if with_error:
        status["error"] = with_error
    return status


def install_rprofile(*, dry_run: bool = False, uninstall: bool = False) -> JsonObject:
    """Add, refresh, or (``uninstall``) remove the managed block in the R profile.

    Everything outside the block is preserved. ``dry_run`` reports the action
    and a unified diff without writing.
    """

    path = rprofile_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    span = _block_span(existing, path)
    payload: JsonObject = {
        "command": "setup-autotrack",
        "language": "r",
        "rprofile": str(path),
        "r_source": str(shipped_r_source()),
        "dry_run": dry_run,
    }
    if uninstall:
        if span is None:
            payload["action"] = "absent"
            return payload
        updated = _without_block(existing, span)
        payload["action"] = "would-remove" if dry_run else "removed"
    else:
        block = rprofile_block()
        payload["lt"] = recorded_lt_path()
        if span is not None and existing[span[0] : span[1]] == block:
            payload["action"] = "current"
            return payload
        if span is None:
            updated = _with_block_appended(existing, block)
            payload["action"] = "would-install" if dry_run else "installed"
        else:
            updated = existing[: span[0]] + block + existing[span[1] :]
            payload["action"] = "would-update" if dry_run else "updated"
    payload["diff"] = "".join(
        difflib.unified_diff(
            existing.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    if dry_run:
        return payload
    if uninstall and not updated.strip():
        # Only the block was ever in the file (this command created it).
        path.unlink()
        payload["file_removed"] = True
        return payload
    _write_text_atomic(path, updated)
    return payload


def _with_block_appended(existing: str, block: str) -> str:
    if not existing:
        return block
    separator = "" if existing.endswith("\n") else "\n"
    return f"{existing}{separator}\n{block}"


def _without_block(existing: str, span: tuple[int, int]) -> str:
    before = existing[: span[0]]
    after = existing[span[1] :]
    if not after and before.endswith("\n\n"):
        # Drop the blank line the install put in front of an appended block.
        before = before[:-1]
    return before + after


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(content, encoding="utf-8", newline="")
    if path.exists():
        shutil.copymode(path, tmp_path)
    os.replace(tmp_path, path)


def _autotrack_env_enabled() -> bool:
    from lab_tracker_client.figure_autotrack import autotrack_env_enabled

    return autotrack_env_enabled()


__all__ = [
    "RPROFILE_BEGIN",
    "RPROFILE_END",
    "install_rprofile",
    "recorded_lt_path",
    "rprofile_block",
    "rprofile_path",
    "rprofile_status",
    "shipped_r_source",
]
