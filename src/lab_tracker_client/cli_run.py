"""``lt run``: the command-line registration for the run wrapper.

Unlike other ``lt`` verbs, ``lt run`` prints no JSON: it must be transparent
in a pipeline, so stdout belongs to the wrapped command and ``lt`` exits with
exactly the command's exit code (see :mod:`lab_tracker_client.run_capture`).
"""

from __future__ import annotations

import argparse
import sys
from typing import NoReturn

from lab_tracker_client.run_capture import RunOptions, run_command

RUN_USAGE_ERROR = (
    "lt run: a command is required after '--' (lt run [options] -- <command> [args...])"
)


def add_run_parsers(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``lt run [options] -- <command> [args...]``."""

    parser = subcommands.add_parser(
        "run",
        help=(
            "Run a command and record what ran (argv, git state, worktree tree, outputs) "
            "as one staged note."
        ),
        description=(
            "Run a command with inherited stdio and exit with its exit code. When the "
            "project is bound (--project, LAB_TRACKER_PROJECT_ID, or the checkout's "
            "lt_ids.json) the run is queued as one staged note in the checkout's watch "
            "outbox and synced best effort."
        ),
    )
    parser.add_argument("--project", help="Project UUID. Defaults to the bound project.")
    parser.add_argument(
        "--session",
        help="Session UUID or link code the run belongs to (a declared note target).",
    )
    parser.add_argument(
        "--question",
        help="Candidate question UUID (a declared note target).",
    )
    parser.add_argument("--label", help="Short name for the run, used as the note title.")
    parser.add_argument(
        "--output",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "Folder (or file) whose created or modified files are recorded as artifact "
            "pointers. Repeatable. Default: none."
        ),
    )
    parser.add_argument(
        "--no-drain",
        action="store_true",
        help="Queue the run in the outbox only; skip the best-effort sync.",
    )
    parser.add_argument(
        "--request-draft",
        action="store_true",
        help="Ask for a graph draft of this run's note when it syncs.",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="The command to run, after '--'.",
    )
    parser.set_defaults(func=_cmd_run, needs_client=False)


def _cmd_run(args: argparse.Namespace) -> NoReturn:
    command = list(args.command or [])
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print(RUN_USAGE_ERROR, file=sys.stderr)
        raise SystemExit(2)
    options = RunOptions(
        project_id=args.project,
        session=args.session,
        question_id=args.question,
        label=args.label,
        outputs=tuple(args.output or ()),
        drain=not args.no_drain,
        request_draft=args.request_draft,
    )
    raise SystemExit(run_command(command, options))


__all__ = ["RUN_USAGE_ERROR", "add_run_parsers"]
