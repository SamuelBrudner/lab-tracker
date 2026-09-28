"""Record one pipeline run as a staged Lab Tracker note (``lt pipeline report``).

A pipeline framework (Snakemake, Nextflow, Kedro, DVC) already knows which
inputs and outputs a run *declares*; this module turns that declaration into a
single durable watch-outbox event per run, which syncs into a staged evidence
note behind the normal human review gate. It is the framework-hook
integration shape endorsed in ``docs/build-vs-buy-boundaries.md``:

* only declared inputs and outputs are recorded, as pointers (URI, sha256 for
  files under a size cap, size, directory summaries); no catalog, no runner,
  no read interception, no bytes copied;
* the run's git HEAD, dirty flag and credential-free remote, and a bounded,
  credential-scrubbed log excerpt, give a reviewer the context;
* nothing is committed or linked beyond the question, datasets and session the
  person declared.

Capture is fail-soft: :func:`capture_pipeline_run` never raises into the
pipeline that calls it, and the CLI's ``--fail-silent`` never changes a
pipeline's exit status. A run whose project is not bound (explicit
``--project``, ``LAB_TRACKER_PROJECT_ID``, or the checkout's ``lt_ids.json`` /
``.lab-tracker`` configs) is skipped with one stderr notice, so it can never
land in a default project it was not meant for. Nothing touches the network
unless a server is configured.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from lab_tracker.file_watch import stable_file_fingerprint
from lab_tracker_client.redaction import redact_capture_text

JsonObject = dict[str, Any]
MetadataScalar = str | int | float | bool

ENGINES = ("snakemake", "nextflow", "kedro", "dvc", "generic")
STATUSES = ("success", "error", "unknown")
ENGINE_LABELS = {
    "snakemake": "Snakemake",
    "nextflow": "Nextflow",
    "kedro": "Kedro",
    "dvc": "DVC",
    "generic": "Pipeline",
}
PIPELINE_ADAPTER = "lt-pipeline"
PIPELINE_CAPTURE_KIND = "pipeline_run"
PIPELINE_SOURCE_PROVIDER = "pipeline"
METADATA_PREFIX = "pipeline_"
# A file above this size is recorded as a pointer (URI + size) without a hash.
DEFAULT_HASH_MAX_BYTES = 64 * 1024 * 1024
# Total bytes one run may hash, so a run declaring many large files stays fast.
HASH_BUDGET_BYTES = 512 * 1024 * 1024
# Declared inputs (and, separately, outputs) listed per run; the rest are counted.
DEFAULT_MAX_ARTIFACTS = 25
# Directory entries walked to summarize one declared directory.
DIRECTORY_WALK_LIMIT = 10_000
# Characters of log text (tail) and error text kept per run.
LOG_EXCERPT_MAX_CHARS = 4000
ERROR_TEXT_MAX_CHARS = 1000
# Hard cap on the rendered note body.
BODY_MAX_CHARS = 60_000
# Kill switch: 0/false/no/off disables every pipeline capture path.
CAPTURE_ENV = "LAB_TRACKER_PIPELINE_CAPTURE"
# The post-write drain is best-effort and bounded.
DRAIN_EVENT_LIMIT = 25
DRAIN_TIMEOUT_SECONDS = 10.0
_OFF_VALUES = frozenset({"0", "false", "no", "off"})
_URI_SCHEME = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://")
_NOTICES_SHOWN: set[str] = set()


@dataclass
class PipelineRun:
    """What one pipeline run declares; built by the CLI or an engine adapter.

    ``inputs``/``outputs`` items are paths or URIs to fingerprint, or ready-made
    pointer mappings (``{"uri", "title", "content_hash", "size_bytes", ...}``)
    for engines that already hash their files (DVC's md5). ``details`` are extra
    markdown lines for the note body; ``metadata`` are engine-specific scalars
    (keys are namespaced ``pipeline_*``).
    """

    engine: str = "generic"
    status: str = "unknown"
    run_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    inputs: Sequence[str | Mapping[str, Any]] = ()
    outputs: Sequence[str | Mapping[str, Any]] = ()
    logs: Sequence[str | Path] = ()
    error_text: str | None = None
    label: str | None = None
    summary: str | None = None
    details: Sequence[str] = ()
    metadata: Mapping[str, MetadataScalar] = field(default_factory=dict)
    tags: Sequence[str] = ()


@dataclass
class _HashBudget:
    remaining: int


def capture_enabled() -> bool:
    """False when ``LAB_TRACKER_PIPELINE_CAPTURE`` turns pipeline capture off."""

    return os.getenv(CAPTURE_ENV, "").strip().lower() not in _OFF_VALUES


def expand_path_args(values: Iterable[str], *, base: Path | None = None) -> list[str]:
    """Expand ``@file`` entries (one path per line, ``#`` comments) into paths.

    Other values pass through. A relative ``@file`` resolves against ``base``
    (default: the current directory); the paths inside it are returned as
    written and resolved later against the run's working directory.
    """

    expanded: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        if not text.startswith("@"):
            expanded.append(text)
            continue
        list_path = Path(text[1:]).expanduser()
        if not list_path.is_absolute():
            list_path = (base or Path.cwd()) / list_path
        for line in list_path.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if entry and not entry.startswith("#"):
                expanded.append(entry)
    return expanded


def artifact_pointer(
    item: str | Mapping[str, Any],
    *,
    role: str,
    base: Path,
    root: Path,
    hash_max_bytes: int = DEFAULT_HASH_MAX_BYTES,
    budget: _HashBudget | None = None,
) -> JsonObject:
    """Pointer for one declared input or output: URI, hash when cheap, never bytes.

    ``item`` is a path or URI, a ``{"path": ..., "title": ...}`` mapping (a path
    to fingerprint under a caller-chosen name, e.g. a Kedro dataset name), or a
    prebuilt pointer mapping with a ``uri`` (e.g. DVC entries with their md5).
    """

    if isinstance(item, Mapping):
        if item.get("path") and not item.get("uri"):
            named = artifact_pointer(
                str(item["path"]),
                role=role,
                base=base,
                root=root,
                hash_max_bytes=hash_max_bytes,
                budget=budget,
            )
            name = _bounded(str(item.get("title") or ""), 200)
            if name:
                named["title"] = f"{name} ({named['title']})"
            return named
        return _prebuilt_pointer(item, role=role)
    title, uri, path = pointer_location(str(item), base=base, root=root)
    if path is None:
        return {
            "role": role,
            "title": title,
            "kind": "remote",
            "uri": uri,
            "summary": "Remote pointer; not dereferenced or hashed.",
        }
    pointer: JsonObject = {"role": role, "title": title, "kind": "file", "uri": uri}
    try:
        if path.is_dir():
            return {**pointer, "kind": "directory", **_directory_summary(path)}
        size = path.stat().st_size
    except FileNotFoundError:
        return {**pointer, "summary": "Missing when the run was recorded."}
    except OSError as exc:
        return {**pointer, "summary": f"Not readable when the run was recorded ({exc.strerror})."}
    pointer["size_bytes"] = size
    if size > hash_max_bytes:
        pointer["summary"] = (
            f"Pointer only: {_format_bytes(size)} is above the "
            f"{_format_bytes(hash_max_bytes)} hashing cap."
        )
        return pointer
    if budget is not None and size > budget.remaining:
        pointer["summary"] = (
            f"Pointer only: the run's {_format_bytes(HASH_BUDGET_BYTES)} hashing budget is used up."
        )
        return pointer
    fingerprint = stable_file_fingerprint(path)
    if fingerprint is None:
        pointer["summary"] = "Pointer only: the file changed or vanished while it was hashed."
        return pointer
    if budget is not None:
        budget.remaining -= fingerprint.size_bytes
    pointer.update(
        {
            "size_bytes": fingerprint.size_bytes,
            "content_hash": f"sha256:{fingerprint.checksum}",
            "summary": "",
        }
    )
    return pointer


def pointer_location(text: str, *, base: Path, root: Path) -> tuple[str, str, Path | None]:
    """``(title, uri, local path)`` for a declared path or URI; no filesystem access.

    A non-``file://`` URI is a remote pointer (credentials stripped, local path
    ``None``); anything else is a local path resolved against ``base`` and
    titled relative to ``root`` when it lies inside it.
    """

    cleaned = text.strip()
    match = _URI_SCHEME.match(cleaned)
    if match and match.group("scheme").lower() != "file":
        from lab_tracker_client.gitinfo import sanitize_remote_url

        uri = sanitize_remote_url(cleaned)
        return uri, uri, None
    path = _local_path(cleaned, base=base)
    return _display_path(path, root), path.as_uri(), path


def artifact_pointers(
    items: Sequence[str | Mapping[str, Any]],
    *,
    role: str,
    base: Path,
    root: Path,
    hash_max_bytes: int = DEFAULT_HASH_MAX_BYTES,
    max_artifacts: int = DEFAULT_MAX_ARTIFACTS,
    budget: _HashBudget | None = None,
) -> tuple[list[JsonObject], int]:
    """Pointers for the first ``max_artifacts`` distinct items, plus the omitted count."""

    distinct: list[str | Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, sort_keys=True, default=str) if isinstance(item, Mapping) else item
        if key in seen:
            continue
        seen.add(key)
        distinct.append(item)
    listed = distinct[: max(0, max_artifacts)]
    pointers = [
        artifact_pointer(
            item,
            role=role,
            base=base,
            root=root,
            hash_max_bytes=hash_max_bytes,
            budget=budget,
        )
        for item in listed
    ]
    return pointers, len(distinct) - len(listed)


def report_pipeline_run(
    run: PipelineRun,
    *,
    cwd: str | Path | None = None,
    project_id: str | None = None,
    session: str | None = None,
    question_id: str | None = None,
    drain: bool = True,
    request_draft: bool = False,
    hash_max_bytes: int | None = None,
    max_artifacts: int | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> JsonObject:
    """Write the run's staged-note event to the checkout's watch outbox; drain it.

    Returns a JSON-able payload whose ``action`` is ``captured``,
    ``already_captured`` (the same engine and run id were recorded before),
    ``skipped`` (no bound project) or ``disabled`` (kill switch). Raises
    ``LTValidationError``/``OSError`` for an invalid run or an unwritable
    outbox; :func:`capture_pipeline_run` is the never-raising wrapper.
    """

    from lab_tracker_client import watch

    engine = _choice(run.engine, ENGINES, "engine")
    status = _choice(run.status, STATUSES, "status")
    base: JsonObject = {"command": "pipeline-report", "engine": engine, "status": status}
    if not capture_enabled():
        return {**base, "action": "disabled", "reason": f"{CAPTURE_ENV} is off"}
    workdir = Path(cwd or Path.cwd()).expanduser().resolve()
    checkout, in_git = _checkout_root(workdir)
    bound = resolve_pipeline_project(checkout, project_id)
    if bound is None:
        _notice_once(
            f"unbound:{checkout}",
            f"lab-tracker: pipeline run not recorded: no project is bound for {checkout} "
            "(pass --project, set LAB_TRACKER_PROJECT_ID, or run 'lt project bind').",
        )
        return {**base, "action": "skipped", "reason": "project_unbound", "checkout": str(checkout)}
    resolved_project, project_source = bound
    git_facts = _git_facts(checkout) if in_git else {}
    remote = str(git_facts.get("repo_remote_url") or "")
    run_id = _clean_run_id(run.run_id) or _new_run_id(engine)
    session_id, session_fields = _session(session, checkout)
    inputs, outputs, counts = _declared_pointers(
        run,
        workdir=workdir,
        checkout=checkout,
        hash_max_bytes=max(1, int(hash_max_bytes or DEFAULT_HASH_MAX_BYTES)),
        max_artifacts=max(
            0, int(max_artifacts if max_artifacts is not None else DEFAULT_MAX_ARTIFACTS)
        ),
    )
    log_excerpt = _log_excerpt(run.logs, run.error_text, base=workdir)
    started_at = _timestamp(run.started_at)
    ended_at = _timestamp(run.ended_at)
    label = _bounded(run.label, 200)
    title = _title(engine, status, run_id, label)
    summary = _bounded(run.summary, 2000) or _default_summary(engine, status, run_id, counts)
    payload: JsonObject = {
        "title": title,
        "summary": summary,
        "status": "staged",
        "body": render_run_note(
            title=title,
            summary=summary,
            engine=engine,
            status=status,
            run_id=run_id,
            label=label,
            started_at=started_at,
            ended_at=ended_at,
            workdir=workdir,
            git_facts=git_facts,
            inputs=inputs,
            outputs=outputs,
            counts=counts,
            details=[redact_capture_text(str(line)) for line in run.details],
            log_excerpt=log_excerpt,
        ),
        "metadata": _event_metadata(
            run,
            engine=engine,
            status=status,
            run_id=run_id,
            label=label,
            started_at=started_at,
            ended_at=ended_at,
            counts=counts,
            remote=remote,
            project_source=project_source,
        ),
    }
    if request_draft:
        payload["request_draft"] = True
    identity = _checkout_identity(remote, checkout)
    event = watch.make_event(
        capture_id=f"pipeline-{engine}-{run_id}",
        # One event per (engine, checkout, run id): a repeated report is a no-op.
        event_id="run-" + _digest(engine, identity, run_id)[:16],
        capture_kind=PIPELINE_CAPTURE_KIND,
        adapter=PIPELINE_ADAPTER,
        sink=watch.SINK_STAGED_NOTE,
        source={
            "provider": PIPELINE_SOURCE_PROVIDER,
            "uri": checkout.as_uri(),
            "external_id": f"pipeline:{engine}:{identity}:{run_id}",
            "engine": engine,
            "checkout": str(checkout),
            "cwd": str(workdir),
            **git_facts,
            **session_fields,
        },
        context={
            "project_id": resolved_project,
            "question_id": _optional(question_id),
            "dataset_ids": [],
            "tags": [str(tag) for tag in run.tags if str(tag).strip()],
            "session_id": session_id,
        },
        artifacts=[*inputs, *outputs],
        log_excerpt=log_excerpt,
        payload=payload,
    )
    result: JsonObject = {
        **base,
        "run_id": run_id,
        "project_id": resolved_project,
        "project_source": project_source,
        "input_count": counts["inputs"],
        "output_count": counts["outputs"],
        **_write_event(event, checkout),
    }
    if not drain:
        result["sync"] = "skipped"
        return result
    result.update(_drain_after_write(Path(result["event_path"]), run_id, client_factory))
    return result


def _declared_pointers(
    run: PipelineRun,
    *,
    workdir: Path,
    checkout: Path,
    hash_max_bytes: int,
    max_artifacts: int,
) -> tuple[list[JsonObject], list[JsonObject], dict[str, int]]:
    budget = _HashBudget(HASH_BUDGET_BYTES)
    options: dict[str, Any] = {
        "base": workdir,
        "root": checkout,
        "hash_max_bytes": hash_max_bytes,
        "max_artifacts": max_artifacts,
        "budget": budget,
    }
    # Outputs first: they are what later runs consume, so they get the budget.
    outputs, outputs_omitted = artifact_pointers(run.outputs, role="output", **options)
    inputs, inputs_omitted = artifact_pointers(run.inputs, role="input", **options)
    counts = {
        "inputs": len(inputs) + inputs_omitted,
        "outputs": len(outputs) + outputs_omitted,
        "inputs_omitted": inputs_omitted,
        "outputs_omitted": outputs_omitted,
    }
    return inputs, outputs, counts


def _write_event(event: JsonObject, checkout: Path) -> JsonObject:
    from lab_tracker_client import watch
    from lab_tracker_client.client import LTValidationError

    config, config_error = _watch_config(checkout)
    outbox = config.outbox_path()
    target = watch.event_path(event, outbox)
    already = target.exists()
    if not already:
        try:
            target = watch.write_event(event, outbox)
        except OSError as exc:
            raise LTValidationError(
                f"could not write the pipeline event to {outbox}: {exc}"
            ) from exc
    written: JsonObject = {
        "action": "already_captured" if already else "captured",
        "event_path": str(target),
        "outbox": str(outbox),
    }
    if config_error:
        written["config_error"] = config_error
    return written


def _drain_after_write(
    target: Path, run_id: str, client_factory: Callable[[], Any] | None
) -> JsonObject:
    outbox = target.parent
    sync, sync_error = _drain(outbox, client_factory)
    drained: JsonObject = {"sync": sync}
    if sync_error or (isinstance(sync, dict) and _event_sync_status(target) != "synced"):
        cause = sync_error or _event_sync_error(target) or "not synced"
        drained["sync_error"] = cause
        _notice_once(
            f"sync:{outbox}",
            f"lab-tracker: pipeline run {run_id} is queued in {outbox} ({cause}); "
            "'lt outbox sync' retries it.",
        )
    return drained


def capture_pipeline_run(run: PipelineRun, **kwargs: Any) -> JsonObject:
    """:func:`report_pipeline_run` that never raises; prints one notice per cause."""

    try:
        return report_pipeline_run(run, **kwargs)
    except Exception as exc:  # noqa: BLE001 - capture must never break a pipeline.
        message = redact_capture_text(str(exc)) or exc.__class__.__name__
        _notice_once(
            f"error:{message}",
            f"lab-tracker: pipeline run not recorded ({message}).",
        )
        return {"command": "pipeline-report", "action": "failed", "error": message}


def resolve_pipeline_project(checkout: Path, project_id: str | None) -> tuple[str, str] | None:
    """The bound project for a run in ``checkout`` and where it came from.

    Explicit argument, then ``LAB_TRACKER_PROJECT_ID``, then the checkout's own
    ``lt_ids.json``, then the project of its ``.lab-tracker`` watch, repo or HPC
    config. The client's or login profile's default project is never used.
    """

    from lab_tracker_client import git_capture

    explicit = _optional(project_id)
    if explicit:
        return explicit, "explicit"
    from_env = _optional(os.getenv("LAB_TRACKER_PROJECT_ID"))
    if from_env:
        return from_env, "environment"
    bound = git_capture.project_from_ids(checkout)
    if bound:
        return bound, "checkout"
    config, _error = _watch_config(checkout)
    if _optional(config.project_id):
        return str(config.project_id).strip(), "watch_config"
    for name in ("repo", "hpc"):
        configured = _config_project(checkout / ".lab-tracker" / f"{name}.json")
        if configured:
            return configured, f"{name}_config"
    return None


def render_run_note(
    *,
    title: str,
    summary: str,
    engine: str,
    status: str,
    run_id: str,
    label: str | None,
    started_at: str | None,
    ended_at: str | None,
    workdir: Path,
    git_facts: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
    outputs: Sequence[Mapping[str, Any]],
    counts: Mapping[str, int],
    details: Sequence[str],
    log_excerpt: str,
) -> str:
    """Markdown body of the staged note (bounded to ``BODY_MAX_CHARS``)."""

    from lab_tracker_client.gitinfo import dirty_label

    lines = [f"# {title}", "", summary, "", "## Run"]
    lines.append(f"- Engine: {ENGINE_LABELS[engine]}")
    lines.append(f"- Status: {status}")
    lines.append(f"- Run ID: `{run_id}`")
    if label:
        lines.append(f"- Label: {label}")
    if started_at:
        lines.append(f"- Started: {started_at}")
    if ended_at:
        lines.append(f"- Ended: {ended_at}")
    lines.append(f"- Working directory: `{workdir}`")
    if git_facts.get("git_commit"):
        lines.append(f"- Git commit: `{git_facts['git_commit']}`")
        lines.append(f"- Git dirty: {dirty_label(git_facts)}")
    elif git_facts.get("git_commit_error"):
        lines.append(f"- Git commit: unknown ({git_facts['git_commit_error']})")
    if git_facts.get("git_branch"):
        lines.append(f"- Git branch: {git_facts['git_branch']}")
    if git_facts.get("repo_remote_url"):
        lines.append(f"- Git remote: {git_facts['repo_remote_url']}")
    for heading, pointers, total, omitted in (
        ("Declared inputs", inputs, counts["inputs"], counts["inputs_omitted"]),
        ("Declared outputs", outputs, counts["outputs"], counts["outputs_omitted"]),
    ):
        lines.extend(["", f"## {heading} ({total})"])
        if not total:
            lines.append("- none declared")
        lines.extend(_pointer_line(pointer) for pointer in pointers)
        if omitted:
            noun = heading.split()[-1]
            lines.append(f"- … and {omitted} more {noun} (not listed; the cap is {len(pointers)})")
    if details:
        lines.extend(["", f"## {ENGINE_LABELS[engine]} details", *details])
    if log_excerpt:
        lines.extend(["", "## Log excerpt", "```text", log_excerpt.strip(), "```"])
    body = "\n".join(lines).strip() + "\n"
    if len(body) > BODY_MAX_CHARS:
        cut = body.rfind("\n", 0, BODY_MAX_CHARS - 200)
        body = body[: max(cut, 0)] + "\n\n… (truncated at the note size cap)\n"
    return body


def _pointer_line(pointer: Mapping[str, Any]) -> str:
    parts = [str(pointer.get("kind") or "file")]
    if pointer.get("content_hash"):
        parts.append(str(pointer["content_hash"]))
    if pointer.get("size_bytes") is not None:
        parts.append(_format_bytes(int(pointer["size_bytes"])))
    if pointer.get("summary"):
        parts.append(str(pointer["summary"]))
    return f"- `{pointer.get('title') or pointer.get('uri')}` — {'; '.join(parts)}"


def _event_metadata(
    run: PipelineRun,
    *,
    engine: str,
    status: str,
    run_id: str,
    label: str | None,
    started_at: str | None,
    ended_at: str | None,
    counts: Mapping[str, int],
    remote: str,
    project_source: str,
) -> dict[str, MetadataScalar]:
    metadata: dict[str, MetadataScalar] = {
        "pipeline_engine": engine,
        "pipeline_status": status,
        "pipeline_run_id": run_id,
        "pipeline_input_count": counts["inputs"],
        "pipeline_output_count": counts["outputs"],
        "pipeline_project_source": project_source,
    }
    optional: dict[str, MetadataScalar | None] = {
        "pipeline_label": label,
        "pipeline_started_at": started_at,
        "pipeline_ended_at": ended_at,
        "pipeline_git_remote": remote or None,
        "pipeline_inputs_omitted": counts["inputs_omitted"] or None,
        "pipeline_outputs_omitted": counts["outputs_omitted"] or None,
    }
    metadata.update({key: value for key, value in optional.items() if value is not None})
    for key, value in run.metadata.items():
        if not isinstance(value, (str, int, float, bool)):
            continue
        name = str(key) if str(key).startswith(METADATA_PREFIX) else f"{METADATA_PREFIX}{key}"
        metadata[name] = redact_capture_text(value)[:500] if isinstance(value, str) else value
    return metadata


def _prebuilt_pointer(item: Mapping[str, Any], *, role: str) -> JsonObject:
    from lab_tracker_client.gitinfo import sanitize_remote_url

    uri = sanitize_remote_url(str(item.get("uri") or ""))
    pointer: JsonObject = {
        "role": role,
        "title": _bounded(str(item.get("title") or uri or "artifact"), 500),
        "kind": str(item.get("kind") or "file"),
        "uri": uri,
        "summary": _bounded(redact_capture_text(str(item.get("summary") or "")), 500) or "",
    }
    if item.get("content_hash"):
        pointer["content_hash"] = str(item["content_hash"])
    size = item.get("size_bytes")
    if isinstance(size, int) and not isinstance(size, bool):
        pointer["size_bytes"] = size
    return pointer


def _directory_summary(path: Path) -> JsonObject:
    files = 0
    total = 0
    entries = 0
    truncated = False
    stack = [path]
    while stack and not truncated:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > DIRECTORY_WALK_LIMIT:
                        truncated = True
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    if truncated:
        return {
            "file_count": files,
            "summary": (
                f"Directory: at least {files} files; the walk stopped at the "
                f"{DIRECTORY_WALK_LIMIT}-entry cap. Not hashed."
            ),
        }
    return {
        "file_count": files,
        "size_bytes": total,
        "summary": f"Directory: {files} files, {_format_bytes(total)}. Not hashed.",
    }


def _log_excerpt(logs: Sequence[str | Path], error_text: str | None, *, base: Path) -> str:
    chunks: list[str] = []
    remaining = LOG_EXCERPT_MAX_CHARS
    if error_text and error_text.strip():
        text = error_text.strip()[:ERROR_TEXT_MAX_CHARS]
        chunks.append(f"==> error <==\n{text}")
        remaining -= len(text)
    for item in logs:
        if remaining <= 0:
            break
        path = Path(item).expanduser()
        if not path.is_absolute():
            path = base / path
        try:
            text = _read_text_tail(path, max_chars=remaining).strip()
        except OSError:
            continue
        if text:
            chunks.append(f"==> {path.name} (last {len(text)} characters) <==\n{text}")
            remaining -= len(text)
    return redact_capture_text("\n\n".join(chunks))


def _read_text_tail(path: Path, *, max_chars: int) -> str:
    from lab_tracker_client.hpc import _read_text_tail as read_tail

    return read_tail(path, max_chars=max_chars)


def _git_facts(checkout: Path) -> JsonObject:
    from lab_tracker_client.gitinfo import (
        dirty_state_fields,
        git_dirty_state,
        git_head_commit,
        git_output,
        head_commit_fields,
        sanitize_remote_url,
    )

    head = git_head_commit(checkout)
    facts: JsonObject = {
        **head_commit_fields(head),
        **dirty_state_fields(git_dirty_state(checkout, head=head)),
    }
    branch = git_output(checkout, "rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        facts["git_branch"] = branch
    remote = sanitize_remote_url(git_output(checkout, "config", "--get", "remote.origin.url"))
    if remote:
        facts["repo_remote_url"] = remote
    return {key: value for key, value in facts.items() if value not in (None, "")}


def _checkout_root(workdir: Path) -> tuple[Path, bool]:
    from lab_tracker_client.gitinfo import run_git

    probe = run_git(workdir, "rev-parse", "--show-toplevel")
    if probe.ok and probe.stdout:
        return Path(probe.stdout).resolve(), True
    return workdir, False


def _checkout_identity(remote: str, checkout: Path) -> str:
    from lab_tracker_client.repo import normalize_remote

    return normalize_remote(remote) or f"local:{checkout.name}"


def _watch_config(checkout: Path) -> tuple[Any, str | None]:
    from lab_tracker_client import git_capture

    return git_capture.resolve_watch_config(checkout)


def _config_project(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return _optional(str(payload.get("project_id") or ""))


def _session(session: str | None, checkout: Path) -> tuple[str | None, dict[str, str]]:
    from lab_tracker_client import watch
    from lab_tracker_client.session_context import read_active_session, session_id_from_reference

    explicit = session_id_from_reference(session)
    if explicit:
        return explicit, {"session_source": watch.SESSION_SOURCE_CONFIG}
    active = read_active_session(checkout)
    if active and active.get("session_id"):
        return str(active["session_id"]), watch.active_session_source(active)
    return None, {}


def _drain(outbox: Path, client_factory: Callable[[], Any] | None) -> tuple[Any, str | None]:
    from lab_tracker_client import watch

    if client_factory is None and not _server_configured():
        return "not_configured", None
    try:
        client = client_factory() if client_factory else _default_client()
        try:
            return (
                watch.sync_outbox_path(
                    client, outbox, request_draft=False, limit=DRAIN_EVENT_LIMIT
                ),
                None,
            )
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - the event is durable; sync retries later.
        return None, redact_capture_text(str(exc)) or exc.__class__.__name__


def _server_configured() -> bool:
    from lab_tracker_client.client import load_connection_profile

    return bool(
        os.getenv("LAB_TRACKER_BASE_URL")
        or os.getenv("LAB_TRACKER_MCP_BASE_URL")
        or load_connection_profile().get("base_url")
    )


def _default_client() -> Any:
    from lab_tracker_client.client import LabTracker

    return LabTracker.from_env(timeout_seconds=DRAIN_TIMEOUT_SECONDS)


def _event_sync_status(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    sync = payload.get("sync") if isinstance(payload, Mapping) else None
    return str(sync.get("status") or "") if isinstance(sync, Mapping) else ""


def _event_sync_error(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return redact_capture_text(str(payload["sync"].get("last_error") or ""))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""


def _local_path(text: str, *, base: Path) -> Path:
    if text.lower().startswith("file://"):
        parsed = urlparse(text)
        path = Path(url2pathname(unquote(parsed.path)))
    else:
        path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _timestamp(value: str | None) -> str | None:
    text = _optional(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:64]
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc).isoformat()


def utc_now() -> str:
    """Current time as an ISO-8601 UTC string (the adapters' run end time)."""

    return datetime.now(timezone.utc).isoformat()


def _title(engine: str, status: str, run_id: str, label: str | None) -> str:
    name = f"{label} ({run_id})" if label else run_id
    return f"{ENGINE_LABELS[engine]} run {name}: {status}"


def _default_summary(engine: str, status: str, run_id: str, counts: Mapping[str, int]) -> str:
    return (
        f"{ENGINE_LABELS[engine]} run {run_id} ended with status {status}; it declared "
        f"{counts['inputs']} input(s) and {counts['outputs']} output(s)."
    )


def _new_run_id(engine: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{engine}-{stamp}-{uuid.uuid4().hex[:8]}"


def _clean_run_id(value: str | None) -> str | None:
    text = _optional(value)
    return text[:200] if text else None


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _choice(value: str, allowed: Sequence[str], name: str) -> str:
    from lab_tracker_client.client import LTValidationError

    cleaned = str(value or "").strip().lower()
    if cleaned not in allowed:
        raise LTValidationError(f"{name} must be one of {', '.join(allowed)}; got {value!r}.")
    return cleaned


def _bounded(value: str | None, limit: int) -> str | None:
    text = _optional(value)
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def _notice_once(key: str, message: str) -> None:
    if key in _NOTICES_SHOWN:
        return
    _NOTICES_SHOWN.add(key)
    print(message, file=sys.stderr, flush=True)


def _reset_notices_for_tests() -> None:
    _NOTICES_SHOWN.clear()


__all__ = [
    "CAPTURE_ENV",
    "DEFAULT_HASH_MAX_BYTES",
    "DEFAULT_MAX_ARTIFACTS",
    "ENGINES",
    "PIPELINE_ADAPTER",
    "PIPELINE_CAPTURE_KIND",
    "PipelineRun",
    "STATUSES",
    "artifact_pointer",
    "artifact_pointers",
    "capture_enabled",
    "capture_pipeline_run",
    "expand_path_args",
    "pointer_location",
    "render_run_note",
    "report_pipeline_run",
    "resolve_pipeline_project",
    "utc_now",
]
