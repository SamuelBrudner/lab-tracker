"""``lt`` wiring for coding-agent lifecycle hooks.

* ``lt agent session-end`` -- the SessionEnd trigger of the session
  retrospective harvester (:mod:`lab_tracker_client.agent_session`).
* ``lt watch touch`` -- event-driven capture of an agent's writes into a
  configured watch folder (:mod:`lab_tracker_client.watch_touch`).
* ``lt setup agent-hooks`` -- the consent-gated installer for both
  (:mod:`lab_tracker_client.agent_hooks`).

When stdin carries an agent hook payload (it names a ``hook_event_name``),
the hook commands print nothing on stdout: Claude Code parses a hook's JSON
stdout as hook-control output and reports keys it does not know as a hook
error. Run them by hand (or with ``--dry-run``) to see the JSON payload.
"""

from __future__ import annotations

import argparse
from typing import Any

import lab_tracker_client.agent_hooks as agent_hooks
import lab_tracker_client.agent_session as agent_session
import lab_tracker_client.registry as repo_registry
import lab_tracker_client.watch_touch as watch_touch
from lab_tracker_client.client import LTValidationError


def add_agent_parsers(subcommands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``lt agent ...`` on the top-level parser."""

    agent_parser = subcommands.add_parser(
        "agent",
        help="Coding-agent lifecycle hooks (session retrospectives).",
    )
    agent_commands = agent_parser.add_subparsers(dest="agent_command", required=True)
    end_parser = agent_commands.add_parser(
        "session-end",
        help=(
            "Queue one bounded, redacted retrospective of a finished coding-agent session "
            "(read from the SessionEnd hook JSON on stdin) as a staged note that asks for "
            "human-reviewed drafts. Only for a checkout bound to a project; sessions with "
            f"no file edits and fewer than {agent_session.MIN_PROMPTS_WITHOUT_EDITS} prompts "
            "are skipped. The transcript itself is never uploaded."
        ),
    )
    end_parser.add_argument(
        "--agent",
        default=agent_session.DEFAULT_AGENT,
        help=f"Agent name recorded on the note. Defaults to {agent_session.DEFAULT_AGENT}.",
    )
    end_parser.add_argument(
        "--project",
        help="Project UUID. Defaults to LAB_TRACKER_PROJECT_ID or the checkout's lt_ids.json.",
    )
    end_parser.add_argument(
        "--repo",
        help="Checkout path. Defaults to CLAUDE_PROJECT_DIR, then the hook's cwd.",
    )
    end_parser.add_argument(
        "--transcript", help="Transcript JSONL path; overrides the hook's transcript_path."
    )
    end_parser.add_argument("--session-id", help="Session id; overrides the hook's session_id.")
    end_parser.add_argument(
        "--min-prompts",
        type=int,
        default=agent_session.MIN_PROMPTS_WITHOUT_EDITS,
        help=(
            "Skip sessions with no file edits and fewer prompts than this "
            f"(default {agent_session.MIN_PROMPTS_WITHOUT_EDITS})."
        ),
    )
    end_parser.add_argument(
        "--no-sync", action="store_true", help="Queue the event only; skip the best-effort sync."
    )
    end_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the retrospective that would be queued (body included); write nothing.",
    )
    end_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Swallow every error so an agent hook can never fail a session.",
    )
    end_parser.set_defaults(func=_cmd_agent_session_end, needs_client=False)


def add_watch_touch_parser(
    watch_commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register ``lt watch touch`` on the ``lt watch`` subparsers."""

    touch_parser = watch_commands.add_parser(
        "touch",
        help=(
            "Queue a just-written file when it falls under a configured watch folder "
            "(the path comes from the arguments or an agent PostToolUse hook payload on "
            "stdin), then sync best-effort. Unwatched paths return at once without "
            "network or folder scans."
        ),
    )
    touch_parser.add_argument("paths", nargs="*", help="Written file paths. Defaults to stdin.")
    touch_parser.add_argument(
        "--config",
        help="Watch config path. Defaults to the checkout's .lab-tracker/watch.json.",
    )
    touch_parser.add_argument(
        "--repo", help="Checkout path. Defaults to CLAUDE_PROJECT_DIR, then the hook's cwd."
    )
    touch_parser.add_argument(
        "--no-sync", action="store_true", help="Queue only; skip the best-effort sync."
    )
    touch_parser.add_argument(
        "--fail-silent",
        action="store_true",
        help="Swallow every error so an agent hook can never fail an edit.",
    )
    touch_parser.set_defaults(func=_cmd_watch_touch, needs_client=False)


def add_setup_agent_hooks_parser(
    setup_commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register ``lt setup agent-hooks`` on the ``lt setup`` subparsers."""

    hooks_parser = setup_commands.add_parser(
        "agent-hooks",
        help=(
            "Add Claude Code hooks to this checkout's .claude/settings.json: SessionEnd runs "
            "'lt agent session-end' (a redacted retrospective of each agent session, staged "
            "for review) and PostToolUse on Write/Edit runs 'lt watch touch'. This captures "
            "agent conversations, so it is a separate opt-in that 'lt setup init' never makes."
        ),
    )
    hooks_parser.add_argument(
        "--target", default=".", help="Checkout path. Defaults to the current directory."
    )
    hooks_parser.add_argument(
        "--local",
        action="store_true",
        help="Edit the personal .claude/settings.local.json instead of .claude/settings.json.",
    )
    hooks_parser.add_argument(
        "--uninstall", action="store_true", help="Remove the managed hook entries."
    )
    hooks_parser.add_argument(
        "--dry-run", action="store_true", help="Show the settings diff without writing."
    )
    hooks_parser.add_argument(
        "--yes", action="store_true", help="Consent to editing the agent settings file."
    )
    hooks_parser.set_defaults(func=_cmd_setup_agent_hooks, needs_client=False)


def _cmd_agent_session_end(args: argparse.Namespace) -> Any:
    # A fully explicit manual run needs no hook payload, so stdin is left alone.
    explicit = bool(args.transcript and args.session_id)
    hook = {} if explicit else agent_session.read_hook_input()
    payload = agent_session.capture_session_end(
        hook,
        agent_session.SessionEndOptions(
            agent=args.agent,
            project_id=args.project,
            repo=args.repo,
            transcript=args.transcript,
            session_id=args.session_id,
            min_prompts=args.min_prompts,
            sync=not args.no_sync,
            dry_run=args.dry_run,
        ),
    )
    return _hook_stdout(hook, payload, dry_run=args.dry_run)


def _cmd_watch_touch(args: argparse.Namespace) -> Any:
    hook = {} if args.paths else agent_session.read_hook_input()
    payload = watch_touch.touch_from_hook(
        hook,
        paths=args.paths,
        repo=args.repo,
        config_path=args.config,
        sync=not args.no_sync,
    )
    return _hook_stdout(hook, payload, dry_run=False)


def _cmd_setup_agent_hooks(args: argparse.Namespace) -> Any:
    if not (args.yes or args.dry_run):
        raise SystemExit(
            "lt setup agent-hooks edits the checkout's agent settings to capture coding-agent "
            "sessions; pass --yes to consent or --dry-run to preview."
        )
    try:
        payload = agent_hooks.install_agent_hooks(
            args.target, local=args.local, uninstall=args.uninstall, dry_run=args.dry_run
        )
    except (LTValidationError, OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"lt setup agent-hooks: {exc}") from None
    if not args.dry_run and not args.uninstall:
        # Sweep metadata for `lt doctor --all`; fail-soft, never an auto-apply trigger.
        repo_registry.record_repo(args.target, "setup-agent-hooks")
    return payload


def _hook_stdout(hook: dict[str, Any], payload: Any, *, dry_run: bool) -> Any:
    """``None`` (print nothing) when an agent's hook runner is reading stdout."""

    if hook.get("hook_event_name") and not dry_run:
        return None
    return payload


__all__ = ["add_agent_parsers", "add_setup_agent_hooks_parser", "add_watch_touch_parser"]
