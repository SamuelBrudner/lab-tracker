"""Create a reviewable analysis graph draft through the Lab Tracker API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


def main() -> int:
    args = _parse_args()
    base_url = _first_value(args.base_url, "LAB_TRACKER_BASE_URL", "LAB_TRACKER_MCP_BASE_URL")
    args.project_id = _first_value(args.project_id, "LAB_TRACKER_PROJECT_ID")
    args.git_commit = _first_value(args.git_commit, "LAB_TRACKER_GIT_COMMIT")
    git_repo = _first_value(
        str(args.git_repo) if args.git_repo else None,
        "LAB_TRACKER_GIT_REPO",
    )
    args.git_repo = Path(git_repo or ".")
    if not base_url:
        _die("Set --base-url, LAB_TRACKER_BASE_URL, or LAB_TRACKER_MCP_BASE_URL.")
    if args.evidence_file and args.git_commit:
        _die("Provide only one evidence source: --evidence-file or --git-commit.")
    if not args.note_id:
        if not args.project_id:
            _die("Provide --project-id or LAB_TRACKER_PROJECT_ID when creating a note.")
        if not args.evidence_file and not args.git_commit:
            _die("Provide --note-id, or create a note with --evidence-file or --git-commit.")

    timeout = httpx.Timeout(args.timeout_seconds)
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        headers = _auth_headers(client, args)
        note_id = args.note_id
        if note_id is None:
            note_id = _create_evidence_note(client, args, headers=headers)
        draft = _post_envelope(
            client,
            f"/notes/{note_id}/analysis-graph-drafts",
            headers=headers,
        )

    print(json.dumps({"note_id": note_id, "change_set": draft}, indent=2, sort_keys=True))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a Lab Tracker analysis graph draft from CI evidence. The script creates "
            "or reuses a source note, then asks Lab Tracker to store an LLM-backed graph "
            "draft for human review."
        )
    )
    parser.add_argument("--base-url", help="Lab Tracker API base URL.")
    parser.add_argument(
        "--project-id",
        help="Project ID for a new evidence note. Defaults to LAB_TRACKER_PROJECT_ID.",
    )
    parser.add_argument("--note-id", help="Existing evidence note ID to draft from.")
    parser.add_argument("--evidence-file", type=Path, help="UTF-8 text or markdown evidence file.")
    parser.add_argument(
        "--git-repo",
        type=Path,
        help=(
            "Git repository to summarize when --git-commit is supplied. "
            "Defaults to LAB_TRACKER_GIT_REPO or the current directory."
        ),
    )
    parser.add_argument(
        "--git-commit",
        help=(
            "Git commit-ish to summarize as analysis evidence. "
            "Defaults to LAB_TRACKER_GIT_COMMIT."
        ),
    )
    parser.add_argument(
        "--git-max-diff-lines",
        type=int,
        default=int(os.environ.get("LAB_TRACKER_GIT_MAX_DIFF_LINES", "800")),
        help="Maximum diff lines to include in generated git evidence.",
    )
    parser.add_argument(
        "--git-context-lines",
        type=int,
        default=int(os.environ.get("LAB_TRACKER_GIT_CONTEXT_LINES", "3")),
        help="Unified diff context lines for generated git evidence.",
    )
    parser.add_argument(
        "--note-status",
        default="committed",
        choices=["staged", "committed", "archived"],
        help="Status for the evidence note when --note-id is not supplied.",
    )
    parser.add_argument(
        "--metadata-json",
        type=Path,
        help="Optional JSON object with scalar metadata to attach to a new evidence note.",
    )
    parser.add_argument("--token", help="Bearer token. Defaults to LAB_TRACKER_TOKEN.")
    parser.add_argument("--username", help="Login username. Defaults to LAB_TRACKER_USERNAME.")
    parser.add_argument("--password", help="Login password. Defaults to LAB_TRACKER_PASSWORD.")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args()


def _first_value(value: str | None, *env_names: str) -> str | None:
    if value:
        return value
    for env_name in env_names:
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value
    return None


def _auth_headers(client: httpx.Client, args: argparse.Namespace) -> dict[str, str]:
    token = _first_value(args.token, "LAB_TRACKER_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}

    username = _first_value(
        args.username,
        "LAB_TRACKER_USERNAME",
        "LAB_TRACKER_MCP_USERNAME",
    )
    password = _first_value(
        args.password,
        "LAB_TRACKER_PASSWORD",
        "LAB_TRACKER_MCP_PASSWORD",
    )
    if not username and not password:
        return {}
    if not username or not password:
        _die("Provide both username and password, or neither for auth-disabled local APIs.")

    login = _post_envelope(
        client,
        "/auth/login",
        json_payload={"username": username, "password": password},
        headers={},
    )
    access_token = login.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        _die("Lab Tracker login response did not include an access token.")
    return {"Authorization": f"Bearer {access_token}"}


def _create_evidence_note(
    client: httpx.Client,
    args: argparse.Namespace,
    *,
    headers: dict[str, str],
) -> str:
    if args.evidence_file:
        evidence_text, metadata = _file_evidence(args.evidence_file)
    else:
        evidence_text, metadata = _git_commit_evidence(
            args.git_repo,
            args.git_commit,
            max_diff_lines=args.git_max_diff_lines,
            context_lines=args.git_context_lines,
        )
    if not evidence_text.strip():
        _die("Evidence file must not be empty.")

    metadata.update(_metadata_from_json(args.metadata_json))
    note = _post_envelope(
        client,
        "/notes",
        json_payload={
            "project_id": args.project_id,
            "raw_content": evidence_text,
            "metadata": metadata,
            "status": args.note_status,
        },
        headers=headers,
    )
    note_id = note.get("note_id")
    if not isinstance(note_id, str) or not note_id:
        _die("Lab Tracker note response did not include note_id.")
    return note_id


def _file_evidence(evidence_path: Path) -> tuple[str, dict[str, str]]:
    try:
        evidence_text = evidence_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _die(f"Could not read evidence file {evidence_path}: {exc}")
    return evidence_text, _ci_metadata(evidence_path, evidence_text)


def _ci_metadata(evidence_path: Path, evidence_text: str) -> dict[str, str]:
    provider = "github-actions" if os.environ.get("GITHUB_REPOSITORY") else "ci"
    run_id = os.environ.get("GITHUB_RUN_ID") or str(evidence_path)
    metadata = {
        "source": "ci-analysis-graph-draft",
        "evidence_path": str(evidence_path),
        "evidence_source_provider": provider,
        "evidence_source_uri": str(evidence_path),
        "evidence_source_external_id": run_id,
        "evidence_source_observed_at": _utc_iso(),
        "evidence_capture_kind": "analysis_evidence",
        "evidence_content_hash": _text_hash(evidence_text),
        "evidence_adapter": "create-analysis-graph-draft",
        "evidence_title": evidence_path.name,
    }
    for env_name in (
        "GITHUB_REPOSITORY",
        "GITHUB_REF",
        "GITHUB_SHA",
        "GITHUB_WORKFLOW",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
    ):
        value = os.environ.get(env_name)
        if value:
            metadata[env_name.lower()] = value
    return metadata


def _git_commit_evidence(
    repo_path: Path,
    commit: str,
    *,
    max_diff_lines: int,
    context_lines: int,
) -> tuple[str, dict[str, str | bool | int | float]]:
    if max_diff_lines < 0:
        _die("--git-max-diff-lines must be zero or greater.")
    if context_lines < 0:
        _die("--git-context-lines must be zero or greater.")

    repo = Path(repo_path)
    root = _git(repo, "rev-parse", "--show-toplevel").strip()
    commit_sha = _git(repo, "rev-parse", f"{commit}^{{commit}}").strip()
    branch = _git_optional(repo, "branch", "--show-current").strip() or "detached"
    remote_url = _git_optional(repo, "config", "--get", "remote.origin.url").strip()
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
    metadata: dict[str, str | bool | int | float] = {
        "source": "git-commit-analysis-graph-draft",
        "git_repository_path": root,
        "git_repository_name": Path(root).name,
        "git_commit": commit_sha,
        "git_branch": branch,
        "git_diff_truncated": truncated,
        "git_max_diff_lines": max_diff_lines,
        "evidence_source_provider": "git",
        "evidence_source_uri": remote_url or root,
        "evidence_source_external_id": commit_sha,
        "evidence_source_observed_at": _utc_iso(),
        "evidence_capture_kind": "git_commit",
        "evidence_content_hash": _text_hash(evidence),
        "evidence_adapter": "create-analysis-graph-draft",
        "evidence_title": f"{Path(root).name}@{commit_sha[:12]}",
    }
    if remote_url:
        metadata["git_remote_origin_url"] = remote_url
    return evidence, metadata


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _truncate_lines(text: str, max_lines: int) -> tuple[str, bool]:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text, False
    visible = lines[:max_lines]
    visible.append(f"... truncated {len(lines) - max_lines} additional diff lines ...")
    return "\n".join(visible), True


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        _die("git executable was not found.")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip()
        _die(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout


def _git_optional(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
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


def _metadata_from_json(path: Path | None) -> dict[str, str | bool | int | float]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _die(f"Could not load metadata JSON {path}: {exc}")
    if not isinstance(payload, dict):
        _die("Metadata JSON must be an object.")
    return {str(key): _metadata_scalar(value) for key, value in payload.items()}


def _metadata_scalar(value: Any) -> str | bool | int | float:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    return json.dumps(value, sort_keys=True)


def _post_envelope(
    client: httpx.Client,
    path: str,
    *,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = client.post(path, json=json_payload, headers=headers)
    if response.status_code >= 400:
        _die(f"POST {path} failed with HTTP {response.status_code}: {response.text}")
    try:
        payload = response.json()
    except ValueError as exc:
        _die(f"POST {path} returned non-JSON content: {exc}")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        _die(f"POST {path} did not return a Lab Tracker envelope.")
    return payload["data"]


def _die(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
