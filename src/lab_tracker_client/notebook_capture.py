"""Jupyter notebook saves as daily lab-notebook pages (staged notes).

:func:`post_save_hook` is a Jupyter Server ``ContentsManager`` post-save hook.
For a ``.ipynb`` saved inside a bound checkout (``lt_ids.json``, or
``LAB_TRACKER_PROJECT_ID`` in the server's environment) it writes one
watch-outbox staged-note event per notebook per local day. The event body is
a bounded page: a pointer to the notebook (path, ``file://`` URI, SHA-256,
size), its kernel, cell counts, the text of its markdown cells (at most
:data:`NOTEBOOK_MARKDOWN_MAX_CHARS` characters), and a one-line summary of
each code cell (line count, imported modules, defined names, output kinds;
never the code itself). The notebook file stays where it is.

Jupyter autosaves every couple of minutes, so a day of saves is coalesced
into one page: while the day's event is unsynced each save replaces it, and
the event carries ``payload.deliver_after`` (the next local midnight), which
``lt watch sync`` / ``lt outbox sync`` honour, so the page is delivered once
the day is over, reflecting the day's last save. A save on a later day starts
a new page. Unbound notebooks are skipped with one stderr notice per checkout
and nothing is queued. The ``LAB_TRACKER_AUTOTRACK=0`` kill switch turns the
hook off, and it never raises into a save.

``lt setup autotrack --jupyter`` enables this module as a Jupyter Server
extension (``jupyter_server_config.d/lab-tracker.json``); on load the
extension registers the hook with ``register_post_save_hook``, alongside any
``post_save_hook`` already configured, never in its place.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client import outbox as _outbox
from lab_tracker_client import watch as _watch
from lab_tracker_client.capture_project import (
    CaptureProject,
    capture_checkout_root,
    resolve_capture_project,
)
from lab_tracker_client.client import LTValidationError
from lab_tracker_client.figure_autotrack import autotrack_env_enabled
from lab_tracker_client.gitinfo import sanitize_remote_url
from lab_tracker_client.session_context import read_active_session

NOTEBOOK_CAPTURE_KIND = "notebook"
NOTEBOOK_ADAPTER = "lab-tracker-client-notebook"
NOTEBOOK_PROVIDER = "jupyter-notebook"
# Bounds on what a page copies out of the notebook.
NOTEBOOK_MARKDOWN_MAX_CHARS = 20_000
NOTEBOOK_CODE_CELLS_MAX = 200
NOTEBOOK_CODE_LINE_MAX_CHARS = 200
NOTEBOOK_SUMMARY_NAMES_MAX = 8
# A notebook larger than this (usually from embedded outputs) is hashed but not
# parsed, so an autosave never stalls the Jupyter server on a huge file.
NOTEBOOK_PARSE_MAX_BYTES = 25 * 1024 * 1024
EXTENSION_MODULE = "lab_tracker_client.notebook_capture"
HOOK_IMPORT_STRING = f"{EXTENSION_MODULE}.post_save_hook"
JUPYTER_CONFIG_DIR_ENV = "JUPYTER_CONFIG_DIR"
JUPYTER_CONFIG_PATH_ENV = "JUPYTER_CONFIG_PATH"
JUPYTER_SERVER_CONFIG_D = "jupyter_server_config.d"
JUPYTER_CONFIG_FILENAME = "lab-tracker.json"
NOTEBOOK_UNBOUND_REASON = "project_unbound"
NOTEBOOK_UNBOUND_NOTICE = (
    "Lab Tracker is not capturing notebook saves in {checkout}: {why}. Nothing was queued. {remedy}"
)
_CHECKOUT_REMEDY = (
    "Bind the checkout with `lt project bind`, or set LAB_TRACKER_PROJECT_ID for the "
    "Jupyter server."
)
_LOOSE_FOLDER_REMEDY = "Set LAB_TRACKER_PROJECT_ID for the Jupyter server."
_IMPORT_PATTERN = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([\w., ]+))")
_DEFINE_PATTERN = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)")
_URL_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>()\[\]\"'`]+")
_DATA_URI_PATTERN = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=\s]+")
_PY_HOOK_PATTERN = re.compile(r"^\s*c\.\w+\.post_save_hook\s*=\s*(.+?)\s*$", re.MULTILINE)
_WARNED: set[str] = set()


@dataclass
class NotebookSummary:
    """Bounded facts about one parsed notebook."""

    kernel_name: str = ""
    kernel_display_name: str = ""
    language: str = ""
    cell_count: int = 0
    code_cell_count: int = 0
    markdown_cell_count: int = 0
    raw_cell_count: int = 0
    executed_code_cell_count: int = 0
    image_output_count: int = 0
    error_output_count: int = 0
    markdown_text: str = ""
    markdown_omitted_chars: int = 0
    code_cells: list[str] = field(default_factory=list)
    code_cells_omitted: int = 0


def post_save_hook(
    model: Mapping[str, Any] | None = None,
    os_path: str | os.PathLike[str] | None = None,
    contents_manager: Any = None,  # noqa: ARG001 - Jupyter's hook signature.
    **_kwargs: Any,
) -> None:
    """Jupyter Server post-save hook: queue the saved notebook's day page.

    Jupyter calls it with keyword arguments after every save; a failure here
    would surface as a failed save, so every error is swallowed (reported
    once per cause on stderr).
    """

    try:
        if not autotrack_env_enabled() or not os_path:
            return
        path = Path(os.fspath(os_path))
        if path.suffix.lower() != ".ipynb":
            return
        if isinstance(model, Mapping) and model.get("type") not in (None, "notebook"):
            return
        record_notebook_save(path)
    except Exception as exc:  # noqa: BLE001 - never break a notebook save.
        _warn_once(
            f"notebook-save-failed:{type(exc).__name__}",
            f"Lab Tracker could not record the notebook save of {os_path}: {exc}",
        )


def record_notebook_save(
    path: str | Path,
    *,
    now: datetime | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Queue (or replace) the notebook's page for the local day of ``now``.

    Returns what happened: ``queued`` (the day's first save), ``replaced``
    (the pending page now reflects this save), ``unchanged`` (same bytes as
    the pending page), ``already_synced`` (the day's page was delivered; the
    next day's save starts a new one), or ``skipped`` for an unbound notebook.
    """

    notebook = Path(path).expanduser().resolve()
    # The local day is the one in ``now``'s own zone (the machine's by default).
    local_now = now if now is not None and now.tzinfo is not None else (now or datetime.now())
    local_now = local_now if local_now.tzinfo is not None else local_now.astimezone()
    day = local_now.date().isoformat()
    result: dict[str, Any] = {"notebook": str(notebook), "local_day": day}
    capture_project = resolve_capture_project(notebook, project_id=project_id)
    if capture_project is None or not capture_project.bound:
        _warn_unbound(notebook, capture_project)
        return {**result, "action": "skipped", "reason": NOTEBOOK_UNBOUND_REASON}
    root = capture_checkout_root(notebook) or notebook.parent
    label = _relative_label(notebook, root)
    raw, content_hash, size_bytes = _read_notebook(notebook)
    summary, summary_error = _summarize(raw)
    deliver_after = _next_local_midnight(local_now)
    session = read_active_session(root)
    session_id = str(session["session_id"]) if session and session.get("session_id") else None
    metadata = _page_metadata(
        label=label,
        content_hash=content_hash,
        size_bytes=size_bytes,
        day=day,
        saved_at=local_now,
        summary=summary,
    )
    event = _watch.make_event(
        capture_id=_capture_id(notebook),
        event_id=f"notebook-day-{day}",
        capture_kind=NOTEBOOK_CAPTURE_KIND,
        adapter=NOTEBOOK_ADAPTER,
        sink=_watch.SINK_STAGED_NOTE,
        observed_at=local_now.astimezone(timezone.utc).isoformat(),
        source={
            "provider": NOTEBOOK_PROVIDER,
            "uri": notebook.as_uri(),
            "external_id": f"notebook:{label}@{day}",
            # Deliberately not ``path``: the sync must not upload the notebook.
            "notebook_path": str(notebook),
            "root": str(root),
            "root_uri": root.as_uri(),
            "relative_path": label,
            "content_hash": content_hash,
            "size_bytes": size_bytes,
            **_watch.active_session_source(session),
        },
        context={"project_id": capture_project.project_id, "session_id": session_id},
        artifacts=[
            {
                "title": notebook.name,
                "kind": NOTEBOOK_CAPTURE_KIND,
                "uri": notebook.as_uri(),
                "content_hash": content_hash,
                "size_bytes": size_bytes,
                "summary": "Jupyter notebook; the file stays in the checkout.",
            }
        ],
        payload={
            "title": f"{notebook.name} notebook page {day}",
            "summary": f"Jupyter notebook page for {label} on {day}.",
            "status": "staged",
            "body": render_notebook_page(
                label=label,
                uri=notebook.as_uri(),
                content_hash=content_hash,
                size_bytes=size_bytes,
                day=day,
                saved_at=local_now,
                summary=summary,
                summary_error=summary_error,
            ),
            "metadata": metadata,
            _watch.DELIVER_AFTER_KEY: deliver_after,
            "local_day": day,
        },
    )
    outbox = _outbox_for(root)
    event_file = _watch.event_path(event, outbox)
    result.update(
        {
            "project_id": capture_project.project_id,
            "event_path": str(event_file),
            "deliver_after": deliver_after,
            "content_hash": content_hash,
        }
    )
    existing = _read_existing(event_file)
    if existing is not None:
        sync = existing.get("sync", {})
        if str(sync.get("status") or "") == "synced" or sync.get("note_id"):
            return {**result, "action": "already_synced"}
        if existing["source"].get("content_hash") == content_hash:
            return {**result, "action": "unchanged"}
        event["sync"] = {
            "status": "pending",
            "attempts": int(sync.get("attempts") or 0),
            "replaced_at": _watch.utc_now(),
        }
    event_file.parent.mkdir(parents=True, exist_ok=True)
    _outbox.write_json_atomic(event_file, _watch.validate_event(event))
    return {**result, "action": "replaced" if existing is not None else "queued"}


def summarize_notebook(notebook: Mapping[str, Any]) -> NotebookSummary:
    """Bounded summary of a parsed ``.ipynb``: counts, markdown text, code cell lines."""

    summary = NotebookSummary()
    metadata = _mapping(notebook.get("metadata"))
    kernelspec = _mapping(metadata.get("kernelspec"))
    language_info = _mapping(metadata.get("language_info"))
    summary.kernel_name = str(kernelspec.get("name") or "")
    summary.kernel_display_name = str(kernelspec.get("display_name") or "")
    summary.language = str(language_info.get("name") or kernelspec.get("language") or "")
    cells = notebook.get("cells")
    cells = cells if isinstance(cells, list) else []
    markdown_parts: list[str] = []
    for index, cell in enumerate(cells, start=1):
        if not isinstance(cell, Mapping):
            continue
        summary.cell_count += 1
        cell_type = str(cell.get("cell_type") or "")
        source = _cell_source(cell)
        if cell_type == "markdown":
            summary.markdown_cell_count += 1
            text = _clean_markdown(source)
            if text:
                markdown_parts.append(text)
        elif cell_type == "code":
            summary.code_cell_count += 1
            line = _summarize_code_cell(index, cell, source, summary)
            if len(summary.code_cells) < NOTEBOOK_CODE_CELLS_MAX:
                summary.code_cells.append(line)
            else:
                summary.code_cells_omitted += 1
        elif cell_type == "raw":
            summary.raw_cell_count += 1
    markdown = "\n\n---\n\n".join(markdown_parts)
    if len(markdown) > NOTEBOOK_MARKDOWN_MAX_CHARS:
        summary.markdown_omitted_chars = len(markdown) - NOTEBOOK_MARKDOWN_MAX_CHARS
        markdown = markdown[:NOTEBOOK_MARKDOWN_MAX_CHARS].rstrip()
    summary.markdown_text = markdown
    return summary


def render_notebook_page(
    *,
    label: str,
    uri: str,
    content_hash: str,
    size_bytes: int,
    day: str,
    saved_at: datetime,
    summary: NotebookSummary | None,
    summary_error: str = "",
) -> str:
    """The page's markdown body (bounded; see the module constants)."""

    lines = [
        f"# Notebook page: {label} ({day})",
        "",
        f"Jupyter saves of `{label}` on {day}, as of the last save at "
        f"{saved_at.isoformat(timespec='seconds')}. The notebook stays in the "
        "checkout; this page is a pointer and a bounded summary.",
        "",
        "## Notebook",
        f"- Path: `{label}`",
        f"- Source URI: {uri}",
        f"- SHA-256: `{content_hash}`",
        f"- Size bytes: {size_bytes}",
    ]
    if summary is None:
        lines.extend(["", f"_Not summarized: {summary_error}._"])
        return "\n".join(lines).strip() + "\n"
    kernel = summary.kernel_name or "unknown"
    if summary.kernel_display_name:
        kernel = f"{kernel} ({summary.kernel_display_name})"
    language = f"; language {summary.language}" if summary.language else ""
    lines.append(f"- Kernel: {kernel}{language}")
    lines.append(
        f"- Cells: {summary.cell_count} ({summary.code_cell_count} code, "
        f"{summary.executed_code_cell_count} executed; {summary.markdown_cell_count} markdown; "
        f"{summary.raw_cell_count} raw)"
    )
    lines.append(
        f"- Outputs: {summary.image_output_count} images, {summary.error_output_count} errors"
    )
    lines.extend(
        [
            "",
            "## Markdown cells",
            "",
            f"_Markdown cell text, up to {NOTEBOOK_MARKDOWN_MAX_CHARS} characters._",
            "",
            summary.markdown_text or "_No markdown text._",
        ]
    )
    if summary.markdown_omitted_chars:
        lines.extend(["", f"_[{summary.markdown_omitted_chars} more characters omitted]_"])
    lines.extend(["", "## Code cells (summarized, not copied)", ""])
    lines.extend(summary.code_cells or ["_No code cells._"])
    if summary.code_cells_omitted:
        lines.append(f"- _{summary.code_cells_omitted} more code cells not listed_")
    return "\n".join(lines).strip() + "\n"


def _summarize_code_cell(
    index: int, cell: Mapping[str, Any], source: str, summary: NotebookSummary
) -> str:
    execution_count = cell.get("execution_count")
    if isinstance(execution_count, int):
        summary.executed_code_cell_count += 1
        run = f"In [{execution_count}]"
    else:
        run = "not run"
    source_lines = [line for line in source.splitlines() if line.strip()]
    imports: list[str] = []
    defines: list[str] = []
    for line in source_lines:
        match = _IMPORT_PATTERN.match(line)
        if match:
            modules = [match.group(1)] if match.group(1) else match.group(2).split(",")
            for module in modules:
                name = module.strip().split(" ")[0].split(".")[0]
                if name and name not in imports:
                    imports.append(name)
        defined = _DEFINE_PATTERN.match(line)
        if defined and defined.group(1) not in defines:
            defines.append(defined.group(1))
    parts = [f"- Cell {index}", run, f"{len(source_lines)} lines"]
    if imports:
        parts.append("imports " + ", ".join(imports[:NOTEBOOK_SUMMARY_NAMES_MAX]))
    if defines:
        parts.append("defines " + ", ".join(defines[:NOTEBOOK_SUMMARY_NAMES_MAX]))
    outputs = _summarize_outputs(cell.get("outputs"), summary)
    if outputs:
        parts.append("outputs " + outputs)
    line = " · ".join(parts)
    if len(line) > NOTEBOOK_CODE_LINE_MAX_CHARS:
        line = line[: NOTEBOOK_CODE_LINE_MAX_CHARS - 1].rstrip() + "…"
    return line


def _summarize_outputs(outputs: Any, summary: NotebookSummary) -> str:
    if not isinstance(outputs, list):
        return ""
    images = streams = results = 0
    errors: list[str] = []
    for output in outputs:
        if not isinstance(output, Mapping):
            continue
        kind = str(output.get("output_type") or "")
        data = _mapping(output.get("data"))
        if kind == "stream":
            streams += 1
        elif kind == "error":
            # The exception class only; its message could carry paths or secrets.
            errors.append(str(output.get("ename") or "error")[:60])
        elif any(str(mime).startswith("image/") for mime in data):
            images += 1
        elif kind in {"execute_result", "display_data"}:
            results += 1
    summary.image_output_count += images
    summary.error_output_count += len(errors)
    parts = []
    if images:
        parts.append(f"{images} image" + ("s" if images != 1 else ""))
    if results:
        parts.append(f"{results} result" + ("s" if results != 1 else ""))
    if streams:
        parts.append(f"{streams} stream" + ("s" if streams != 1 else ""))
    if errors:
        parts.append("error " + ", ".join(errors[:NOTEBOOK_SUMMARY_NAMES_MAX]))
    return ", ".join(parts)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _cell_source(cell: Mapping[str, Any]) -> str:
    source = cell.get("source")
    if isinstance(source, list):
        return "".join(str(part) for part in source)
    return str(source or "")


def _clean_markdown(text: str) -> str:
    """Markdown text without embedded images or URL-carried credentials."""

    text = _DATA_URI_PATTERN.sub("data:(embedded image omitted)", text)
    return _URL_PATTERN.sub(lambda match: sanitize_remote_url(match.group(0)), text).strip()


def _summarize(raw: bytes | None) -> tuple[NotebookSummary | None, str]:
    if raw is None:
        return None, f"the notebook is larger than {NOTEBOOK_PARSE_MAX_BYTES} bytes"
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "the file is not valid notebook JSON"
    if not isinstance(parsed, Mapping):
        return None, "the file is not a notebook object"
    return summarize_notebook(parsed), ""


def _read_notebook(path: Path) -> tuple[bytes | None, str, int]:
    """(bytes when small enough to parse, SHA-256, size) from one read."""

    size_bytes = path.stat().st_size
    if size_bytes > NOTEBOOK_PARSE_MAX_BYTES:
        return None, _watch.file_sha256(path), size_bytes
    raw = path.read_bytes()
    return raw, hashlib.sha256(raw).hexdigest(), len(raw)


def _page_metadata(
    *,
    label: str,
    content_hash: str,
    size_bytes: int,
    day: str,
    saved_at: datetime,
    summary: NotebookSummary | None,
) -> dict[str, NoteMetadataScalar]:
    metadata: dict[str, NoteMetadataScalar] = {
        "notebook_path": label,
        "notebook_sha256": content_hash,
        "notebook_size_bytes": size_bytes,
        "notebook_local_day": day,
        "notebook_saved_at": saved_at.isoformat(timespec="seconds"),
        "notebook_page": True,
    }
    if summary is not None:
        metadata.update(
            {
                "notebook_kernel": summary.kernel_name,
                "notebook_language": summary.language,
                "notebook_cell_count": summary.cell_count,
                "notebook_code_cell_count": summary.code_cell_count,
                "notebook_markdown_cell_count": summary.markdown_cell_count,
                "notebook_markdown_truncated": summary.markdown_omitted_chars > 0,
            }
        )
    return metadata


def _next_local_midnight(local_now: datetime) -> str:
    midnight = datetime.combine(local_now.date() + timedelta(days=1), time.min, local_now.tzinfo)
    return midnight.astimezone(timezone.utc).isoformat()


def _relative_label(notebook: Path, root: Path) -> str:
    with suppress(ValueError):
        return notebook.relative_to(root.resolve()).as_posix()
    return notebook.name


def _capture_id(notebook: Path) -> str:
    """One capture id per notebook file: a readable stem plus a path digest."""

    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", notebook.stem).strip("-")[:32] or "notebook"
    digest = hashlib.sha256(str(notebook).encode("utf-8")).hexdigest()[:10]
    return f"notebook-{stem}-{digest}"


def _outbox_for(root: Path) -> Path:
    from lab_tracker_client import git_capture

    config, _config_error = git_capture.resolve_watch_config(root)
    return config.outbox_path()


def _read_existing(event_file: Path) -> dict[str, Any] | None:
    if not event_file.exists():
        return None
    try:
        return _watch.read_event(event_file)
    except Exception:  # noqa: BLE001 - an unreadable page is simply replaced.
        return None


def _warn_unbound(notebook: Path, capture_project: CaptureProject | None) -> None:
    root = capture_checkout_root(notebook)
    if root is None:
        _warn_once(
            f"{NOTEBOOK_UNBOUND_REASON}:{notebook.parent}",
            NOTEBOOK_UNBOUND_NOTICE.format(
                checkout=notebook.parent,
                why="that folder is not inside a git checkout",
                remedy=_LOOSE_FOLDER_REMEDY,
            ),
        )
        return
    why = (
        "that checkout names its project only in its watch config, not in lt_ids.json"
        if capture_project is not None
        else "that checkout is not bound to a project (no lt_ids.json)"
    )
    _warn_once(
        f"{NOTEBOOK_UNBOUND_REASON}:{root}",
        NOTEBOOK_UNBOUND_NOTICE.format(checkout=root, why=why, remedy=_CHECKOUT_REMEDY),
    )


def _warn_once(key: str, message: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    with suppress(Exception):
        print(message, file=sys.stderr)


def _reset_notebook_capture_state_for_tests() -> None:
    _WARNED.clear()


# --- Jupyter Server extension ------------------------------------------------


def _jupyter_server_extension_points() -> list[dict[str, str]]:
    """Jupyter Server extension metadata: this module is the extension."""

    return [{"module": EXTENSION_MODULE}]


def _load_jupyter_server_extension(serverapp: Any) -> None:
    """Register :func:`post_save_hook` on the server's contents manager."""

    log = getattr(serverapp, "log", None)
    try:
        outcome = register_post_save_hook(serverapp.contents_manager)
    except Exception as exc:  # noqa: BLE001 - never stop the server from starting.
        with suppress(Exception):
            log.warning("Lab Tracker notebook capture was not registered: %s", exc)
        return
    with suppress(Exception):
        if outcome == "refused":
            log.warning(
                "Lab Tracker notebook capture was not registered: this Jupyter Server "
                "has no register_post_save_hook and another post_save_hook is "
                "configured, which Lab Tracker never replaces."
            )
        else:
            log.info("Lab Tracker notebook capture: post-save hook %s.", outcome)


def register_post_save_hook(contents_manager: Any) -> str:
    """Add the hook next to any configured one; never replace another hook.

    Returns ``configured`` (already set as ``post_save_hook``),
    ``already_registered``, ``registered`` (via ``register_post_save_hook``),
    ``installed`` (an older server with no hook set), or ``refused`` (an older
    server whose single ``post_save_hook`` slot holds someone else's hook).
    """

    configured = getattr(contents_manager, "post_save_hook", None)
    if _is_this_hook(configured):
        return "configured"
    registered: Sequence[Any] = getattr(contents_manager, "_post_save_hooks", None) or ()
    if any(_is_this_hook(hook) for hook in registered):
        return "already_registered"
    register = getattr(contents_manager, "register_post_save_hook", None)
    if callable(register):
        register(post_save_hook)
        return "registered"
    if configured is None:
        contents_manager.post_save_hook = post_save_hook
        return "installed"
    return "refused"


def _is_this_hook(hook: Any) -> bool:
    if hook is post_save_hook:
        return True
    if isinstance(hook, str):
        return hook == HOOK_IMPORT_STRING
    return (
        getattr(hook, "__module__", None) == EXTENSION_MODULE
        and getattr(hook, "__name__", None) == "post_save_hook"
    )


# --- `lt setup autotrack --jupyter` ------------------------------------------


def jupyter_config_dir() -> Path:
    """The user's Jupyter config dir: ``JUPYTER_CONFIG_DIR`` or ``~/.jupyter``."""

    override = os.getenv(JUPYTER_CONFIG_DIR_ENV)
    return Path(override).expanduser() if override else Path.home() / ".jupyter"


def jupyter_hook_config_path() -> Path:
    return jupyter_config_dir() / JUPYTER_SERVER_CONFIG_D / JUPYTER_CONFIG_FILENAME


def jupyter_hook_source() -> str:
    """The managed config file: enable this module as a server extension."""

    config = {"ServerApp": {"jpserver_extensions": {EXTENSION_MODULE: True}}}
    return json.dumps(config, indent=2) + "\n"


def jupyter_hook_status() -> dict[str, Any]:
    """Read-only state of the Jupyter save hook for ``lt setup status``."""

    path = jupyter_hook_config_path()
    installed = False
    up_to_date: bool | None = None
    conflict = False
    if path.exists():
        content = ""
        with suppress(OSError, UnicodeDecodeError):
            content = path.read_text(encoding="utf-8")
        installed = _enables_this_extension(content)
        conflict = not installed
        up_to_date = content == jupyter_hook_source() if installed else None
    return {
        "config_file": str(path),
        "installed": installed,
        "up_to_date": up_to_date,
        "conflict": conflict,
        "other_post_save_hooks": other_post_save_hooks(),
        "kill_switch_set": not autotrack_env_enabled(),
    }


def install_jupyter_hook(*, dry_run: bool = False, uninstall: bool = False) -> dict[str, Any]:
    """Write (or remove) the Jupyter config file that enables the save hook.

    The file is owned entirely by Lab Tracker: a same-named file with other
    content is refused, never overwritten, and a ``post_save_hook`` configured
    elsewhere is reported and left in place (the hook is registered next to it).
    """

    path = jupyter_hook_config_path()
    status = jupyter_hook_status()
    payload: dict[str, Any] = {
        "command": "setup-autotrack",
        "target": "jupyter",
        "config_file": str(path),
        "hook": HOOK_IMPORT_STRING,
        "dry_run": dry_run,
        "python": sys.executable,
    }
    if status["other_post_save_hooks"]:
        payload["other_post_save_hooks"] = status["other_post_save_hooks"]
        payload["note"] = (
            "Another post_save_hook is configured; it is kept, and Lab Tracker's hook "
            "is registered alongside it."
        )
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
            f"{path} exists and does not enable {EXTENSION_MODULE}; it was not written "
            "by Lab Tracker, so it is left alone. Move it aside and run this again."
        )
    if status["installed"] and status["up_to_date"]:
        payload["action"] = "current"
        return payload
    payload["action"] = (
        ("would-update" if status["installed"] else "would-install")
        if dry_run
        else ("updated" if status["installed"] else "installed")
    )
    payload["restart_required"] = True
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(jupyter_hook_source(), encoding="utf-8")
    return payload


def other_post_save_hooks() -> list[dict[str, str]]:
    """``post_save_hook`` settings in the Jupyter config files that are not ours."""

    found: list[dict[str, str]] = []
    for directory in _jupyter_config_dirs():
        json_path = directory / "jupyter_server_config.json"
        with suppress(OSError, ValueError):
            config = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(config, Mapping):
                for section in config.values():
                    if isinstance(section, Mapping) and "post_save_hook" in section:
                        value = section["post_save_hook"]
                        if value is not None and not _is_this_hook(value):
                            found.append({"file": str(json_path), "value": str(value)[:200]})
        py_path = directory / "jupyter_server_config.py"
        with suppress(OSError, UnicodeDecodeError):
            for match in _PY_HOOK_PATTERN.finditer(py_path.read_text(encoding="utf-8")):
                value = match.group(1).strip().strip("'\"")
                if value != "None" and not _is_this_hook(value):
                    found.append({"file": str(py_path), "value": value[:200]})
    return found


def _jupyter_config_dirs() -> list[Path]:
    dirs: list[Path] = []
    for entry in os.getenv(JUPYTER_CONFIG_PATH_ENV, "").split(os.pathsep):
        if entry.strip():
            dirs.append(Path(entry).expanduser())
    dirs.append(jupyter_config_dir())
    dirs.append(Path(sys.prefix) / "etc" / "jupyter")
    unique: list[Path] = []
    for directory in dirs:
        if directory not in unique:
            unique.append(directory)
    return unique


def _enables_this_extension(content: str) -> bool:
    with suppress(ValueError, AttributeError, TypeError):
        config = json.loads(content)
        extensions = config.get("ServerApp", {}).get("jpserver_extensions", {})
        return isinstance(extensions, Mapping) and extensions.get(EXTENSION_MODULE) is True
    return False


__all__ = [
    "HOOK_IMPORT_STRING",
    "NOTEBOOK_CODE_CELLS_MAX",
    "NOTEBOOK_MARKDOWN_MAX_CHARS",
    "NotebookSummary",
    "install_jupyter_hook",
    "jupyter_hook_config_path",
    "jupyter_hook_status",
    "other_post_save_hooks",
    "post_save_hook",
    "record_notebook_save",
    "register_post_save_hook",
    "render_notebook_page",
    "summarize_notebook",
]
