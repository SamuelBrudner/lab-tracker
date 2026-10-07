"""``lab-tracker integrations poll``: run the due capture pollers once, for cron."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from lab_tracker.capture_channels.dispatch import POLLER_NAMES, PollRunReport


def add_integrations_parsers(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register ``integrations poll`` on the server CLI."""

    integrations = subcommands.add_parser(
        "integrations",
        help="Server capture channels (email inbox, instrument calendars, store scans).",
    )
    actions = integrations.add_subparsers(dest="integrations_command", required=True)
    poll = actions.add_parser(
        "poll",
        help=(
            "Run each configured capture poller that is due (at most once per "
            "LAB_TRACKER_INTEGRATIONS_POLL_MIN_INTERVAL_SECONDS) and print a JSON report. "
            "Exits 1 when a poller failed."
        ),
    )
    poll.add_argument(
        "--only",
        action="append",
        choices=POLLER_NAMES,
        default=None,
        help="Run only this poller (repeatable).",
    )
    poll.add_argument(
        "--force",
        action="store_true",
        help="Ignore the minimum interval for this run (operator testing).",
    )


def run_integrations_command(args: argparse.Namespace) -> int:
    """Execute the parsed ``integrations`` command; return the process exit code."""

    if args.integrations_command != "poll":  # pragma: no cover - argparse enforces choices
        return 2
    try:
        report = poll_once(only=args.only, force=args.force)
    except Exception as exc:
        print(f"Capture polling could not start: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report.model_dump(mode="json"), indent=2))
    return 1 if any(poller.status == "failed" for poller in report.pollers) else 0


def poll_once(*, only: Sequence[str] | None = None, force: bool = False) -> PollRunReport:
    """Compose the pollers from the environment, run them once, and release resources."""

    from lab_tracker.api import LabTrackerAPI
    from lab_tracker.app_parts.middleware import system_auth_context
    from lab_tracker.app_parts.runtime import build_app_runtime
    from lab_tracker.capture_channels.dispatch import CaptureRuntime, run_due_pollers
    from lab_tracker.capture_channels.poll_state import PollState
    from lab_tracker.capture_channels.store_scan import LocalStoreScanAccess
    from lab_tracker.config import get_settings
    from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

    settings = get_settings()
    runtime = build_app_runtime(settings)
    try:
        capture = CaptureRuntime(
            settings=settings,
            session_factory=runtime.session_factory,
            api_for_session=lambda session: LabTrackerAPI(
                raw_storage=runtime.raw_note_storage,
                repository=SQLAlchemyLabTrackerRepository(session),
                settings=settings,
                store_authority_registry=runtime.store_authority_registry,
                surface="cli",
            ),
            users=runtime.auth_service,
            system_actor=system_auth_context(),
            outbound_http_policy=runtime.outbound_http_policy,
            outbound_http_client=runtime.outbound_http_client,
            rclone_remote_policy=runtime.rclone_remote_policy,
            process_executor=runtime.process_executor,
            local_store_access=LocalStoreScanAccess(runtime.local_filesystem_operations),
            store_authority_snapshot_provider=runtime.store_authority_snapshot_provider,
            state=PollState.from_settings(settings),
        )
        return run_due_pollers(capture, trigger="cli", only=only, force=force)
    finally:
        try:
            runtime.engine.dispose()
        finally:
            runtime.cleanup_git_health_workdir()
