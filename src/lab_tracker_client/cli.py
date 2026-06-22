"""Small CLI for the Lab Tracker consumer client."""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path
from typing import Any

from lab_tracker.assistant_next_questions import is_research_facing_prompt
from lab_tracker_client.client import (
    NOTE_STATUS_VALUES,
    EntityRef,
    EvidenceImportResult,
    LabTracker,
    ids,
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
    files = [path for path in root.rglob("*") if not path.is_symlink() and path.is_file()]
    return [
        path
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix())
        if _matches_any(path, root=root, patterns=include_patterns)
        and not _matches_any(path, root=root, patterns=exclude_patterns)
    ]


def _matches_any(path: Path, *, root: Path, patterns: list[str]) -> bool:
    if not patterns:
        return False
    relative_path = path.relative_to(root).as_posix()
    return any(
        fnmatch.fnmatch(relative_path, pattern) or fnmatch.fnmatch(path.name, pattern)
        for pattern in patterns
    )


def _local_folder_external_id(root: Path, relative_path: str) -> str:
    return f"{root.as_uri()}::{relative_path}"


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
    return 0
