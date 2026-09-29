"""``lt pipeline``: record one pipeline run as a staged Lab Tracker note.

* ``lt pipeline report`` — the generic verb any pipeline (or a Nextflow
  ``workflow.onComplete`` handler) calls with its declared inputs and outputs;
* ``lt pipeline nextflow --trace trace.txt`` — the same, with task status read
  from a Nextflow trace file;
* ``lt pipeline dvc [--lock dvc.lock]`` — the same, with each stage's command,
  dependencies and outputs (and DVC's md5 hashes) read from ``dvc.lock``.

Every verb writes exactly one event per run to the checkout's watch outbox
and then drains that outbox best-effort. ``--fail-silent`` makes a verb exit 0
with no output on any error, so a pipeline hook can never change the
pipeline's own exit status.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lab_tracker_client.pipeline_capture import (
    DEFAULT_HASH_MAX_BYTES,
    DEFAULT_MAX_ARTIFACTS,
    ENGINES,
    STATUSES,
    PipelineRun,
    expand_path_args,
    report_pipeline_run,
)
from lab_tracker_client.redaction import redact_capture_text


def add_pipeline_parsers(subcommands: argparse._SubParsersAction[Any]) -> None:
    """Register ``lt pipeline report|nextflow|dvc``."""

    pipeline_parser = subcommands.add_parser(
        "pipeline",
        help="Record a pipeline run's declared inputs/outputs as one staged note.",
    )
    commands = pipeline_parser.add_subparsers(dest="pipeline_command", required=True)

    report = commands.add_parser(
        "report",
        help="Record one pipeline run (any engine) with its declared inputs and outputs.",
    )
    report.add_argument("--engine", choices=ENGINES, default="generic")
    report.add_argument("--status", choices=STATUSES, default="unknown")
    _add_run_arguments(report)
    report.set_defaults(func=_command(_report_run, "pipeline-report"), needs_client=False)

    nextflow = commands.add_parser(
        "nextflow",
        help="Record a Nextflow run from its trace file (task status per process).",
    )
    nextflow.add_argument("--trace", required=True, help="Nextflow trace file (TSV).")
    nextflow.add_argument(
        "--status",
        choices=STATUSES,
        help="Run status (default: derived from the trace's last task attempts).",
    )
    _add_run_arguments(nextflow)
    nextflow.set_defaults(func=_command(_nextflow_run, "pipeline-nextflow"), needs_client=False)

    dvc = commands.add_parser(
        "dvc",
        help="Record the pipeline state in dvc.lock (stage cmds, deps and outs with md5).",
    )
    dvc.add_argument("--lock", default="dvc.lock", help="dvc.lock path (default: ./dvc.lock).")
    dvc.add_argument(
        "--status",
        choices=STATUSES,
        help="Run status (default: unknown; pass success after 'dvc repro' succeeds).",
    )
    _add_run_arguments(dvc)
    dvc.set_defaults(func=_command(_dvc_run, "pipeline-dvc"), needs_client=False)


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="Declared input path or URI; repeatable; @FILE reads one path per line.",
    )
    parser.add_argument(
        "--output",
        action="append",
        default=[],
        help="Declared output path, directory or URI; repeatable; @FILE reads one per line.",
    )
    parser.add_argument("--run-id", help="Engine run id (default: generated or engine-derived).")
    parser.add_argument("--started-at", help="Run start time (ISO-8601).")
    parser.add_argument("--ended-at", help="Run end time (ISO-8601).")
    parser.add_argument(
        "--log",
        action="append",
        default=[],
        help="Log file whose tail is excerpted (bounded, credentials scrubbed). Repeatable.",
    )
    parser.add_argument("--project", help="Project UUID (default: the checkout's bound project).")
    parser.add_argument("--session", help="Session UUID or link code the run belongs to.")
    parser.add_argument("--question", help="Question UUID the run addresses (declared target).")
    parser.add_argument("--label", help="Short human label for the run.")
    parser.add_argument("--summary", help="One-paragraph summary for review.")
    parser.add_argument("--tag", action="append", default=[], help="Tag. Repeatable.")
    parser.add_argument(
        "--cwd",
        help="Pipeline working directory; relative paths resolve here (default: cwd).",
    )
    parser.add_argument(
        "--hash-max-bytes",
        type=int,
        default=DEFAULT_HASH_MAX_BYTES,
        help=f"Hash files up to this size; larger ones are pointers (default: "
        f"{DEFAULT_HASH_MAX_BYTES}).",
    )
    parser.add_argument(
        "--max-artifacts",
        type=int,
        default=DEFAULT_MAX_ARTIFACTS,
        help=f"List at most this many inputs and outputs each (default: {DEFAULT_MAX_ARTIFACTS}).",
    )
    parser.add_argument(
        "--no-drain",
        action="store_true",
        help="Queue the event only; skip the best-effort sync of the watch outbox.",
    )
    parser.add_argument(
        "--request-draft",
        action="store_true",
        help="Ask for a graph draft for this run's note when it syncs.",
    )
    parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Exit 0 with no output on any error (for pipeline hooks).",
    )


def _command(
    build: Callable[[argparse.Namespace, Path], PipelineRun], name: str
) -> Callable[[argparse.Namespace], Any]:
    def run(args: argparse.Namespace) -> Any:
        try:
            workdir = Path(args.cwd or Path.cwd()).expanduser().resolve()
            payload = report_pipeline_run(
                build(args, workdir),
                cwd=workdir,
                project_id=args.project,
                session=args.session,
                question_id=args.question,
                drain=not args.no_drain,
                request_draft=args.request_draft,
                hash_max_bytes=args.hash_max_bytes,
                max_artifacts=args.max_artifacts,
            )
        except Exception as exc:  # noqa: BLE001 - one clean line, never a traceback.
            if args.fail_silent:
                return None
            message = redact_capture_text(str(exc)) or exc.__class__.__name__
            raise SystemExit(f"lt pipeline {args.pipeline_command}: {message}") from None
        payload["command"] = name
        return payload

    return run


def _common(args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    return {
        "run_id": args.run_id,
        "inputs": expand_path_args(args.input, base=workdir),
        "outputs": expand_path_args(args.output, base=workdir),
        "logs": list(args.log),
        "label": args.label,
        "summary": args.summary,
        "started_at": args.started_at,
        "ended_at": args.ended_at,
        "tags": list(args.tag),
    }


def _report_run(args: argparse.Namespace, workdir: Path) -> PipelineRun:
    return PipelineRun(engine=args.engine, status=args.status, **_common(args, workdir))


def _nextflow_run(args: argparse.Namespace, workdir: Path) -> PipelineRun:
    from lab_tracker_client.integrations.nextflow import pipeline_run_from_trace

    trace = Path(args.trace).expanduser()
    return pipeline_run_from_trace(
        trace if trace.is_absolute() else workdir / trace,
        status=args.status,
        **_common(args, workdir),
    )


def _dvc_run(args: argparse.Namespace, workdir: Path) -> PipelineRun:
    from lab_tracker_client.integrations.dvc import pipeline_run_from_lock

    lock = Path(args.lock).expanduser()
    return pipeline_run_from_lock(
        lock if lock.is_absolute() else workdir / lock,
        status=args.status,
        **_common(args, workdir),
    )


__all__ = ["add_pipeline_parsers"]
