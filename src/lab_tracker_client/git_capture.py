"""Outbox-backed git commit capture.

``snapshot_commit`` renders a commit as evidence text (byte-compatible with the
legacy ``scripts/create-analysis-graph-draft.py`` rendering, so notes created by
either path dedupe against each other on content hash) and queues it as a
deterministic watch-outbox event. Commits made while the server is unreachable
stay queued and drain on any later sync — the hook never loses a commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import lab_tracker_client.watch as watch_capture
from lab_tracker.repository_conventions import (
    REPOSITORY_CONVENTIONS_HASH_METADATA_KEY,
    capture_repository_conventions,
    repository_conventions_metadata,
)
from lab_tracker_client.client import LTValidationError
from lab_tracker_client.repo import normalize_remote

JsonObject = dict[str, Any]

GIT_EVIDENCE_ADAPTER = "lt-git-snapshot"
GIT_CAPTURE_KIND = "git_commit"
DEFAULT_MAX_DIFF_LINES = 800
DEFAULT_CONTEXT_LINES = 3


def snapshot_commit(
    *,
    repo: str | Path = ".",
    commit: str = "HEAD",
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_ids: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    config_path: str | Path | None = None,
    max_diff_lines: int | None = None,
    context_lines: int | None = None,
    request_draft: bool = False,
) -> JsonObject:
    """Queue one commit as a staged-note outbox event; idempotent per commit.

    ``request_draft`` is recorded on the event itself (reserved
    ``payload.request_draft`` key), so a later outbox drain requests a graph
    draft for this commit only — never for unrelated queued captures.
    """

    resolved_max = (
        max_diff_lines
        if max_diff_lines is not None
        else _env_int("LAB_TRACKER_GIT_MAX_DIFF_LINES", DEFAULT_MAX_DIFF_LINES)
    )
    resolved_context = (
        context_lines
        if context_lines is not None
        else _env_int("LAB_TRACKER_GIT_CONTEXT_LINES", DEFAULT_CONTEXT_LINES)
    )
    repo_root = repo_toplevel(repo)
    evidence, facts = commit_evidence(
        repo_root,
        commit,
        max_diff_lines=resolved_max,
        context_lines=resolved_context,
    )
    agent_context_error = ""
    remote_for_id = normalize_remote(str(facts.get("git_remote_origin_url") or ""))
    try:
        convention_snapshot = capture_repository_conventions(
            repo_root,
            commit=str(facts["git_commit"]),
            repository=remote_for_id or f"local:{repo_root.name}",
        )
    except ValueError as exc:
        # A malformed optional enrollment must never lose the commit itself.
        convention_snapshot = None
        agent_context_error = str(exc)
    facts.update(repository_conventions_metadata(convention_snapshot))
    config, config_error = resolve_watch_config(repo_root, config_path)
    # The repo's own lt_ids.json binding outranks a (possibly parent-dir)
    # watch config: repo-local intent wins.
    resolved_project = (
        _optional(project_id)
        or _optional(os.getenv("LAB_TRACKER_PROJECT_ID"))
        or _project_from_ids(repo_root)
        or _optional(config.project_id)
    )
    if not resolved_project:
        raise LTValidationError(
            "No project id for git snapshot. Pass --project, set "
            "LAB_TRACKER_PROJECT_ID, or bind one with 'lt project bind'."
        )
    content_hash = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    commit_sha = str(facts["git_commit"])
    repo_name = str(facts["git_repository_name"])
    # Shared git-evidence identity (lt-81s6.8/.17): <normalized-remote>@<commit>,
    # owned by lab_tracker_client.repo, so hook-, CLI-, and CI-captured evidence
    # for one commit dedup to a single identity instead of parallel note streams.
    source_external_id = f"{remote_for_id or 'local'}@{commit_sha}"
    event = watch_capture.make_event(
        capture_id=f"git-{repo_name}-{commit_sha[:12]}",
        event_id=f"commit-{content_hash[:16]}",
        capture_kind=GIT_CAPTURE_KIND,
        adapter=GIT_EVIDENCE_ADAPTER,
        sink=watch_capture.SINK_STAGED_NOTE,
        source={
            "provider": "git",
            "uri": facts.get("git_remote_origin_url") or facts["git_repository_path"],
            "external_id": source_external_id,
            "content_hash": content_hash,
            **facts,
        },
        context={
            "project_id": resolved_project,
            "question_id": question_id,
            "dataset_ids": dataset_ids,
            "tags": tags,
        },
        payload={
            "title": f"{repo_name}@{commit_sha[:12]}",
            "summary": f"Captured git commit {commit_sha[:12]} on {facts['git_branch']}.",
            "status": "staged",
            "body": evidence,
            **({"request_draft": True} if request_draft else {}),
        },
    )
    outbox = config.outbox_path()
    event_file = watch_capture.event_path(event, outbox)
    already_queued = event_file.exists()
    context_ignored = False
    stored_conventions_hash = ""
    if already_queued:
        # write_event is a no-op for an existing file (preserving its sync
        # state), so report the STORED context rather than pretending the
        # newly resolved flags were applied.
        with suppress(Exception):
            existing = watch_capture.read_event(event_file)
            stored_conventions_hash = str(
                existing["source"].get(REPOSITORY_CONVENTIONS_HASH_METADATA_KEY) or ""
            )
            if existing["context"] != event["context"] or (
                bool(existing["payload"].get("request_draft")) != request_draft
            ) or (
                stored_conventions_hash
                != str(
                    event["source"].get(REPOSITORY_CONVENTIONS_HASH_METADATA_KEY)
                    or ""
                )
            ):
                context_ignored = True
            resolved_project = existing["context"].get("project_id") or resolved_project
    written_path = watch_capture.write_event(event, outbox)
    payload: JsonObject = {
        "command": "git-snapshot",
        "repo": str(repo_root),
        "commit": commit_sha,
        "project_id": resolved_project,
        "capture_id": event["capture_id"],
        "event_id": event["event_id"],
        "event_path": str(written_path),
        "outbox": str(outbox),
        "already_queued": already_queued,
        "queued": True,
    }
    if context_ignored:
        payload["context_ignored"] = True
    if config_error:
        payload["config_error"] = config_error
    if convention_snapshot is not None:
        requested_hash = convention_snapshot["snapshot_hash"]
        payload["repository_conventions_hash"] = (
            stored_conventions_hash if already_queued else requested_hash
        )
        if already_queued and stored_conventions_hash != requested_hash:
            payload["requested_repository_conventions_hash"] = requested_hash
        payload["repository_convention_files"] = sum(
            len(document["paths"]) for document in convention_snapshot["documents"]
        )
    if agent_context_error:
        payload["agent_context_error"] = agent_context_error
    return payload


def repo_toplevel(repo: str | Path = ".") -> Path:
    root = _git(Path(repo).expanduser(), "rev-parse", "--show-toplevel").strip()
    return Path(root).resolve()


def resolve_watch_config(
    repo_root: Path,
    config_path: str | Path | None = None,
) -> tuple[watch_capture.WatchConfig, str | None]:
    """Return (config, error-detail) for the repo's own watch config.

    Only the repo-local ``.lab-tracker/watch.json`` (or an explicit
    ``--config`` / ``LAB_TRACKER_WATCH_CONFIG``) is considered — no
    parent-directory walk, so a lab-wide config cannot silently claim a
    repo's commits. Snapshot runs inside a post-commit hook, so ANY broken
    config must not lose the commit: fall back to a default config, salvaging
    the configured outbox string leniently when the JSON still parses so
    queued events land where later syncs will drain them.
    """

    explicit = config_path or os.getenv("LAB_TRACKER_WATCH_CONFIG")
    candidate = (
        Path(explicit).expanduser().resolve()
        if explicit
        else repo_root / ".lab-tracker" / "watch.json"
    )
    if not candidate.exists():
        return (
            watch_capture.WatchConfig(config_path=repo_root / ".lab-tracker" / "watch.json"),
            None,
        )
    try:
        return watch_capture.load_config(config_path=candidate), None
    except Exception as exc:  # noqa: BLE001 - a broken config must never lose a commit.
        fallback = watch_capture.WatchConfig(config_path=candidate)
        with suppress(Exception):
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and str(raw.get("outbox") or "").strip():
                fallback.outbox = str(raw["outbox"]).strip()
        return fallback, f"watch config could not be loaded ({exc}); using {fallback.outbox}"


def commit_evidence(
    repo_path: str | Path,
    commit: str,
    *,
    max_diff_lines: int = DEFAULT_MAX_DIFF_LINES,
    context_lines: int = DEFAULT_CONTEXT_LINES,
) -> tuple[str, JsonObject]:
    """Render a commit as evidence markdown plus flat ``git_*`` facts.

    The markdown must stay byte-compatible with the legacy
    ``create-analysis-graph-draft.py`` rendering: identical text means an
    identical ``evidence_content_hash``, so commits captured by either path
    dedupe against each other server-side.
    """

    if max_diff_lines < 0:
        raise LTValidationError("--max-diff-lines must be zero or greater.")
    if context_lines < 0:
        raise LTValidationError("--context-lines must be zero or greater.")

    repo = Path(repo_path)
    root = _git(repo, "rev-parse", "--show-toplevel").strip()
    commit_sha = _git(repo, "rev-parse", f"{commit}^{{commit}}").strip()
    branch = _git_optional(repo, "branch", "--show-current").strip() or "detached"
    remote_url = _strip_url_credentials(
        _git_optional(repo, "config", "--get", "remote.origin.url").strip()
    )
    metadata_text = _git(
        repo,
        "show",
        "-s",
        "--format=fuller",
        "--no-ext-diff",
        commit_sha,
    ).strip()
    stat_text = _git(
        repo,
        "show",
        "-s",
        "--stat",
        "--summary",
        "--format=",
        "--no-ext-diff",
        commit_sha,
    ).strip()
    diff_text = _git(
        repo,
        "show",
        "--find-renames",
        "--find-copies",
        f"--unified={context_lines}",
        "--format=",
        "--no-color",
        "--no-ext-diff",
        commit_sha,
    )
    diff_text, truncated = _truncate_lines(diff_text.strip(), max_diff_lines)

    evidence = "\n\n".join(
        part
        for part in (
            "# Git Commit Evidence",
            (
                "This note was generated from a git post-commit workflow. Treat the commit as "
                "analysis or provenance evidence for human-reviewed Lab Tracker graph draft "
                "proposals. Prefer conservative proposals when the diff does not by itself "
                "establish a scientific claim."
            ),
            "## Repository\n"
            f"- path: {root}\n"
            f"- branch_at_draft_time: {branch}\n"
            f"- remote_origin: {remote_url or 'not configured'}\n"
            f"- commit: {commit_sha}",
            f"## Commit Metadata\n\n```text\n{metadata_text}\n```",
            f"## File Summary\n\n```text\n{stat_text or 'No file summary reported.'}\n```",
            f"## Diff\n\n```diff\n{diff_text or 'No textual diff reported.'}\n```",
            (
                "Diff was truncated by LAB_TRACKER_GIT_MAX_DIFF_LINES."
                if truncated
                else ""
            ),
        )
        if part
    )
    facts: JsonObject = {
        "git_repository_path": root,
        "git_repository_name": Path(root).name,
        "git_commit": commit_sha,
        "git_branch": branch,
        "git_diff_truncated": truncated,
        "git_max_diff_lines": max_diff_lines,
    }
    if remote_url:
        facts["git_remote_origin_url"] = remote_url
    return evidence, facts


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_lines(text: str, max_lines: int) -> tuple[str, bool]:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    visible = lines[:max_lines]
    visible.append(f"... truncated {len(lines) - max_lines} additional diff lines ...")
    return "\n".join(visible), True


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable, no shell.
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise LTValidationError("git executable was not found.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or (exc.stdout or "").strip()
        raise LTValidationError(f"git {' '.join(args)} failed: {stderr}") from exc
    return result.stdout


def _git_optional(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable, no shell.
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _project_from_ids(repo_root: Path) -> str | None:
    """Fail-soft read of the repo's lt_ids.json project binding."""

    with suppress(Exception):
        payload = json.loads((repo_root / "lt_ids.json").read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return _optional(str(payload.get("project_id") or ""))
    return None


def _strip_url_credentials(url: str) -> str:
    """Drop user:password userinfo from remote URLs so tokens never enter
    evidence text or metadata. Bare usernames (ssh's ``git@``) are kept —
    they are addressing, not secrets."""

    if not url or "@" not in url:
        return url
    with suppress(ValueError):
        parts = urlsplit(url)
        if parts.netloc and "@" in parts.netloc:
            userinfo, _, host = parts.netloc.rpartition("@")
            if ":" in userinfo:
                return urlunsplit(
                    (parts.scheme, host, parts.path, parts.query, parts.fragment)
                )
    return url
