"""Generic offline-first watch-folder capture helpers."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab_tracker.file_watch import (
    FileFingerprint,
)
from lab_tracker.file_watch import (
    discover_files as _discover_files,
)
from lab_tracker.file_watch import (
    file_sha256 as _file_sha256,
)
from lab_tracker.file_watch import (
    is_hidden_relative as _is_hidden_relative,
)
from lab_tracker.file_watch import (
    local_folder_external_id as _local_folder_external_id,
)
from lab_tracker.file_watch import (
    matches_any as _matches_any,
)
from lab_tracker.file_watch import (
    stable_file_fingerprint as _stable_file_fingerprint,
)
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
DEFAULT_CONFIG_RELATIVE_PATH = Path(".lab-tracker") / "watch.json"
DEFAULT_OUTBOX = ".lab-tracker/outbox/watch"
DEFAULT_MANIFEST_PATTERN = "lab-tracker-evidence.json"
WATCH_EVIDENCE_ADAPTER = "lt-watch"
SINK_STAGED_NOTE = "staged-note"
SINK_ACQUISITION_OUTPUT = "acquisition-output"
ALLOWED_SINKS = {SINK_STAGED_NOTE, SINK_ACQUISITION_OUTPUT}
MODE_MANIFEST = "manifest"
MODE_FILES = "files"
ALLOWED_MODES = {MODE_MANIFEST, MODE_FILES}
TERMINAL_SYNC_STATES = {"synced"}

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class FileObservation:
    """Root-relative file facts used by staged-note and acquisition sinks."""

    path: Path
    root: Path
    relative_path: str
    source_uri: str
    source_external_id: str
    content_hash: str
    size_bytes: int
    mtime: float


@dataclass
class WatchConfig:
    """Configuration for generic watch-folder capture."""

    project_id: str | None = None
    outbox: str = DEFAULT_OUTBOX
    watches: list[JsonObject] = field(default_factory=list)
    version: int = CONFIG_VERSION
    config_path: Path | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "version": self.version,
            "outbox": self.outbox,
            "watches": list(self.watches),
        }
        if self.project_id:
            payload["project_id"] = self.project_id
        return payload

    def outbox_path(self) -> Path:
        override = os.getenv("LAB_TRACKER_WATCH_OUTBOX")
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
class WatchSyncResult:
    """Result for one watch outbox event processed by sync."""

    action: str
    path: str
    event_id: str
    capture_id: str
    sink: str
    note_id: str | None = None
    output_id: str | None = None
    change_set_id: str | None = None
    reason: str = ""
    error: str = ""

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "action": self.action,
            "path": self.path,
            "event_id": self.event_id,
            "capture_id": self.capture_id,
            "sink": self.sink,
        }
        if self.note_id:
            payload["note_id"] = self.note_id
        if self.output_id:
            payload["output_id"] = self.output_id
        if self.change_set_id:
            payload["change_set_id"] = self.change_set_id
        if self.reason:
            payload["reason"] = self.reason
        if self.error:
            payload["error"] = self.error
        return payload


def default_config_path(start: str | Path | None = None) -> Path:
    return (Path(start or Path.cwd()).expanduser().resolve() / DEFAULT_CONFIG_RELATIVE_PATH)


def find_config_path(start: str | Path | None = None) -> Path | None:
    env_path = os.getenv("LAB_TRACKER_WATCH_CONFIG")
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
    project_id: str | None = None,
    outbox: str = DEFAULT_OUTBOX,
    watches: Sequence[Mapping[str, Any]] | None = None,
    config_path: str | Path | None = None,
    force: bool = False,
) -> WatchConfig:
    path = Path(config_path).expanduser().resolve() if config_path else default_config_path()
    if path.exists() and not force:
        raise LTValidationError(f"Watch config already exists: {path}")
    config = WatchConfig(
        project_id=_optional_str(project_id),
        outbox=_non_empty(outbox, "outbox"),
        watches=[_watch_config_payload(item) for item in watches or []],
        config_path=path,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, config.to_dict())
    config.outbox_path().mkdir(parents=True, exist_ok=True)
    return config


def add_watch(
    *,
    root: str,
    mode: str = MODE_FILES,
    sink: str = SINK_STAGED_NOTE,
    include: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    name: str | None = None,
    project_id: str | None = None,
    question_id: str | None = None,
    session_id: str | None = None,
    tags: Sequence[str] | None = None,
    config_path: str | Path | None = None,
    dry_run: bool = False,
) -> JsonObject:
    """Append a validated watch entry to watch.json, creating the config if absent.

    A malformed existing config raises rather than being silently recreated;
    an exact-duplicate entry is a no-op reported as ``unchanged``.
    """

    path = Path(config_path).expanduser().resolve() if config_path else find_config_path()
    created_config = False
    if path is not None and path.exists():
        config = load_config(config_path=path)
    else:
        path = path or default_config_path()
        config = WatchConfig(config_path=path)
        created_config = True
    entry: JsonObject = {
        "root": _non_empty(root, "root"),
        "mode": mode,
        "sink": sink,
    }
    if include:
        entry["include"] = _string_list(include)
    if exclude:
        entry["exclude"] = _string_list(exclude)
    if name:
        entry["name"] = name
    if project_id:
        entry["project_id"] = project_id
    if question_id:
        entry["question_id"] = question_id
    if session_id:
        entry["session_id"] = session_id
    if tags:
        entry["tags"] = _string_list(tags)
    entry = _watch_config_payload(entry)
    if entry["sink"] == SINK_ACQUISITION_OUTPUT and not entry.get("session_id"):
        raise LTValidationError("acquisition-output watches require --session.")
    action = "unchanged" if entry in config.watches else "added"
    if action == "added":
        config.watches.append(entry)
    if not dry_run and (action == "added" or created_config):
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(path, config.to_dict())
        config.outbox_path().mkdir(parents=True, exist_ok=True)
    return {
        "command": "watch-add",
        "config": str(path),
        "created_config": created_config,
        "action": action,
        "watch": entry,
        "watches": list(config.watches),
        "dry_run": dry_run,
    }


def remove_watch(
    *,
    root: str,
    config_path: str | Path | None = None,
    dry_run: bool = False,
) -> JsonObject:
    """Remove watch entries whose root matches ``root`` (literally or resolved)."""

    config = load_config(config_path=config_path)
    target = _comparable_root(root)
    kept: list[JsonObject] = []
    removed: list[JsonObject] = []
    for entry in config.watches:
        entry_root = str(entry.get("root") or "")
        if entry_root == root or _comparable_root(entry_root) == target:
            removed.append(entry)
        else:
            kept.append(entry)
    if not removed:
        raise LTValidationError(f"No watch entry matches root: {root}")
    config.watches = kept
    if not dry_run:
        _write_json_atomic(config.config_path, config.to_dict())
    return {
        "command": "watch-remove",
        "config": str(config.config_path),
        "removed": removed,
        "watches": kept,
        "dry_run": dry_run,
    }


def _comparable_root(value: str) -> str:
    # normcase folds case and separators on Windows (resolve() preserves the
    # typed case for paths that no longer exist on disk) and is a no-op on
    # POSIX, so removing a watch for a deleted folder still matches.
    try:
        return os.path.normcase(str(Path(value).expanduser().resolve()))
    except OSError:
        return os.path.normcase(value)


def load_config(
    *,
    config_path: str | Path | None = None,
    start: str | Path | None = None,
) -> WatchConfig:
    path = Path(config_path).expanduser().resolve() if config_path else find_config_path(start)
    if path is None or not path.exists():
        raise LTValidationError(
            "Watch config not found. Run 'lt watch init' or set LAB_TRACKER_WATCH_CONFIG."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LTValidationError(f"Watch config is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise LTValidationError("Watch config must be a JSON object.")
    config = WatchConfig(
        project_id=_optional_str(payload.get("project_id")),
        outbox=_non_empty(str(payload.get("outbox") or DEFAULT_OUTBOX), "outbox"),
        watches=[_watch_config_payload(item) for item in payload.get("watches") or []],
        version=int(payload.get("version") or CONFIG_VERSION),
        config_path=path,
    )
    if config.version != CONFIG_VERSION:
        raise LTValidationError(
            f"Unsupported watch config version {config.version}; expected {CONFIG_VERSION}."
        )
    return config


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    return _file_sha256(path, chunk_size=chunk_size)


def stable_file_fingerprint(path: str | Path) -> FileFingerprint | None:
    return _stable_file_fingerprint(path)


def discover_files(
    root: str | Path,
    *,
    include_patterns: Sequence[str] | None = None,
    exclude_patterns: Sequence[str] | None = None,
    ignore_hidden: bool = True,
) -> list[Path]:
    try:
        return _discover_files(
            root,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            ignore_hidden=ignore_hidden,
        )
    except ValueError as exc:
        raise LTValidationError(str(exc)) from exc


def observe_file(
    path: str | Path,
    *,
    root: str | Path,
    source_external_id: str | None = None,
) -> FileObservation:
    resolved_root = Path(root).expanduser().resolve()
    resolved_path = Path(path).expanduser().resolve()
    fingerprint = stable_file_fingerprint(resolved_path)
    if fingerprint is None:
        raise LTValidationError(f"file is not stable enough to capture: {resolved_path}")
    relative_path = _relative_posix(resolved_path, resolved_root)
    return FileObservation(
        path=resolved_path,
        root=resolved_root,
        relative_path=relative_path,
        source_uri=resolved_path.as_uri(),
        source_external_id=source_external_id
        or local_folder_external_id(resolved_root, relative_path),
        content_hash=fingerprint.checksum,
        size_bytes=fingerprint.size_bytes,
        mtime=fingerprint.mtime,
    )


def event_from_file(
    config: WatchConfig,
    path: str | Path,
    *,
    root: str | Path,
    sink: str = SINK_STAGED_NOTE,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    session_id: str | None = None,
    source_provider: str = "local-folder",
    adapter: str = "lt-watch-files",
    capture_kind: str | None = None,
    title: str | None = None,
    status: str = "staged",
) -> JsonObject:
    observation = observe_file(path, root=root)
    resolved_sink = _sink(sink)
    resolved_capture_kind = capture_kind or (
        "acquisition_output" if resolved_sink == SINK_ACQUISITION_OUTPUT else "file"
    )
    return make_event(
        capture_id=observation.source_external_id,
        event_id=f"file-{observation.content_hash[:16]}",
        capture_kind=resolved_capture_kind,
        adapter=adapter,
        sink=resolved_sink,
        source={
            "provider": source_provider,
            "uri": observation.source_uri,
            "external_id": observation.source_external_id,
            "path": str(observation.path),
            "root": str(observation.root),
            "root_uri": observation.root.as_uri(),
            "relative_path": observation.relative_path,
            "content_hash": observation.content_hash,
            "size_bytes": observation.size_bytes,
            "mtime": observation.mtime,
        },
        context={
            "project_id": _optional_str(project_id or config.project_id),
            "question_id": _optional_str(question_id),
            "dataset_ids": _string_list(dataset_ids),
            "tags": _string_list(tags),
            "session_id": _optional_str(session_id),
        },
        payload={
            "title": title or observation.path.name,
            "summary": f"Captured {observation.relative_path}.",
            "status": status,
        },
    )


def event_from_manifest(
    config: WatchConfig,
    manifest_path: str | Path,
    *,
    sink: str | None = None,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    session_id: str | None = None,
) -> JsonObject:
    path = Path(manifest_path).expanduser().resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LTValidationError(f"Watch manifest is not valid JSON: {path}") from exc
    if not isinstance(manifest, dict):
        raise LTValidationError(f"Watch manifest must be a JSON object: {path}")
    content_hash = file_sha256(path)
    manifest_context = _json_mapping(manifest.get("context") or {})
    source = _json_mapping(manifest.get("source") or {})
    source.update(
        {
            "provider": _optional_str(source.get("provider")) or "watch-manifest",
            "uri": _optional_str(source.get("uri")) or path.as_uri(),
            "external_id": _optional_str(source.get("external_id"))
            or f"{path.as_uri()}::{content_hash}",
            "manifest_uri": path.as_uri(),
            "manifest_path": str(path),
            "manifest_content_hash": content_hash,
        }
    )
    payload = _json_mapping(manifest.get("payload") or {})
    if manifest.get("title") is not None:
        payload["title"] = str(manifest["title"])
    if manifest.get("summary") is not None:
        payload["summary"] = str(manifest["summary"])
    payload.setdefault("title", f"Watch capture {manifest.get('capture_id') or path.parent.name}")
    payload.setdefault("summary", "Captured watch-folder manifest.")
    payload.setdefault("status", str(manifest.get("status") or "staged"))
    return make_event(
        capture_id=_optional_str(manifest.get("capture_id"))
        or _optional_str(manifest.get("run_id"))
        or path.parent.name,
        event_id=_optional_str(manifest.get("event_id")) or f"manifest-{content_hash[:16]}",
        capture_kind=_optional_str(manifest.get("capture_kind")) or "analysis_evidence",
        adapter=_optional_str(manifest.get("adapter")) or "lt-watch-manifest",
        sink=sink or _optional_str(manifest.get("sink")) or SINK_STAGED_NOTE,
        observed_at=_optional_str(manifest.get("observed_at")),
        source=source,
        context={
            "project_id": _optional_str(
                project_id
                or manifest_context.get("project_id")
                or manifest.get("project_id")
                or config.project_id
            ),
            "question_id": _optional_str(
                question_id or manifest_context.get("question_id") or manifest.get("question_id")
            ),
            "dataset_ids": _string_list(
                dataset_ids or manifest_context.get("dataset_ids") or manifest.get("dataset_ids")
            ),
            "tags": _string_list(tags or manifest_context.get("tags") or manifest.get("tags")),
            "session_id": _optional_str(
                session_id or manifest_context.get("session_id") or manifest.get("session_id")
            ),
        },
        artifacts=_mapping_list(manifest.get("artifacts")),
        metrics=_json_mapping(manifest.get("metrics") or {}),
        log_excerpt=_optional_str(manifest.get("log_excerpt")) or "",
        payload=payload,
    )


def make_event(
    *,
    capture_id: str,
    capture_kind: str,
    adapter: str,
    sink: str,
    event_id: str | None = None,
    observed_at: str | None = None,
    source: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    metrics: Mapping[str, Any] | None = None,
    log_excerpt: str = "",
    payload: Mapping[str, Any] | None = None,
) -> JsonObject:
    event = {
        "version": EVENT_VERSION,
        "event_id": event_id or uuid.uuid4().hex,
        "capture_id": _non_empty(capture_id, "capture_id"),
        "capture_kind": _non_empty(capture_kind, "capture_kind"),
        "adapter": _non_empty(adapter, "adapter"),
        "sink": _sink(sink),
        "observed_at": observed_at or utc_now(),
        "source": _json_mapping(source or {}),
        "context": _context_payload(context or {}),
        "artifacts": [_json_mapping(item) for item in artifacts or []],
        "metrics": _json_mapping(metrics or {}),
        "log_excerpt": log_excerpt or "",
        "payload": _json_mapping(payload or {}),
        "host": _json_mapping(capture_host_metadata()),
        "sync": {"status": "pending", "attempts": 0},
    }
    return validate_event(event)


def validate_event(payload: Mapping[str, Any]) -> JsonObject:
    event = dict(payload)
    version = int(event.get("version") or EVENT_VERSION)
    if version != EVENT_VERSION:
        raise LTValidationError(
            f"Unsupported watch event version {version}; expected {EVENT_VERSION}."
        )
    event["version"] = version
    event["event_id"] = _non_empty(str(event.get("event_id") or ""), "event_id")
    event["capture_id"] = _non_empty(str(event.get("capture_id") or ""), "capture_id")
    event["capture_kind"] = _non_empty(str(event.get("capture_kind") or ""), "capture_kind")
    event["adapter"] = _non_empty(str(event.get("adapter") or WATCH_EVIDENCE_ADAPTER), "adapter")
    event["sink"] = _sink(str(event.get("sink") or ""))
    event["observed_at"] = _non_empty(str(event.get("observed_at") or ""), "observed_at")
    event["source"] = _json_mapping(event.get("source") or {})
    event["context"] = _context_payload(event.get("context") or {})
    event["artifacts"] = [_json_mapping(item) for item in event.get("artifacts") or []]
    event["metrics"] = _json_mapping(event.get("metrics") or {})
    event["log_excerpt"] = str(event.get("log_excerpt") or "")
    event["payload"] = _json_mapping(event.get("payload") or {})
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


def write_event(event: Mapping[str, Any], outbox: str | Path) -> Path:
    payload = validate_event(event)
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
        raise LTValidationError(f"Watch event is not valid JSON: {resolved}") from exc
    if not isinstance(payload, dict):
        raise LTValidationError(f"Watch event must be a JSON object: {resolved}")
    return validate_event(payload)


def event_path(event: Mapping[str, Any], outbox: str | Path) -> Path:
    payload = validate_event(event)
    capture_id = _safe_filename_part(payload["capture_id"])
    sink = _safe_path_part(payload["sink"])
    event_id = _safe_path_part(payload["event_id"])
    return Path(outbox).expanduser().resolve() / f"{capture_id}.{sink}.{event_id}.json"


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
                "event_id": event["event_id"],
                "capture_id": event["capture_id"],
                "capture_kind": event["capture_kind"],
                "sink": event["sink"],
                "project_id": event["context"].get("project_id"),
                "session_id": event["context"].get("session_id"),
                "sync_status": status,
                "note_id": event.get("sync", {}).get("note_id"),
                "output_id": event.get("sync", {}).get("output_id"),
                "change_set_id": event.get("sync", {}).get("change_set_id"),
                "last_error": event.get("sync", {}).get("last_error"),
            }
        )
    return {
        "outbox": str(Path(outbox).expanduser()),
        "total": len(events),
        "quarantined": _outbox.count_quarantined(outbox),
        "unreadable": unreadable,
        "events": events,
        **counts,
    }


def scan_watch(
    config: WatchConfig,
    *,
    mode: str,
    root: str | Path,
    sink: str = SINK_STAGED_NOTE,
    pattern: str = DEFAULT_MANIFEST_PATTERN,
    include_patterns: Sequence[str] | None = None,
    exclude_patterns: Sequence[str] | None = None,
    limit: int | None = None,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    session_id: str | None = None,
    source_provider: str | None = None,
    adapter: str | None = None,
    dry_run: bool = False,
) -> JsonObject:
    resolved_mode = _mode(mode)
    resolved_root = Path(root).expanduser().resolve()
    if not resolved_root.exists():
        raise LTValidationError(f"watch root is not an existing path: {resolved_root}")
    matched: list[Path]
    if resolved_mode == MODE_MANIFEST:
        if not resolved_root.is_dir():
            raise LTValidationError(f"manifest watch root must be a directory: {resolved_root}")
        matched = sorted(path for path in resolved_root.rglob(pattern) if path.is_file())
    else:
        matched = discover_files(
            resolved_root,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
    matched_count = len(matched)
    if limit is not None:
        matched = matched[: max(0, limit)]
    imported: list[JsonObject] = []
    errors: list[JsonObject] = []
    for path in matched:
        try:
            if resolved_mode == MODE_MANIFEST:
                event = event_from_manifest(
                    config,
                    path,
                    sink=sink,
                    project_id=project_id,
                    question_id=question_id,
                    dataset_ids=dataset_ids,
                    tags=tags,
                    session_id=session_id,
                )
            else:
                event = event_from_file(
                    config,
                    path,
                    root=resolved_root,
                    sink=sink,
                    project_id=project_id,
                    question_id=question_id,
                    dataset_ids=dataset_ids,
                    tags=tags,
                    session_id=session_id,
                    source_provider=source_provider or "local-folder",
                    adapter=adapter
                    or (
                        "lt-watch-acquisition"
                        if sink == SINK_ACQUISITION_OUTPUT
                        else "lt-watch-files"
                    ),
                )
            target_path = event_path(event, config.outbox_path())
            already_present = target_path.exists()
            if not dry_run:
                target_path = write_event(event, config.outbox_path())
            imported.append(
                {
                    "source": str(path),
                    "event_path": str(target_path),
                    "event_id": event["event_id"],
                    "capture_id": event["capture_id"],
                    "sink": event["sink"],
                    "already_present": already_present,
                }
            )
        except Exception as exc:  # noqa: BLE001 - report every bad source in the batch.
            errors.append({"source": str(path), "error": str(exc)})
    return {
        "command": "watch-scan",
        "mode": resolved_mode,
        "root": str(resolved_root),
        "sink": sink,
        "dry_run": dry_run,
        "matched": matched_count,
        "processed": len(matched),
        "imported": imported,
        "errors": errors,
    }


def scan_configured(
    config: WatchConfig,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> JsonObject:
    summaries = []
    errors = []
    for watch in config.watches:
        try:
            summary = scan_watch(
                config,
                mode=str(watch.get("mode") or MODE_FILES),
                root=str(watch["root"]),
                sink=str(watch.get("sink") or SINK_STAGED_NOTE),
                pattern=str(watch.get("pattern") or DEFAULT_MANIFEST_PATTERN),
                include_patterns=_string_list(watch.get("include")) or None,
                exclude_patterns=_string_list(watch.get("exclude")) or None,
                limit=limit,
                project_id=_optional_str(watch.get("project_id")),
                question_id=_optional_str(watch.get("question_id")),
                dataset_ids=_string_list(watch.get("dataset_ids")),
                tags=_string_list(watch.get("tags")),
                session_id=_optional_str(watch.get("session_id")),
                source_provider=_optional_str(watch.get("source_provider")),
                adapter=_optional_str(watch.get("adapter")),
                dry_run=dry_run,
            )
            summary["name"] = watch.get("name")
            summaries.append(summary)
        except Exception as exc:  # noqa: BLE001 - keep processing configured watches.
            errors.append({"watch": watch.get("name") or watch.get("root"), "error": str(exc)})
    return {
        "command": "watch-scan",
        "configured": True,
        "dry_run": dry_run,
        "watches": summaries,
        "errors": errors,
    }


def sync_outbox(
    client: LabTracker,
    config: WatchConfig,
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
        needs_draft = (
            (request_draft or _event_requests_draft(event))
            and event["sink"] == SINK_STAGED_NOTE
            and sync.get("note_id")
            and not sync.get("change_set_id")
        )
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
        return WatchSyncResult(
            action="skipped",
            path=str(path),
            event_id=event["event_id"],
            capture_id=event["capture_id"],
            sink=event["sink"],
            note_id=_optional_str(sync.get("note_id")),
            output_id=_optional_str(sync.get("output_id")),
            change_set_id=_optional_str(sync.get("change_set_id")),
            reason="already_synced",
        ).to_dict()

    def _failed(path: Path, event: JsonObject, exc: Exception) -> JsonObject:
        event = _record_sync_failure(path, event, str(exc), dry_run=dry_run)
        return WatchSyncResult(
            action="failed",
            path=str(path),
            event_id=event["event_id"],
            capture_id=event["capture_id"],
            sink=event["sink"],
            error=str(exc),
        ).to_dict()

    return _outbox.drain_outbox(
        outbox=outbox,
        command="watch-sync",
        dry_run=dry_run,
        request_draft=request_draft,
        limit=limit,
        read_event=read_event,
        is_actionable=_is_actionable,
        process=_process,
        on_skipped=_skipped,
        on_failure=_failed,
    )


def render_event_note(event: Mapping[str, Any]) -> str:
    payload = validate_event(event)
    context = payload["context"]
    source = payload["source"]
    title = str(payload["payload"].get("title") or f"Watch capture {payload['capture_id']}")
    summary = str(payload["payload"].get("summary") or "Captured watch-folder evidence.")
    lines = [
        f"# {title}",
        "",
        summary,
        "",
        "## Capture",
        f"- Capture ID: `{payload['capture_id']}`",
        f"- Event ID: `{payload['event_id']}`",
        f"- Capture kind: `{payload['capture_kind']}`",
        f"- Sink: `{payload['sink']}`",
        f"- Adapter: `{payload['adapter']}`",
        f"- Observed at: {payload['observed_at']}",
    ]
    if source.get("uri"):
        lines.append(f"- Source URI: {source['uri']}")
    if source.get("external_id"):
        lines.append(f"- Source external ID: `{source['external_id']}`")
    if source.get("relative_path"):
        lines.append(f"- Relative path: `{source['relative_path']}`")
    if source.get("content_hash"):
        lines.append(f"- Content hash: `{source['content_hash']}`")
    if source.get("size_bytes") is not None:
        lines.append(f"- Size bytes: {source['size_bytes']}")
    if context.get("project_id"):
        lines.extend(["", "## Research Context", f"- Project: `{context['project_id']}`"])
        if context.get("question_id"):
            lines.append(f"- Candidate question: `{context['question_id']}`")
        if context["dataset_ids"]:
            lines.append(
                "- Candidate datasets: "
                + ", ".join(f"`{item}`" for item in context["dataset_ids"])
            )
        if context["tags"]:
            lines.append(f"- Tags: {', '.join(context['tags'])}")
    if context.get("session_id"):
        if not context.get("project_id"):
            lines.extend(["", "## Research Context"])
        lines.append(f"- Session: `{context['session_id']}`")
    if payload["artifacts"]:
        lines.extend(["", "## Artifact Pointers"])
        for artifact in payload["artifacts"]:
            label = artifact.get("title") or artifact.get("uri") or "artifact"
            lines.append(f"- {label}")
            for key in ("kind", "uri", "summary", "content_hash", "size_bytes"):
                if artifact.get(key) is not None:
                    lines.append(f"  - {key}: {artifact[key]}")
    if payload["metrics"]:
        lines.extend(["", "## Metrics"])
        for key, value in sorted(payload["metrics"].items()):
            lines.append(f"- {key}: {value}")
    if payload["log_excerpt"]:
        lines.extend(["", "## Log Excerpt", "```text", payload["log_excerpt"].strip(), "```"])
    return "\n".join(lines).strip() + "\n"


def is_hidden_relative(path: Path, *, relative_to: Path) -> bool:
    return _is_hidden_relative(path, relative_to=relative_to)


def matches_any(path: Path, *, root: Path, patterns: Sequence[str]) -> bool:
    return _matches_any(path, root=root, patterns=patterns)


def local_folder_external_id(root: Path, relative_path: str) -> str:
    return _local_folder_external_id(root, relative_path)


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
) -> WatchSyncResult:
    stale_reason = _stale_reason(event)
    if stale_reason:
        if not dry_run:
            _record_sync_failure(path, event, stale_reason, dry_run=False, status="stale")
        return WatchSyncResult(
            action="stale",
            path=str(path),
            event_id=event["event_id"],
            capture_id=event["capture_id"],
            sink=event["sink"],
            error=stale_reason,
        )
    if event["sink"] == SINK_STAGED_NOTE:
        return _sync_staged_note(
            client,
            path=path,
            event=event,
            note_indexes=note_indexes,
            dry_run=dry_run,
            request_draft=request_draft,
        )
    if event["sink"] == SINK_ACQUISITION_OUTPUT:
        return _sync_acquisition_output(client, path=path, event=event, dry_run=dry_run)
    raise LTValidationError(f"Unsupported watch sink: {event['sink']}")


def _sync_staged_note(
    client: LabTracker,
    *,
    path: Path,
    event: JsonObject,
    note_indexes: dict[str, EvidenceNoteIndex],
    dry_run: bool,
    request_draft: bool,
) -> WatchSyncResult:
    source_path = _optional_str(event["source"].get("path"))
    note_id = _optional_str(event.get("sync", {}).get("note_id"))
    note: LTRecord | None = None
    change_set_id = _optional_str(event.get("sync", {}).get("change_set_id"))
    project_id = _non_empty(_optional_str(event["context"].get("project_id")) or "", "project_id")
    if project_id not in note_indexes:
        note_indexes[project_id] = client.build_evidence_note_index(project_id=project_id)
    index = note_indexes[project_id]
    if not note_id and source_path:
        result = client.import_evidence_file(
            project_id=project_id,
            file_path=source_path,
            source_provider=str(event["source"].get("provider") or "local-folder"),
            source_external_id=_event_source_external_id(event),
            source_uri=str(event["source"].get("uri") or Path(source_path).as_uri()),
            adapter=str(event["adapter"]),
            title=str(event["payload"].get("title") or Path(source_path).name),
            metadata=_event_metadata(event),
            status=str(event["payload"].get("status") or "staged"),
            dry_run=dry_run,
            evidence_note_index=index,
        )
        if dry_run:
            return WatchSyncResult(
                action="skipped",
                path=str(path),
                event_id=event["event_id"],
                capture_id=event["capture_id"],
                sink=event["sink"],
                reason="dry_run",
            )
        note = result.note
        action = result.action
        reason = result.reason
        note_id = str(note.id) if note is not None else None
    elif not note_id:
        # Content-bearing events (e.g. git snapshots) carry their evidence
        # text in the reserved payload.body key; other events render.
        body = event["payload"].get("body")
        evidence = body if isinstance(body, str) and body.strip() else render_event_note(event)
        evidence_bytes = evidence.encode("utf-8")
        content_hash = hashlib.sha256(evidence_bytes).hexdigest()
        metadata = build_evidence_metadata(
            source_provider=str(event["source"].get("provider") or "watch-manifest"),
            source_uri=str(event["source"].get("uri") or path.as_uri()),
            source_external_id=_event_source_external_id(event),
            content_hash=content_hash,
            capture_kind=str(event["capture_kind"]),
            adapter=str(event["adapter"]),
            title=str(event["payload"].get("title") or f"Watch capture {event['capture_id']}"),
            observed_at=str(event["observed_at"]),
            metadata=_event_metadata(event),
        )
        evidence_key = (
            str(metadata["evidence_source_provider"]),
            str(metadata["evidence_source_external_id"]),
            str(metadata["evidence_content_hash"]),
        )
        note = index.get(evidence_key)
        if dry_run:
            return WatchSyncResult(
                action="skipped",
                path=str(path),
                event_id=event["event_id"],
                capture_id=event["capture_id"],
                sink=event["sink"],
                reason="dry_run",
            )
        if note is None:
            note = client._upload_note_file_payload(
                project_id=project_id,
                path=path.with_suffix(".md"),
                payload=evidence_bytes,
                metadata=metadata,
                status=str(event["payload"].get("status") or "staged"),
                content_type="text/markdown",
                # Include the event id (which embeds the content hash) so the
                # server-side first-wins capture-id dedupe agrees with the
                # evidence-key dedupe: new content for the same source gets a
                # new note instead of silently returning the stale one.
                client_capture_id=_client_capture_id(
                    f"{_event_source_external_id(event)}:{event['event_id']}"
                ),
            )
            index[evidence_key] = note
            action = "imported"
            reason = ""
        else:
            action = "skipped"
            reason = "duplicate"
        note_id = str(note.id)
    else:
        action = "synced"
        reason = ""
    draft_error = ""
    if (
        (request_draft or _event_requests_draft(event))
        and note_id
        and not change_set_id
        and not dry_run
    ):
        try:
            draft = client.create_analysis_graph_draft(note_id)
            change_set_id = str(draft.id)
        except Exception as exc:  # noqa: BLE001 - evidence sync succeeded; draft can retry later.
            draft_error = str(exc)
    if not dry_run and note_id:
        _record_sync_success(
            path,
            event,
            note_id=note_id,
            output_id=None,
            change_set_id=change_set_id,
            draft_error=draft_error,
        )
    return WatchSyncResult(
        action=action,
        path=str(path),
        event_id=event["event_id"],
        capture_id=event["capture_id"],
        sink=event["sink"],
        note_id=note_id,
        change_set_id=change_set_id,
        reason=reason,
        error=draft_error,
    )


def _sync_acquisition_output(
    client: LabTracker,
    *,
    path: Path,
    event: JsonObject,
    dry_run: bool,
) -> WatchSyncResult:
    session_id = _non_empty(_optional_str(event["context"].get("session_id")) or "", "session_id")
    source = event["source"]
    file_path = _non_empty(
        _optional_str(source.get("relative_path")) or _optional_str(source.get("path")) or "",
        "file_path",
    )
    checksum = _non_empty(_optional_str(source.get("content_hash")) or "", "content_hash")
    size_bytes = source.get("size_bytes")
    if dry_run:
        return WatchSyncResult(
            action="skipped",
            path=str(path),
            event_id=event["event_id"],
            capture_id=event["capture_id"],
            sink=event["sink"],
            reason="dry_run",
        )
    output = client.register_acquisition_output(
        session_id,
        file_path=file_path,
        checksum=checksum,
        size_bytes=int(size_bytes) if size_bytes is not None else None,
    )
    output_id = str(output.id)
    _record_sync_success(
        path,
        event,
        note_id=None,
        output_id=output_id,
        change_set_id=None,
        draft_error="",
    )
    return WatchSyncResult(
        action="registered",
        path=str(path),
        event_id=event["event_id"],
        capture_id=event["capture_id"],
        sink=event["sink"],
        output_id=output_id,
    )


def _record_sync_success(
    path: Path,
    event: JsonObject,
    *,
    note_id: str | None,
    output_id: str | None,
    change_set_id: str | None,
    draft_error: str,
) -> None:
    sync = dict(event.get("sync") or {})
    sync.update(
        {
            "status": "synced",
            "synced_at": utc_now(),
            "last_error": draft_error or None,
        }
    )
    if note_id:
        sync["note_id"] = note_id
    if output_id:
        sync["output_id"] = output_id
    if change_set_id:
        sync["change_set_id"] = change_set_id
    if draft_error:
        sync["last_draft_error"] = draft_error
    else:
        sync.pop("last_draft_error", None)
    event["sync"] = {key: value for key, value in sync.items() if value is not None}
    _write_json_atomic(path, event)


def _record_sync_failure(
    path: Path,
    event: JsonObject,
    error: str,
    *,
    dry_run: bool,
    status: str = "failed",
) -> JsonObject:
    sync = dict(event.get("sync") or {})
    sync.update(
        {
            "status": status,
            "attempts": int(sync.get("attempts") or 0) + 1,
            "last_error": error,
            "last_attempt_at": utc_now(),
        }
    )
    event["sync"] = sync
    if not dry_run:
        _write_json_atomic(path, event)
    return event


def _stale_reason(event: Mapping[str, Any]) -> str:
    source = _json_mapping(event.get("source") or {})
    path = _optional_str(source.get("path"))
    if path:
        fingerprint = stable_file_fingerprint(path)
        if fingerprint is None:
            return f"watched file is missing or still changing: {path}"
        if (
            fingerprint.checksum != source.get("content_hash")
            or fingerprint.size_bytes != source.get("size_bytes")
            or fingerprint.mtime != source.get("mtime")
        ):
            return f"watched file changed since scan: {path}"
    manifest_path = _optional_str(source.get("manifest_path"))
    if manifest_path and source.get("manifest_content_hash"):
        manifest = Path(manifest_path)
        if not manifest.exists():
            return f"watch manifest is missing: {manifest}"
        if file_sha256(manifest) != source.get("manifest_content_hash"):
            return f"watch manifest changed since scan: {manifest}"
    return ""


def _event_metadata(event: Mapping[str, Any]) -> dict[str, NoteMetadataScalar]:
    payload = validate_event(event)
    context = payload["context"]
    source = payload["source"]
    metadata: dict[str, NoteMetadataScalar] = {
        "watch_event_id": payload["event_id"],
        "watch_capture_id": payload["capture_id"],
        "watch_capture_kind": payload["capture_kind"],
        "watch_sink": payload["sink"],
        "watch_adapter": payload["adapter"],
        "watch_artifact_count": len(payload["artifacts"]),
    }
    for key in ("project_id", "question_id", "session_id"):
        if context.get(key):
            metadata[f"watch_{key}"] = str(context[key])
    if context["dataset_ids"]:
        metadata["watch_dataset_ids"] = ",".join(context["dataset_ids"])
    if context["tags"]:
        metadata["watch_tags"] = ",".join(context["tags"])
    for key in ("relative_path", "content_hash", "size_bytes", "manifest_content_hash"):
        if source.get(key) is not None:
            metadata[f"watch_{key}"] = source[key]
    for key, value in source.items():
        if str(key).startswith("git_") and isinstance(value, (str, bool, int, float)):
            metadata[str(key)] = value
    host = payload.get("host") if isinstance(payload.get("host"), Mapping) else {}
    for key in CAPTURE_HOST_METADATA_KEYS:
        if host.get(key):
            metadata[key] = str(host[key])
    return metadata


def _event_requests_draft(event: Mapping[str, Any]) -> bool:
    """Reserved payload.request_draft key: this event wants a graph draft.

    Lets a queuing command (e.g. `lt git snapshot --request-draft`) scope the
    draft request to its own event, so draining the shared outbox does not
    request LLM drafts for unrelated captures.
    """

    return bool(_json_mapping(event.get("payload") or {}).get("request_draft"))


def _event_source_external_id(event: Mapping[str, Any]) -> str:
    payload = validate_event(event)
    source = payload["source"]
    if source.get("external_id"):
        return str(source["external_id"])
    return f"watch:{payload['sink']}:{payload['capture_id']}:{payload['event_id']}"


def _context_payload(context: Mapping[str, Any]) -> JsonObject:
    return {
        "project_id": _optional_str(context.get("project_id")),
        "question_id": _optional_str(context.get("question_id")),
        "dataset_ids": _string_list(context.get("dataset_ids")),
        "tags": _string_list(context.get("tags")),
        "session_id": _optional_str(context.get("session_id")),
    }


def _watch_config_payload(watch: Mapping[str, Any]) -> JsonObject:
    if not isinstance(watch, Mapping):
        raise LTValidationError("watch entries must be JSON objects.")
    payload = dict(watch)
    payload["mode"] = _mode(str(payload.get("mode") or MODE_FILES))
    payload["sink"] = _sink(str(payload.get("sink") or SINK_STAGED_NOTE))
    payload["root"] = _non_empty(str(payload.get("root") or ""), "root")
    return payload


def _mode(mode: str) -> str:
    resolved = mode.strip()
    if resolved not in ALLOWED_MODES:
        raise LTValidationError(f"watch mode must be one of: {', '.join(sorted(ALLOWED_MODES))}")
    return resolved


def _sink(sink: str) -> str:
    resolved = sink.strip()
    if resolved not in ALLOWED_SINKS:
        raise LTValidationError(f"watch sink must be one of: {', '.join(sorted(ALLOWED_SINKS))}")
    return resolved


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _mapping_list(value: Any) -> list[JsonObject]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise LTValidationError("artifacts must be a list of JSON objects.")
    return [_json_mapping(item) for item in value]


def _json_mapping(value: Any) -> JsonObject:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if item is not None}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if str(item).strip()]
    raise LTValidationError("Expected a string or list of strings.")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _non_empty(value: str, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise LTValidationError(f"{field_name} must not be empty.")
    return text


def _safe_path_part(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)
    return safe.strip(".-") or "event"


def _safe_filename_part(value: str) -> str:
    safe = _safe_path_part(value)
    if len(safe) <= 48:
        return safe
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{safe[:31]}-{digest}"


def _client_capture_id(value: str) -> str:
    return f"lt-watch-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]}"


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    _outbox.write_json_atomic(path, payload)
