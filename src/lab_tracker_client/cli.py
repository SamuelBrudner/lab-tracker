"""Small CLI for the Lab Tracker consumer client."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any

import lab_tracker_client.auth as auth_helpers
import lab_tracker_client.git_capture as git_capture
import lab_tracker_client.hooks as hook_install
import lab_tracker_client.registry as repo_registry
import lab_tracker_client.repo as repo_capture
import lab_tracker_client.schedule as schedule_helpers
import lab_tracker_client.setup as setup_helpers
import lab_tracker_client.watch as watch_capture
from lab_tracker import repository_conventions as repo_context
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
        "--all",
        action="store_true",
        help="Sweep every repo recorded in ~/.lab-tracker/applied-repos.json.",
    )
    doctor_parser.add_argument(
        "--prune-missing",
        action="store_true",
        help="With --all: drop registry entries whose directory no longer exists.",
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
    update_parser.add_argument(
        "--install-skills",
        action="store_true",
        help="Also refresh the lab-tracker-setup skill in the Claude and Codex homes.",
    )
    update_parser.set_defaults(func=_cmd_update, needs_client=False)

    _add_setup_parsers(subcommands)
    _add_auth_parsers(subcommands)
    _add_project_parsers(subcommands)
    _add_git_parsers(subcommands)
    _add_hooks_parsers(subcommands)
    _add_agent_context_parsers(subcommands)

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
    note_parser.add_argument(
        "--status",
        choices=NOTE_STATUS_VALUES,
        default=None,
        help=(
            "Note status. Defaults to the server's staged state (the human "
            "review gate); pass --status committed to commit explicitly."
        ),
    )
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
    _add_outbox_parsers(subcommands)
    _add_hpc_parsers(subcommands)
    _add_repo_parsers(subcommands)

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


def _add_setup_parsers(subcommands: argparse._SubParsersAction) -> None:
    setup_parser = subcommands.add_parser(
        "setup",
        help="Inspect and (with consent) configure Lab Tracker capture for this machine/repo.",
    )
    setup_commands = setup_parser.add_subparsers(dest="setup_command", required=True)

    status_parser = setup_commands.add_parser(
        "status",
        help="Read-only inventory: server, profile, repo scaffold, watch, hpc, hooks.",
    )
    status_parser.add_argument(
        "--target",
        default=".",
        help="Consumer repo path to inspect. Defaults to the current directory.",
    )
    status_parser.add_argument(
        "--brief",
        action="store_true",
        help="One-line summary plus suggestions; sized for session hooks.",
    )
    status_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Suppress errors so agent/session hooks never block.",
    )
    status_parser.set_defaults(func=_cmd_setup_status, needs_client=False)

    init_parser = setup_commands.add_parser(
        "init",
        help="Scaffold Lab Tracker integration files into a consumer repo.",
    )
    init_parser.add_argument(
        "--target",
        default=".",
        help="Consumer repo path to scaffold. Defaults to the current directory.",
    )
    init_parser.add_argument(
        "--project-name",
        default=None,
        help="Optional project name recorded in the lt_ids.json placeholder.",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing scaffolded files.",
    )
    init_parser.add_argument(
        "--yes",
        action="store_true",
        help="Consent to recommended managed code-conventions blocks.",
    )
    init_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show scaffold and managed-block diffs without writing files.",
    )
    init_parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Strip Lab Tracker managed blocks while preserving surrounding content.",
    )
    init_parser.add_argument(
        "--install-skills",
        action="store_true",
        help=(
            "Also render the lab-tracker-setup skill into the Claude and Codex "
            "skill homes (with --uninstall: remove it)."
        ),
    )
    init_parser.set_defaults(func=_cmd_setup_init, needs_client=False)

    connect_parser = setup_commands.add_parser(
        "connect",
        help="Persist server URL / default project (token only with --save-token).",
    )
    connect_parser.add_argument("--base-url", help="Lab Tracker API base URL to persist.")
    connect_parser.add_argument("--project", help="Default project UUID to persist.")
    connect_parser.add_argument(
        "--save-token",
        action="store_true",
        help="Also persist an access token (separate, explicit consent).",
    )
    connect_parser.add_argument(
        "--token",
        help="Access token for --save-token. Defaults to LAB_TRACKER_ACCESS_TOKEN.",
    )
    connect_parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the persisted connection profile.",
    )
    connect_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the profile diff without writing.",
    )
    connect_parser.add_argument(
        "--yes",
        action="store_true",
        help="Consent to writing the machine-level connection profile.",
    )
    connect_parser.set_defaults(func=_cmd_setup_connect, needs_client=False)

    verify_client_parser = setup_commands.add_parser(
        "verify-client",
        help="Verify this environment's client was installed from the server's exact revision.",
    )
    verify_client_parser.add_argument(
        "--expected-revision",
        required=True,
        help="Full source revision shown by the Lab Tracker Setup page.",
    )
    verify_client_parser.set_defaults(func=_cmd_setup_verify_client, needs_client=False)

    verify_mcp_parser = setup_commands.add_parser(
        "verify-mcp",
        help="Launch lt-mcp and run initialize, health, and authenticated project-read checks.",
    )
    verify_mcp_parser.add_argument(
        "--expected-revision",
        required=True,
        help="Full source revision shown by the Lab Tracker Setup page.",
    )
    verify_mcp_parser.add_argument(
        "--command",
        default="lt-mcp",
        help="MCP executable to launch. Defaults to lt-mcp on PATH.",
    )
    verify_mcp_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Maximum seconds for the MCP exchange. Defaults to 15.",
    )
    verify_mcp_parser.set_defaults(func=_cmd_setup_verify_mcp, needs_client=False)

    schedule_parser = setup_commands.add_parser(
        "schedule",
        help="Register an OS-scheduler job running 'lt watch run' for this repo's config.",
    )
    schedule_parser.add_argument(
        "--config",
        help="Watch config path. Defaults to ./.lab-tracker/watch.json.",
    )
    schedule_parser.add_argument(
        "--interval-minutes",
        type=int,
        default=schedule_helpers.DEFAULT_INTERVAL_MINUTES,
        help=f"Recurrence interval. Defaults to {schedule_helpers.DEFAULT_INTERVAL_MINUTES}.",
    )
    schedule_parser.add_argument(
        "--lt-path",
        help="Absolute lt executable for the scheduled command. Defaults to the installed lt.",
    )
    schedule_parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the scheduled job for this config.",
    )
    schedule_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the scheduler change without applying it.",
    )
    schedule_parser.add_argument(
        "--yes",
        action="store_true",
        help="Consent to modifying the OS scheduler.",
    )
    schedule_parser.set_defaults(func=_cmd_setup_schedule, needs_client=False)


def _add_project_parsers(subcommands: argparse._SubParsersAction) -> None:
    project_parser = subcommands.add_parser(
        "project",
        help="Project-level helpers for consumer repos.",
    )
    project_commands = project_parser.add_subparsers(dest="project_command", required=True)

    bind_parser = project_commands.add_parser(
        "bind",
        help="Resolve or create a project and write its id into lt_ids.json.",
    )
    resolver = bind_parser.add_mutually_exclusive_group(required=True)
    resolver.add_argument("--project-id", help="Existing project UUID to bind.")
    resolver.add_argument("--name", help="Project name to resolve (exact match).")
    bind_parser.add_argument(
        "--create",
        action="store_true",
        help="Create the project when no exact --name match exists.",
    )
    bind_parser.add_argument(
        "--ids",
        default="lt_ids.json",
        help="Path to lt_ids.json. Defaults to ./lt_ids.json.",
    )
    bind_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the lt_ids.json diff without writing.",
    )
    bind_parser.add_argument(
        "--yes",
        action="store_true",
        help="Consent to writing lt_ids.json.",
    )
    bind_parser.set_defaults(func=_cmd_project_bind)


def _add_git_parsers(subcommands: argparse._SubParsersAction) -> None:
    git_parser = subcommands.add_parser(
        "git",
        help="Capture git commits as queued Lab Tracker evidence.",
    )
    git_commands = git_parser.add_subparsers(dest="git_command", required=True)

    snapshot_parser = git_commands.add_parser(
        "snapshot",
        help="Queue a commit as staged evidence (outbox-backed), then best-effort sync.",
    )
    snapshot_parser.add_argument("--commit", default="HEAD", help="Commit-ish. Defaults to HEAD.")
    snapshot_parser.add_argument("--repo", default=".", help="Repository path. Defaults to cwd.")
    snapshot_parser.add_argument(
        "--project",
        help="Project UUID. Defaults to LAB_TRACKER_PROJECT_ID, watch config, or lt_ids.json.",
    )
    snapshot_parser.add_argument("--question", help="Candidate question UUID.")
    snapshot_parser.add_argument(
        "--dataset",
        action="append",
        default=[],
        help="Candidate dataset UUID. Repeatable.",
    )
    snapshot_parser.add_argument("--tag", action="append", default=[], help="Tag. Repeatable.")
    snapshot_parser.add_argument("--config", help="Watch config path. Defaults to discovered.")
    snapshot_parser.add_argument(
        "--max-diff-lines",
        type=int,
        help="Maximum diff lines. Defaults to LAB_TRACKER_GIT_MAX_DIFF_LINES or 800.",
    )
    snapshot_parser.add_argument(
        "--context-lines",
        type=int,
        help="Unified diff context lines. Defaults to LAB_TRACKER_GIT_CONTEXT_LINES or 3.",
    )
    snapshot_parser.add_argument(
        "--request-draft",
        action="store_true",
        help="Request an analysis graph draft when the snapshot syncs.",
    )
    snapshot_parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Queue the event only; skip the best-effort sync.",
    )
    snapshot_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Swallow all errors so a post-commit hook can never block a commit.",
    )
    snapshot_parser.set_defaults(func=_cmd_git_snapshot, needs_client=False)


def _add_hooks_parsers(subcommands: argparse._SubParsersAction) -> None:
    hooks_parser = subcommands.add_parser(
        "hooks",
        help="Enroll repositories with the Lab Tracker post-commit snapshot hook.",
    )
    hooks_commands = hooks_parser.add_subparsers(dest="hooks_command", required=True)

    install_parser = hooks_commands.add_parser(
        "install",
        help="Write the managed post-commit block (upgrades legacy installs in place).",
    )
    install_parser.add_argument("--repo", default=".", help="Repository path. Defaults to cwd.")
    install_parser.add_argument(
        "--project",
        help="Project UUID baked into the hook as an overridable default.",
    )
    install_parser.add_argument(
        "--base-url",
        help="API base URL baked into the hook as an overridable default.",
    )
    install_parser.add_argument(
        "--lt-path",
        help="Absolute lt executable for the hook body. Defaults to the installed lt.",
    )
    install_parser.add_argument(
        "--no-request-draft",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    install_parser.add_argument(
        "--force",
        action="store_true",
        help="Append the managed block to a pre-existing unmanaged hook.",
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the hook diff without writing.",
    )
    install_parser.add_argument(
        "--yes",
        action="store_true",
        help="Consent to writing the repository's post-commit hook.",
    )
    install_parser.set_defaults(func=_cmd_hooks_install, needs_client=False)

    uninstall_parser = hooks_commands.add_parser(
        "uninstall",
        help="Strip the managed block, preserving any surrounding hook content.",
    )
    uninstall_parser.add_argument("--repo", default=".", help="Repository path. Defaults to cwd.")
    uninstall_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without writing.",
    )
    uninstall_parser.add_argument(
        "--yes",
        action="store_true",
        help="Consent to modifying the repository's post-commit hook.",
    )
    uninstall_parser.set_defaults(func=_cmd_hooks_uninstall, needs_client=False)

    status_parser = hooks_commands.add_parser(
        "status",
        help="Report hook presence, managed block, and lt-path health for a repo.",
    )
    status_parser.add_argument("--repo", default=".", help="Repository path. Defaults to cwd.")
    status_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Suppress errors for hook/scheduler invocations.",
    )
    status_parser.set_defaults(func=_cmd_hooks_status, needs_client=False)


def _add_agent_context_parsers(subcommands: argparse._SubParsersAction) -> None:
    context_parser = subcommands.add_parser(
        "agent-context",
        help="Select repo convention files for Lab Tracker draft context.",
    )
    context_commands = context_parser.add_subparsers(
        dest="agent_context_command",
        required=True,
    )

    status_parser = context_commands.add_parser(
        "status",
        help="Discover convention files and show the enrolled selection (read-only).",
    )
    status_parser.add_argument("--repo", default=".", help="Repository path. Defaults to cwd.")
    status_parser.set_defaults(func=_cmd_agent_context_status, needs_client=False)

    add_parser = context_commands.add_parser(
        "add",
        help="Enroll one tracked UTF-8 convention file.",
    )
    add_parser.add_argument("path", help="Repository-relative convention file path.")
    add_parser.add_argument("--repo", default=".", help="Repository path. Defaults to cwd.")
    add_parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    add_parser.add_argument(
        "--yes",
        action="store_true",
        help="Consent to storing this repo-local context selection.",
    )
    add_parser.set_defaults(func=_cmd_agent_context_add, needs_client=False)

    remove_parser = context_commands.add_parser(
        "remove",
        help="Stop including one enrolled convention file.",
    )
    remove_parser.add_argument("path", help="Repository-relative convention file path.")
    remove_parser.add_argument("--repo", default=".", help="Repository path. Defaults to cwd.")
    remove_parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    remove_parser.add_argument(
        "--yes",
        action="store_true",
        help="Consent to changing the repo-local context selection.",
    )
    remove_parser.set_defaults(func=_cmd_agent_context_remove, needs_client=False)


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

    add_parser = watch_commands.add_parser(
        "add",
        help="Register a folder in watch.json without hand-editing JSON.",
    )
    add_parser.add_argument("root", help="Folder to watch.")
    add_parser.add_argument(
        "--mode",
        choices=sorted(watch_capture.ALLOWED_MODES),
        default=watch_capture.MODE_FILES,
        help="Capture mode. Defaults to files.",
    )
    add_parser.add_argument(
        "--sink",
        choices=sorted(watch_capture.ALLOWED_SINKS),
        default=watch_capture.SINK_STAGED_NOTE,
        help="Sync sink for captured events. Defaults to staged-note.",
    )
    add_parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Glob for relative paths to include. Repeatable.",
    )
    add_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob for relative paths to exclude. Repeatable.",
    )
    add_parser.add_argument("--name", help="Optional label for this watch entry.")
    add_parser.add_argument("--project", help="Project UUID override for this watch.")
    add_parser.add_argument("--question", help="Candidate question UUID recorded in metadata.")
    add_parser.add_argument("--session", help="Session UUID for the acquisition-output sink.")
    add_parser.add_argument("--tag", action="append", default=[], help="Tag. Repeatable.")
    add_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    add_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the resulting entry without writing watch.json.",
    )
    add_parser.set_defaults(func=_cmd_watch_add, needs_client=False)

    list_parser = watch_commands.add_parser("list", help="List registered watch entries.")
    list_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    list_parser.set_defaults(func=_cmd_watch_list, needs_client=False)

    remove_parser = watch_commands.add_parser(
        "remove",
        help="Remove watch entries matching a root folder.",
    )
    remove_parser.add_argument("root", help="Watched folder to remove.")
    remove_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    remove_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without writing watch.json.",
    )
    remove_parser.set_defaults(func=_cmd_watch_remove, needs_client=False)

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
    scan_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Suppress errors and error exit codes for hook/scheduler runs.",
    )
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
    sync_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Suppress errors and error exit codes for hook/scheduler runs.",
    )
    sync_parser.set_defaults(func=_cmd_watch_sync)

    run_parser = watch_commands.add_parser(
        "run",
        help="Scan configured watches then sync the outbox in one process (for schedulers).",
    )
    run_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    run_parser.add_argument("--request-draft", action="store_true")
    run_parser.add_argument("--limit", type=int, help="Maximum items per stage.")
    run_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Suppress errors and error exit codes for scheduler runs.",
    )
    run_parser.set_defaults(func=_cmd_watch_run)


def _add_outbox_parsers(subcommands: argparse._SubParsersAction) -> None:
    outbox_parser = subcommands.add_parser(
        "outbox",
        help=(
            "Inspect and drain the local capture outbox directly, without a "
            "watch.json (works for commit-snapshot events)."
        ),
    )
    outbox_commands = outbox_parser.add_subparsers(dest="outbox_command", required=True)

    status_parser = outbox_commands.add_parser(
        "status",
        help="Summarize queued outbox events (count, oldest, last error).",
    )
    status_parser.add_argument(
        "--repo",
        default=".",
        help="Repository whose .lab-tracker/outbox to inspect. Defaults to cwd.",
    )
    status_parser.add_argument("--config", help="Watch config path. Optional; not required.")
    status_parser.set_defaults(func=_cmd_outbox_status, needs_client=False)

    sync_parser = outbox_commands.add_parser(
        "sync",
        help="Drain queued outbox events into Lab Tracker (no watch.json required).",
    )
    sync_parser.add_argument(
        "--repo",
        default=".",
        help="Repository whose .lab-tracker/outbox to drain. Defaults to cwd.",
    )
    sync_parser.add_argument("--config", help="Watch config path. Optional; not required.")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--request-draft", action="store_true")
    sync_parser.add_argument("--limit", type=int, help="Maximum events to process.")
    sync_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Suppress errors and error exit codes for hook/scheduler runs.",
    )
    sync_parser.set_defaults(func=_cmd_outbox_sync)


def _add_auth_parsers(subcommands: argparse._SubParsersAction) -> None:
    auth_parser = subcommands.add_parser(
        "auth",
        help="Audit lab-tracker MCP auth across CLI, Desktop, and repo surfaces.",
    )
    auth_commands = auth_parser.add_subparsers(dest="auth_command", required=True)

    doctor_parser = auth_commands.add_parser(
        "doctor",
        help=(
            "Enumerate every lab-tracker MCP registration and flag deprecated "
            "username/password auth (drift that causes silent 401s)."
        ),
    )
    doctor_parser.add_argument(
        "--target",
        default=".",
        help="Repo whose local MCP configs to inspect. Defaults to the cwd.",
    )
    doctor_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Suppress the nonzero exit for deprecated configs (for hooks).",
    )
    doctor_parser.set_defaults(func=_cmd_auth_doctor, needs_client=False)


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
    _add_capture_context_args(submit_parser)
    submit_parser.add_argument("--summary", help="Short run summary for review.")
    submit_parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after '--', usually sbatch ...",
    )
    submit_parser.set_defaults(func=_cmd_hpc_submit, needs_client=False)

    begin_parser = hpc_commands.add_parser("begin", help="Write a begin event from inside a job.")
    _add_capture_context_args(begin_parser)
    begin_parser.add_argument("--run", help="Run id. Defaults to LAB_TRACKER_HPC_RUN_ID.")
    begin_parser.add_argument("--summary", help="Short run summary for review.")
    begin_parser.set_defaults(func=_cmd_hpc_begin, needs_client=False)

    finish_parser = hpc_commands.add_parser(
        "finish",
        help="Write a finish event from inside a job.",
    )
    _add_capture_context_args(finish_parser)
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
    _add_capture_context_args(watch_parser)
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


def _add_capture_context_args(parser: argparse.ArgumentParser) -> None:
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


def _add_repo_parsers(subcommands: argparse._SubParsersAction) -> None:
    repo_parser = subcommands.add_parser(
        "repo",
        help="Capture analysis-repo commit provenance into a local outbox.",
    )
    repo_commands = repo_parser.add_subparsers(dest="repo_command", required=True)

    init_parser = repo_commands.add_parser("init", help="Create .lab-tracker/repo.json.")
    init_parser.add_argument("--project", required=True, help="Lab Tracker project UUID.")
    init_parser.add_argument(
        "--outbox",
        default=repo_capture.DEFAULT_OUTBOX,
        help=f"Outbox path. Defaults to {repo_capture.DEFAULT_OUTBOX}.",
    )
    init_parser.add_argument("--default-question", help="Optional candidate question UUID.")
    init_parser.add_argument("--config", help="Config path. Defaults to .lab-tracker/repo.json.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    init_parser.set_defaults(func=_cmd_repo_init, needs_client=False)

    report_parser = repo_commands.add_parser(
        "report",
        help="Record the current repo state (commit, dirty, remote) as an outbox event.",
    )
    _add_capture_context_args(report_parser)
    report_parser.add_argument(
        "--run",
        help="Run id. Defaults to LAB_TRACKER_REPO_RUN_ID, then the commit SHA.",
    )
    report_parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Produced file to fingerprint as an artifact pointer. Repeatable.",
    )
    report_parser.add_argument("--summary", help="Short summary for review.")
    report_parser.add_argument(
        "--fail-silent",
        action="store_true",
        dest="fail_silent",
        help="Exit quietly on any error (for git hooks; never blocks a commit).",
    )
    report_parser.set_defaults(func=_cmd_repo_report, needs_client=False)

    finish_parser = repo_commands.add_parser(
        "finish",
        help="Record a finished analysis run with its produced artifacts.",
    )
    _add_capture_context_args(finish_parser)
    finish_parser.add_argument(
        "--run",
        help="Run id. Defaults to LAB_TRACKER_REPO_RUN_ID, then the commit SHA.",
    )
    finish_parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Produced file to fingerprint as an artifact pointer. Repeatable.",
    )
    finish_parser.add_argument("--summary", help="Short run summary for review.")
    finish_parser.set_defaults(func=_cmd_repo_finish, needs_client=False)

    status_parser = repo_commands.add_parser("status", help="Summarize local repo outbox state.")
    status_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    status_parser.set_defaults(func=_cmd_repo_status, needs_client=False)

    sync_parser = repo_commands.add_parser(
        "sync",
        help="Sync outbox events into staged Lab Tracker evidence notes.",
    )
    sync_parser.add_argument("--config", help="Config path. Defaults to discovered config.")
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--request-draft", action="store_true")
    sync_parser.add_argument("--limit", type=int, help="Maximum events to process.")
    sync_parser.set_defaults(func=_cmd_repo_sync)

    hook_parser = repo_commands.add_parser(
        "install-hook",
        help="Install the managed post-commit hook that runs 'lt repo report'.",
    )
    hook_parser.add_argument("--repo", default=".", help="Repository path. Defaults to cwd.")
    hook_parser.add_argument(
        "--config",
        help="Repo config to pin into the hook. Defaults to discovered config.",
    )
    hook_parser.add_argument(
        "--lt-command",
        help="Path to the lt executable the hook should call. Defaults to the lt on PATH.",
    )
    hook_parser.add_argument(
        "--force",
        action="store_true",
        help="Append to an existing non-managed post-commit hook.",
    )
    hook_parser.set_defaults(func=_cmd_repo_install_hook, needs_client=False)


def _cmd_repo_init(args: argparse.Namespace) -> Any:
    config = repo_capture.init_config(
        project_id=args.project,
        outbox=args.outbox,
        default_question_id=args.default_question,
        config_path=args.config,
        force=args.force,
    )
    return {
        "command": "repo-init",
        "config": str(config.config_path),
        "outbox": str(config.outbox_path()),
        "project_id": config.project_id,
    }


def _cmd_repo_report(args: argparse.Namespace) -> Any:
    return _repo_capture_payload("repo-report", args, event_type="commit")


def _cmd_repo_finish(args: argparse.Namespace) -> Any:
    return _repo_capture_payload("repo-finish", args, event_type="finish")


def _repo_capture_payload(command: str, args: argparse.Namespace, *, event_type: str) -> Any:
    config = repo_capture.load_config(config_path=args.config)
    artifacts = [repo_capture.artifact_from_path(item) for item in args.artifact]
    event, path, action = repo_capture.capture_commit(
        config,
        event_type=event_type,
        run_id=args.run,
        project_id=args.project,
        question_id=args.question,
        dataset_ids=args.dataset,
        tags=args.tag,
        artifacts=artifacts,
        summary=args.summary,
    )
    return {
        "command": command,
        "action": action,
        "run_id": event["run_id"],
        "event_type": event["event_type"],
        "git_commit": event["source"].get("git_commit", ""),
        "git_dirty": event["source"].get("git_dirty", False),
        "artifact_count": len(event["artifacts"]),
        "event_path": str(path),
        "outbox": str(config.outbox_path()),
    }


def _cmd_repo_status(args: argparse.Namespace) -> Any:
    config = repo_capture.load_config(config_path=args.config)
    summary = repo_capture.outbox_status(config.outbox_path())
    summary.update(
        {
            "command": "repo-status",
            "config": str(config.config_path),
            "project_id": config.project_id,
        }
    )
    return summary


def _cmd_repo_sync(client: LabTracker, args: argparse.Namespace) -> Any:
    config = repo_capture.load_config(config_path=args.config)
    return repo_capture.sync_outbox(
        client,
        config,
        dry_run=args.dry_run,
        request_draft=args.request_draft,
        limit=args.limit,
    )


def _cmd_repo_install_hook(args: argparse.Namespace) -> Any:
    return repo_capture.install_post_commit_hook(
        args.repo,
        lt_command=args.lt_command,
        config_path=args.config,
        force=args.force,
    )


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


def _cmd_watch_add(args: argparse.Namespace) -> Any:
    payload = watch_capture.add_watch(
        root=args.root,
        mode=args.mode,
        sink=args.sink,
        include=args.include or None,
        exclude=args.exclude or None,
        name=args.name,
        project_id=args.project,
        question_id=args.question,
        session_id=args.session,
        tags=args.tag or None,
        config_path=args.config,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        # Registry entries are sweep metadata for `lt doctor --all`, never an
        # auto-apply trigger; recording is fail-soft.
        repo_registry.record_repo(
            repo_registry.repo_root_for_config(payload["config"]), "watch-add"
        )
    return payload


def _cmd_watch_list(args: argparse.Namespace) -> Any:
    config = watch_capture.load_config(config_path=args.config)
    return {
        "command": "watch-list",
        "config": str(config.config_path),
        "project_id": config.project_id,
        "watches": config.watches,
    }


def _cmd_watch_remove(args: argparse.Namespace) -> Any:
    return watch_capture.remove_watch(
        root=args.root,
        config_path=args.config,
        dry_run=args.dry_run,
    )


def _cmd_setup_status(args: argparse.Namespace) -> Any:
    return setup_helpers.setup_status(args.target, brief=args.brief)


def _cmd_setup_init(args: argparse.Namespace) -> Any:
    from lab_tracker.cli import init_consumer_repo

    mcp_base_url, mcp_base_url_source = setup_helpers.resolved_base_url_for_setup()
    result = init_consumer_repo(
        args.target,
        project_name=args.project_name,
        mcp_base_url=mcp_base_url,
        force=args.force,
        yes=args.yes,
        dry_run=args.dry_run,
        uninstall=args.uninstall,
        install_skills=args.install_skills,
    )
    payload = result.as_dict()
    payload["command"] = "setup-init"
    payload["mcp_base_url"] = mcp_base_url
    payload["mcp_base_url_source"] = mcp_base_url_source
    return payload


def _cmd_setup_connect(args: argparse.Namespace) -> Any:
    if not (args.yes or args.dry_run):
        raise SystemExit(
            "lt setup connect writes the machine-level connection profile; "
            "pass --yes to consent or --dry-run to preview."
        )
    if args.uninstall:
        return setup_helpers.delete_connection_profile(dry_run=args.dry_run)
    token = None
    if args.save_token:
        token = args.token or os.getenv("LAB_TRACKER_ACCESS_TOKEN")
        if not token:
            raise SystemExit(
                "--save-token needs --token or LAB_TRACKER_ACCESS_TOKEN in the environment."
            )
    elif args.token:
        raise SystemExit("--token persists only with explicit --save-token consent.")
    try:
        payload = setup_helpers.save_connection_profile(
            base_url=args.base_url,
            default_project_id=args.project,
            access_token=token,
            dry_run=args.dry_run,
        )
    except setup_helpers.ConnectionProfileSecurityError as exc:
        raise SystemExit(str(exc)) from None
    if args.base_url:
        payload["server_reachable"] = setup_helpers.probe_health(args.base_url)
    return payload


def _cmd_setup_verify_client(args: argparse.Namespace) -> Any:
    return setup_helpers.verify_client_revision(args.expected_revision)


def _cmd_setup_verify_mcp(args: argparse.Namespace) -> Any:
    return setup_helpers.verify_mcp_launch(
        expected_revision=args.expected_revision,
        command=args.command,
        timeout_seconds=args.timeout,
    )


def _cmd_project_bind(client: LabTracker, args: argparse.Namespace) -> Any:
    if not (args.yes or args.dry_run):
        raise SystemExit(
            "lt project bind writes lt_ids.json; pass --yes to consent or "
            "--dry-run to preview."
        )
    return setup_helpers.bind_project(
        client,
        project_id=args.project_id,
        name=args.name,
        create=args.create,
        ids_path=args.ids,
        dry_run=args.dry_run,
    )


def _cmd_git_snapshot(args: argparse.Namespace) -> Any:
    payload = git_capture.snapshot_commit(
        repo=args.repo,
        commit=args.commit,
        project_id=args.project,
        question_id=args.question,
        dataset_ids=args.dataset,
        tags=args.tag,
        config_path=args.config,
        max_diff_lines=args.max_diff_lines,
        context_lines=args.context_lines,
        request_draft=args.request_draft,
    )
    if args.no_sync:
        return payload
    # Best-effort drain: the event above is durable, so a down server means
    # "queued for a later sync", never a lost commit. Draining the whole
    # outbox here is what empties the backlog on the next commit. The draft
    # request rides on the git event itself (payload.request_draft), so
    # request_draft stays False here — unrelated queued captures must never
    # get LLM drafts as a side effect of a commit.
    config, _config_error = git_capture.resolve_watch_config(
        Path(payload["repo"]), args.config
    )
    try:
        client = LabTracker.from_env()
        try:
            payload["sync"] = watch_capture.sync_outbox(
                client,
                config,
                request_draft=False,
            )
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - queued events retry on later syncs.
        payload["sync_error"] = str(exc)
    diagnostic = _git_snapshot_sync_diagnostic(payload)
    if diagnostic:
        print(diagnostic, file=sys.stderr, flush=True)
    return payload


def _git_snapshot_sync_diagnostic(payload: Any) -> str | None:
    """Human-readable cause+remediation for a failed commit-snapshot sync (GH #77).

    Returns ``None`` when the sync succeeded. The post-commit hook suppresses
    stdout but not stderr, so this line is what the user actually sees instead of
    the old unactionable "did not fully sync" one-liner.
    """
    if not isinstance(payload, dict):
        return None
    sync = payload.get("sync")
    sync_errors = sync.get("errors") if isinstance(sync, dict) else None
    if not (payload.get("sync_error") or payload.get("config_error") or sync_errors):
        return None
    outbox = payload.get("outbox") or "<outbox>"
    try:
        queued = watch_capture.outbox_status(outbox).get("total", "?")
    except Exception:  # noqa: BLE001 - a diagnostic must never raise.
        queued = "?"
    if payload.get("sync_error"):
        cause = (
            "could not reach Lab Tracker or start the client "
            f"({payload['sync_error']}). If 'lt' or lab_tracker_client is not "
            "resolvable here, run 'lt setup status' to diagnose"
        )
    elif sync_errors:
        first = sync_errors[0].get("error") if isinstance(sync_errors[0], dict) else None
        cause = (
            f"{len(sync_errors)} event(s) failed to sync "
            f"({first or 'see the queued event files'})"
        )
    else:
        cause = f"the watch config could not be used ({payload['config_error']})"
    return (
        f"lab-tracker: commit capture did not fully sync — {cause}. "
        f"{queued} event(s) are queued at {outbox}; the commit was kept. "
        "Drain later with 'lt outbox sync' (inspect with 'lt outbox status')."
    )


def _cmd_hooks_install(args: argparse.Namespace) -> Any:
    if not (args.yes or args.dry_run):
        raise SystemExit(
            "lt hooks install writes the repository's post-commit hook; "
            "pass --yes to consent or --dry-run to preview."
        )
    return hook_install.install_hook(
        repo=args.repo,
        project_id=args.project,
        base_url=args.base_url,
        lt_path=args.lt_path,
        force=args.force,
        dry_run=args.dry_run,
    )


def _cmd_hooks_uninstall(args: argparse.Namespace) -> Any:
    if not (args.yes or args.dry_run):
        raise SystemExit(
            "lt hooks uninstall modifies the repository's post-commit hook; "
            "pass --yes to consent or --dry-run to preview."
        )
    return hook_install.uninstall_hook(repo=args.repo, dry_run=args.dry_run)


def _cmd_hooks_status(args: argparse.Namespace) -> Any:
    return hook_install.hook_status(repo=args.repo)


def _cmd_agent_context_status(args: argparse.Namespace) -> Any:
    root = git_capture.repo_toplevel(args.repo)
    try:
        config = repo_context.load_agent_context_config(root)
    except ValueError as exc:
        raise LTValidationError(str(exc)) from exc
    discovered = repo_context.discover_repository_convention_files(root)
    hook = hook_install.hook_status(repo=root)
    automatic_refresh = bool(
        hook.get("managed_block_present") or hook.get("legacy_repo_block_present")
    )
    return {
        "command": "agent-context-status",
        "repo": str(root),
        "config": str(config.config_path),
        "configured": list(config.paths),
        "discovered": [
            {"path": path, "enrolled": path in config.paths} for path in discovered
        ],
        "refresh": {
            "automatic": automatic_refresh,
            "hook_path": hook.get("hook_path"),
            "detail": (
                "The installed post-commit hook snapshots enrolled files from each "
                "new commit."
                if automatic_refresh
                else "No Lab Tracker post-commit hook is installed; preview with "
                "`lt hooks install --dry-run`, then enable automatic refresh with "
                "`lt hooks install --yes`."
            ),
        },
    }


def _cmd_agent_context_add(args: argparse.Namespace) -> Any:
    if not (args.yes or args.dry_run):
        raise SystemExit(
            "lt agent-context add writes .lab-tracker/agent-context.json; "
            "pass --yes to consent or --dry-run to preview."
        )
    root = git_capture.repo_toplevel(args.repo)
    try:
        config = repo_context.load_agent_context_config(root)
        relative = repo_context.validate_enrolled_convention_path(root, args.path)
    except ValueError as exc:
        raise LTValidationError(str(exc)) from exc
    paths = sorted({*config.paths, relative})
    if len(paths) > repo_context.MAX_REPOSITORY_CONVENTION_FILES:
        raise LTValidationError(
            "Agent context may enroll at most "
            f"{repo_context.MAX_REPOSITORY_CONVENTION_FILES} convention files."
        )
    proposed = repo_context.AgentContextConfig(
        paths=paths,
        config_path=config.config_path,
    )
    payload = _agent_context_change_payload(
        command="agent-context-add",
        root=root,
        before=config,
        after=proposed,
        path=relative,
        dry_run=args.dry_run,
    )
    payload["action"] = "unchanged" if paths == config.paths else "added"
    if not args.dry_run and paths != config.paths:
        try:
            repo_context.save_agent_context_config(proposed)
        except ValueError as exc:
            raise LTValidationError(str(exc)) from exc
    return payload


def _cmd_agent_context_remove(args: argparse.Namespace) -> Any:
    if not (args.yes or args.dry_run):
        raise SystemExit(
            "lt agent-context remove writes .lab-tracker/agent-context.json; "
            "pass --yes to consent or --dry-run to preview."
        )
    root = git_capture.repo_toplevel(args.repo)
    try:
        config = repo_context.load_agent_context_config(root)
        relative = repo_context.normalize_enrolled_convention_path(args.path)
    except ValueError as exc:
        raise LTValidationError(str(exc)) from exc
    paths = [path for path in config.paths if path != relative]
    proposed = repo_context.AgentContextConfig(
        paths=paths,
        config_path=config.config_path,
    )
    payload = _agent_context_change_payload(
        command="agent-context-remove",
        root=root,
        before=config,
        after=proposed,
        path=relative,
        dry_run=args.dry_run,
    )
    payload["action"] = "removed" if paths != config.paths else "absent"
    if not args.dry_run and paths != config.paths:
        try:
            repo_context.save_agent_context_config(proposed)
        except ValueError as exc:
            raise LTValidationError(str(exc)) from exc
    return payload


def _agent_context_change_payload(
    *,
    command: str,
    root: Path,
    before: repo_context.AgentContextConfig,
    after: repo_context.AgentContextConfig,
    path: str,
    dry_run: bool,
) -> dict[str, Any]:
    before_text = json.dumps(before.to_dict(), indent=2, sort_keys=True) + "\n"
    after_text = json.dumps(after.to_dict(), indent=2, sort_keys=True) + "\n"
    config_path = after.config_path or repo_context.default_agent_context_config_path(root)
    return {
        "command": command,
        "repo": str(root),
        "config": str(config_path),
        "path": path,
        "dry_run": dry_run,
        "diff": "\n".join(
            difflib.unified_diff(
                before_text.splitlines(),
                after_text.splitlines(),
                fromfile=f"{config_path} (current)",
                tofile=f"{config_path} (proposed)",
                lineterm="",
            )
        ),
        "configured": list(after.paths),
    }


def _cmd_watch_run(client: LabTracker, args: argparse.Namespace) -> Any:
    config = watch_capture.load_config(config_path=args.config)
    scan = watch_capture.scan_configured(config, limit=args.limit)
    sync = watch_capture.sync_outbox(
        client,
        config,
        request_draft=args.request_draft,
        limit=args.limit,
    )
    return {
        "command": "watch-run",
        "config": str(config.config_path),
        "scan": scan,
        "sync": sync,
        "errors": list(scan.get("errors") or []) + list(sync.get("errors") or []),
    }


def _cmd_setup_schedule(args: argparse.Namespace) -> Any:
    if not (args.yes or args.dry_run):
        raise SystemExit(
            "lt setup schedule modifies the OS scheduler; "
            "pass --yes to consent or --dry-run to preview."
        )
    config_path = args.config or Path(".") / ".lab-tracker" / "watch.json"
    if args.uninstall:
        return schedule_helpers.uninstall_schedule(
            config_path=config_path,
            dry_run=args.dry_run,
        )
    payload = schedule_helpers.install_schedule(
        config_path=config_path,
        interval_minutes=args.interval_minutes,
        lt_path=args.lt_path,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        repo_registry.record_repo(
            repo_registry.repo_root_for_config(payload["config"]), "setup-schedule"
        )
    return payload


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


def _cmd_auth_doctor(args: argparse.Namespace) -> Any:
    payload = auth_helpers.auth_doctor(args.target)
    if not getattr(args, "fail_silent", False):
        print(auth_helpers.render_report(payload), file=sys.stderr)
    return payload


def _resolve_outbox_config(
    args: argparse.Namespace,
) -> tuple[watch_capture.WatchConfig, str | None]:
    # Commit-snapshot capture has no watch.json, so resolve the outbox from the
    # repo layout directly (git toplevel, else the given path). This is what lets
    # `lt outbox` drain events that `lt watch sync` refuses without watch.json.
    try:
        repo_root = git_capture.repo_toplevel(args.repo)
    except Exception:  # noqa: BLE001 - not a git repo: treat --repo as the root.
        repo_root = Path(args.repo).expanduser().resolve()
    return git_capture.resolve_watch_config(repo_root, args.config)


def _cmd_outbox_status(args: argparse.Namespace) -> Any:
    config, config_error = _resolve_outbox_config(args)
    summary = watch_capture.outbox_status(config.outbox_path())
    summary.update(
        {
            "command": "outbox-status",
            "config": str(config.config_path),
        }
    )
    if config_error:
        summary["config_error"] = config_error
    return summary


def _cmd_outbox_sync(client: LabTracker, args: argparse.Namespace) -> Any:
    config, config_error = _resolve_outbox_config(args)
    result = watch_capture.sync_outbox(
        client,
        config,
        dry_run=args.dry_run,
        request_draft=args.request_draft,
        limit=args.limit,
    )
    result["command"] = "outbox-sync"
    if config_error:
        result["config_error"] = config_error
    return result


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
    if result.identifier_note:
        print(f"note: {result.identifier_note}", file=sys.stderr)
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

    if not getattr(args, "all", False):
        return _doctor(args.target)
    repos = []
    pruned = []
    for entry in repo_registry.list_repos():
        root = str(entry.get("root") or "")
        if not root:
            continue
        summary: dict[str, Any] = {"root": root, "actions": entry.get("actions") or []}
        if not Path(root).exists():
            if getattr(args, "prune_missing", False):
                repo_registry.remove_repo(root)
                pruned.append(root)
                continue
            summary["missing"] = True
        else:
            try:
                payload = _doctor(root)
            except Exception as exc:  # noqa: BLE001 - one bad repo must not end the sweep.
                summary["error"] = str(exc)
                repos.append(summary)
                continue
            targets = payload.get("targets") or []
            summary["drifted"] = [
                target["name"]
                for target in targets
                if isinstance(target, dict) and target.get("drifted")
            ]
            if payload.get("suggestion"):
                summary["suggestion"] = payload["suggestion"]
        repos.append(summary)
    result: dict[str, Any] = {
        "command": "doctor-all",
        "registry": str(repo_registry.registry_path()),
        "repos": repos,
    }
    if pruned:
        result["pruned"] = pruned
    return result


def _cmd_update(args: argparse.Namespace) -> Any:
    from lab_tracker.cli import update_consumer_repo

    mcp_base_url, _ = setup_helpers.resolved_base_url_for_setup()
    result = update_consumer_repo(
        args.target,
        mcp_base_url=mcp_base_url,
        yes=args.yes,
        dry_run=args.dry_run,
        install_skills=args.install_skills,
    )
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
        and payload.get("command") in {"hpc-sync", "hpc-watch", "repo-sync"}
        and payload.get("errors")
    ):
        return 1
    if (
        isinstance(payload, dict)
        and payload.get("command") in {"watch-scan", "watch-sync", "outbox-sync"}
        and payload.get("errors")
    ):
        return 1
    if (
        isinstance(payload, dict)
        and payload.get("command") == "watch-run"
        and payload.get("errors")
    ):
        return 1
    if (
        isinstance(payload, dict)
        and payload.get("command") == "auth-doctor"
        and payload.get("deprecated_count")
    ):
        return 1
    if (
        isinstance(payload, dict)
        and payload.get("command") in {"setup-verify-client", "setup-verify-mcp"}
        and payload.get("ok") is not True
    ):
        return 1
    if isinstance(payload, dict) and payload.get("command") == "doctor-all":
        repos = payload.get("repos")
        if not isinstance(repos, list):
            return 1
        if any(
            repo.get("missing") or repo.get("drifted") or repo.get("error")
            for repo in repos
        ):
            return 1
    if isinstance(payload, dict) and payload.get("command") == "git-snapshot":
        sync = payload.get("sync")
        if (
            payload.get("sync_error")
            or payload.get("config_error")
            or (isinstance(sync, dict) and sync.get("errors"))
        ):
            # The commit is safely queued; nonzero signals "not fully synced"
            # so the hook's one-line warning stays reachable.
            return 1
    return 0
