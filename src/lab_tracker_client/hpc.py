"""Offline-first HPC provenance capture helpers for Lab Tracker clients."""

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
DEFAULT_CONFIG_RELATIVE_PATH = Path(".lab-tracker") / "hpc.json"
DEFAULT_OUTBOX = ".lab-tracker/outbox/hpc"
DEFAULT_MANIFEST_PATTERN = "lab-tracker-hpc-run.json"
HPC_EVIDENCE_PROVIDER = "hpc-outbox"
HPC_EVIDENCE_ADAPTER = "lt-hpc"
ALLOWED_EVENT_TYPES = {"submit", "begin", "finish"}
TERMINAL_SYNC_STATES = {"synced"}


JsonObject = dict[str, Any]


@dataclass
class HpcConfig:
    """Configuration for a consumer repository or HPC checkout."""

    project_id: str
    cluster: str
    scheduler: str = "slurm"
    outbox: str = DEFAULT_OUTBOX
    default_question_id: str | None = None
    version: int = CONFIG_VERSION
    config_path: Path | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "version": self.version,
            "project_id": self.project_id,
            "cluster": self.cluster,
            "scheduler": self.scheduler,
            "outbox": self.outbox,
        }
        if self.default_question_id:
            payload["default_question_id"] = self.default_question_id
        return payload

    def outbox_path(self) -> Path:
        override = os.getenv("LAB_TRACKER_HPC_OUTBOX")
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
class SbatchJob:
    """Parsed Slurm job identity from sbatch output."""

    job_id: str
    array_task_id: str | None = None
    cluster: str | None = None


@dataclass(frozen=True)
class HpcSyncResult:
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
    return (Path(start or Path.cwd()).expanduser().resolve() / DEFAULT_CONFIG_RELATIVE_PATH)


def find_config_path(start: str | Path | None = None) -> Path | None:
    env_path = os.getenv("LAB_TRACKER_HPC_CONFIG")
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
    cluster: str,
    scheduler: str = "slurm",
    outbox: str = DEFAULT_OUTBOX,
    default_question_id: str | None = None,
    config_path: str | Path | None = None,
    force: bool = False,
) -> HpcConfig:
    path = Path(config_path).expanduser().resolve() if config_path else default_config_path()
    if path.exists() and not force:
        raise LTValidationError(f"HPC config already exists: {path}")
    config = HpcConfig(
        project_id=_non_empty(project_id, "project_id"),
        cluster=_non_empty(cluster, "cluster"),
        scheduler=_non_empty(scheduler, "scheduler"),
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
) -> HpcConfig:
    path = Path(config_path).expanduser().resolve() if config_path else find_config_path(start)
    if path is None or not path.exists():
        raise LTValidationError(
            "HPC config not found. Run 'lt hpc init' or set LAB_TRACKER_HPC_CONFIG."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LTValidationError(f"HPC config is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise LTValidationError("HPC config must be a JSON object.")
    config = HpcConfig(
        version=int(payload.get("version") or CONFIG_VERSION),
        project_id=_non_empty(str(payload.get("project_id") or ""), "project_id"),
        cluster=_non_empty(str(payload.get("cluster") or ""), "cluster"),
        scheduler=_non_empty(str(payload.get("scheduler") or "slurm"), "scheduler"),
        outbox=_non_empty(str(payload.get("outbox") or DEFAULT_OUTBOX), "outbox"),
        default_question_id=_optional_str(payload.get("default_question_id")),
        config_path=path,
    )
    if config.version != CONFIG_VERSION:
        raise LTValidationError(
            f"Unsupported HPC config version {config.version}; expected {CONFIG_VERSION}."
        )
    return config


def new_run_id(prefix: str = "hpc") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def make_event(
    config: HpcConfig,
    *,
    event_type: str,
    run_id: str | None = None,
    event_id: str | None = None,
    observed_at: str | None = None,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    command: Sequence[str] | None = None,
    cwd: str | Path | None = None,
    scheduler: Mapping[str, Any] | None = None,
    source: Mapping[str, Any] | None = None,
    artifacts: Sequence[Mapping[str, Any]] | None = None,
    metrics: Mapping[str, Any] | None = None,
    log_excerpt: str | None = None,
    summary: str | None = None,
) -> JsonObject:
    resolved_event_type = _event_type(event_type)
    resolved_cwd = str(Path(cwd or Path.cwd()).expanduser().resolve())
    resolved_run_id = _non_empty(run_id or os.getenv("LAB_TRACKER_HPC_RUN_ID") or new_run_id())
    resolved_summary = summary or _default_summary(resolved_event_type, resolved_run_id)
    resolved_project_id = _non_empty(project_id or config.project_id, "project_id")
    resolved_question_id = _optional_str(question_id or config.default_question_id)
    resolved_dataset_ids = [str(item) for item in dataset_ids or [] if str(item).strip()]
    resolved_tags = [str(item) for item in tags or [] if str(item).strip()]
    payload: JsonObject = {
        "version": EVENT_VERSION,
        "run_id": resolved_run_id,
        "event_id": event_id or uuid.uuid4().hex,
        "event_type": resolved_event_type,
        "observed_at": observed_at or utc_now(),
        "project_id": resolved_project_id,
        "question_id": resolved_question_id,
        "dataset_ids": resolved_dataset_ids,
        "tags": resolved_tags,
        "command": [str(item) for item in command or []],
        "cwd": resolved_cwd,
        "source": {
            **git_context(resolved_cwd),
            **dict(source or {}),
        },
        "scheduler": {
            "kind": config.scheduler,
            "cluster": config.cluster,
            **dict(scheduler or {}),
        },
        "artifacts": [_artifact_payload(item) for item in artifacts or []],
        "metrics": _json_mapping(metrics or {}),
        "log_excerpt": log_excerpt or "",
        "summary": resolved_summary,
        "capture_id": resolved_run_id,
        "capture_kind": "hpc_run_event",
        "adapter": HPC_EVIDENCE_ADAPTER,
        "sink": "staged-note",
        "context": {
            "project_id": resolved_project_id,
            "question_id": resolved_question_id,
            "dataset_ids": resolved_dataset_ids,
            "tags": resolved_tags,
        },
        "payload": {
            "title": f"HPC {resolved_event_type} {resolved_run_id}",
            "summary": resolved_summary,
            "status": "staged",
        },
        "host": _json_mapping(capture_host_metadata()),
        "sync": {"status": "pending", "attempts": 0},
    }
    return validate_event(payload)


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
        raise LTValidationError(f"HPC event is not valid JSON: {resolved}") from exc
    if not isinstance(payload, dict):
        raise LTValidationError(f"HPC event must be a JSON object: {resolved}")
    return validate_event(payload)


def validate_event(payload: Mapping[str, Any]) -> JsonObject:
    event = dict(payload)
    version = int(event.get("version") or EVENT_VERSION)
    if version != EVENT_VERSION:
        raise LTValidationError(
            f"Unsupported HPC event version {version}; expected {EVENT_VERSION}."
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
    event["command"] = _string_list(event.get("command"))
    event["cwd"] = str(event.get("cwd") or "")
    event["source"] = _json_mapping(event.get("source") or {})
    event["scheduler"] = _json_mapping(event.get("scheduler") or {})
    event["artifacts"] = [_artifact_payload(item) for item in event.get("artifacts") or []]
    event["metrics"] = _json_mapping(event.get("metrics") or {})
    event["log_excerpt"] = str(event.get("log_excerpt") or "")
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
    path = Path(outbox).expanduser()
    if not path.exists():
        return []
    return sorted(item for item in path.glob("*.json") if item.is_file())


def outbox_status(outbox: str | Path) -> JsonObject:
    events: list[JsonObject] = []
    counts: dict[str, int] = {}
    for path in list_event_files(outbox):
        event = read_event(path)
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
        "events": events,
    }


def parse_sbatch_job(output: str, *, fallback_cluster: str | None = None) -> SbatchJob | None:
    cleaned_lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in cleaned_lines:
        submitted = re.search(r"Submitted batch job\s+([A-Za-z0-9_.-]+)", line)
        if submitted:
            return _job_from_token(submitted.group(1), fallback_cluster=fallback_cluster)
        token = line.split()[0]
        if re.match(r"^[A-Za-z0-9_.-]+(?:;[A-Za-z0-9_.-]+)?$", token):
            return _job_from_token(token, fallback_cluster=fallback_cluster)
    return None


def run_submit_command(
    config: HpcConfig,
    command: Sequence[str],
    *,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    summary: str | None = None,
    cwd: str | Path | None = None,
) -> JsonObject:
    resolved_command = [str(part) for part in command if str(part)]
    if not resolved_command:
        raise LTValidationError("lt hpc submit requires a command after '--'.")
    run_id = new_run_id()
    outbox = config.outbox_path()
    env = {
        **os.environ,
        "LAB_TRACKER_HPC_RUN_ID": run_id,
        "LAB_TRACKER_HPC_OUTBOX": str(outbox),
    }
    if config.config_path is not None:
        env["LAB_TRACKER_HPC_CONFIG"] = str(config.config_path)
    result = subprocess.run(
        resolved_command,
        check=False,
        capture_output=True,
        text=True,
        cwd=str(Path(cwd).expanduser().resolve()) if cwd else None,
        env=env,
    )
    parsed_job = parse_sbatch_job(
        "\n".join([result.stdout, result.stderr]),
        fallback_cluster=config.cluster,
    )
    scheduler: JsonObject = {
        "state": "submitted" if result.returncode == 0 else "submit_failed",
        "exit_code": result.returncode,
    }
    if parsed_job is not None:
        scheduler["job_id"] = parsed_job.job_id
        if parsed_job.array_task_id:
            scheduler["array_task_id"] = parsed_job.array_task_id
        if parsed_job.cluster:
            scheduler["cluster"] = parsed_job.cluster
    event = make_event(
        config,
        event_type="submit",
        run_id=run_id,
        project_id=project_id,
        question_id=question_id,
        dataset_ids=dataset_ids,
        tags=tags,
        command=resolved_command,
        cwd=cwd,
        scheduler=scheduler,
        summary=summary or f"Submitted HPC run {run_id}.",
        log_excerpt=_join_log_excerpt(result.stdout, result.stderr),
    )
    path = write_event(event, outbox)
    return {
        "command": "hpc-submit",
        "run_id": run_id,
        "job_id": parsed_job.job_id if parsed_job else None,
        "array_task_id": parsed_job.array_task_id if parsed_job else None,
        "cluster": parsed_job.cluster if parsed_job and parsed_job.cluster else config.cluster,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "event_path": str(path),
        "outbox": str(outbox),
    }


def event_from_manifest(
    config: HpcConfig,
    manifest_path: str | Path,
    *,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
) -> JsonObject:
    path = Path(manifest_path).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LTValidationError(f"HPC manifest is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise LTValidationError(f"HPC manifest must be a JSON object: {path}")
    content_hash = _path_sha256(path)
    observed = _optional_str(payload.get("observed_at")) or datetime.fromtimestamp(
        path.stat().st_mtime,
        timezone.utc,
    ).isoformat()
    event_id = _optional_str(payload.get("event_id")) or f"watch-{content_hash[:16]}"
    source = _json_mapping(payload.get("source") or {})
    source.update(
        {
            "manifest_uri": path.as_uri(),
            "manifest_content_hash": content_hash,
        }
    )
    return make_event(
        config,
        event_type=str(payload.get("event_type") or "finish"),
        run_id=_optional_str(payload.get("run_id")) or path.parent.name,
        event_id=event_id,
        observed_at=observed,
        project_id=project_id or _optional_str(payload.get("project_id")),
        question_id=question_id or _optional_str(payload.get("question_id")),
        dataset_ids=dataset_ids or _string_list(payload.get("dataset_ids")),
        tags=tags or _string_list(payload.get("tags")),
        command=_string_list(payload.get("command")),
        cwd=_optional_str(payload.get("cwd")) or path.parent,
        scheduler=_json_mapping(payload.get("scheduler") or {}),
        source=source,
        artifacts=payload.get("artifacts") or [],
        metrics=_json_mapping(payload.get("metrics") or {}),
        log_excerpt=_optional_str(payload.get("log_excerpt")),
        summary=_optional_str(payload.get("summary")),
    )


def watch_manifests(
    config: HpcConfig,
    *,
    root: str | Path,
    pattern: str = DEFAULT_MANIFEST_PATTERN,
    limit: int | None = None,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    dry_run: bool = False,
) -> JsonObject:
    resolved_root = Path(root).expanduser().resolve()
    if not resolved_root.is_dir():
        raise LTValidationError(f"watch root is not an existing directory: {resolved_root}")
    manifests = sorted(path for path in resolved_root.rglob(pattern) if path.is_file())
    if limit is not None:
        manifests = manifests[: max(0, limit)]
    discovered: list[JsonObject] = []
    errors: list[JsonObject] = []
    for manifest in manifests:
        try:
            event = event_from_manifest(
                config,
                manifest,
                project_id=project_id,
                question_id=question_id,
                dataset_ids=dataset_ids,
                tags=tags,
            )
            target_path = event_path(event, config.outbox_path())
            already_present = target_path.exists()
            if not dry_run:
                target_path = write_event(event, config.outbox_path())
            discovered.append(
                {
                    "manifest": str(manifest),
                    "event_path": str(target_path),
                    "run_id": event["run_id"],
                    "event_type": event["event_type"],
                    "already_present": already_present,
                }
            )
        except Exception as exc:  # noqa: BLE001 - report every bad manifest in the batch.
            errors.append({"manifest": str(manifest), "error": str(exc)})
    return {
        "command": "hpc-watch",
        "root": str(resolved_root),
        "pattern": pattern,
        "dry_run": dry_run,
        "matched": len(manifests),
        "imported": discovered,
        "errors": errors,
    }


def sync_outbox(
    client: LabTracker,
    config: HpcConfig,
    *,
    dry_run: bool = False,
    request_draft: bool = False,
    limit: int | None = None,
) -> JsonObject:
    outbox = config.outbox_path()
    files = list_event_files(outbox)
    if limit is not None:
        files = files[: max(0, limit)]
    results: list[JsonObject] = []
    errors: list[JsonObject] = []
    note_indexes: dict[str, EvidenceNoteIndex] = {}
    for path in files:
        event = read_event(path)
        sync = event.get("sync", {})
        already_synced = str(sync.get("status") or "") in TERMINAL_SYNC_STATES
        needs_draft = request_draft and sync.get("note_id") and not sync.get("change_set_id")
        if already_synced and not needs_draft:
            results.append(
                HpcSyncResult(
                    action="skipped",
                    path=str(path),
                    run_id=str(event["run_id"]),
                    event_type=str(event["event_type"]),
                    note_id=_optional_str(sync.get("note_id")),
                    change_set_id=_optional_str(sync.get("change_set_id")),
                    reason="already_synced",
                ).to_dict()
            )
            continue
        try:
            result = _sync_event(
                client,
                path=path,
                event=event,
                note_indexes=note_indexes,
                dry_run=dry_run,
                request_draft=request_draft,
            )
            results.append(result.to_dict())
            if result.error:
                errors.append(result.to_dict())
        except Exception as exc:  # noqa: BLE001 - keep draining the outbox.
            event = _record_sync_failure(path, event, str(exc), dry_run=dry_run)
            result = HpcSyncResult(
                action="failed",
                path=str(path),
                run_id=str(event["run_id"]),
                event_type=str(event["event_type"]),
                error=str(exc),
            ).to_dict()
            results.append(result)
            errors.append(result)
    return {
        "command": "hpc-sync",
        "outbox": str(outbox),
        "dry_run": dry_run,
        "request_draft": request_draft,
        "processed": len(results),
        "results": results,
        "errors": errors,
    }


def begin_event(
    config: HpcConfig,
    *,
    run_id: str | None = None,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    summary: str | None = None,
) -> tuple[JsonObject, Path]:
    event = make_event(
        config,
        event_type="begin",
        run_id=run_id,
        project_id=project_id,
        question_id=question_id,
        dataset_ids=dataset_ids,
        tags=tags,
        summary=summary,
        scheduler={
            "job_id": os.getenv("SLURM_JOB_ID"),
            "array_task_id": os.getenv("SLURM_ARRAY_TASK_ID"),
            "state": "running",
        },
    )
    path = write_event(event, config.outbox_path())
    return event, path


def finish_event(
    config: HpcConfig,
    *,
    run_id: str | None = None,
    exit_code: int | None = None,
    state: str | None = None,
    manifest: str | Path | None = None,
    logs: Sequence[str | Path] | None = None,
    artifacts: Sequence[str] | None = None,
    metrics: Sequence[str] | None = None,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    summary: str | None = None,
) -> tuple[JsonObject, Path]:
    manifest_payload: JsonObject = {}
    if manifest is not None:
        manifest_path = Path(manifest).expanduser().resolve()
        manifest_payload = event_from_manifest(config, manifest_path)
    scheduler = _json_mapping(manifest_payload.get("scheduler") or {})
    scheduler.update(
        {
            "job_id": os.getenv("SLURM_JOB_ID") or scheduler.get("job_id"),
            "array_task_id": os.getenv("SLURM_ARRAY_TASK_ID") or scheduler.get("array_task_id"),
            "state": state or scheduler.get("state") or _state_from_exit_code(exit_code),
            "exit_code": exit_code if exit_code is not None else scheduler.get("exit_code"),
        }
    )
    merged_artifacts = list(manifest_payload.get("artifacts") or [])
    merged_artifacts.extend(_artifact_from_uri(uri) for uri in artifacts or [])
    event = make_event(
        config,
        event_type="finish",
        run_id=run_id or _optional_str(manifest_payload.get("run_id")),
        event_id=_optional_str(manifest_payload.get("event_id")),
        observed_at=_optional_str(manifest_payload.get("observed_at")),
        project_id=project_id or _optional_str(manifest_payload.get("project_id")),
        question_id=question_id or _optional_str(manifest_payload.get("question_id")),
        dataset_ids=dataset_ids or _string_list(manifest_payload.get("dataset_ids")),
        tags=tags or _string_list(manifest_payload.get("tags")),
        command=_string_list(manifest_payload.get("command")),
        cwd=_optional_str(manifest_payload.get("cwd")) or Path.cwd(),
        scheduler=scheduler,
        source=_json_mapping(manifest_payload.get("source") or {}),
        artifacts=merged_artifacts,
        metrics={**_json_mapping(manifest_payload.get("metrics") or {}), **_metrics(metrics)},
        log_excerpt=_join_log_excerpt(
            _optional_str(manifest_payload.get("log_excerpt")) or "",
            _read_log_excerpt(logs or []),
        ),
        summary=summary or _optional_str(manifest_payload.get("summary")),
    )
    path = write_event(event, config.outbox_path())
    return event, path


def git_context(cwd: str | Path | None = None) -> JsonObject:
    root = Path(cwd or Path.cwd()).expanduser()
    commit = _git_output(root, "rev-parse", "HEAD")
    dirty = bool(_git_output(root, "status", "--porcelain"))
    return {
        "git_commit": commit,
        "git_dirty": dirty,
    }


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
) -> HpcSyncResult:
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
            return HpcSyncResult(
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
            reason = ""
        else:
            action = "skipped"
            reason = "duplicate"
        note_id = str(note.id)
    else:
        action = "synced"
        reason = ""
    change_set_id = _optional_str(event.get("sync", {}).get("change_set_id"))
    draft_error = ""
    if request_draft and note_id and not change_set_id and not dry_run:
        try:
            draft = client.create_analysis_graph_draft(note_id)
            change_set_id = str(draft.id)
        except Exception as exc:  # noqa: BLE001 - evidence sync succeeded; draft can retry later.
            draft_error = str(exc)
    if not dry_run:
        _record_sync_success(
            path,
            event,
            note_id=note_id,
            change_set_id=change_set_id,
            draft_error=draft_error,
        )
    return HpcSyncResult(
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
    scheduler = payload["scheduler"]
    source = payload["source"]
    lines = [
        f"# HPC run {payload['run_id']} {payload['event_type']}",
        "",
        payload["summary"] or _default_summary(payload["event_type"], payload["run_id"]),
        "",
        "## Execution",
        f"- Cluster: {scheduler.get('cluster') or 'unknown'}",
        f"- Scheduler: {scheduler.get('kind') or 'unknown'}",
    ]
    for label, key in (
        ("Job ID", "job_id"),
        ("Array task ID", "array_task_id"),
        ("State", "state"),
        ("Exit code", "exit_code"),
    ):
        if scheduler.get(key) is not None:
            lines.append(f"- {label}: {scheduler[key]}")
    if payload["command"]:
        lines.append(f"- Command: `{' '.join(payload['command'])}`")
    if payload["cwd"]:
        lines.append(f"- Working directory: `{payload['cwd']}`")
    if source.get("git_commit"):
        lines.append(f"- Git commit: `{source['git_commit']}`")
        lines.append(f"- Git dirty: {bool(source.get('git_dirty'))}")
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
    if payload["metrics"]:
        lines.extend(["", "## Metrics"])
        for key, value in sorted(payload["metrics"].items()):
            lines.append(f"- {key}: {value}")
    if payload["log_excerpt"]:
        lines.extend(["", "## Log Excerpt", "```text", payload["log_excerpt"].strip(), "```"])
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
    scheduler = payload["scheduler"]
    source = payload["source"]
    metadata: dict[str, NoteMetadataScalar] = {
        "hpc_run_id": payload["run_id"],
        "hpc_event_id": payload["event_id"],
        "hpc_event_type": payload["event_type"],
        "hpc_project_id": payload["project_id"],
        "hpc_cluster": str(scheduler.get("cluster") or ""),
        "hpc_scheduler": str(scheduler.get("kind") or ""),
        "hpc_artifact_count": len(payload["artifacts"]),
    }
    for key in ("question_id",):
        if payload.get(key):
            metadata[f"hpc_{key}"] = str(payload[key])
    if payload["dataset_ids"]:
        metadata["hpc_dataset_ids"] = ",".join(payload["dataset_ids"])
    if payload["tags"]:
        metadata["hpc_tags"] = ",".join(payload["tags"])
    for key in ("job_id", "array_task_id", "state", "exit_code"):
        if scheduler.get(key) is not None:
            metadata[f"hpc_{key}"] = scheduler[key]
    if source.get("git_commit"):
        metadata["hpc_git_commit"] = str(source["git_commit"])
        metadata["hpc_git_dirty"] = bool(source.get("git_dirty"))
    host = payload.get("host") if isinstance(payload.get("host"), Mapping) else {}
    for key in CAPTURE_HOST_METADATA_KEYS:
        if host.get(key):
            metadata[key] = str(host[key])
    return build_evidence_metadata(
        source_provider=HPC_EVIDENCE_PROVIDER,
        source_uri=source_uri,
        source_external_id=source_external_id,
        content_hash=content_hash,
        capture_kind="hpc_run_event",
        adapter=HPC_EVIDENCE_ADAPTER,
        title=f"HPC {payload['event_type']} {payload['run_id']}",
        observed_at=str(payload["observed_at"]),
        metadata=metadata,
    )


def event_source_external_id(event: Mapping[str, Any]) -> str:
    payload = validate_event(dict(event))
    cluster = str(payload["scheduler"].get("cluster") or "cluster")
    return f"hpc:{cluster}:{payload['run_id']}:{payload['event_type']}:{payload['event_id']}"


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
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _job_from_token(token: str, *, fallback_cluster: str | None) -> SbatchJob:
    job_token, separator, cluster = token.partition(";")
    task_id = None
    if "_" in job_token:
        job_token, task_id = job_token.split("_", 1)
    return SbatchJob(
        job_id=job_token,
        array_task_id=task_id,
        cluster=cluster if separator else fallback_cluster,
    )


def _git_output(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _artifact_from_uri(uri: str) -> JsonObject:
    cleaned = _non_empty(uri, "artifact uri")
    return {
        "uri": cleaned,
        "kind": "file",
        "title": Path(cleaned).name or cleaned,
        "summary": "",
    }


def _metrics(items: Sequence[str] | None) -> JsonObject:
    parsed: JsonObject = {}
    for item in items or []:
        key, separator, value = str(item).partition("=")
        if not separator:
            raise LTValidationError(f"Metric {item!r} must use key=value.")
        parsed[_non_empty(key, "metric key")] = _metric_value(value)
    return parsed


def _metric_value(value: str) -> str | int | float | bool:
    cleaned = value.strip()
    if cleaned.lower() in {"true", "false"}:
        return cleaned.lower() == "true"
    try:
        return int(cleaned)
    except ValueError:
        pass
    try:
        return float(cleaned)
    except ValueError:
        return cleaned


def _read_log_excerpt(paths: Sequence[str | Path], *, max_chars: int = 4000) -> str:
    chunks: list[str] = []
    remaining = max_chars
    for item in paths:
        if remaining <= 0:
            break
        path = Path(item).expanduser()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        excerpt = text[-remaining:]
        chunks.append(f"==> {path} <==\n{excerpt.strip()}")
        remaining -= len(excerpt)
    return "\n\n".join(chunks)


def _join_log_excerpt(*parts: str) -> str:
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _state_from_exit_code(exit_code: int | None) -> str:
    if exit_code is None:
        return "finished"
    return "completed" if exit_code == 0 else "failed"


def _default_summary(event_type: str, run_id: str) -> str:
    return f"HPC run {run_id} emitted a {event_type} event."


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
