"""Offline-first analysis-repo provenance capture for Lab Tracker clients.

The ``lt repo`` adapter is the third offline-first sibling of ``lt watch`` and
``lt hpc``: a post-commit git hook (or a direct call) records the *state* of an
analysis repository — commit SHA, dirty flag, remote, branch — as a durable
local event, which later drains into a staged Lab Tracker note. Like the other
adapters it never blocks the caller, never sends bytes (only metadata + hashes),
and lands behind the human review gate.

Modeled 1:1 on :mod:`lab_tracker_client.hpc`; the shared evidence transport,
dedup index, and host metadata come from :mod:`lab_tracker_client.client`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client import outbox as _outbox
from lab_tracker_client.client import (
    CAPTURE_HOST_METADATA_KEYS,
    EvidenceNoteIndex,
    LabTracker,
    LTRecord,
    LTValidationError,
    build_evidence_metadata,
    capture_host_metadata,
)

CONFIG_VERSION = 1
EVENT_VERSION = 1
DEFAULT_CONFIG_RELATIVE_PATH = Path(".lab-tracker") / "repo.json"
DEFAULT_OUTBOX = ".lab-tracker/outbox/repo"
REPO_EVIDENCE_PROVIDER = "git"
REPO_EVIDENCE_ADAPTER = "lt-repo"
REPO_CAPTURE_KIND = "repo_event"
ALLOWED_EVENT_TYPES = {"commit", "report", "finish"}
TERMINAL_SYNC_STATES = {"synced"}
_GIT_TIMEOUT_SECONDS = 1

JsonObject = dict[str, Any]


@dataclass
class RepoConfig:
    """Configuration for a watched analysis repository (``.lab-tracker/repo.json``)."""

    project_id: str
    outbox: str = DEFAULT_OUTBOX
    default_question_id: str | None = None
    version: int = CONFIG_VERSION
    config_path: Path | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "version": self.version,
            "project_id": self.project_id,
            "outbox": self.outbox,
        }
        if self.default_question_id:
            payload["default_question_id"] = self.default_question_id
        return payload

    def outbox_path(self) -> Path:
        override = os.getenv("LAB_TRACKER_REPO_OUTBOX")
        configured = Path(override or self.outbox).expanduser()
        if configured.is_absolute():
            return configured.resolve()
        root = Path.cwd()
        if self.config_path is not None:
            root = (
                self.config_path.parent.parent
                if self.config_path.parent.name == ".lab-tracker"
                else self.config_path.parent
            )
        return (root / configured).resolve()


@dataclass(frozen=True)
class RepoSyncResult:
    """Result for one outbox event processed by sync."""

    action: str
    path: str
    run_id: str
    event_type: str
    note_id: str | None = None
    change_set_id: str | None = None
    reason: str = ""
    error: str = ""

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "action": self.action,
            "path": self.path,
            "run_id": self.run_id,
            "event_type": self.event_type,
        }
        if self.note_id:
            payload["note_id"] = self.note_id
        if self.change_set_id:
            payload["change_set_id"] = self.change_set_id
        if self.reason:
            payload["reason"] = self.reason
        if self.error:
            payload["error"] = self.error
        return payload


def default_config_path(start: str | Path | None = None) -> Path:
    return Path(start or Path.cwd()).expanduser().resolve() / DEFAULT_CONFIG_RELATIVE_PATH


def find_config_path(start: str | Path | None = None) -> Path | None:
    env_path = os.getenv("LAB_TRACKER_REPO_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()
    cursor = Path(start or Path.cwd()).expanduser().resolve()
    if cursor.is_file():
        cursor = cursor.parent
    for parent in (cursor, *cursor.parents):
        candidate = parent / DEFAULT_CONFIG_RELATIVE_PATH
        if candidate.exists():
            return candidate
    return None


def init_config(
    *,
    project_id: str,
    outbox: str = DEFAULT_OUTBOX,
    default_question_id: str | None = None,
    config_path: str | Path | None = None,
    force: bool = False,
) -> RepoConfig:
    path = Path(config_path).expanduser().resolve() if config_path else default_config_path()
    if path.exists() and not force:
        raise LTValidationError(f"Repo config already exists: {path}")
    config = RepoConfig(
        project_id=_non_empty(project_id, "project_id"),
        outbox=_non_empty(outbox, "outbox"),
        default_question_id=default_question_id,
        config_path=path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, config.to_dict())
    config.outbox_path().mkdir(parents=True, exist_ok=True)
    return config


def load_config(
    *,
    config_path: str | Path | None = None,
    start: str | Path | None = None,
) -> RepoConfig:
    path = Path(config_path).expanduser().resolve() if config_path else find_config_path(start)
    if path is None or not path.exists():
        raise LTValidationError(
            "Repo config not found. Run 'lt repo init' or set LAB_TRACKER_REPO_CONFIG."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LTValidationError(f"Repo config is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise LTValidationError("Repo config must be a JSON object.")
    config = RepoConfig(
        version=int(payload.get("version") or CONFIG_VERSION),
        project_id=_non_empty(str(payload.get("project_id") or ""), "project_id"),
        outbox=_non_empty(str(payload.get("outbox") or DEFAULT_OUTBOX), "outbox"),
        default_question_id=_optional_str(payload.get("default_question_id")),
        config_path=path,
    )
    if config.version != CONFIG_VERSION:
        raise LTValidationError(
            f"Unsupported repo config version {config.version}; expected {CONFIG_VERSION}."
        )
    return config


def new_run_id(prefix: str = "repo") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def make_event(
    config: RepoConfig,
    *,
    event_type: str,
    run_id: str | None = None,
    event_id: str | None = None,
    observed_at: str | None = None,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    cwd: str | Path | None = None,
    source: Mapping[str, Any] | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    summary: str | None = None,
) -> JsonObject:
    resolved_event_type = _event_type(event_type)
    resolved_cwd = str(Path(cwd or Path.cwd()).expanduser().resolve())
    resolved_source = {**git_context(resolved_cwd), **dict(source or {})}
    commit = str(resolved_source.get("git_commit") or "")
    resolved_run_id = _non_empty(
        run_id or os.getenv("LAB_TRACKER_REPO_RUN_ID") or commit or new_run_id()
    )
    resolved_event_id = event_id or _default_event_id(
        resolved_event_type, resolved_source, commit
    )
    resolved_project_id = _non_empty(project_id or config.project_id, "project_id")
    resolved_question_id = _optional_str(question_id or config.default_question_id)
    resolved_dataset_ids = [str(item) for item in dataset_ids or [] if str(item).strip()]
    resolved_tags = [str(item) for item in tags or [] if str(item).strip()]
    resolved_summary = summary or _default_summary(resolved_event_type, resolved_source)
    payload: JsonObject = {
        "version": EVENT_VERSION,
        "run_id": resolved_run_id,
        "event_id": resolved_event_id,
        "event_type": resolved_event_type,
        "observed_at": observed_at or utc_now(),
        "project_id": resolved_project_id,
        "question_id": resolved_question_id,
        "dataset_ids": resolved_dataset_ids,
        "tags": resolved_tags,
        "cwd": resolved_cwd,
        "source": _json_mapping(resolved_source),
        "environment": environment_fingerprint(resolved_cwd),
        "artifacts": [_artifact_payload(item) for item in artifacts or []],
        "summary": resolved_summary,
        "capture_id": resolved_run_id,
        "capture_kind": REPO_CAPTURE_KIND,
        "adapter": REPO_EVIDENCE_ADAPTER,
        "sink": "staged-note",
        "context": {
            "project_id": resolved_project_id,
            "question_id": resolved_question_id,
            "dataset_ids": resolved_dataset_ids,
            "tags": resolved_tags,
        },
        "payload": {
            "title": _event_title(resolved_event_type, resolved_source, resolved_run_id),
            "summary": resolved_summary,
            "status": "staged",
        },
        "host": _json_mapping(capture_host_metadata()),
        "sync": {"status": "pending", "attempts": 0},
    }
    return validate_event(payload)


def capture_commit(
    config: RepoConfig,
    *,
    event_type: str = "commit",
    run_id: str | None = None,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    cwd: str | Path | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    summary: str | None = None,
) -> tuple[JsonObject, Path, str]:
    """Capture the current repo state as an event and write it to the outbox.

    Returns ``(event, path, action)``. Event ids are deterministic per commit so
    a repeated post-commit hook stays idempotent — but annotations must never be
    silently dropped, so an existing event is handled by content:

    * ``captured`` — no event existed; a new one was written.
    * ``unchanged`` — an identical event already exists (the hook re-fired).
    * ``updated`` — a pending event existed and its content differed (e.g. the
      user annotated the commit with ``--summary``/``--question``); it was
      rewritten in place, preserving its sync state.
    * ``recaptured`` — the existing event was already synced, so mutating it
      would desync the staged note; the annotation was written as a new event.
    """

    event = make_event(
        config,
        event_type=event_type,
        run_id=run_id,
        project_id=project_id,
        question_id=question_id,
        dataset_ids=dataset_ids,
        tags=tags,
        cwd=cwd,
        artifacts=artifacts,
        summary=summary,
    )
    outbox = config.outbox_path()
    path = event_path(event, outbox)
    if not path.exists():
        write_event(event, outbox)
        return event, path, "captured"
    existing = read_event(path)
    # Merge argument-wise so neither direction drops data: explicit flags win,
    # unspecified fields keep the existing event's values (a bare hook re-fire
    # must not revert an earlier annotation to defaults), and the git source
    # state is always refreshed.
    merged = make_event(
        config,
        event_type=event_type,
        run_id=run_id or str(existing["run_id"]),
        event_id=str(existing["event_id"]),
        observed_at=str(existing["observed_at"]),
        project_id=project_id or str(existing["project_id"]),
        question_id=question_id or existing.get("question_id"),
        dataset_ids=dataset_ids or existing.get("dataset_ids"),
        tags=tags or existing.get("tags"),
        cwd=cwd,
        artifacts=artifacts or existing.get("artifacts"),
        summary=summary or _optional_str(existing.get("summary")),
    )
    if _capture_content(existing) == _capture_content(merged):
        return existing, path, "unchanged"
    merged["observed_at"] = utc_now()
    if str(existing.get("sync", {}).get("status") or "") in TERMINAL_SYNC_STATES:
        # The synced note reflects the old content; a new event (fresh id) keeps
        # the annotation instead of desyncing the existing record.
        merged["event_id"] = uuid.uuid4().hex
        path = write_event(merged, outbox)
        return merged, path, "recaptured"
    merged = validate_event({**merged, "sync": existing.get("sync") or merged["sync"]})
    _write_json_atomic(path, merged)
    return merged, path, "updated"


_CAPTURE_CONTENT_KEYS = (
    "event_type",
    "project_id",
    "question_id",
    "dataset_ids",
    "tags",
    "artifacts",
    "summary",
)
_CAPTURE_SOURCE_KEYS = ("git_commit", "git_dirty", "repo_remote_url", "git_branch")


def _capture_content(event: Mapping[str, Any]) -> str:
    """Canonical form of an event's capture-relevant content (not sync/times)."""

    payload = validate_event(dict(event))
    content = {key: payload[key] for key in _CAPTURE_CONTENT_KEYS}
    content["source"] = {
        key: payload["source"].get(key) for key in _CAPTURE_SOURCE_KEYS
    }
    content["environment"] = payload["environment"]
    return json.dumps(content, sort_keys=True)


def write_event(event: Mapping[str, Any], outbox: str | Path) -> Path:
    payload = validate_event(dict(event))
    path = event_path(payload, outbox)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path
    _write_json_atomic(path, payload)
    return path


def read_event(path: str | Path) -> JsonObject:
    resolved = Path(path).expanduser()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LTValidationError(f"Repo event is not valid JSON: {resolved}") from exc
    if not isinstance(payload, dict):
        raise LTValidationError(f"Repo event must be a JSON object: {resolved}")
    return validate_event(payload)


def validate_event(payload: Mapping[str, Any]) -> JsonObject:
    event = dict(payload)
    version = int(event.get("version") or EVENT_VERSION)
    if version != EVENT_VERSION:
        raise LTValidationError(
            f"Unsupported repo event version {version}; expected {EVENT_VERSION}."
        )
    event["version"] = version
    event["run_id"] = _non_empty(str(event.get("run_id") or ""), "run_id")
    event["event_id"] = _non_empty(str(event.get("event_id") or ""), "event_id")
    event["event_type"] = _event_type(str(event.get("event_type") or ""))
    event["observed_at"] = _non_empty(str(event.get("observed_at") or ""), "observed_at")
    event["project_id"] = _non_empty(str(event.get("project_id") or ""), "project_id")
    event["question_id"] = _optional_str(event.get("question_id"))
    event["dataset_ids"] = _string_list(event.get("dataset_ids"))
    event["tags"] = _string_list(event.get("tags"))
    event["cwd"] = str(event.get("cwd") or "")
    event["source"] = _json_mapping(event.get("source") or {})
    event["environment"] = _json_mapping(event.get("environment") or {})
    event["artifacts"] = [_artifact_payload(item) for item in event.get("artifacts") or []]
    event["summary"] = str(event.get("summary") or "")
    event["host"] = _json_mapping(event.get("host") or {})
    sync = event.get("sync") if isinstance(event.get("sync"), Mapping) else {}
    event["sync"] = {
        "status": str(sync.get("status") or "pending"),
        "attempts": int(sync.get("attempts") or 0),
        **{
            str(key): value
            for key, value in sync.items()
            if key not in {"status", "attempts"} and value is not None
        },
    }
    return event


def event_path(event: Mapping[str, Any], outbox: str | Path) -> Path:
    run_id = _safe_path_part(str(event["run_id"]))
    event_type = _safe_path_part(str(event["event_type"]))
    event_id = _safe_path_part(str(event["event_id"]))
    return Path(outbox).expanduser().resolve() / f"{run_id}.{event_type}.{event_id}.json"


def list_event_files(outbox: str | Path) -> list[Path]:
    return _outbox.list_event_files(outbox)


def outbox_status(outbox: str | Path) -> JsonObject:
    events: list[JsonObject] = []
    counts: dict[str, int] = {}
    unreadable = 0
    for path in list_event_files(outbox):
        try:
            event = read_event(path)
        except Exception as exc:  # noqa: BLE001 - surface, don't abort status.
            unreadable += 1
            events.append(
                {"path": str(path), "sync_status": "unreadable", "last_error": str(exc)}
            )
            continue
        status = str(event.get("sync", {}).get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
        events.append(
            {
                "path": str(path),
                "run_id": event["run_id"],
                "event_type": event["event_type"],
                "event_id": event["event_id"],
                "project_id": event["project_id"],
                "sync_status": status,
                "note_id": event.get("sync", {}).get("note_id"),
                "change_set_id": event.get("sync", {}).get("change_set_id"),
                "last_error": event.get("sync", {}).get("last_error"),
            }
        )
    return {
        "outbox": str(Path(outbox).expanduser().resolve()),
        "total": len(events),
        "pending": counts.get("pending", 0),
        "failed": counts.get("failed", 0),
        "synced": counts.get("synced", 0),
        "quarantined": _outbox.count_quarantined(outbox),
        "unreadable": unreadable,
        "events": events,
    }


def sync_outbox(
    client: LabTracker,
    config: RepoConfig,
    *,
    dry_run: bool = False,
    request_draft: bool = False,
    limit: int | None = None,
) -> JsonObject:
    outbox = config.outbox_path()
    note_indexes: dict[str, EvidenceNoteIndex] = {}

    def _is_actionable(event: JsonObject) -> bool:
        sync = event.get("sync", {})
        already_synced = str(sync.get("status") or "") in TERMINAL_SYNC_STATES
        needs_draft = request_draft and sync.get("note_id") and not sync.get("change_set_id")
        return (not already_synced) or bool(needs_draft)

    def _process(path: Path, event: JsonObject) -> JsonObject:
        return _sync_event(
            client,
            path=path,
            event=event,
            note_indexes=note_indexes,
            dry_run=dry_run,
            request_draft=request_draft,
        ).to_dict()

    def _skipped(path: Path, event: JsonObject) -> JsonObject:
        sync = event.get("sync", {})
        return RepoSyncResult(
            action="skipped",
            path=str(path),
            run_id=str(event["run_id"]),
            event_type=str(event["event_type"]),
            note_id=_optional_str(sync.get("note_id")),
            change_set_id=_optional_str(sync.get("change_set_id")),
            reason="already_synced",
        ).to_dict()

    def _failed(path: Path, event: JsonObject, exc: Exception) -> JsonObject:
        event = _record_sync_failure(path, event, str(exc), dry_run=dry_run)
        return RepoSyncResult(
            action="failed",
            path=str(path),
            run_id=str(event["run_id"]),
            event_type=str(event["event_type"]),
            error=str(exc),
        ).to_dict()

    return _outbox.drain_outbox(
        outbox=outbox,
        command="repo-sync",
        dry_run=dry_run,
        request_draft=request_draft,
        limit=limit,
        read_event=read_event,
        is_actionable=_is_actionable,
        process=_process,
        on_skipped=_skipped,
        on_failure=_failed,
    )


HOOK_BEGIN_MARKER = "# --- BEGIN LAB TRACKER REPO HOOK ---"
HOOK_END_MARKER = "# --- END LAB TRACKER REPO HOOK ---"
_HOOK_SHEBANG = "#!/usr/bin/env sh"


def install_post_commit_hook(
    repo_root: str | Path | None = None,
    *,
    lt_command: str | None = None,
    config_path: str | Path | None = None,
    force: bool = False,
) -> JsonObject:
    """Install (or update) the managed ``lt repo`` post-commit hook.

    Git runs hooks under ``sh`` on every platform (Git for Windows bundles one),
    so a single sh hook body covers POSIX and Windows checkouts alike. The hook
    lives between BEGIN/END markers so reinstalls update it in place, and a
    foreign hook is never clobbered: without ``force`` the install refuses, and
    with ``force`` the managed block is inserted *before* the foreign body
    (right after its shebang), so a trailing ``exit``/``exec`` in the foreign
    hook cannot turn the block into dead code — and the block itself never
    exits, so the foreign hook still runs. The repo config the hook needs is
    resolved at install time and pinned into the block, so a missing or
    mis-located config fails here, loudly, instead of silently on every commit.
    """

    root = Path(repo_root or Path.cwd()).expanduser()
    toplevel = _git_output(root, "rev-parse", "--show-toplevel")
    if not toplevel:
        raise LTValidationError(f"Not a git repository: {root}")
    try:
        config = load_config(config_path=config_path, start=toplevel)
    except LTValidationError as exc:
        raise LTValidationError(
            f"No repo config found from {toplevel}. Run 'lt repo init' in the "
            "repository first (or pass --config)."
        ) from exc
    hook_path = _hook_path(Path(toplevel))
    resolved_lt = _hook_command_path(lt_command)
    config_for_hook = str(config.config_path).replace("\\", "/")
    managed_block = _hook_managed_block(resolved_lt, config_for_hook)

    if hook_path.exists():
        try:
            existing = hook_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise LTValidationError(
                f"Existing post-commit hook is not UTF-8 text: {hook_path}. "
                "Fix or remove it, then re-run."
            ) from exc
        # hooks.py imports this module at top level, so import lazily here.
        from lab_tracker_client.hooks import HOOK_BLOCK_BEGIN as DRAFT_HOOK_BLOCK_BEGIN

        if (
            DRAFT_HOOK_BLOCK_BEGIN in existing
            and HOOK_BEGIN_MARKER not in existing  # updating our own block is fine
            and not force
        ):
            # Both Lab Tracker capture hooks in one repo record two staged
            # notes per commit (lt-81s6.17). One adapter per repo unless forced.
            raise LTValidationError(
                "This repo already has the 'lt git snapshot' capture hook "
                "installed (GRAPH DRAFT block). Running both capture hooks "
                "records every commit twice. Remove that block (lt hooks "
                "uninstall) first, or pass --force only if you deliberately "
                "want both."
            )
        has_begin = HOOK_BEGIN_MARKER in existing
        has_end = HOOK_END_MARKER in existing
        if has_begin and has_end:
            pattern = re.compile(
                re.escape(HOOK_BEGIN_MARKER) + r".*?" + re.escape(HOOK_END_MARKER),
                re.DOTALL,
            )
            if not pattern.search(existing):
                raise LTValidationError(
                    f"Lab Tracker markers in {hook_path} are out of order "
                    "(END before BEGIN?). Repair the hook manually, then re-run."
                )
            updated = pattern.sub(lambda _match: managed_block, existing)
            action = "updated"
        elif has_begin or has_end:
            raise LTValidationError(
                f"Post-commit hook has an unmatched Lab Tracker marker: {hook_path}. "
                "Repair the hook manually, then re-run."
            )
        elif existing.strip() and not force:
            raise LTValidationError(
                f"Existing post-commit hook is not Lab Tracker-managed: {hook_path}. "
                "Re-run with --force to add the managed block ahead of it."
            )
        elif existing.strip():
            _require_sh_hook(existing, hook_path)
            updated = _insert_block_after_shebang(existing, managed_block)
            action = "prepended"
        else:
            updated = f"{_HOOK_SHEBANG}\n{managed_block}\n"
            action = "installed"
    else:
        updated = f"{_HOOK_SHEBANG}\n{managed_block}\n"
        action = "installed"

    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(updated.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    hook_path.chmod(hook_path.stat().st_mode | 0o755)
    return {
        "command": "repo-install-hook",
        "action": action,
        "repo": str(Path(toplevel)),
        "hook": str(hook_path),
        "lt_command": resolved_lt,
        "config": str(config.config_path),
    }


def _hook_path(repo_root: Path) -> Path:
    """Resolve the post-commit hook path, honouring ``core.hooksPath``."""

    raw = _git_output(repo_root, "rev-parse", "--git-path", "hooks/post-commit")
    candidate = Path(raw) if raw else Path(".git") / "hooks" / "post-commit"
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate


def _hook_command_path(lt_command: str | None) -> str:
    """Pick the ``lt`` the hook should call, as an sh-friendly path."""

    import shutil

    resolved = lt_command or shutil.which("lt") or "lt"
    # Git hooks run under sh even on Windows; sh wants forward slashes.
    return resolved.replace("\\", "/")


def _sh_single_quote(value: str) -> str:
    """Quote ``value`` for sh so no character in it can expand or execute."""

    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise LTValidationError("Hook paths must not contain newlines or NULs.")
    return "'" + value.replace("'", "'\\''") + "'"


_SH_SHELLS = ("sh", "bash", "dash", "ksh", "ash", "zsh")


def _require_sh_hook(existing: str, hook_path: Path) -> None:
    """Refuse to merge sh code into a hook written for another interpreter."""

    first_line = existing.split("\n", 1)[0].strip()
    if not first_line.startswith("#!"):
        return  # git runs shebang-less hooks under sh
    interpreter = first_line[2:].strip()
    if any(re.search(rf"(^|/|\s){shell}$", interpreter) for shell in _SH_SHELLS):
        return
    raise LTValidationError(
        f"Existing post-commit hook uses a non-sh interpreter ({first_line}): "
        f"{hook_path}. Chain 'lt repo report' from it manually instead."
    )


def _insert_block_after_shebang(existing: str, block: str) -> str:
    """Insert the managed block before the foreign hook body.

    Running first means a trailing ``exit``/``exec`` in the foreign body cannot
    make the block unreachable, and since the block never exits, the foreign
    hook still runs exactly as before.
    """

    if existing.startswith("#!"):
        newline = existing.find("\n")
        if newline == -1:
            return f"{existing}\n{block}\n"
        return f"{existing[: newline + 1]}{block}\n{existing[newline + 1 :]}"
    return f"{block}\n{existing}"


def _hook_managed_block(lt_command: str, config_path: str) -> str:
    lt_quoted = _sh_single_quote(lt_command)
    config_quoted = _sh_single_quote(config_path)
    return "\n".join(
        [
            HOOK_BEGIN_MARKER,
            'LAB_TRACKER_REPO_HOOK_ENABLED="${LAB_TRACKER_REPO_HOOK_ENABLED:-1}"',
            'if [ "$LAB_TRACKER_REPO_HOOK_ENABLED" != "0" ]; then',
            '  LT="$LAB_TRACKER_LT"',
            f"  [ -n \"$LT\" ] || LT={lt_quoted}",
            '  if [ -z "$LAB_TRACKER_REPO_CONFIG" ]; then',
            f"    LAB_TRACKER_REPO_CONFIG={config_quoted}",
            "    export LAB_TRACKER_REPO_CONFIG",
            "  fi",
            '  if command -v "$LT" >/dev/null 2>&1; then',
            # post-commit exit codes never block the commit; the redirect hides
            # tracebacks and the || branch surfaces a one-line warning instead.
            '    "$LT" repo report >/dev/null 2>&1 || '
            'echo "lab-tracker: repo hook could not record the commit; commit kept." >&2',
            "  fi",
            "fi",
            HOOK_END_MARKER,
        ]
    )


# Lockfiles that pin an analysis environment, in the order they are hashed.
ENVIRONMENT_LOCKFILES = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.txt",
    "pyproject.toml",
    "environment.yml",
    "environment.yaml",
)
LAB_TRACKER_CONTAINER_REF_ENV = "LAB_TRACKER_CONTAINER_REF"


def environment_fingerprint(
    cwd: str | Path | None = None,
    *,
    container_ref: str | None = None,
) -> JsonObject:
    """Fingerprint the analysis environment declared in the repo.

    Hashes whichever :data:`ENVIRONMENT_LOCKFILES` exist at the repo root,
    together with the capturing interpreter's version and an optional container
    reference (``container_ref`` or ``LAB_TRACKER_CONTAINER_REF``), into one
    ``sha256:`` digest. The digest lands in note metadata as
    ``repo_environment_hash`` so curation can later populate
    ``Analysis.environment_hash`` (see ``lab_tracker.repo_bridge``). Returns an
    empty mapping when no lockfile is present — no fingerprint is better than a
    meaningless one.
    """

    import platform

    root = Path(cwd or Path.cwd()).expanduser()
    hasher = hashlib.sha256()
    hashed_files: list[str] = []
    for name in ENVIRONMENT_LOCKFILES:
        candidate = root / name
        if not candidate.is_file():
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        hasher.update(f"{name}:{digest}\n".encode())
        hashed_files.append(name)
    if not hashed_files:
        return {}
    python_version = platform.python_version()
    hasher.update(f"python:{python_version}\n".encode())
    resolved_container = _optional_str(
        container_ref or os.getenv(LAB_TRACKER_CONTAINER_REF_ENV)
    )
    if resolved_container:
        hasher.update(f"container:{resolved_container}\n".encode())
    fingerprint: JsonObject = {
        "repo_environment_hash": f"sha256:{hasher.hexdigest()}",
        "repo_environment_files": ",".join(hashed_files),
        "repo_environment_python": python_version,
    }
    if resolved_container:
        fingerprint["repo_environment_container"] = resolved_container
    return fingerprint


def git_context(cwd: str | Path | None = None) -> JsonObject:
    root = Path(cwd or Path.cwd()).expanduser()
    commit = _git_output(root, "rev-parse", "HEAD")
    context: JsonObject = {
        "git_commit": commit,
        "git_dirty": bool(_git_output(root, "status", "--porcelain")),
    }
    if commit:
        context["git_commit_short"] = commit[:12]
    for key, args in (
        ("repo_remote_url", ("config", "--get", "remote.origin.url")),
        ("git_branch", ("rev-parse", "--abbrev-ref", "HEAD")),
        ("git_subject", ("log", "-1", "--pretty=%s")),
        ("git_author", ("log", "-1", "--pretty=%an")),
    ):
        value = _git_output(root, *args)
        if value:
            context[key] = value
    return context


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sync_event(
    client: LabTracker,
    *,
    path: Path,
    event: JsonObject,
    note_indexes: dict[str, EvidenceNoteIndex],
    dry_run: bool,
    request_draft: bool,
) -> RepoSyncResult:
    evidence = render_event_note(event)
    evidence_bytes = evidence.encode("utf-8")
    content_hash = hashlib.sha256(evidence_bytes).hexdigest()
    source_external_id = event_source_external_id(event)
    metadata = event_metadata(
        event,
        source_uri=path.as_uri(),
        source_external_id=source_external_id,
        content_hash=content_hash,
    )
    note_id = _optional_str(event.get("sync", {}).get("note_id"))
    note: LTRecord | None = None
    project_id = str(event["project_id"])
    action = "synced"
    reason = ""
    if not note_id:
        if project_id not in note_indexes:
            note_indexes[project_id] = client.build_evidence_note_index(project_id=project_id)
        index = note_indexes[project_id]
        evidence_key = (
            str(metadata["evidence_source_provider"]),
            str(metadata["evidence_source_external_id"]),
            str(metadata["evidence_content_hash"]),
        )
        note = index.get(evidence_key)
        if dry_run:
            return RepoSyncResult(
                action="skipped",
                path=str(path),
                run_id=str(event["run_id"]),
                event_type=str(event["event_type"]),
                reason="dry_run",
            )
        if note is None:
            note = client._upload_note_file_payload(
                project_id=project_id,
                path=path.with_suffix(".md"),
                payload=evidence_bytes,
                metadata=metadata,
                status="staged",
                content_type="text/markdown",
                client_capture_id=_client_capture_id(source_external_id),
            )
            index[evidence_key] = note
            action = "imported"
        else:
            action = "skipped"
            reason = "duplicate"
        note_id = str(note.id)
    change_set_id = _optional_str(event.get("sync", {}).get("change_set_id"))
    draft_error = ""
    if request_draft and note_id and not change_set_id and not dry_run:
        try:
            draft = client.create_analysis_graph_draft(note_id)
            change_set_id = str(draft.id)
        except Exception as exc:  # noqa: BLE001 - evidence synced; draft can retry later.
            draft_error = str(exc)
    if not dry_run:
        _record_sync_success(
            path,
            event,
            note_id=note_id,
            change_set_id=change_set_id,
            draft_error=draft_error,
        )
    return RepoSyncResult(
        action=action,
        path=str(path),
        run_id=str(event["run_id"]),
        event_type=str(event["event_type"]),
        note_id=note_id,
        change_set_id=change_set_id,
        reason=reason,
        error=draft_error,
    )


def render_event_note(event: Mapping[str, Any]) -> str:
    payload = validate_event(dict(event))
    source = payload["source"]
    commit = str(source.get("git_commit") or "")
    short = str(source.get("git_commit_short") or commit[:12] or payload["run_id"])
    branch = str(source.get("git_branch") or "")
    heading = f"# Repo {payload['event_type']} {short}"
    if branch:
        heading += f" on {branch}"
    lines = [
        heading,
        "",
        payload["summary"] or _default_summary(payload["event_type"], source),
        "",
        "## Repository State",
    ]
    for label, key in (
        ("Remote", "repo_remote_url"),
        ("Branch", "git_branch"),
        ("Commit", "git_commit"),
        ("Author", "git_author"),
    ):
        if source.get(key):
            lines.append(f"- {label}: `{source[key]}`")
    if source.get("git_subject"):
        lines.append(f"- Commit subject: {source['git_subject']}")
    lines.append(f"- Dirty working tree: {bool(source.get('git_dirty'))}")
    if payload["cwd"]:
        lines.append(f"- Working directory: `{payload['cwd']}`")
    lines.extend(["", "## Research Context", f"- Project: `{payload['project_id']}`"])
    if payload.get("question_id"):
        lines.append(f"- Candidate question: `{payload['question_id']}`")
    if payload["dataset_ids"]:
        dataset_ids = ", ".join(f"`{item}`" for item in payload["dataset_ids"])
        lines.append(f"- Candidate datasets: {dataset_ids}")
    if payload["tags"]:
        lines.append(f"- Tags: {', '.join(payload['tags'])}")
    if payload["artifacts"]:
        lines.extend(["", "## Artifact Pointers"])
        for artifact in payload["artifacts"]:
            label = artifact.get("title") or artifact.get("uri") or "artifact"
            lines.append(f"- {label}")
            for key in ("kind", "uri", "summary", "content_hash", "size_bytes"):
                if artifact.get(key) is not None:
                    lines.append(f"  - {key}: {artifact[key]}")
    lines.extend(
        [
            "",
            "## Lab Tracker Capture",
            f"- Event ID: `{payload['event_id']}`",
            f"- Observed at: {payload['observed_at']}",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def event_metadata(
    event: Mapping[str, Any],
    *,
    source_uri: str,
    source_external_id: str,
    content_hash: str,
) -> dict[str, NoteMetadataScalar]:
    payload = validate_event(dict(event))
    source = payload["source"]
    metadata: dict[str, NoteMetadataScalar] = {
        "repo_run_id": payload["run_id"],
        "repo_event_id": payload["event_id"],
        "repo_event_type": payload["event_type"],
        "repo_project_id": payload["project_id"],
        "repo_artifact_count": len(payload["artifacts"]),
    }
    if payload.get("question_id"):
        metadata["repo_question_id"] = str(payload["question_id"])
    if payload["dataset_ids"]:
        metadata["repo_dataset_ids"] = ",".join(payload["dataset_ids"])
    if payload["tags"]:
        metadata["repo_tags"] = ",".join(payload["tags"])
    if source.get("git_commit"):
        metadata["repo_git_commit"] = str(source["git_commit"])
    if source.get("repo_remote_url"):
        metadata["repo_remote_url"] = str(source["repo_remote_url"])
    if source.get("git_branch"):
        metadata["repo_git_branch"] = str(source["git_branch"])
    metadata["repo_git_dirty"] = bool(source.get("git_dirty"))
    for key, value in payload["environment"].items():
        if isinstance(value, (str, bool, int, float)) and str(key).startswith("repo_environment"):
            metadata[str(key)] = value
    if payload["artifacts"]:
        # Structured (JSON-encoded scalar) so the curation bridge can lift the
        # pointers into ExternalArtifactReferences without re-parsing markdown.
        metadata["repo_artifacts"] = json.dumps(payload["artifacts"], sort_keys=True)
    host = payload.get("host") if isinstance(payload.get("host"), Mapping) else {}
    for key in CAPTURE_HOST_METADATA_KEYS:
        if host.get(key):
            metadata[key] = str(host[key])
    return build_evidence_metadata(
        source_provider=REPO_EVIDENCE_PROVIDER,
        source_uri=source_uri,
        source_external_id=source_external_id,
        content_hash=content_hash,
        capture_kind=REPO_CAPTURE_KIND,
        adapter=REPO_EVIDENCE_ADAPTER,
        title=_event_title(payload["event_type"], source, payload["run_id"]),
        observed_at=str(payload["observed_at"]),
        metadata=metadata,
    )


def event_source_external_id(event: Mapping[str, Any]) -> str:
    """Stable evidence id for a captured commit: ``<normalized-remote>@<commit>``.

    Shared with the analysis-graph-draft CI script so the hook path and the CI
    path dedup to the same evidence key for one commit (see lt-81s6.8).
    """

    payload = validate_event(dict(event))
    source = payload["source"]
    remote = normalize_remote(str(source.get("repo_remote_url") or "")) or "local"
    commit = str(source.get("git_commit") or payload["run_id"])
    return f"{remote}@{commit}"


def normalize_remote(remote: str) -> str:
    """Canonicalize a git remote URL for a stable, credential-free identity."""

    cleaned = remote.strip()
    if not cleaned:
        return ""
    # Drop a scheme (https://, ssh://, git://) or scp-style user@host: prefix.
    cleaned = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", cleaned)
    if "@" in cleaned and "://" not in remote:
        cleaned = cleaned.split("@", 1)[1]
    else:
        cleaned = re.sub(r"^[^@/]+@", "", cleaned)
    cleaned = cleaned.replace(":", "/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    return cleaned.strip("/").lower()


def _record_sync_success(
    path: Path,
    event: JsonObject,
    *,
    note_id: str,
    change_set_id: str | None,
    draft_error: str,
) -> None:
    sync = dict(event.get("sync") or {})
    sync.update(
        {
            "status": "synced",
            "note_id": note_id,
            "synced_at": utc_now(),
            "last_error": draft_error or None,
        }
    )
    if change_set_id:
        sync["change_set_id"] = change_set_id
    if draft_error:
        sync["last_draft_error"] = draft_error
    else:
        sync.pop("last_draft_error", None)
    event["sync"] = {key: value for key, value in sync.items() if value is not None}
    _write_json_atomic(path, event)


def _record_sync_failure(path: Path, event: JsonObject, error: str, *, dry_run: bool) -> JsonObject:
    if dry_run:
        return event
    sync = dict(event.get("sync") or {})
    sync.update(
        {
            "status": "failed",
            "attempts": int(sync.get("attempts") or 0) + 1,
            "last_error": error,
            "last_attempted_at": utc_now(),
        }
    )
    event["sync"] = sync
    _write_json_atomic(path, event)
    return event


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _outbox.write_json_atomic(path, payload)


def _git_output(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _default_event_id(event_type: str, source: Mapping[str, Any], commit: str) -> str:
    # Only hook-style "commit" events are idempotent per commit; report/finish
    # events describe a particular run, so two runs at the same commit must stay
    # distinct events rather than merging into one.
    if event_type != "commit" or not commit:
        return uuid.uuid4().hex
    remote = normalize_remote(str(source.get("repo_remote_url") or ""))
    seed = f"{event_type}:{remote}:{commit}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _event_title(event_type: str, source: Mapping[str, Any], run_id: str) -> str:
    short = (
        str(source.get("git_commit_short") or "")
        or str(source.get("git_commit") or "")[:12]
        or run_id
    )
    return f"Repo {event_type} {short}"


def _default_summary(event_type: str, source: Mapping[str, Any]) -> str:
    short = str(source.get("git_commit_short") or str(source.get("git_commit") or "")[:12])
    branch = str(source.get("git_branch") or "")
    where = f" on {branch}" if branch else ""
    target = short or "the working tree"
    return f"Captured repo {event_type} for {target}{where}."


def artifact_from_path(
    path: str | Path,
    *,
    root: str | Path | None = None,
    summary: str | None = None,
) -> JsonObject:
    """Fingerprint a produced file as an artifact pointer (path + hash, no bytes).

    Reuses the watch adapter's stability-sandwiched fingerprint so a file still
    being written is refused rather than captured with a wrong hash. The hash is
    ``sha256:``-prefixed, matching the resolver convention, so the pointer can
    later be lifted into a verifiable ExternalArtifactReference (lt-81s6.8).
    """

    from lab_tracker_client.watch import observe_file

    # Resolve the file before touching the working directory: resolving a
    # relative path (or defaulting root to cwd) raises OSError when the process
    # sits in a deleted directory, and callers are promised LTValidationError.
    candidate = Path(path).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError as exc:
        raise LTValidationError(f"Artifact file not found: {candidate}") from exc
    if not candidate.is_file():
        raise LTValidationError(f"Artifact file not found: {candidate}")
    try:
        resolved_root = Path(root or Path.cwd()).expanduser().resolve()
    except OSError as exc:
        raise LTValidationError(
            "Cannot resolve the artifact root: the working directory is unavailable."
        ) from exc
    observation = observe_file(candidate, root=resolved_root)
    return {
        "uri": observation.source_uri,
        "kind": "file",
        "title": observation.relative_path,
        "summary": _optional_str(summary) or "",
        "content_hash": f"sha256:{observation.content_hash}",
        "size_bytes": observation.size_bytes,
    }


def _artifact_payload(value: Mapping[str, Any]) -> JsonObject:
    if not isinstance(value, Mapping):
        raise LTValidationError("artifact entries must be JSON objects.")
    uri = _optional_str(value.get("uri"))
    title = _optional_str(value.get("title"))
    return {
        "uri": uri or "",
        "kind": _optional_str(value.get("kind")) or "file",
        "title": title or (Path(uri).name if uri else "artifact"),
        "summary": _optional_str(value.get("summary")) or "",
        **{
            key: value[key]
            for key in ("content_hash", "size_bytes")
            if value.get(key) is not None
        },
    }


def _client_capture_id(value: str) -> str:
    if len(value) <= 120:
        return value
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{value[:104]}:{suffix}"


def _event_type(value: str) -> str:
    cleaned = _non_empty(value, "event_type").lower()
    if cleaned not in ALLOWED_EVENT_TYPES:
        allowed = ", ".join(sorted(ALLOWED_EVENT_TYPES))
        raise LTValidationError(f"event_type must be one of {allowed}.")
    return cleaned


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, Sequence):
        raise LTValidationError("Expected a list of strings.")
    return [str(item) for item in value if str(item).strip()]


def _json_mapping(value: Any) -> JsonObject:
    if not isinstance(value, Mapping):
        raise LTValidationError("Expected a JSON object.")
    return {str(key): _json_value(item) for key, item in value.items() if item is not None}


def _json_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return _json_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return str(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _non_empty(value: str, field_name: str = "value") -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise LTValidationError(f"{field_name} must not be empty.")
    return cleaned


def _safe_path_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip(".-") or "event"
