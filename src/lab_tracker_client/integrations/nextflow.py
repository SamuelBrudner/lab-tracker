"""Nextflow trace-file parsing for ``lt pipeline nextflow --trace trace.txt``.

Nextflow's trace report (``-with-trace`` or ``trace.enabled = true``) is a
tab-separated file with one row per task attempt. The default columns are
``task_id hash native_id name status exit submit duration realtime %cpu
peak_rss peak_vmem rchar wchar``; any subset in any order is accepted, and the
parser only needs ``name`` and ``status``. It summarizes task outcomes per
process (``COMPLETED``, ``CACHED``, ``FAILED``, ``ABORTED``), names the tasks
whose last attempt failed, and reads the run's first ``submit`` and last
``complete`` times when those columns are present.

The trace does not list published files: pass the ``publishDir`` (or specific
files) with ``--output`` so the run's outputs are recorded as pointers.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lab_tracker_client.pipeline_capture import PipelineRun

FAILED_STATES = frozenset({"FAILED", "ABORTED"})
SUCCESS_STATES = frozenset({"COMPLETED", "CACHED"})
MAX_DETAIL_PROCESSES = 40
MAX_FAILED_TASKS_LISTED = 20
# A trace larger than this is read from its tail only (one row per task attempt).
MAX_TRACE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class NextflowTask:
    """One task attempt (trace row)."""

    name: str
    process: str
    status: str
    task_id: str = ""
    exit: str = ""
    submit: str = ""
    complete: str = ""


@dataclass
class NextflowTrace:
    """Summary of a trace file."""

    tasks: list[NextflowTask] = field(default_factory=list)

    @property
    def last_attempts(self) -> dict[str, NextflowTask]:
        """Each task name's final attempt (retries replace earlier failures)."""

        latest: dict[str, NextflowTask] = {}
        for task in self.tasks:
            latest[task.name] = task
        return latest

    @property
    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.last_attempts.values():
            counts[task.status] = counts.get(task.status, 0) + 1
        return counts

    @property
    def failed_tasks(self) -> list[NextflowTask]:
        return [task for task in self.last_attempts.values() if task.status in FAILED_STATES]

    @property
    def started_at(self) -> str | None:
        values = sorted(task.submit for task in self.tasks if task.submit and task.submit != "-")
        return values[0] if values else None

    @property
    def ended_at(self) -> str | None:
        values = sorted(
            task.complete for task in self.tasks if task.complete and task.complete != "-"
        )
        return values[-1] if values else None

    def derived_status(self) -> str:
        """``error`` if any task's last attempt failed, ``success`` if any ran, else unknown.

        A workflow using ``errorStrategy 'ignore'`` can succeed despite failed
        tasks, so the caller's explicit status (``workflow.success``) wins.
        """

        if not self.tasks:
            return "unknown"
        return "error" if self.failed_tasks else "success"


def parse_trace(text: str) -> NextflowTrace:
    """Parse a Nextflow trace TSV (header row required)."""

    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    fields = {name.strip().lower() for name in reader.fieldnames or []}
    if not {"name", "status"} <= fields:
        raise ValueError("a Nextflow trace needs at least the 'name' and 'status' columns")
    trace = NextflowTrace()
    for row in reader:
        cleaned = {str(key).strip().lower(): (value or "").strip() for key, value in row.items()}
        name = cleaned.get("name", "")
        if not name:
            continue
        trace.tasks.append(
            NextflowTask(
                name=name,
                process=cleaned.get("process") or name.split(" (", 1)[0],
                status=cleaned.get("status", "").upper(),
                task_id=cleaned.get("task_id", ""),
                exit=cleaned.get("exit", ""),
                submit=cleaned.get("submit", ""),
                complete=cleaned.get("complete", ""),
            )
        )
    return trace


def read_trace(path: str | Path) -> NextflowTrace:
    """Read and parse a trace file; a huge trace is read from its tail."""

    resolved = Path(path).expanduser()
    with resolved.open("rb") as handle:
        header = handle.readline()
        size = handle.seek(0, 2)
        start = max(len(header), size - MAX_TRACE_BYTES)
        handle.seek(start)
        data = handle.read()
    if start > len(header):
        data = data.split(b"\n", 1)[1] if b"\n" in data else b""
    return parse_trace((header + data).decode("utf-8", errors="replace"))


def trace_details(trace: NextflowTrace) -> list[str]:
    """Markdown lines summarizing the trace per process (bounded)."""

    per_process: dict[str, dict[str, int]] = {}
    for task in trace.last_attempts.values():
        counts = per_process.setdefault(task.process, {})
        counts[task.status] = counts.get(task.status, 0) + 1
    totals = ", ".join(
        f"{count} {state.lower()}" for state, count in sorted(trace.status_counts.items())
    )
    lines = [f"- Tasks: {len(trace.last_attempts)} ({totals or 'none'})"]
    retried = len(trace.tasks) - len(trace.last_attempts)
    if retried:
        lines.append(f"- Retried attempts: {retried}")
    for process, counts in sorted(per_process.items())[:MAX_DETAIL_PROCESSES]:
        summary = ", ".join(f"{count} {state.lower()}" for state, count in sorted(counts.items()))
        lines.append(f"- Process `{process}`: {summary}")
    if len(per_process) > MAX_DETAIL_PROCESSES:
        lines.append(f"- … and {len(per_process) - MAX_DETAIL_PROCESSES} more processes")
    failed = trace.failed_tasks
    for task in failed[:MAX_FAILED_TASKS_LISTED]:
        exit_code = f", exit {task.exit}" if task.exit and task.exit != "-" else ""
        lines.append(f"- Failed task `{task.name}` ({task.status.lower()}{exit_code})")
    if len(failed) > MAX_FAILED_TASKS_LISTED:
        lines.append(f"- … and {len(failed) - MAX_FAILED_TASKS_LISTED} more failed tasks")
    return lines


def pipeline_run_from_trace(
    trace_path: str | Path,
    *,
    status: str | None = None,
    run_id: str | None = None,
    inputs: Sequence[str | Mapping[str, Any]] = (),
    outputs: Sequence[str | Mapping[str, Any]] = (),
    logs: Sequence[str | Path] = (),
    label: str | None = None,
    summary: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    tags: Sequence[str] = (),
) -> PipelineRun:
    """A :class:`PipelineRun` for a Nextflow run described by its trace file."""

    trace = read_trace(trace_path)
    counts = trace.status_counts
    metadata: dict[str, str | int | float | bool] = {
        "pipeline_nextflow_task_count": len(trace.last_attempts),
        "pipeline_nextflow_failed_count": len(trace.failed_tasks),
        "pipeline_nextflow_cached_count": counts.get("CACHED", 0),
        "pipeline_nextflow_trace": Path(trace_path).name,
    }
    return PipelineRun(
        engine="nextflow",
        status=status or trace.derived_status(),
        run_id=run_id,
        started_at=started_at or trace.started_at,
        ended_at=ended_at or trace.ended_at,
        inputs=inputs,
        outputs=outputs,
        logs=logs,
        label=label,
        summary=summary,
        details=trace_details(trace),
        metadata=metadata,
        tags=tags,
    )


__all__ = [
    "NextflowTask",
    "NextflowTrace",
    "parse_trace",
    "pipeline_run_from_trace",
    "read_trace",
    "trace_details",
]
