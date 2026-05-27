"""Small CLI for the Lab Tracker consumer client."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from lab_tracker_client.client import (
    NOTE_STATUS_VALUES,
    EntityRef,
    LabTracker,
    ids,
)


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    client = LabTracker.from_env()
    try:
        payload = args.func(client, args)
    finally:
        client.close()
    if payload is not None:
        print(json.dumps(_jsonable(payload), indent=2))


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


def _cmd_list_questions(client: LabTracker, args: argparse.Namespace) -> Any:
    return client.list_questions(
        project_id=args.project,
        status=args.status,
        search=args.search,
    )


def _cmd_list_notes(client: LabTracker, args: argparse.Namespace) -> Any:
    return client.list_notes(project_id=args.project, status=args.status)


def _target(value: str) -> EntityRef:
    entity_type, separator, entity_id = value.partition(":")
    if not separator:
        raise SystemExit(f"Bad target {value!r}; expected kind:uuid.")
    return EntityRef(entity_type, entity_id)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
