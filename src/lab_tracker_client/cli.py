"""Small CLI for the Lab Tracker consumer client."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import lab_tracker_client.watch as watch_capture
from lab_tracker.assistant_next_questions import is_research_facing_prompt
from lab_tracker_client.client import (
    NOTE_STATUS_VALUES,
    EntityRef,
    EvidenceImportResult,
    LabTracker,
    LTValidationError,
    ids,
)
from lab_tracker_client.hpc import (
    DEFAULT_MANIFEST_PATTERN,
    DEFAULT_OUTBOX,
    begin_event,
    finish_event,
    init_config,
    load_config,
    outbox_status,
    run_submit_command,
    sync_outbox,
    watch_manifests,
)


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "needs_client", True):
            client = LabTracker.from_env()
            try:
                payload = args.func(client, args)
            finally:
                client.close()
        else:
            payload = args.func(args)
    except Exception:
        if getattr(args, "fail_silent", False):
            return
        raise
    exit_code = _payload_exit_code(payload)
    if exit_code and getattr(args, "fail_silent", False):
        return
    if payload is not None:
        print(json.dumps(_jsonable(payload), indent=2))
    if exit_code:
        raise SystemExit(exit_code)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lt", description="Lab Tracker consumer CLI.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    health_parser = subcommands.add_parser(
        "health",
        help="Check the Lab Tracker API health endpoint.",
    )
    health_parser.set_defaults(func=lambda client, _args: client.health())
    subcommands.add_parser("readiness", help="Check Lab Tracker readiness.").set_defaults(
        func=lambda client, _args: client.readiness()
    )
    subcommands.add_parser("ids", help="Print lt_ids.json.").set_defaults(
        func=lambda _client, _args: ids()
    )
    doctor_parser = subcommands.add_parser(
        "doctor",
        aliases=["check-idioms"],
        help="Check managed Lab Tracker code-facing idiom blocks.",
    )
    doctor_parser.add_argument(
        "--target",
        default=".",
        help="Consumer repo path to inspect. Defaults to the current directory.",
    )
    doctor_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Suppress drift exit codes and errors for prompt hooks.",
    )
    doctor_parser.set_defaults(func=_cmd_doctor, needs_client=False)

    update_parser = subcommands.add_parser(
        "update",
        help=(
            "Refresh Lab Tracker integration files (hooks, agent prompts, managed "
            "blocks) in this repo to the installed package version."
        ),
    )
    update_parser.add_argument(
        "--target",
        default=".",
        help="Consumer repo path to update. Defaults to the current directory.",
    )
    update_parser.add_argument(
        "--yes",
        action="store_true",
        help="Also install managed code-conventions blocks that are not present yet.",
    )
    update_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files.",
    )
    update_parser.set_defaults(func=_cmd_update, needs_client=False)

    prime_parser = subcommands.add_parser(
        "prime",
        help="Emit a lightweight active-goal/open-question prime for agent hooks.",
    )
    prime_parser.add_argument("--project", help="Optional project UUID.")
    prime_parser.add_argument("--limit", type=int, default=5)
    prime_parser.add_argument(
        "--if-research-facing",
        action="store_true",
        help="Read stdin and emit nothing unless the prompt is research-facing.",
    )
    prime_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Suppress API errors so prompt hooks never block a session.",
    )
    prime_parser.add_argument(
        "--prompt",
        help="Prompt text to classify instead of stdin; primarily for tests.",
    )
    prime_parser.set_defaults(func=_cmd_prime)

    note_parser = subcommands.add_parser("note", help="Idempotently upsert a text note.")
    note_parser.add_argument("--project", required=True, help="Project UUID.")
    note_parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Target as kind:uuid. Repeat for multiple targets.",
    )
    source = note_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="Path to note content.")
    source.add_argument("--text", help="Inline note content.")
    note_parser.add_argument("--status", choices=NOTE_STATUS_VALUES, default="committed")
    note_parser.add_argument("--metadata", help="JSON object of scalar metadata.")
    note_parser.set_defaults(func=_cmd_note)

    quick_parser = subcommands.add_parser("quick", help="Upload a quick text capture.")
    quick_parser.add_argument("text")
    quick_parser.add_argument("--project", help="Project UUID; defaults to LAB_TRACKER_PROJECT_ID.")
    quick_parser.add_argument("--source")
    quick_parser.set_defaults(func=_cmd_quick)

    import_folder_parser = subcommands.add_parser(
        "import-folder",
        help="Import files from a local or synced folder as staged evidence notes.",
    )
    import_folder_parser.add_argument("--project", required=True, help="Project UUID.")
    import_folder_parser.add_argument("--root", required=True, help="Folder to scan.")
    import_folder_parser.add_argument(
        "--provider",
        default="local-folder",
        help="Evidence source provider name. Defaults to local-folder.",
    )
    import_folder_parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Glob for relative paths to include. Repeatable. Defaults to all files.",
    )
    import_folder_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob for relative paths to exclude. Repeatable.",
    )
    import_folder_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be imported without creating notes.",
    )
    import_folder_parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of matched files to process.",
    )
    import_folder_parser.add_argument(
        "--adapter-name",
        default="lt-import-folder",
        help="Adapter identifier recorded in evidence metadata.",
    )
    import_folder_parser.add_argument(
        "--status",
        choices=NOTE_STATUS_VALUES,
        default="staged",
        help="Note status for imported files. Defaults to staged.",
    )
    import_folder_parser.set_defaults(func=_cmd_import_folder)

    _add_watch_parsers(subcommands)
    _add_hpc_parsers(subcommands)

    export_parser = subcommands.add_parser(
        "export",
        help="Write PROV-O/JSON-LD provenance sidecars that survive without a server.",
    )
    export_parser.add_argument("--project", required=True, help="Project UUID.")
    export_parser.add_argument(
        "--out",
        default="lab-tracker-export",
        help="Directory for sidecar files. Defaults to ./lab-tracker-export.",
    )
    export_parser.add_argument(
        "--since",
        help="ISO 8601 lower bound (inclusive) for windowed analyses.",
    )
    export_parser.add_argument(
        "--until",
        help="ISO 8601 upper bound (exclusive) for windowed analyses.",
    )
    export_parser.add_argument(
        "--data-root",
        help="Resolve dataset file paths under this root and co-locate sidecars beside the data.",
    )
    export_parser.set_defaults(func=_cmd_export)

    questions_parser = subcommands.add_parser("list-questions")
    questions_parser.add_argument("--project", required=True)
    questions_parser.add_argument("--status")
    questions_parser.add_argument("--search")
    questions_parser.set_defaults(func=_cmd_list_questions)

    notes_parser = subcommands.add_parser("list-notes")
    notes_parser.add_argument("--project", required=True)
    notes_parser.add_argument("--status")
    notes_parser.set_defaults(func=_cmd_list_notes)

    return parser


def _add_watch_parsers(subcommands: argparse._SubParsersAction) -> None:
    watch_parser = subcommands.add_parser(
        "watch",
        help="Capture watched folders into an offline Lab Tracker outbox.",
    )
    watch_commands = watch_parser.add_subparsers(dest="watch_command", required=True)

    init_parser = watch_commands.add_parser("init", help="Create .lab-tracker/watch.json.")
    init_parser.add_argument("--project", help="Default Lab Tracker project UUID.")
    init_parser.add_argument(
        "--outbox",
        default=watch_capture.DEFAULT_OUTBOX,
        help=f"Outbox path. Defaults to {watch_capture.DEFAULT_OUTBOX}.",
    )
    init_parser.add_argument("--config", help="Config path. Defaults to .lab-tracker/watch.json.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    init_parser.set_defaults(func=_cmd_watch_init, needs_client=False)

    scan_parser = watch_commands.add_parser(
        "scan",
        help="Scan configured or CLI-specified folders into the watch outbox.",
    )
    scan_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    scan_parser.add_argument("--root", help="Folder or file to scan. Omit to scan config watches.")
    scan_parser.add_argument(
        "--mode",
        choices=sorted(watch_capture.ALLOWED_MODES),
        default=watch_capture.MODE_FILES,
        help="Capture mode. Defaults to files.",
    )
    scan_parser.add_argument(
        "--sink",
        choices=sorted(watch_capture.ALLOWED_SINKS),
        default=watch_capture.SINK_STAGED_NOTE,
        help="Sync sink for captured events. Defaults to staged-note.",
    )
    scan_parser.add_argument(
        "--pattern",
        default=watch_capture.DEFAULT_MANIFEST_PATTERN,
        help=f"Manifest filename pattern. Defaults to {watch_capture.DEFAULT_MANIFEST_PATTERN}.",
    )
    scan_parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Glob for relative paths to include in files mode. Repeatable.",
    )
    scan_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob for relative paths to exclude in files mode. Repeatable.",
    )
    scan_parser.add_argument("--project", help="Override project UUID from config.")
    scan_parser.add_argument("--question", help="Candidate question UUID recorded in metadata.")
    scan_parser.add_argument(
        "--dataset",
        action="append",
        default=[],
        help="Candidate dataset UUID.",
    )
    scan_parser.add_argument("--tag", action="append", default=[], help="Tag. Repeatable.")
    scan_parser.add_argument("--session", help="Session UUID for acquisition-output sink.")
    scan_parser.add_argument(
        "--provider",
        help="Evidence source provider for files mode. Defaults to local-folder.",
    )
    scan_parser.add_argument("--adapter", help="Adapter identifier recorded in metadata.")
    scan_parser.add_argument("--limit", type=int, help="Maximum items to process.")
    scan_parser.add_argument("--dry-run", action="store_true")
    scan_parser.set_defaults(func=_cmd_watch_scan, needs_client=False)

    status_parser = watch_commands.add_parser("status", help="Summarize local watch outbox state.")
    status_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    status_parser.set_defaults(func=_cmd_watch_status, needs_client=False)

    sync_parser = watch_commands.add_parser(
        "sync",
        help="Sync watch outbox events into Lab Tracker.",
    )
    sync_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--request-draft", action="store_true")
    sync_parser.add_argument("--limit", type=int, help="Maximum events to process.")
    sync_parser.set_defaults(func=_cmd_watch_sync)


def _add_hpc_parsers(subcommands: argparse._SubParsersAction) -> None:
    hpc_parser = subcommands.add_parser(
        "hpc",
        help="Capture Slurm/HPC run provenance into a local outbox.",
    )
    hpc_commands = hpc_parser.add_subparsers(dest="hpc_command", required=True)

    init_parser = hpc_commands.add_parser("init", help="Create .lab-tracker/hpc.json.")
    init_parser.add_argument("--project", required=True, help="Lab Tracker project UUID.")
    init_parser.add_argument("--cluster", required=True, help="Cluster name, e.g. bouchet.")
    init_parser.add_argument(
        "--scheduler",
        default="slurm",
        help="Scheduler kind. Defaults to slurm.",
    )
    init_parser.add_argument(
        "--outbox",
        default=DEFAULT_OUTBOX,
        help=f"Outbox path. Defaults to {DEFAULT_OUTBOX}.",
    )
    init_parser.add_argument("--default-question", help="Optional candidate question UUID.")
    init_parser.add_argument("--config", help="Config path. Defaults to .lab-tracker/hpc.json.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    init_parser.set_defaults(func=_cmd_hpc_init, needs_client=False)

    submit_parser = hpc_commands.add_parser(
        "submit",
        help="Wrap an HPC submission command and write a submit event.",
    )
    _add_hpc_context_args(submit_parser)
    submit_parser.add_argument("--summary", help="Short run summary for review.")
    submit_parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after '--', usually sbatch ...",
    )
    submit_parser.set_defaults(func=_cmd_hpc_submit, needs_client=False)

    begin_parser = hpc_commands.add_parser("begin", help="Write a begin event from inside a job.")
    _add_hpc_context_args(begin_parser)
    begin_parser.add_argument("--run", help="Run id. Defaults to LAB_TRACKER_HPC_RUN_ID.")
    begin_parser.add_argument("--summary", help="Short run summary for review.")
    begin_parser.set_defaults(func=_cmd_hpc_begin, needs_client=False)

    finish_parser = hpc_commands.add_parser(
        "finish",
        help="Write a finish event from inside a job.",
    )
    _add_hpc_context_args(finish_parser)
    finish_parser.add_argument("--run", help="Run id. Defaults to LAB_TRACKER_HPC_RUN_ID.")
    finish_parser.add_argument("--exit-code", type=int, help="Job or script exit code.")
    finish_parser.add_argument("--state", help="Scheduler/run state, e.g. completed or failed.")
    finish_parser.add_argument("--manifest", help="Optional run manifest JSON.")
    finish_parser.add_argument("--log", action="append", default=[], help="Log file to excerpt.")
    finish_parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Artifact pointer URI/path. Repeatable; manifests allow richer summaries.",
    )
    finish_parser.add_argument(
        "--metric",
        action="append",
        default=[],
        help="Metric as key=value. Repeatable.",
    )
    finish_parser.add_argument("--summary", help="Short run summary for review.")
    finish_parser.set_defaults(func=_cmd_hpc_finish, needs_client=False)

    watch_parser = hpc_commands.add_parser(
        "watch",
        help="Import run manifests from designated output folders into the outbox.",
    )
    _add_hpc_context_args(watch_parser)
    watch_parser.add_argument("--root", required=True, help="Output folder to scan.")
    watch_parser.add_argument(
        "--pattern",
        default=DEFAULT_MANIFEST_PATTERN,
        help=f"Manifest filename pattern. Defaults to {DEFAULT_MANIFEST_PATTERN}.",
    )
    watch_parser.add_argument("--limit", type=int, help="Maximum manifests to process.")
    watch_parser.add_argument("--dry-run", action="store_true")
    watch_parser.set_defaults(func=_cmd_hpc_watch, needs_client=False)

    status_parser = hpc_commands.add_parser("status", help="Summarize local HPC outbox state.")
    status_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    status_parser.set_defaults(func=_cmd_hpc_status, needs_client=False)

    sync_parser = hpc_commands.add_parser(
        "sync",
        help="Sync outbox events into staged Lab Tracker evidence notes.",
    )
    sync_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--request-draft", action="store_true")
    sync_parser.add_argument("--limit", type=int, help="Maximum events to process.")
    sync_parser.set_defaults(func=_cmd_hpc_sync)


def _add_hpc_context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    parser.add_argument("--project", help="Override project UUID from config.")
    parser.add_argument("--question", help="Candidate question UUID recorded in evidence metadata.")
    parser.add_argument(
        "--dataset",
        action="append",
        default=[],
        help="Candidate dataset UUID. Repeatable.",
    )
    parser.add_argument("--tag", action="append", default=[], help="Tag. Repeatable.")


def _cmd_watch_init(args: argparse.Namespace) -> Any:
    config = watch_capture.init_config(
        project_id=args.project,
        outbox=args.outbox,
        config_path=args.config,
        force=args.force,
    )
    return {
        "command": "watch-init",
        "config": str(config.config_path),
        "outbox": str(config.outbox_path()),
        "project_id": config.project_id,
        "watches": config.watches,
    }


def _cmd_watch_scan(args: argparse.Namespace) -> Any:
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit must be 0 or greater.")
    try:
        config = watch_capture.load_config(config_path=args.config)
    except LTValidationError:
        if not args.root:
            raise
        config = watch_capture.WatchConfig(project_id=args.project)
    if not args.root:
        return watch_capture.scan_configured(
            config,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    return watch_capture.scan_watch(
        config,
        mode=args.mode,
        root=args.root,
        sink=args.sink,
        pattern=args.pattern,
        include_patterns=args.include or None,
        exclude_patterns=args.exclude or None,
        limit=args.limit,
        project_id=args.project,
        question_id=args.question,
        dataset_ids=args.dataset,
        tags=args.tag,
        session_id=args.session,
        source_provider=args.provider,
        adapter=args.adapter,
        dry_run=args.dry_run,
    )


def _cmd_watch_status(args: argparse.Namespace) -> Any:
    config = watch_capture.load_config(config_path=args.config)
    summary = watch_capture.outbox_status(config.outbox_path())
    summary.update(
        {
            "command": "watch-status",
            "config": str(config.config_path),
            "project_id": config.project_id,
        }
    )
    return summary


def _cmd_watch_sync(client: LabTracker, args: argparse.Namespace) -> Any:
    config = watch_capture.load_config(config_path=args.config)
    return watch_capture.sync_outbox(
        client,
        config,
        dry_run=args.dry_run,
        request_draft=args.request_draft,
        limit=args.limit,
    )


def _cmd_note(client: LabTracker, args: argparse.Namespace) -> Any:
    content = args.text if args.text is not None else Path(args.file).read_text(encoding="utf-8")
    metadata = json.loads(args.metadata) if args.metadata else None
    return client.upsert_note(
        project_id=args.project,
        content=content,
        targets=[_target(value) for value in args.target],
        metadata=metadata,
        status=args.status,
    )


def _cmd_quick(client: LabTracker, args: argparse.Namespace) -> Any:
    return client.quick_capture(args.text, project_id=args.project, source=args.source)


def _cmd_prime(client: LabTracker, args: argparse.Namespace) -> Any:
    prompt = args.prompt
    if prompt is None and args.if_research_facing:
        prompt = sys.stdin.read()
    if args.if_research_facing and not is_research_facing_prompt(prompt or ""):
        return None
    return client.next_questions(project_id=args.project, limit=args.limit)


def _cmd_export(client: LabTracker, args: argparse.Namespace) -> Any:
    from lab_tracker_client.export import export_project_provenance

    result = export_project_provenance(
        client,
        project_id=args.project,
        out_dir=args.out,
        since=args.since,
        until=args.until,
        data_root=args.data_root,
    )
    return result.to_dict()


def _cmd_import_folder(client: LabTracker, args: argparse.Namespace) -> Any:
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"--root must be an existing directory: {root}")
    if args.limit is not None and args.limit < 0:
        raise SystemExit("--limit must be 0 or greater.")
    candidates = _discover_import_files(
        root,
        include_patterns=args.include or ["*"],
        exclude_patterns=args.exclude,
    )
    matched_count = len(candidates)
    if args.limit is not None:
        candidates = candidates[: args.limit]
    summary: dict[str, Any] = {
        "command": "import-folder",
        "root": str(root),
        "project_id": args.project,
        "provider": args.provider,
        "adapter": args.adapter_name,
        "dry_run": bool(args.dry_run),
        "matched": matched_count,
        "processed": len(candidates),
        "imported": [],
        "skipped": [],
        "errors": [],
    }
    try:
        evidence_note_index = (
            client.build_evidence_note_index(project_id=args.project) if candidates else {}
        )
    except Exception as exc:  # noqa: BLE001 - report batch setup failures as JSON.
        for path in candidates:
            relative_path = path.relative_to(root).as_posix()
            summary["errors"].append(
                {
                    "path": str(path),
                    "source_external_id": _local_folder_external_id(root, relative_path),
                    "error": str(exc),
                }
            )
        return summary
    for path in candidates:
        relative_path = path.relative_to(root).as_posix()
        source_external_id = _local_folder_external_id(root, relative_path)
        try:
            result = client.import_evidence_file(
                project_id=args.project,
                file_path=path,
                source_provider=args.provider,
                source_external_id=source_external_id,
                source_uri=path.as_uri(),
                adapter=args.adapter_name,
                title=path.name,
                status=args.status,
                dry_run=args.dry_run,
                evidence_note_index=evidence_note_index,
            )
        except Exception as exc:  # noqa: BLE001 - continue importing remaining inbox files.
            summary["errors"].append(
                {
                    "path": str(path),
                    "source_external_id": source_external_id,
                    "error": str(exc),
                }
            )
            continue
        item = result.to_dict()
        if result.action == "imported":
            summary["imported"].append(item)
        else:
            summary["skipped"].append(item)
    return summary


def _cmd_hpc_init(args: argparse.Namespace) -> Any:
    config = init_config(
        project_id=args.project,
        cluster=args.cluster,
        scheduler=args.scheduler,
        outbox=args.outbox,
        default_question_id=args.default_question,
        config_path=args.config,
        force=args.force,
    )
    return {
        "command": "hpc-init",
        "config": str(config.config_path),
        "outbox": str(config.outbox_path()),
        "project_id": config.project_id,
        "cluster": config.cluster,
        "scheduler": config.scheduler,
    }


def _cmd_hpc_submit(args: argparse.Namespace) -> Any:
    config = load_config(config_path=args.config)
    return run_submit_command(
        config,
        _strip_remainder(args.command),
        project_id=args.project,
        question_id=args.question,
        dataset_ids=args.dataset,
        tags=args.tag,
        summary=args.summary,
    )


def _cmd_hpc_begin(args: argparse.Namespace) -> Any:
    config = load_config(config_path=args.config)
    event, path = begin_event(
        config,
        run_id=args.run,
        project_id=args.project,
        question_id=args.question,
        dataset_ids=args.dataset,
        tags=args.tag,
        summary=args.summary,
    )
    return {
        "command": "hpc-begin",
        "run_id": event["run_id"],
        "event_type": event["event_type"],
        "event_path": str(path),
        "outbox": str(config.outbox_path()),
    }


def _cmd_hpc_finish(args: argparse.Namespace) -> Any:
    config = load_config(config_path=args.config)
    event, path = finish_event(
        config,
        run_id=args.run,
        exit_code=args.exit_code,
        state=args.state,
        manifest=args.manifest,
        logs=args.log,
        artifacts=args.artifact,
        metrics=args.metric,
        project_id=args.project,
        question_id=args.question,
        dataset_ids=args.dataset,
        tags=args.tag,
        summary=args.summary,
    )
    return {
        "command": "hpc-finish",
        "run_id": event["run_id"],
        "event_type": event["event_type"],
        "event_path": str(path),
        "outbox": str(config.outbox_path()),
    }


def _cmd_hpc_watch(args: argparse.Namespace) -> Any:
    config = load_config(config_path=args.config)
    return watch_manifests(
        config,
        root=args.root,
        pattern=args.pattern,
        limit=args.limit,
        project_id=args.project,
        question_id=args.question,
        dataset_ids=args.dataset,
        tags=args.tag,
        dry_run=args.dry_run,
    )


def _cmd_hpc_status(args: argparse.Namespace) -> Any:
    config = load_config(config_path=args.config)
    summary = outbox_status(config.outbox_path())
    summary.update(
        {
            "command": "hpc-status",
            "config": str(config.config_path),
            "project_id": config.project_id,
            "cluster": config.cluster,
            "scheduler": config.scheduler,
        }
    )
    return summary


def _cmd_hpc_sync(client: LabTracker, args: argparse.Namespace) -> Any:
    config = load_config(config_path=args.config)
    return sync_outbox(
        client,
        config,
        dry_run=args.dry_run,
        request_draft=args.request_draft,
        limit=args.limit,
    )


def _cmd_list_questions(client: LabTracker, args: argparse.Namespace) -> Any:
    return client.list_questions(
        project_id=args.project,
        status=args.status,
        search=args.search,
    )


def _cmd_list_notes(client: LabTracker, args: argparse.Namespace) -> Any:
    return client.list_notes(project_id=args.project, status=args.status)


def _cmd_doctor(args: argparse.Namespace) -> Any:
    from lab_tracker.cli import _doctor

    return _doctor(args.target)


def _cmd_update(args: argparse.Namespace) -> Any:
    from lab_tracker.cli import update_consumer_repo

    result = update_consumer_repo(args.target, yes=args.yes, dry_run=args.dry_run)
    return result.as_dict()


def _target(value: str) -> EntityRef:
    entity_type, separator, entity_id = value.partition(":")
    if not separator:
        raise SystemExit(f"Bad target {value!r}; expected kind:uuid.")
    return EntityRef(entity_type, entity_id)


def _discover_import_files(
    root: Path,
    *,
    include_patterns: list[str],
    exclude_patterns: list[str],
) -> list[Path]:
    return watch_capture.discover_files(
        root,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
    )


def _matches_any(path: Path, *, root: Path, patterns: list[str]) -> bool:
    return watch_capture.matches_any(path, root=root, patterns=patterns)


def _local_folder_external_id(root: Path, relative_path: str) -> str:
    return watch_capture.local_folder_external_id(root, relative_path)


def _strip_remainder(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


def _jsonable(value: Any) -> Any:
    if isinstance(value, EvidenceImportResult):
        return value.to_dict()
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _payload_exit_code(payload: Any) -> int:
    if (
        isinstance(payload, dict)
        and payload.get("command") == "import-folder"
        and payload.get("errors")
    ):
        return 1
    if isinstance(payload, dict) and payload.get("command") == "doctor":
        targets = payload.get("targets")
        if not isinstance(targets, list):
            return 1
        if not all(isinstance(target, dict) and not target.get("drifted") for target in targets):
            return 1
    if isinstance(payload, dict) and payload.get("command") == "hpc-submit":
        return int(payload.get("exit_code") or 0)
    if (
        isinstance(payload, dict)
        and payload.get("command") in {"hpc-sync", "hpc-watch"}
        and payload.get("errors")
    ):
        return 1
    if (
        isinstance(payload, dict)
        and payload.get("command") in {"watch-scan", "watch-sync"}
        and payload.get("errors")
    ):
        return 1
    return 0
