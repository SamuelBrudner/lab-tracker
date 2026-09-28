"""Offline-first HPC provenance capture helpers for Lab Tracker clients."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client import outbox as _outbox
from lab_tracker_client.client import (
    CAPTURE_HOST_METADATA_KEYS,
    DECLARED_TARGET_SOURCE_KEY,
    EvidenceNoteIndex,
    LabTracker,
    LTRecord,
    LTValidationError,
    build_evidence_metadata,
    capture_host_metadata,
    declared_target_source_for,
    declared_targets,
    resolve_declared_question,
    validate_declared_target_source,
)
from lab_tracker_client.evidence_index import outbox_note_index
from lab_tracker_client.gitinfo import (
    dirty_label,
    dirty_metadata,
    dirty_state_fields,
    git_dirty_state,
    git_head_commit,
    git_timeout_seconds,
    head_commit_fields,
    worktree_tree_id,
)
from lab_tracker_client.redaction import redact_capture_text

CONFIG_VERSION = 1
EVENT_VERSION = 1
DEFAULT_CONFIG_RELATIVE_PATH = Path(".lab-tracker") / "hpc.json"
DEFAULT_OUTBOX = ".lab-tracker/outbox/hpc"
DEFAULT_MANIFEST_PATTERN = "lab-tracker-hpc-run.json"
HPC_EVIDENCE_PROVIDER = "hpc-outbox"
HPC_EVIDENCE_ADAPTER = "lt-hpc"
ALLOWED_EVENT_TYPES = {"submit", "begin", "finish"}
TERMINAL_SYNC_STATES = {"synced"}
_UTF8_MAX_BYTES_PER_CHAR = 4
# ``lt hpc submit`` records each accepted job's run in the submit directory
# (``<submit dir>/.lab-tracker/hpc-runs/job-<id>.json``) so a job started with
# ``--export=NONE`` -- and the TaskEpilog -- can find its run from Slurm's own
# ``SLURM_SUBMIT_DIR`` and ``SLURM_JOB_ID`` alone.
SUBMIT_MANIFEST_DIR = Path(".lab-tracker") / "hpc-runs"
SUBMIT_MANIFEST_VERSION = 1
SUBMIT_MANIFEST_KIND = "lab-tracker-hpc-submit"
EPILOG_ENABLED_ENV = "LAB_TRACKER_HPC_EPILOG_ENABLED"
_OFF_VALUES = frozenset({"0", "false", "no", "off"})
# At most this many ``slurm-*.out`` files per folder are left out of a
# recomputed worktree tree.
_MAX_JOB_LOGS = 1_000


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
    if start is None:
        # Inside a job started with --export=NONE (no LAB_TRACKER_HPC_CONFIG)
        # whose working directory is outside the checkout: the submit
        # manifest names the config the job was submitted with.
        manifest = find_submit_manifest()
        configured = _optional_str(manifest.get("config")) if manifest else None
        if configured and Path(configured).expanduser().exists():
            return Path(configured).expanduser().resolve()
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


def resolve_outbox_path(repo_root: str | Path) -> tuple[Path, str | None]:
    """Return ``(outbox, error-detail)`` for the HPC adapter outbox of ``repo_root``.

    Mirrors ``lab_tracker_client.repo.resolve_outbox_path``: without ``hpc.json``
    the default outbox (``LAB_TRACKER_HPC_OUTBOX`` or ``.lab-tracker/outbox/hpc``
    under ``repo_root``) is returned with no error; a config that exists but
    cannot be loaded returns that default plus the load error, so ``lt outbox``
    still finds queued events and reports the broken config.
    """

    root = Path(repo_root).expanduser().resolve()
    override = os.getenv("LAB_TRACKER_HPC_OUTBOX")
    fallback = Path(override).expanduser() if override else root / DEFAULT_OUTBOX
    if not fallback.is_absolute():
        fallback = root / fallback
    fallback = fallback.resolve()
    config_path = find_config_path(root)
    if config_path is None:
        return fallback, None
    try:
        return load_config(config_path=config_path).outbox_path(), None
    except Exception as exc:  # noqa: BLE001 - a broken config must not hide queued events.
        return fallback, f"HPC config could not be loaded ({exc}); using {fallback}"


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
    resolved_question_id, question_id_source = resolve_declared_question(
        _optional_str(question_id), _optional_str(config.default_question_id)
    )
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
        "question_id_source": question_id_source,
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
    # Legacy events recorded no source: None means "not recorded", not a guess.
    event["question_id_source"] = validate_declared_target_source(
        event.get("question_id_source")
    )
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
        "skipped_commits": _outbox.count_skipped_commits(outbox),
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
    # Fail on a bad LAB_TRACKER_GIT_TIMEOUT_SECONDS before submitting: raising
    # after the scheduler accepted the job would lose the job's record.
    git_timeout_seconds()
    run_id = new_run_id()
    outbox = config.outbox_path()
    submit_dir = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
    # The code the job will run is the code submitted now: take its tree before
    # sbatch (and before any slurm-<job>.out exists); begin/finish reuse it.
    submitted_tree = worktree_source(submit_dir, exclude=job_output_files(submit_dir))
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
    sbatch_output = "\n".join([result.stdout, result.stderr])
    parsed_job = parse_sbatch_job(sbatch_output, fallback_cluster=config.cluster)
    printed_job = parse_sbatch_job(sbatch_output, fallback_cluster=None)
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
        source=submitted_tree,
        summary=summary or f"Submitted HPC run {run_id}.",
        log_excerpt=_join_log_excerpt(result.stdout, result.stderr),
    )
    path = write_event(event, outbox)
    payload: JsonObject = {
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
    if result.returncode == 0 and parsed_job is not None:
        # The submit event above is already durable; a manifest that cannot be
        # written only costs --export=NONE jobs their run link, never the job.
        try:
            payload["run_manifest"] = str(
                write_submit_manifest(
                    config,
                    submit_dir=submit_dir,
                    run_id=run_id,
                    job_id=parsed_job.job_id,
                    sbatch_cluster=printed_job.cluster if printed_job else None,
                    outbox=outbox,
                    project_id=project_id,
                    question_id=question_id,
                    dataset_ids=dataset_ids,
                    tags=tags,
                    submit_event_id=str(event["event_id"]),
                    worktree=submitted_tree,
                )
            )
        except OSError as exc:
            payload["run_manifest_error"] = str(exc)
            print(
                f"lab-tracker: warning: could not record run {run_id} in the submit "
                f"directory ({exc}); jobs started with --export=NONE and the Slurm "
                "epilog will not find it.",
                file=sys.stderr,
            )
    return payload


def submit_manifest_path(submit_dir: str | Path, job_id: str, cluster: str | None = None) -> Path:
    """Where ``lt hpc submit`` records job ``job_id``'s run under ``submit_dir``."""

    suffix = f".{_safe_path_part(cluster)}" if cluster else ""
    name = f"job-{_safe_path_part(job_id)}{suffix}.json"
    return Path(submit_dir).expanduser() / SUBMIT_MANIFEST_DIR / name


def write_submit_manifest(
    config: HpcConfig,
    *,
    submit_dir: Path,
    run_id: str,
    job_id: str,
    sbatch_cluster: str | None,
    outbox: Path,
    project_id: str | None,
    question_id: str | None,
    dataset_ids: Sequence[str] | None,
    tags: Sequence[str] | None,
    submit_event_id: str,
    worktree: Mapping[str, Any] | None = None,
) -> Path:
    """Record a submitted job's run in its submit directory; return the file.

    Only explicit ``--project``/``--question``/``--dataset``/``--tag`` values are
    recorded: config defaults are re-resolved from the config when the run
    finishes, so their provenance label stays ``config_default``. ``worktree``
    (``git_worktree_tree`` or its ``_error``) is the tree taken before sbatch,
    which the job's begin/finish events reuse.
    """

    path = submit_manifest_path(submit_dir, job_id, sbatch_cluster)
    manifest: JsonObject = {
        "version": SUBMIT_MANIFEST_VERSION,
        "kind": SUBMIT_MANIFEST_KIND,
        "run_id": run_id,
        "job_id": job_id,
        "cluster": config.cluster,
        "scheduler": config.scheduler,
        "outbox": str(outbox),
        "submit_dir": str(Path(submit_dir).resolve()),
        "submitted_at": utc_now(),
        "submit_event_id": submit_event_id,
        "dataset_ids": [str(item) for item in dataset_ids or [] if str(item).strip()],
        "tags": [str(item) for item in tags or [] if str(item).strip()],
    }
    optional = {
        "config": str(config.config_path) if config.config_path else None,
        "sbatch_cluster": sbatch_cluster,
        "project_id": _optional_str(project_id),
        "question_id": _optional_str(question_id),
        "lt_command": _lt_command_path(),
        **{
            key: _optional_str((worktree or {}).get(key))
            for key in ("git_worktree_tree", "git_worktree_tree_error")
        },
    }
    manifest.update({key: value for key, value in optional.items() if value})
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, manifest)
    return path


def find_submit_manifest(environ: Mapping[str, str] | None = None) -> JsonObject | None:
    """The submit manifest of the Slurm job described by ``environ``, or ``None``.

    Uses only variables Slurm sets in every job and task epilog, even under
    ``--export=NONE``: ``SLURM_ARRAY_JOB_ID`` (array tasks) or ``SLURM_JOB_ID``,
    ``SLURM_SUBMIT_DIR`` (or ``SLURM_JOB_WORK_DIR``) and ``SLURM_CLUSTER_NAME``.
    Never raises: an unreadable manifest is treated as absent.
    """

    env = os.environ if environ is None else environ
    job_id = (env.get("SLURM_ARRAY_JOB_ID") or env.get("SLURM_JOB_ID") or "").strip()
    if not job_id:
        return None
    cluster = (env.get("SLURM_CLUSTER_NAME") or "").strip() or None
    directories: list[str] = []
    for key in ("SLURM_SUBMIT_DIR", "SLURM_JOB_WORK_DIR"):
        value = (env.get(key) or "").strip()
        if value and value not in directories:
            directories.append(value)
    for directory in directories:
        candidates = [submit_manifest_path(directory, job_id, cluster)] if cluster else []
        candidates.append(submit_manifest_path(directory, job_id))
        for path in candidates:
            manifest = _read_submit_manifest(path)
            if manifest is not None and str(manifest.get("job_id")) == job_id:
                manifest["manifest_path"] = str(path)
                return manifest
    return None


def _read_submit_manifest(path: Path) -> JsonObject | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != SUBMIT_MANIFEST_KIND:
        return None
    if not _optional_str(payload.get("run_id")):
        return None
    return payload


def _lt_command_path() -> str | None:
    """The ``lt`` executable of this client, so a TaskEpilog can call the same one."""

    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name in {"lt", "lt.exe"}:
        try:
            return str(argv0.resolve())
        except OSError:
            pass
    sibling = Path(sys.executable).parent / ("lt.exe" if os.name == "nt" else "lt")
    if sibling.exists():
        return str(sibling)
    return shutil.which("lt")


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
    return sync_outbox_path(
        client,
        config.outbox_path(),
        dry_run=dry_run,
        request_draft=request_draft,
        limit=limit,
    )


def sync_outbox_path(
    client: LabTracker,
    outbox: Path,
    *,
    dry_run: bool = False,
    request_draft: bool = False,
    limit: int | None = None,
) -> JsonObject:
    """Drain the HPC outbox at ``outbox`` (no config needed; see ``lt outbox``)."""

    outbox = Path(outbox).expanduser()
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
        return HpcSyncResult(
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
        return HpcSyncResult(
            action="failed",
            path=str(path),
            run_id=str(event["run_id"]),
            event_type=str(event["event_type"]),
            error=str(exc),
        ).to_dict()

    return _outbox.drain_outbox(
        outbox=outbox,
        command="hpc-sync",
        dry_run=dry_run,
        request_draft=request_draft,
        limit=limit,
        read_event=read_event,
        is_actionable=_is_actionable,
        process=_process,
        on_skipped=_skipped,
        on_failure=_failed,
    )


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
    run_id, job_manifest = _job_run(run_id)
    outbox = _event_outbox(config, job_manifest)
    worktree = _job_worktree_source(
        run_id or os.getenv("LAB_TRACKER_HPC_RUN_ID"),
        cwd=Path.cwd(),
        outbox=outbox,
        job_manifest=job_manifest,
    )
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
        source=worktree,
    )
    path = write_event(event, outbox)
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
    event_id: str | None = None,
    cwd: str | Path | None = None,
    outbox: Path | None = None,
    scheduler_extra: Mapping[str, Any] | None = None,
) -> tuple[JsonObject, Path]:
    """Write a ``finish`` event; ``lt hpc finish`` and ``lt hpc epilog`` both land here.

    The run id is ``run_id``, else ``LAB_TRACKER_HPC_RUN_ID``, else the
    ``manifest``'s, else the run the job's submit manifest names (see
    :func:`find_submit_manifest`), so a job started with ``--export=NONE``
    still finishes the run it was submitted as.
    """

    manifest_payload: JsonObject = {}
    if manifest is not None:
        manifest_path = Path(manifest).expanduser().resolve()
        manifest_payload = event_from_manifest(config, manifest_path)
    run_id, job_manifest = _job_run(run_id or _optional_str(manifest_payload.get("run_id")))
    scheduler = _json_mapping(manifest_payload.get("scheduler") or {})
    scheduler.update(
        {
            "job_id": os.getenv("SLURM_JOB_ID") or scheduler.get("job_id"),
            "array_task_id": os.getenv("SLURM_ARRAY_TASK_ID") or scheduler.get("array_task_id"),
            "state": state or scheduler.get("state") or _state_from_exit_code(exit_code),
            "exit_code": exit_code if exit_code is not None else scheduler.get("exit_code"),
        }
    )
    scheduler.update({str(key): value for key, value in (scheduler_extra or {}).items() if value})
    merged_artifacts = list(manifest_payload.get("artifacts") or [])
    merged_artifacts.extend(_artifact_from_uri(uri) for uri in artifacts or [])
    cwd = cwd or _optional_str(manifest_payload.get("cwd")) or Path.cwd()
    target_outbox = outbox or _event_outbox(config, job_manifest)
    manifest_source = _json_mapping(manifest_payload.get("source") or {})
    worktree: JsonObject = {}
    if not manifest_source.get("git_worktree_tree"):
        worktree = _job_worktree_source(
            run_id or os.getenv("LAB_TRACKER_HPC_RUN_ID"),
            cwd=cwd,
            outbox=target_outbox,
            job_manifest=job_manifest,
            logs=list(logs or []),
        )
    event = make_event(
        config,
        event_type="finish",
        run_id=run_id,
        event_id=event_id or _optional_str(manifest_payload.get("event_id")),
        observed_at=_optional_str(manifest_payload.get("observed_at")),
        project_id=project_id or _optional_str(manifest_payload.get("project_id")),
        question_id=question_id or _optional_str(manifest_payload.get("question_id")),
        dataset_ids=dataset_ids or _string_list(manifest_payload.get("dataset_ids")),
        tags=tags or _string_list(manifest_payload.get("tags")),
        command=_string_list(manifest_payload.get("command")),
        cwd=cwd,
        scheduler=scheduler,
        # A manifest that recorded its own worktree tree keeps it; otherwise
        # the tree submitted with the job, else one without the job's output.
        source={**worktree, **manifest_source},
        artifacts=merged_artifacts,
        metrics={**_json_mapping(manifest_payload.get("metrics") or {}), **_metrics(metrics)},
        log_excerpt=_join_log_excerpt(
            _optional_str(manifest_payload.get("log_excerpt")) or "",
            _read_log_excerpt(logs or []),
        ),
        summary=summary or _optional_str(manifest_payload.get("summary")),
    )
    path = write_event(event, target_outbox)
    return event, path


def epilog_finish(
    *,
    exit_code: int | None = None,
    logs: Sequence[str | Path] | None = None,
    config_path: str | Path | None = None,
) -> JsonObject:
    """Finish the current Slurm job's ``lt hpc submit`` run from an epilog.

    Reads only Slurm's own environment (``SLURM_JOB_ID``, ``SLURM_SUBMIT_DIR``,
    array ids, and ``SLURM_JOB_EXIT_CODE``/``SLURM_JOB_EXIT_CODE2`` where the
    epilog type provides them) plus the job's submit manifest, then writes the
    same ``finish`` event ``lt hpc finish`` would, with the submit's declared
    question, datasets and tags. Idempotent: a run (or array task) that already
    has a finish event -- because the job called ``lt hpc finish`` itself, or
    an earlier task epilog ran -- is left alone. Without a job, a manifest, or
    with ``LAB_TRACKER_HPC_EPILOG_ENABLED=0`` it writes nothing.
    """

    base: JsonObject = {"command": "hpc-epilog"}
    if os.getenv(EPILOG_ENABLED_ENV, "").strip().lower() in _OFF_VALUES:
        return {**base, "action": "disabled"}
    job_id = _optional_str(os.getenv("SLURM_JOB_ID"))
    if job_id is None:
        return {**base, "action": "not_in_job"}
    job_manifest = find_submit_manifest()
    if job_manifest is None:
        return {**base, "action": "no_run", "job_id": job_id}
    config = load_config(config_path=config_path or _optional_str(job_manifest.get("config")))
    run_id = str(job_manifest["run_id"])
    array_task_id = _optional_str(os.getenv("SLURM_ARRAY_TASK_ID"))
    outbox = _event_outbox(config, job_manifest)
    result: JsonObject = {**base, "run_id": run_id, "job_id": job_id, "outbox": str(outbox)}
    if array_task_id:
        result["array_task_id"] = array_task_id
    existing = find_finish_event(outbox, run_id=run_id, array_task_id=array_task_id)
    if existing is not None:
        return {**result, "action": "already_finished", "event_path": str(existing)}
    resolved_exit = exit_code if exit_code is not None else slurm_exit_code(os.environ)
    submit_dir = Path(str(job_manifest.get("submit_dir") or os.getenv("SLURM_SUBMIT_DIR")))
    event, path = finish_event(
        config,
        run_id=run_id,
        exit_code=resolved_exit,
        state=None if resolved_exit is not None else "ended",
        logs=list(logs or []) or _default_job_logs(submit_dir, job_id, array_task_id),
        project_id=_optional_str(job_manifest.get("project_id")),
        question_id=_optional_str(job_manifest.get("question_id")),
        dataset_ids=_string_list(job_manifest.get("dataset_ids")),
        tags=_string_list(job_manifest.get("tags")),
        summary=_epilog_summary(job_id, array_task_id, resolved_exit),
        event_id="epilog-" + _epilog_digest(run_id, job_id, array_task_id),
        cwd=submit_dir,
        outbox=outbox,
        scheduler_extra={
            "finish_source": "epilog",
            "epilog_context": _optional_str(os.getenv("SLURM_SCRIPT_CONTEXT")),
        },
    )
    return {
        **result,
        "action": "finished",
        "exit_code": resolved_exit,
        "state": event["scheduler"].get("state"),
        "event_path": str(path),
    }


def find_finish_event(outbox: str | Path, *, run_id: str, array_task_id: str | None) -> Path | None:
    """An existing ``finish`` event for ``run_id`` (and array task), or ``None``."""

    prefix = f"{_safe_path_part(run_id)}.finish."
    for path in sorted(Path(outbox).expanduser().glob(f"{prefix}*.json")):
        try:
            event = read_event(path)
        except Exception:  # noqa: BLE001 - an unreadable event is not a finish record.
            continue
        if event["run_id"] != run_id:
            continue
        if array_task_id is None or str(event["scheduler"].get("array_task_id")) == array_task_id:
            return path
    return None


def slurm_exit_code(environ: Mapping[str, str]) -> int | None:
    """The job's exit code from ``SLURM_JOB_EXIT_CODE2``/``SLURM_JOB_EXIT_CODE``.

    ``SLURM_JOB_EXIT_CODE2`` is ``<exit>:<signal>``; ``SLURM_JOB_EXIT_CODE`` is
    a ``wait(2)`` status. A signal maps to ``128 + signal`` like a shell does.
    Slurm sets these only for its privileged epilogs (EpilogSlurmctld and, on
    recent releases, the node Epilog), never for a TaskEpilog: ``None`` then.
    """

    code2 = (environ.get("SLURM_JOB_EXIT_CODE2") or "").strip()
    if code2:
        exit_text, _separator, signal_text = code2.partition(":")
        try:
            code = int(exit_text or 0)
            signal = int(signal_text or 0)
        except ValueError:
            pass
        else:
            return 128 + signal if signal else code
    raw = (environ.get("SLURM_JOB_EXIT_CODE") or "").strip()
    if raw:
        try:
            status = int(raw)
        except ValueError:
            return None
        signal = status & 0x7F
        return 128 + signal if signal else (status >> 8) & 0xFF
    return None


def _job_run(run_id: str | None) -> tuple[str | None, JsonObject | None]:
    """``run_id`` as given or from the environment, else the job's submit manifest."""

    if run_id or os.getenv("LAB_TRACKER_HPC_RUN_ID"):
        return run_id, None
    job_manifest = find_submit_manifest()
    if job_manifest is None:
        return None, None
    return str(job_manifest["run_id"]), job_manifest


def _event_outbox(config: HpcConfig, job_manifest: Mapping[str, Any] | None) -> Path:
    """The submit's outbox for a run found through its manifest, else the config's.

    ``lt hpc submit`` resolved the outbox with the submitting shell's
    ``LAB_TRACKER_HPC_OUTBOX``, which a ``--export=NONE`` job does not see.
    """

    recorded = _optional_str(job_manifest.get("outbox")) if job_manifest else None
    if recorded is None or os.getenv("LAB_TRACKER_HPC_OUTBOX"):
        return config.outbox_path()
    manifest_config = _optional_str(job_manifest.get("config")) if job_manifest else None
    if (
        manifest_config
        and config.config_path is not None
        and Path(manifest_config).expanduser().resolve() != config.config_path.resolve()
    ):
        return config.outbox_path()
    return Path(recorded).expanduser()


def _default_job_logs(submit_dir: Path, job_id: str, array_task_id: str | None) -> list[Path]:
    """Slurm's default output file for the job, when it exists in the submit directory."""

    array_job_id = _optional_str(os.getenv("SLURM_ARRAY_JOB_ID"))
    name = (
        f"slurm-{array_job_id}_{array_task_id}.out"
        if array_job_id and array_task_id
        else f"slurm-{job_id}.out"
    )
    candidate = submit_dir / name
    return [candidate] if candidate.is_file() else []


def _epilog_summary(job_id: str, array_task_id: str | None, exit_code: int | None) -> str:
    task = f" array task {array_task_id}" if array_task_id else ""
    outcome = (
        f"exited with code {exit_code}"
        if exit_code is not None
        else "ended (a TaskEpilog does not receive the job's exit code)"
    )
    return f"Slurm job {job_id}{task} {outcome}; finish recorded by lt hpc epilog."


def _epilog_digest(run_id: str, job_id: str, array_task_id: str | None) -> str:
    seed = "\0".join((run_id, job_id, array_task_id or ""))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def git_context(cwd: str | Path | None = None) -> JsonObject:
    root = Path(cwd or Path.cwd()).expanduser()
    head = git_head_commit(root)
    return {
        **head_commit_fields(head),
        **dirty_state_fields(git_dirty_state(root, head=head)),
    }


def worktree_source(cwd: str | Path, *, exclude: Sequence[str | Path] = ()) -> JsonObject:
    """Event ``source`` keys naming a working-copy tree (``exclude`` left out).

    ``git_worktree_tree`` identifies the exact code a job ran even when it was
    never committed; ``git_worktree_tree_error`` says why it is unknown.
    Outside a checkout there is nothing to record.
    """

    tree = worktree_tree_id(Path(cwd).expanduser(), exclude=exclude)
    return dict(tree.as_fields("git_worktree_tree"))


def job_output_files(*directories: str | Path, logs: Sequence[str | Path] = ()) -> list[Path]:
    """A job's own output: ``logs`` plus Slurm's ``slurm-*.out`` in ``directories``.

    These grow while the job runs, so they are never part of the code a
    worktree tree identifies.
    """

    found: list[Path] = [Path(item).expanduser() for item in logs]
    for directory in directories:
        with suppress(OSError):
            found.extend(sorted(Path(directory).expanduser().glob("slurm-*.out"))[:_MAX_JOB_LOGS])
    return found


def submitted_worktree_source(
    run_id: str | None,
    *,
    outbox: Path,
    job_manifest: Mapping[str, Any] | None = None,
) -> JsonObject:
    """The worktree tree ``lt hpc submit`` recorded for ``run_id`` before sbatch, or ``{}``.

    The job's code is the code that was submitted: the tree is taken from the
    job's submit manifest (``job_manifest`` or the one Slurm's environment
    names), else from the run's ``submit`` event in ``outbox``. Only a tree is
    reused; a submit that could not compute one leaves the job to try.
    """

    if not run_id:
        return {}
    manifest = job_manifest if job_manifest is not None else find_submit_manifest()
    if manifest and str(manifest.get("run_id") or "") == run_id:
        tree = _optional_str(manifest.get("git_worktree_tree"))
        if tree:
            return {"git_worktree_tree": tree}
    with suppress(OSError):
        for path in sorted(Path(outbox).glob(f"{_safe_path_part(run_id)}.submit.*.json")):
            try:
                event = read_event(path)
            except Exception:  # noqa: BLE001 - an unreadable event names no tree.
                continue
            tree = _optional_str(event["source"].get("git_worktree_tree"))
            if event["run_id"] == run_id and tree:
                return {"git_worktree_tree": tree}
    return {}


def _job_worktree_source(
    run_id: str | None,
    *,
    cwd: str | Path,
    outbox: Path,
    job_manifest: Mapping[str, Any] | None,
    logs: Sequence[str | Path] = (),
) -> JsonObject:
    """The submitted tree for a begin/finish event, else one computed without job output."""

    submitted = submitted_worktree_source(run_id, outbox=outbox, job_manifest=job_manifest)
    if submitted:
        return submitted
    directories = [cwd]
    submit_dir = _optional_str(os.getenv("SLURM_SUBMIT_DIR"))
    if submit_dir:
        directories.append(submit_dir)
    return worktree_source(cwd, exclude=job_output_files(*directories, logs=logs))


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
        index = outbox_note_index(
            client,
            note_indexes,
            project_id=project_id,
            outbox=path.parent,
            dry_run=dry_run,
        )
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
                targets=declared_targets(
                    question_id=_optional_str(event.get("question_id")),
                    dataset_ids=event["dataset_ids"],
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
        lines.append(f"- Git dirty: {dirty_label(source)}")
    if source.get("git_worktree_tree"):
        lines.append(f"- Git worktree tree: `{source['git_worktree_tree']}`")
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
    declared_target_source = declared_target_source_for(
        question_id=payload.get("question_id"),
        question_id_source=payload.get("question_id_source"),
        dataset_ids=payload["dataset_ids"],
    )
    if declared_target_source:
        metadata[DECLARED_TARGET_SOURCE_KEY] = declared_target_source
    if payload["tags"]:
        metadata["hpc_tags"] = ",".join(payload["tags"])
    for key in ("job_id", "array_task_id", "state", "exit_code"):
        if scheduler.get(key) is not None:
            metadata[f"hpc_{key}"] = scheduler[key]
    if source.get("git_commit"):
        metadata["hpc_git_commit"] = str(source["git_commit"])
        metadata.update(dirty_metadata(source, "hpc_"))
    elif source.get("git_commit_error"):
        metadata["hpc_git_commit_error"] = str(source["git_commit_error"])
    if source.get("git_worktree_tree"):
        metadata["hpc_git_worktree_tree"] = str(source["git_worktree_tree"])
    elif source.get("git_worktree_tree_error"):
        metadata["hpc_git_worktree_tree_error"] = str(source["git_worktree_tree_error"])
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
    _outbox.write_json_atomic(path, payload)


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
            text = _read_text_tail(path, max_chars=remaining)
        except OSError:
            continue
        excerpt = text[-remaining:]
        chunks.append(f"==> {path} <==\n{excerpt.strip()}")
        remaining -= len(excerpt)
    # Job logs routinely echo tokens and connection strings; never store them.
    return redact_capture_text("\n\n".join(chunks))


def _read_text_tail(path: Path, *, max_chars: int) -> str:
    """Decode at most the last ``max_chars`` characters of a UTF-8 text file.

    Scheduler logs can be gigabytes, and ``lt hpc finish`` runs inside a
    memory-limited job epilogue, so only the final ``4 * max_chars`` bytes (the
    UTF-8 worst case per character) are read. A multi-byte character split by
    the seek point is dropped rather than decoded as a replacement character.
    """

    max_bytes = _UTF8_MAX_BYTES_PER_CHAR * max_chars
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        handle.seek(start)
        data = handle.read(size - start)
    if start > 0:
        skip = 0
        while skip < min(len(data), _UTF8_MAX_BYTES_PER_CHAR - 1) and (data[skip] & 0xC0 == 0x80):
            skip += 1
        data = data[skip:]
    return data.decode("utf-8", errors="replace")[-max_chars:]


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
