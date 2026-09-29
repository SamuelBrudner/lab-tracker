"""``lt run``: run a local command and record what ran as one staged note.

The local analogue of ``lt hpc submit``. ``lt run [options] -- <command>``
runs the command with inherited stdin/stdout/stderr, exits with exactly the
command's exit code, and records -- fail-soft, never altering that code:

* the argv with obvious secrets redacted (:func:`redact_argv`), the working
  directory, start/end UTC and duration, and the exit code (``128+N`` for a
  command killed by signal N, as the shell reports it);
* the checkout's git HEAD, dirty state, branch and credential-free remote,
  plus the working copy's git tree id (:func:`gitinfo.worktree_tree_id`), the
  identity of the exact code even when it was never committed;
* the lockfile environment fingerprint ``lt repo`` computes;
* the files created or modified under each declared ``--output`` folder, by a
  before/after size+mtime snapshot, as bounded artifact pointers (sha256 for
  files up to :data:`MAX_HASH_FILE_BYTES`; size and mtime only above it).

The record is ONE watch-outbox event with ``sink=staged-note``: its markdown
lives in ``payload.body`` and its scalar ``run_*`` metadata in
``payload.metadata``, both of which the watch sync carries into the staged
note. After the command exits, the checkout's watch outbox is drained best
effort (like the ``lt repo`` hook) when a server is configured. The run lands
behind the review gate like every other capture: a staged note, nothing
committed.

Only a *bound* project is captured (``--project``, ``LAB_TRACKER_PROJECT_ID``
or the checkout's ``lt_ids.json``); otherwise the command still runs and one
notice line says why nothing was recorded. ``lt run`` itself never writes to
stdout and prints at most one line to stderr.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import lab_tracker_client.git_capture as git_capture
import lab_tracker_client.watch as watch_capture
from lab_tracker.instance_url import BASE_URL_ENV, LEGACY_MCP_BASE_URL_ENV
from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client.capture_project import (
    CaptureProject,
    resolve_capture_project_in_checkout,
)
from lab_tracker_client.client import LabTracker, LTValidationError, load_connection_profile
from lab_tracker_client.gitinfo import (
    DirtyState,
    WorktreeTree,
    dirty_label,
    dirty_metadata,
    dirty_state_fields,
    git_dirty_state,
    git_head_commit,
    git_output,
    head_commit_fields,
    run_git,
    sanitize_remote_url,
    worktree_state,
)
from lab_tracker_client.redaction import REDACTED, looks_secret_name, redact_capture_text
from lab_tracker_client.repo import environment_fingerprint, normalize_remote
from lab_tracker_client.session_context import read_active_session, strict_session_id

JsonObject = dict[str, Any]

RUN_CAPTURE_KIND = "command_run"
RUN_EVIDENCE_ADAPTER = "lt-run"
RUN_EVIDENCE_PROVIDER = "lt-run"
# How the run's session was chosen (event ``source.session_source``): a
# ``--session`` flag is a per-run choice; the checkout's active session keeps
# the watch sync's ``active`` label so it is verified against the project.
RUN_SESSION_SOURCE_EXPLICIT = "explicit"
# Bounds on what one run records. Outputs beyond MAX_OUTPUT_ARTIFACTS are
# counted (run_output_count) but not listed; files above MAX_HASH_FILE_BYTES,
# or past MAX_HASH_TOTAL_BYTES hashed in one run, keep size + mtime only; a
# snapshot stops after MAX_SNAPSHOT_FILES files across all output folders.
MAX_OUTPUT_ARTIFACTS = 200
MAX_HASH_FILE_BYTES = 64 * 1024 * 1024
MAX_HASH_TOTAL_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_FILES = 20_000
MAX_COMMAND_METADATA_CHARS = 500
MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 64_000
# The variable part of a stderr notice (an error message) is clipped to this,
# so the fixed remedy at the end of the line is always printed.
MAX_NOTICE_CAUSE_CHARS = 160
DRAIN_TIMEOUT_SECONDS = 10.0
# The post-run drain probes /health once with this timeout and then syncs at
# most DRAIN_LIMIT queued events, so a slow or unreachable server never holds
# the shell for long after the command exits.
HEALTH_PROBE_TIMEOUT_SECONDS = 2.0
DRAIN_LIMIT = 10
EXIT_COMMAND_NOT_FOUND = 127
EXIT_CANNOT_EXECUTE = 126
EXIT_INTERRUPTED = 128 + 2  # SIGINT, as the shell reports an interrupted command
# Folders a snapshot never descends into: VCS internals and Lab Tracker's own
# host-local scratch are never a run's outputs.
_SNAPSHOT_SKIP_DIRS = frozenset({".git", ".hg", ".svn", ".lab-tracker"})

RUN_UNBOUND_NOTICE = (
    "lab-tracker: `lt run` is not capturing runs in {where}: {why}. The command ran; "
    "nothing was sent or queued. {remedy}"
)
RUN_NO_PROJECT_WHY = "that checkout is not bound to a project (no lt_ids.json)"
RUN_WATCH_CONFIG_WHY = (
    "that checkout names its project only in its watch config, not in lt_ids.json"
)
RUN_OUTSIDE_CHECKOUT_WHY = (
    "this folder is not inside a git checkout, so no lt_ids.json binds it to a project"
)
RUN_CHECKOUT_REMEDY = (
    "Bind the checkout with `lt project bind`, set LAB_TRACKER_PROJECT_ID, or pass --project."
)
RUN_LOOSE_FOLDER_REMEDY = "Set LAB_TRACKER_PROJECT_ID or pass --project."
RUN_CAPTURE_FAILED_NOTICE = (
    "lab-tracker: `lt run` could not record this run ({error}); the command's exit code "
    "is unchanged."
)
RUN_SESSION_IGNORED_NOTICE = (
    "lab-tracker: `lt run` ignored --session ({error}); the run was captured without it."
)
RUN_SYNC_FAILED_NOTICE = (
    "lab-tracker: `lt run` queued this run in {outbox} but could not sync it ({cause}); "
    "`lt outbox sync` retries."
)


@dataclass(frozen=True)
class RunOptions:
    """What ``lt run`` was asked to attach to the run."""

    project_id: str | None = None
    session: str | None = None
    question_id: str | None = None
    label: str | None = None
    outputs: tuple[str, ...] = ()
    drain: bool = True
    request_draft: bool = False


@dataclass(frozen=True)
class RunOutcome:
    """How the command ended: the shell-style exit code, or why it never started."""

    exit_code: int
    started: bool = True
    signal_number: int | None = None
    error: str = ""


@dataclass(frozen=True)
class FileStamp:
    """Size and mtime of one output file; a change in either is a modification."""

    size: int
    mtime_ns: int


@dataclass
class OutputSnapshot:
    """Regular files under the declared output folders at one moment."""

    files: dict[Path, FileStamp] = field(default_factory=dict)
    truncated: bool = False


@dataclass(frozen=True)
class OutputChanges:
    """Files a run created or modified under its outputs, and how many it removed."""

    created: list[Path]
    modified: list[Path]
    removed: int
    truncated: bool = False

    @property
    def count(self) -> int:
        return len(self.created) + len(self.modified)


@dataclass
class _Plan:
    """Everything decided before the command starts, for the capture after it."""

    run_id: str
    cwd: Path
    checkout: Path | None
    project_id: str
    question_id: str | None
    session_id: str | None
    session_fields: dict[str, str]
    label: str | None
    outputs: list[Path]
    before: OutputSnapshot
    git: JsonObject
    worktree: WorktreeTree
    environment: JsonObject
    outbox: Path
    drain: bool
    request_draft: bool


def run_command(
    command: Sequence[str],
    options: RunOptions,
    *,
    stderr: TextIO | None = None,
    client_factory: Callable[[], LabTracker] | None = None,
) -> int:
    """Run ``command`` with inherited stdio, capture it fail-soft, return its exit code.

    ``client_factory`` builds the client for the best-effort drain; by default
    a client is built from the environment only when a server is configured.
    """

    err = stderr if stderr is not None else sys.stderr
    argv = [str(part) for part in command]
    notices: list[str] = []
    try:
        plan = _quietly(lambda: _prepare(options, notices), notices)
    except KeyboardInterrupt:
        # Interrupted before the command started: nothing ran, nothing recorded.
        return EXIT_INTERRUPTED
    started_at = datetime.now(timezone.utc)
    clock = time.monotonic()
    outcome = run_child(argv)
    duration = time.monotonic() - clock
    ended_at = datetime.now(timezone.utc)
    if not outcome.started:
        _print_line(err, f"lt run: {argv[0] if argv else ''}: {outcome.error}")
        return outcome.exit_code
    if plan is not None:
        # An interrupt during the capture or drain abandons them (a written
        # event stays queued) but never replaces the command's exit code.
        with contextlib.suppress(KeyboardInterrupt):
            _quietly(
                lambda: _finish(
                    plan,
                    argv=argv,
                    outcome=outcome,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration=duration,
                    notices=notices,
                    client_factory=client_factory,
                ),
                notices,
            )
    if notices:
        _print_line(err, notices[0])
    return outcome.exit_code


# --- running the command ---------------------------------------------------------


def run_child(argv: Sequence[str]) -> RunOutcome:
    """Run ``argv`` with inherited stdio; the exit code the shell would report.

    A missing executable is 127 and one that cannot be executed is 126. A
    child killed by signal N is ``128 + N``. While the child runs, SIGINT and
    SIGQUIT are ignored here (the terminal delivers them to the child) and
    SIGTERM/SIGHUP are forwarded to it, so ``lt run`` outlives the child and
    still records how it ended.
    """

    if not argv:
        return RunOutcome(EXIT_COMMAND_NOT_FOUND, started=False, error="command not found")
    try:
        process = subprocess.Popen(list(argv))  # noqa: S603 - the user's own command, no shell.
    except FileNotFoundError:
        return RunOutcome(EXIT_COMMAND_NOT_FOUND, started=False, error="command not found")
    except PermissionError:
        return RunOutcome(EXIT_CANNOT_EXECUTE, started=False, error="permission denied")
    except OSError as exc:
        return RunOutcome(EXIT_CANNOT_EXECUTE, started=False, error=exc.strerror or str(exc))
    with _relay_signals(process):
        returncode = _wait(process)
    if returncode < 0:
        return RunOutcome(128 - returncode, signal_number=-returncode)
    return RunOutcome(returncode)


def _wait(process: subprocess.Popen[Any]) -> int:
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # A SIGINT that arrived before the handlers were installed: the
            # child got it too; keep waiting for it to decide how to end.
            continue


@contextlib.contextmanager
def _relay_signals(process: subprocess.Popen[Any]) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(Exception):
            process.send_signal(signum)

    # Ctrl-C / Ctrl-\ at a terminal reach every process in the foreground group,
    # the command included, so they are only ignored here. Anywhere else (a
    # pipeline, `kill -INT <lt pid>`, a notebook kernel interrupting `!lt run`)
    # only this process got the signal and it must reach the command.
    interrupt = signal.SIG_IGN if _terminal_delivers_interrupts() else forward
    wanted: list[tuple[str, Any]] = [
        ("SIGINT", interrupt),
        ("SIGQUIT", interrupt),
        ("SIGTERM", forward),
        ("SIGHUP", forward),
    ]
    previous: dict[int, Any] = {}
    for name, handler in wanted:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            previous[signum] = signal.signal(signum, handler)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(signum, handler)


def _terminal_delivers_interrupts() -> bool:
    """True when stdin is a terminal whose foreground process group is ours.

    Only then does the terminal send Ctrl-C/Ctrl-\\ to the command as well.
    Windows has no process groups, so the signal is always forwarded there
    (a console Ctrl-C already reaches the command; forwarding it is harmless).
    """

    if sys.platform == "win32":
        return False
    try:
        return os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp()
    except OSError:
        return False


# --- before the command -----------------------------------------------------------


def _prepare(options: RunOptions, notices: list[str]) -> _Plan | None:
    cwd = Path.cwd().resolve()
    # Every git probe before the command delays its start, so the checkout
    # root is resolved once (bounded) and one read-only `git status` serves
    # both the dirty flag and the worktree tree.
    checkout = _checkout_root(cwd)
    capture_project = resolve_capture_project_in_checkout(checkout, project_id=options.project_id)
    if capture_project is None or not capture_project.bound:
        notices.append(_unbound_notice(cwd, checkout, capture_project))
        return None
    session_id, session_fields = _resolve_session(options.session, checkout or cwd, notices)
    outputs = _resolve_outputs(cwd, options.outputs)
    before = snapshot_outputs(outputs)
    root = checkout or cwd
    git: JsonObject = {}
    worktree = WorktreeTree()
    if checkout is not None:
        state = worktree_state(checkout, exclude=outputs, toplevel=checkout)
        git = _git_facts(checkout, dirty=state.dirty)
        worktree = state.tree
    config, _config_error = git_capture.resolve_watch_config(root)
    return _Plan(
        run_id=new_run_id(),
        cwd=cwd,
        checkout=checkout,
        project_id=capture_project.project_id,
        question_id=_optional(options.question_id),
        session_id=session_id,
        session_fields=session_fields,
        label=_optional(options.label),
        outputs=outputs,
        before=before,
        git=git,
        worktree=worktree,
        environment=environment_fingerprint(root),
        outbox=config.outbox_path(),
        drain=options.drain,
        request_draft=options.request_draft,
    )


def _unbound_notice(cwd: Path, checkout: Path | None, project: CaptureProject | None) -> str:
    if checkout is None:
        return RUN_UNBOUND_NOTICE.format(
            where=cwd, why=RUN_OUTSIDE_CHECKOUT_WHY, remedy=RUN_LOOSE_FOLDER_REMEDY
        )
    why = RUN_WATCH_CONFIG_WHY if project is not None else RUN_NO_PROJECT_WHY
    return RUN_UNBOUND_NOTICE.format(where=checkout, why=why, remedy=RUN_CHECKOUT_REMEDY)


def _resolve_session(
    reference: str | None, start: Path, notices: list[str]
) -> tuple[str | None, dict[str, str]]:
    """``--session`` (a per-run choice), else the checkout's active session."""

    if _optional(reference):
        try:
            return strict_session_id(reference), {"session_source": RUN_SESSION_SOURCE_EXPLICIT}
        except LTValidationError as exc:
            notices.append(RUN_SESSION_IGNORED_NOTICE.format(error=_one_line(str(exc))))
            return None, {}
    active = read_active_session(start)
    if active and active.get("session_id"):
        return str(active["session_id"]), watch_capture.active_session_source(active)
    return None, {}


def _resolve_outputs(cwd: Path, outputs: Sequence[str]) -> list[Path]:
    resolved: list[Path] = []
    for item in outputs:
        text = str(item or "").strip()
        if not text:
            continue
        path = (cwd / Path(text).expanduser()).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


def _checkout_root(cwd: Path) -> Path | None:
    """The git checkout containing ``cwd`` (one bounded probe), or ``None``."""

    probe = run_git(cwd, "rev-parse", "--show-toplevel")
    if not probe.ok or not probe.stdout:
        return None
    try:
        return Path(probe.stdout).resolve()
    except OSError:
        return None


def _git_facts(checkout: Path, *, dirty: DirtyState | None = None) -> JsonObject:
    """HEAD, dirty flag (reusing ``dirty`` when a status already answered), branch, remote."""

    head = git_head_commit(checkout)
    facts: JsonObject = {
        **head_commit_fields(head),
        **dirty_state_fields(dirty or git_dirty_state(checkout, head=head)),
    }
    branch = git_output(checkout, "rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        facts["git_branch"] = branch
    remote = normalize_remote(
        sanitize_remote_url(git_output(checkout, "config", "--get", "remote.origin.url"))
    )
    if remote:
        facts["repo_remote_url"] = remote
    return facts


# --- output snapshots -------------------------------------------------------------


def snapshot_outputs(roots: Sequence[Path], *, limit: int | None = None) -> OutputSnapshot:
    """Size and mtime of every regular file under ``roots`` (a root may be a file).

    VCS folders and ``.lab-tracker/`` are skipped; symlinked folders are not
    followed; missing roots contribute nothing. Stops after ``limit`` files
    (default :data:`MAX_SNAPSHOT_FILES`) and marks the snapshot truncated.
    """

    cap = MAX_SNAPSHOT_FILES if limit is None else limit
    snapshot = OutputSnapshot()
    for root in roots:
        if snapshot.truncated:
            break
        if root.is_file():
            _record_file(snapshot, root, cap)
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in _SNAPSHOT_SKIP_DIRS)
            for name in sorted(filenames):
                _record_file(snapshot, Path(dirpath) / name, cap)
                if snapshot.truncated:
                    break
            if snapshot.truncated:
                break
    return snapshot


def _record_file(snapshot: OutputSnapshot, path: Path, cap: int) -> None:
    if path in snapshot.files:
        return
    if len(snapshot.files) >= cap:
        snapshot.truncated = True
        return
    try:
        info = path.stat()
    except OSError:
        return
    if stat.S_ISREG(info.st_mode):
        snapshot.files[path] = FileStamp(size=info.st_size, mtime_ns=info.st_mtime_ns)


def diff_snapshots(before: OutputSnapshot, after: OutputSnapshot) -> OutputChanges:
    """Files created or modified (size or mtime changed) between two snapshots."""

    created = sorted(path for path in after.files if path not in before.files)
    modified = sorted(
        path
        for path, stamp in after.files.items()
        if path in before.files and before.files[path] != stamp
    )
    removed = sum(1 for path in before.files if path not in after.files)
    return OutputChanges(
        created=created,
        modified=modified,
        removed=removed,
        truncated=before.truncated or after.truncated,
    )


def output_artifacts(
    changes: OutputChanges, after: OutputSnapshot, *, base: Path
) -> list[JsonObject]:
    """Artifact pointers for the first :data:`MAX_OUTPUT_ARTIFACTS` changed files.

    Each carries its URI, size and mtime; a file up to
    :data:`MAX_HASH_FILE_BYTES` (within the run's :data:`MAX_HASH_TOTAL_BYTES`
    budget) that did not change while it was hashed also carries a
    ``sha256:`` ``content_hash``. Bytes are never copied.
    """

    kinds = {path: "created" for path in changes.created}
    kinds.update({path: "modified" for path in changes.modified})
    budget = MAX_HASH_TOTAL_BYTES
    artifacts: list[JsonObject] = []
    for path in sorted(kinds)[:MAX_OUTPUT_ARTIFACTS]:
        stamp = after.files[path]
        artifact: JsonObject = {
            "title": _display_path(path, base),
            "kind": "file",
            "uri": path.as_uri(),
            "summary": kinds[path],
            "size_bytes": stamp.size,
            "modified_at": _iso_from_ns(stamp.mtime_ns),
        }
        if stamp.size <= MAX_HASH_FILE_BYTES and stamp.size <= budget:
            digest = _stable_sha256(path, stamp)
            if digest:
                artifact["content_hash"] = f"sha256:{digest}"
                budget -= stamp.size
        artifacts.append(artifact)
    return artifacts


def _stable_sha256(path: Path, stamp: FileStamp) -> str:
    """sha256 of ``path``, or ``""`` if it is unreadable or changed while hashed."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        info = path.stat()
    except OSError:
        return ""
    if FileStamp(size=info.st_size, mtime_ns=info.st_mtime_ns) != stamp:
        return ""
    return digest.hexdigest()


# --- after the command ------------------------------------------------------------


def _finish(
    plan: _Plan,
    *,
    argv: Sequence[str],
    outcome: RunOutcome,
    started_at: datetime,
    ended_at: datetime,
    duration: float,
    notices: list[str],
    client_factory: Callable[[], LabTracker] | None,
) -> None:
    after = snapshot_outputs(plan.outputs)
    changes = diff_snapshots(plan.before, after)
    artifacts = output_artifacts(changes, after, base=plan.cwd)
    command_text = display_command(redact_argv(argv))
    metadata = run_metadata(
        plan,
        command_text=command_text,
        outcome=outcome,
        started_at=started_at,
        ended_at=ended_at,
        duration=duration,
        changes=changes,
        listed=len(artifacts),
    )
    title = _run_title(plan.label, argv)
    summary = _run_summary(argv, outcome, duration)
    body = render_run_note(
        plan,
        title=title,
        summary=summary,
        command_text=command_text,
        outcome=outcome,
        started_at=started_at,
        ended_at=ended_at,
        duration=duration,
        changes=changes,
        artifacts=artifacts,
    )
    payload: JsonObject = {
        "title": title,
        "summary": summary,
        "status": "staged",
        "body": body,
        "metadata": metadata,
    }
    if plan.request_draft:
        payload["request_draft"] = True
    event = watch_capture.make_event(
        capture_id=plan.run_id,
        capture_kind=RUN_CAPTURE_KIND,
        adapter=RUN_EVIDENCE_ADAPTER,
        sink=watch_capture.SINK_STAGED_NOTE,
        observed_at=ended_at.isoformat(),
        source={
            "provider": RUN_EVIDENCE_PROVIDER,
            "external_id": f"{RUN_EVIDENCE_ADAPTER}:{plan.run_id}",
            **plan.session_fields,
        },
        context={
            "project_id": plan.project_id,
            "question_id": plan.question_id,
            "session_id": plan.session_id,
        },
        artifacts=artifacts,
        payload=payload,
    )
    watch_capture.write_event(event, plan.outbox)
    if plan.drain:
        notice = _drain(plan.outbox, client_factory)
        if notice:
            notices.append(notice)


def run_metadata(
    plan: _Plan,
    *,
    command_text: str,
    outcome: RunOutcome,
    started_at: datetime,
    ended_at: datetime,
    duration: float,
    changes: OutputChanges,
    listed: int,
) -> dict[str, NoteMetadataScalar]:
    """The note's scalar ``run_*`` metadata (namespaced like ``run_context``)."""

    metadata: dict[str, NoteMetadataScalar] = {
        "run_id": plan.run_id,
        "run_command": _clip(command_text, MAX_COMMAND_METADATA_CHARS),
        "run_cwd": str(plan.cwd),
        "run_started_at": started_at.isoformat(),
        "run_ended_at": ended_at.isoformat(),
        "run_duration_seconds": round(duration, 3),
        "run_exit_code": outcome.exit_code,
        "run_output_count": changes.count,
    }
    if outcome.signal_number is not None:
        metadata["run_exit_signal"] = outcome.signal_number
    if plan.label:
        metadata["run_label"] = plan.label
    if plan.outputs:
        metadata["run_output_roots"] = ",".join(
            _display_path(path, plan.cwd) for path in plan.outputs
        )
    if changes.truncated or listed < changes.count:
        metadata["run_outputs_truncated"] = True
    git = plan.git
    if git.get("git_commit"):
        metadata["run_git_commit"] = str(git["git_commit"])
    elif git.get("git_commit_error"):
        metadata["run_git_commit_error"] = str(git["git_commit_error"])
    if git:
        metadata.update(dirty_metadata(git, "run_"))
    if git.get("git_branch"):
        metadata["run_git_branch"] = str(git["git_branch"])
    if git.get("repo_remote_url"):
        metadata["run_repo_remote_url"] = str(git["repo_remote_url"])
    metadata.update(plan.worktree.as_fields("run_git_worktree_tree"))
    for key, value in plan.environment.items():
        if key.startswith("repo_environment") and isinstance(value, (str, int, float, bool)):
            metadata["run_environment" + key[len("repo_environment") :]] = value
    return metadata


def render_run_note(
    plan: _Plan,
    *,
    title: str,
    summary: str,
    command_text: str,
    outcome: RunOutcome,
    started_at: datetime,
    ended_at: datetime,
    duration: float,
    changes: OutputChanges,
    artifacts: Sequence[Mapping[str, Any]],
) -> str:
    """The staged note's markdown, bounded to :data:`MAX_BODY_CHARS`."""

    fence = _fence_for(command_text)
    lines = [
        f"# {title}",
        "",
        summary,
        "",
        "## Command",
        "",
        f"{fence}text",
        command_text,
        fence,
        "",
        f"- Working directory: `{plan.cwd}`",
        f"- Started: {started_at.isoformat()}",
        f"- Ended: {ended_at.isoformat()}",
        f"- Duration: {duration:.3f} s",
        f"- Exit code: {_exit_label(outcome)}",
    ]
    if plan.label:
        lines.append(f"- Label: {plan.label}")
    lines.extend(_code_lines(plan))
    environment = plan.environment
    if environment.get("repo_environment_hash"):
        lines.extend(["", "## Environment"])
        lines.append(f"- Environment hash: `{environment['repo_environment_hash']}`")
        lines.append(f"- Lockfiles: {environment.get('repo_environment_files') or 'none'}")
        lines.append(f"- Python: {environment.get('repo_environment_python') or 'unknown'}")
        if environment.get("repo_environment_container"):
            lines.append(f"- Container: `{environment['repo_environment_container']}`")
    lines.extend(_output_lines(plan, changes, artifacts))
    lines.extend(["", "## Research Context", f"- Project: `{plan.project_id}`"])
    if plan.question_id:
        lines.append(f"- Candidate question: `{plan.question_id}`")
    if plan.session_id:
        lines.append(f"- Session: `{plan.session_id}`")
    lines.extend(["", "## Lab Tracker Capture", f"- Run ID: `{plan.run_id}`"])
    body = "\n".join(lines).strip() + "\n"
    if len(body) > MAX_BODY_CHARS:
        marker = f"\n\n_(Note truncated at {MAX_BODY_CHARS} characters.)_\n"
        body = body[: MAX_BODY_CHARS - len(marker)] + marker
    return body


def _code_lines(plan: _Plan) -> list[str]:
    git = plan.git
    if not git and not plan.worktree.tree and not plan.worktree.error:
        return []
    lines = ["", "## Code"]
    if git.get("git_commit"):
        lines.append(f"- Git commit: `{git['git_commit']}`")
    elif git.get("git_commit_error"):
        lines.append(f"- Git commit: unknown ({git['git_commit_error']})")
    if git.get("git_branch"):
        lines.append(f"- Branch: `{git['git_branch']}`")
    if git:
        lines.append(f"- Dirty working tree: {dirty_label(git)}")
    if plan.worktree.tree:
        lines.append(f"- Worktree tree: `{plan.worktree.tree}`")
    elif plan.worktree.error:
        lines.append(f"- Worktree tree: unknown ({plan.worktree.error})")
    if git.get("repo_remote_url"):
        lines.append(f"- Remote: `{git['repo_remote_url']}`")
    return lines


def _output_lines(
    plan: _Plan, changes: OutputChanges, artifacts: Sequence[Mapping[str, Any]]
) -> list[str]:
    if not plan.outputs:
        return []
    roots = ", ".join(f"`{_display_path(path, plan.cwd)}`" for path in plan.outputs)
    lines = ["", "## Outputs", f"- Declared output folders: {roots}"]
    counts = (
        f"- {len(changes.created)} created, {len(changes.modified)} modified, "
        f"{changes.removed} removed"
    )
    if len(artifacts) < changes.count:
        counts += f" (first {len(artifacts)} listed)"
    if changes.truncated:
        counts += f"; snapshot stopped at {MAX_SNAPSHOT_FILES} files, so this may be incomplete"
    lines.append(counts)
    for artifact in artifacts:
        detail = f"{artifact['summary']}, {artifact['size_bytes']} bytes"
        if artifact.get("content_hash"):
            detail += f", `{artifact['content_hash']}`"
        else:
            detail += f", modified {artifact['modified_at']}, not hashed"
        lines.append(f"- `{artifact['title']}` — {detail}")
    return lines


# --- drain ----------------------------------------------------------------------


def _drain(outbox: Path, client_factory: Callable[[], LabTracker] | None) -> str | None:
    """Best-effort, bounded sync of the checkout's watch outbox; a notice when it fails.

    One ``/health`` probe with a short timeout first, so an unreachable or
    black-holed server costs :data:`HEALTH_PROBE_TIMEOUT_SECONDS`, not a
    timeout per queued event; then at most :data:`DRAIN_LIMIT` events. The
    rest (and anything that failed) waits for ``lt outbox sync``/``lt watch run``.
    """

    if client_factory is None and not server_configured():
        return None
    factory = client_factory or _client_from_env
    try:
        client = factory()
        try:
            try:
                client._request(
                    "GET", "/health", authenticated=False, timeout=HEALTH_PROBE_TIMEOUT_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 - reported as one notice below.
                cause = f"no answer from /health: {exc}"
                return RUN_SYNC_FAILED_NOTICE.format(outbox=outbox, cause=_one_line(cause))
            summary = watch_capture.sync_outbox_path(
                client, outbox, request_draft=False, limit=DRAIN_LIMIT
            )
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 - the event is durable; a later sync retries.
        return RUN_SYNC_FAILED_NOTICE.format(outbox=outbox, cause=_one_line(str(exc)))
    errors = summary.get("errors") if isinstance(summary, Mapping) else None
    if errors:
        first = errors[0].get("error") if isinstance(errors[0], Mapping) else None
        cause = f"{len(errors)} queued capture(s) failed: {first or 'see the event files'}"
        return RUN_SYNC_FAILED_NOTICE.format(outbox=outbox, cause=_one_line(cause))
    return None


def _client_from_env() -> LabTracker:
    return LabTracker.from_env(timeout_seconds=DRAIN_TIMEOUT_SECONDS)


def server_configured() -> bool:
    """True when a Lab Tracker server is named by the environment or saved profile."""

    if os.getenv(BASE_URL_ENV) or os.getenv(LEGACY_MCP_BASE_URL_ENV):
        return True
    return bool(load_connection_profile().get("base_url"))


# --- redaction --------------------------------------------------------------------

# Characters the displayed command leaves unquoted (shlex's safe set plus the
# brackets of the redaction marker); anything else is shell-quoted.
_UNQUOTED_ARG = re.compile(r"\A[\w@%+=:,./\[\]-]+\Z", re.ASCII)
# Programs whose ``-p`` carries a password, and how: mysql-family clients take
# it attached (``-pSECRET``; a bare ``-p`` prompts), sshpass and ``docker
# login`` take it attached or as the next argument.
_ATTACHED_PASSWORD_PROGRAMS = frozenset({"mysql", "mysqladmin", "mysqldump", "mariadb"})
_SEPARATE_PASSWORD_PROGRAMS = frozenset({"sshpass"})
_USER_FLAGS = frozenset({"-u", "--user"})


def redact_argv(argv: Sequence[str]) -> list[str]:
    """``argv`` with obvious secret values replaced by :data:`REDACTED`.

    Every element goes through the shared redactor
    (:func:`lab_tracker_client.redaction.redact_capture_text`: headers, URL
    credentials and query secrets, ``name=value`` pairs, token shapes, ...).
    On top of that, what only the argv boundaries reveal:

    * a secret-named flag given alone (``--token VALUE``, ``-password VALUE``)
      redacts the next argument unless it is itself a ``--`` option;
    * ``-u``/``--user USER:PW`` keeps the user and drops the password;
    * ``-p<pw>`` for mysql/mysqladmin/mysqldump/mariadb, and ``-p <pw>`` or
      ``-p<pw>`` for sshpass and ``docker login``; any other ``-p`` is left.

    Names that only point at a secret (``--password-file``, ``--token-name``)
    and negations (``--no-password``) are kept. This is a best-effort filter
    for the obvious cases, not a guarantee.
    """

    redacted: list[str] = []
    program = Path(str(argv[0])).name.lower() if argv else ""
    program = program[:-4] if program.endswith(".exe") else program
    docker_login = program == "docker" and "login" in [str(item) for item in argv[1:3]]
    short_password = program in _SEPARATE_PASSWORD_PROGRAMS or docker_login
    password_program = short_password or program in _ATTACHED_PASSWORD_PROGRAMS
    pending: str | None = None
    for index, raw in enumerate(argv):
        text = str(raw)
        if pending is not None:
            kind, pending = pending, None
            if kind == "user" and ":" in text:
                user = text.split(":", 1)[0]
                redacted.append(f"{user}:{REDACTED}")
                continue
            if kind == "value" and not text.startswith("--"):
                redacted.append(REDACTED)
                continue
        if index > 0 and text in _USER_FLAGS:
            pending = "user"
        elif password_program and index > 0 and text.startswith("-p") and text[2:3] != "-":
            if len(text) > 2:
                text = f"-p{REDACTED}"
            elif short_password:
                pending = "value"
        else:
            option = _option_name(text)
            if option is not None and not option[1] and looks_secret_name(option[0]):
                pending = "value"
        redacted.append(redact_capture_text(text))
    return redacted


def _option_name(text: str) -> tuple[str, bool] | None:
    """``(name, has_inline_value)`` for ``--name[=v]`` / ``-name[=v]``, else ``None``."""

    if text.startswith("--") and len(text) > 2:
        body = text[2:]
    elif text.startswith("-") and len(text) > 2 and not text[1].isdigit() and text[1] != "-":
        body = text[1:]
    else:
        return None
    name, equals, _value = body.partition("=")
    return name, bool(equals)


# --- helpers ----------------------------------------------------------------------


def new_run_id() -> str:
    """``run-<UTC stamp>-<8 hex>``: unique per run, sortable by start."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"


def _quietly(action: Callable[[], Any], notices: list[str]) -> Any:
    """Run a capture step with stderr buffered; turn any failure into a notice.

    Helpers the capture reuses (git probes) warn on stderr; the facts they
    warn about are recorded in the event instead, so ``lt run`` keeps its
    one-line stderr budget.
    """

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return action()
    except Exception as exc:  # noqa: BLE001 - capture must never break the command.
        notices.append(RUN_CAPTURE_FAILED_NOTICE.format(error=_one_line(str(exc))))
        return None


def _print_line(stream: TextIO, message: str) -> None:
    with contextlib.suppress(Exception):
        print(" ".join(str(message).split()), file=stream, flush=True)


def display_command(argv: Sequence[str]) -> str:
    """A (display-only) shell rendering of an already-redacted argv."""

    return " ".join(
        part if _UNQUOTED_ARG.match(part) else shlex.quote(part) for part in map(str, argv)
    )


def _run_title(label: str | None, argv: Sequence[str]) -> str:
    what = label or display_command(redact_argv(argv[:3]))
    return _clip(f"lt run: {what}", MAX_TITLE_CHARS)


def _run_summary(argv: Sequence[str], outcome: RunOutcome, duration: float) -> str:
    program = Path(argv[0]).name if argv else "command"
    verdict = "succeeded" if outcome.exit_code == 0 else f"exited {_exit_label(outcome)}"
    return f"`{program}` {verdict} after {duration:.1f} s."


def _exit_label(outcome: RunOutcome) -> str:
    if outcome.signal_number is None:
        return str(outcome.exit_code)
    name = ""
    with contextlib.suppress(ValueError):
        name = f", {signal.Signals(outcome.signal_number).name}"
    return f"{outcome.exit_code} (terminated by signal {outcome.signal_number}{name})"


def _display_path(path: Path, base: Path) -> str:
    try:
        relative = path.relative_to(base).as_posix()
    except ValueError:
        return str(path)
    return relative or "."


def _iso_from_ns(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, timezone.utc).isoformat()


def _fence_for(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _one_line(text: str) -> str:
    return _clip(" ".join(str(text).split()), MAX_NOTICE_CAUSE_CHARS)


def _optional(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "MAX_HASH_FILE_BYTES",
    "MAX_OUTPUT_ARTIFACTS",
    "REDACTED",
    "RUN_CAPTURE_KIND",
    "RUN_EVIDENCE_ADAPTER",
    "OutputChanges",
    "OutputSnapshot",
    "RunOptions",
    "RunOutcome",
    "diff_snapshots",
    "looks_secret_name",
    "output_artifacts",
    "redact_argv",
    "run_child",
    "run_command",
    "server_configured",
    "snapshot_outputs",
]
