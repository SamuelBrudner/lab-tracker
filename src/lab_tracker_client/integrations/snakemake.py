"""Snakemake ``onsuccess:``/``onerror:`` capture: one staged note per workflow run.

Add to the Snakefile (the ``try`` keeps a missing client from ever failing the
workflow; :func:`report` itself never raises)::

    onsuccess:
        try:
            from lab_tracker_client.integrations.snakemake import report
            report(log, status="success")
        except Exception:
            pass

    onerror:
        try:
            from lab_tracker_client.integrations.snakemake import report
            report(log, status="error")
        except Exception:
            pass

Snakemake passes the handlers ``log``, the path of this run's log file
(``.snakemake/log/<start time>.snakemake.log``). The declared inputs and
outputs are derived from what Snakemake exposes there, in this order:

1. explicit ``inputs=``/``outputs=`` arguments, when given;
2. the run log: every job block (``rule``/``localrule``/``checkpoint``) names
   its ``input:``, ``output:`` and ``jobid:``, and ``Finished job N.`` marks the
   jobs that completed. Outputs are the finished jobs' outputs; inputs are the
   executed jobs' inputs that no executed job produced (the run's free
   inputs), ignoring aggregate target rules without outputs (``rule all``);
3. ``.snakemake/metadata`` — the per-output records Snakemake keeps (rule,
   inputs, start/end time) — for outputs whose record ended after the run
   started, when the log names no jobs (``--quiet``).

The run id and start time come from the log's file name; the end time is the
moment the handler runs. Paths that contain ``", "`` cannot be split from the
log reliably; pass those explicitly.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab_tracker_client.pipeline_capture import PipelineRun, capture_pipeline_run, utc_now

_JOB_HEADER = re.compile(r"^(?:local)?(?:rule|checkpoint)\s+(?P<rule>[^\s:]+):\s*$")
_ERROR_HEADER = re.compile(r"^Error in rule\s+(?P<rule>[^\s:]+):\s*$")
_FINISHED = re.compile(r"^Finished job(?:id:)?\s+(?P<id>\d+)\b")
_FIELD = re.compile(r"^\s+(?P<key>input|output|jobid)\s*:\s*(?P<value>.*)$")
_LOG_NAME = re.compile(r"(?P<stamp>\d{4}-\d{2}-\d{2}T\d{6}(?:\.\d+)?)\.snakemake\.log$")
_FAILED_WORKFLOW = re.compile(
    r"^(?:Exiting because a job execution failed|Error in rule |.*\bWorkflowError\b)"
)
MAX_METADATA_RECORDS = 20_000
MAX_RULES_LISTED = 40


@dataclass
class SnakemakeJob:
    """One job block of a Snakemake log."""

    rule: str
    jobid: str = ""
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    failed: bool = False


@dataclass
class SnakemakeLog:
    """What a Snakemake run log says about the run's jobs."""

    jobs: dict[str, SnakemakeJob] = field(default_factory=dict)
    finished: set[str] = field(default_factory=set)
    failed_rules: list[str] = field(default_factory=list)
    workflow_failed: bool = False

    @property
    def finished_jobs(self) -> list[SnakemakeJob]:
        return [
            job for jobid, job in self.jobs.items() if jobid in self.finished and not job.failed
        ]

    def declared_outputs(self) -> list[str]:
        return _unique(path for job in self.finished_jobs for path in job.outputs)

    def declared_inputs(self) -> list[str]:
        produced = {path for job in self.jobs.values() for path in job.outputs}
        return _unique(
            path
            for job in self.jobs.values()
            if job.outputs  # aggregate target rules (rule all) are not consumers
            for path in job.inputs
            if path not in produced
        )


def parse_log(text: str) -> SnakemakeLog:
    """Parse the job blocks, finished markers and failures of a run log."""

    parsed = SnakemakeLog()
    current: SnakemakeJob | None = None
    for line in text.splitlines():
        header = _JOB_HEADER.match(line)
        error = _ERROR_HEADER.match(line)
        if header or error:
            match = header or error
            assert match is not None
            current = SnakemakeJob(rule=match.group("rule"), failed=bool(error))
            if error:
                parsed.failed_rules.append(match.group("rule"))
                parsed.workflow_failed = True
            continue
        finished = _FINISHED.match(line)
        if finished:
            parsed.finished.add(finished.group("id"))
            current = None
            continue
        if _FAILED_WORKFLOW.match(line):
            parsed.workflow_failed = True
        field_match = _FIELD.match(line)
        if current is not None and field_match:
            key = field_match.group("key")
            value = field_match.group("value").strip()
            if key == "jobid":
                current.jobid = value
                existing = parsed.jobs.get(value)
                if current.failed and existing is not None:
                    existing.failed = True
                    current = existing
                else:
                    parsed.jobs[value] = current
            else:
                target = current.inputs if key == "input" else current.outputs
                target.extend(_split_paths(value))
            continue
        if current is not None and line.strip() and not line[:1].isspace():
            current = None
    return parsed


def run_started_at(log: str | os.PathLike[str] | None) -> str | None:
    """The run's start time (ISO-8601 UTC) from the log's file name, if it has one."""

    if not log:
        return None
    match = _LOG_NAME.search(Path(log).name)
    if not match:
        return None
    stamp = match.group("stamp")
    pattern = "%Y-%m-%dT%H%M%S.%f" if "." in stamp else "%Y-%m-%dT%H%M%S"
    try:
        local = datetime.strptime(stamp, pattern).astimezone()
    except ValueError:
        return None
    return local.astimezone(timezone.utc).isoformat()


def metadata_records(workdir: Path, *, since: float) -> list[tuple[str, Mapping[str, Any]]]:
    """``(output, record)`` pairs from ``.snakemake/metadata`` that ended at or after ``since``.

    Snakemake names each record after the urlsafe-base64 output path, split
    into ``@``-prefixed directories when the name is too long.
    """

    root = workdir / ".snakemake" / "metadata"
    if not root.is_dir():
        return []
    records: list[tuple[str, Mapping[str, Any]]] = []
    for count, path in enumerate(sorted(root.rglob("*"))):
        if count >= MAX_METADATA_RECORDS:
            break
        if not path.is_file():
            continue
        parts = [part.lstrip("@") for part in path.relative_to(root).parts]
        encoded = "".join(parts)
        try:
            output = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(record, Mapping) or record.get("incomplete"):
            continue
        ended = record.get("endtime")
        if isinstance(ended, (int, float)) and ended >= since:
            records.append((output, record))
    return records


def snakemake_run(
    log: str | os.PathLike[str] | None = None,
    *,
    status: str | None = None,
    inputs: Sequence[str] | None = None,
    outputs: Sequence[str] | None = None,
    workflow: Any = None,
    run_id: str | None = None,
    label: str | None = None,
    summary: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> PipelineRun:
    """Build the :class:`PipelineRun` for a finished Snakemake workflow."""

    workdir = Path(cwd or os.getcwd()).resolve()
    log_path = _resolve(log, workdir)
    parsed = SnakemakeLog()
    if log_path is not None:
        try:
            parsed = parse_log(log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            parsed = SnakemakeLog()
    started_at = run_started_at(log_path)
    derived_outputs = parsed.declared_outputs()
    derived_inputs = parsed.declared_inputs()
    source = "log"
    if not parsed.jobs and started_at:
        since = datetime.fromisoformat(started_at).timestamp()
        records = metadata_records(workdir, since=since)
        derived_outputs = _unique(output for output, _record in records)
        produced = set(derived_outputs)
        derived_inputs = _unique(
            str(path)
            for _output, record in records
            for path in record.get("input") or []
            if str(path) not in produced
        )
        source = "metadata" if records else "none"
    resolved_status = status or (
        "error" if parsed.workflow_failed else "success" if parsed.finished else "unknown"
    )
    details = _details(parsed, log_path, workflow, workdir, source)
    metadata: dict[str, str | int | float | bool] = {
        "pipeline_snakemake_jobs_finished": len(parsed.finished_jobs),
        "pipeline_snakemake_jobs_failed": len(parsed.failed_rules),
        "pipeline_snakemake_declared_from": "arguments" if outputs is not None else source,
    }
    snakefile = _snakefile(workflow)
    if snakefile:
        metadata["pipeline_snakemake_snakefile"] = _relative(snakefile, workdir)
    version = _snakemake_version()
    if version:
        metadata["pipeline_snakemake_version"] = version
    return PipelineRun(
        engine="snakemake",
        status=resolved_status,
        run_id=run_id or _run_id_from_log(log_path),
        started_at=started_at,
        ended_at=utc_now(),
        inputs=list(inputs) if inputs is not None else derived_inputs,
        outputs=list(outputs) if outputs is not None else derived_outputs,
        logs=[log_path] if log_path is not None else [],
        label=label,
        summary=summary,
        details=details,
        metadata=metadata,
    )


def report(
    log: str | os.PathLike[str] | None = None,
    *,
    status: str | None = None,
    inputs: Sequence[str] | None = None,
    outputs: Sequence[str] | None = None,
    workflow: Any = None,
    run_id: str | None = None,
    project: str | None = None,
    question: str | None = None,
    session: str | None = None,
    label: str | None = None,
    summary: str | None = None,
    drain: bool = True,
    request_draft: bool = False,
    hash_max_bytes: int | None = None,
    max_artifacts: int | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Record this Snakemake run as one staged note. Never raises.

    Call from ``onsuccess:`` with ``status="success"`` and from ``onerror:``
    with ``status="error"``; see the module docstring for the snippet and for
    how inputs and outputs are derived.
    """

    try:
        run = snakemake_run(
            log,
            status=status,
            inputs=inputs,
            outputs=outputs,
            workflow=workflow,
            run_id=run_id,
            label=label,
            summary=summary,
            cwd=cwd,
        )
    except Exception as exc:  # noqa: BLE001 - never break the workflow's handler.
        run = PipelineRun(
            engine="snakemake",
            status=status or "unknown",
            run_id=run_id,
            ended_at=utc_now(),
            inputs=list(inputs or []),
            outputs=list(outputs or []),
            error_text=f"lab-tracker could not read the Snakemake log: {exc}",
        )
    return capture_pipeline_run(
        run,
        cwd=cwd,
        project_id=project,
        session=session,
        question_id=question,
        drain=drain,
        request_draft=request_draft,
        hash_max_bytes=hash_max_bytes,
        max_artifacts=max_artifacts,
    )


def _details(
    parsed: SnakemakeLog,
    log_path: Path | None,
    workflow: Any,
    workdir: Path,
    source: str,
) -> list[str]:
    lines: list[str] = []
    per_rule: dict[str, int] = {}
    for job in parsed.finished_jobs:
        per_rule[job.rule] = per_rule.get(job.rule, 0) + 1
    if per_rule:
        shown = sorted(per_rule.items())[:MAX_RULES_LISTED]
        lines.append(
            f"- Jobs finished: {sum(per_rule.values())} ("
            + ", ".join(f"{rule}: {count}" for rule, count in shown)
            + (", …" if len(per_rule) > MAX_RULES_LISTED else "")
            + ")"
        )
    if parsed.failed_rules:
        lines.append(
            f"- Failed rules: {', '.join(_unique(parsed.failed_rules)[:MAX_RULES_LISTED])}"
        )
    snakefile = _snakefile(workflow)
    if snakefile:
        lines.append(f"- Snakefile: `{_relative(snakefile, workdir)}`")
    if log_path is not None:
        lines.append(f"- Snakemake log: `{_relative(str(log_path), workdir)}`")
    lines.append(
        {
            "log": "- Inputs and outputs: derived from the run log's job blocks",
            "metadata": "- Inputs and outputs: derived from .snakemake/metadata records",
            "none": "- Inputs and outputs: none could be derived; pass inputs=/outputs=",
        }[source]
    )
    return lines


def _split_paths(value: str) -> list[str]:
    return [part.strip() for part in value.split(", ") if part.strip()]


def _unique(values: Any) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(str(value), None)
    return list(seen)


def _resolve(log: str | os.PathLike[str] | None, workdir: Path) -> Path | None:
    if not log:
        return None
    path = Path(log).expanduser()
    return path if path.is_absolute() else workdir / path


def _run_id_from_log(log_path: Path | None) -> str | None:
    if log_path is None:
        return None
    match = _LOG_NAME.search(log_path.name)
    return f"snakemake-{match.group('stamp')}" if match else None


def _snakefile(workflow: Any) -> str | None:
    if workflow is None:
        return None
    for attribute in ("main_snakefile", "snakefile"):
        value = getattr(workflow, attribute, None)
        if value:
            return str(value)
    return None


def _relative(path: str, workdir: Path) -> str:
    try:
        return Path(path).resolve().relative_to(workdir).as_posix()
    except (ValueError, OSError):
        return str(path)


def _snakemake_version() -> str | None:
    try:
        import snakemake  # type: ignore[import-not-found, unused-ignore]
    except Exception:  # noqa: BLE001 - snakemake is optional here.
        return None
    return str(getattr(snakemake, "__version__", "") or "") or None


__all__ = [
    "SnakemakeJob",
    "SnakemakeLog",
    "metadata_records",
    "parse_log",
    "report",
    "run_started_at",
    "snakemake_run",
]
