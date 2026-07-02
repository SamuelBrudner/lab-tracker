"""Consent-gated setup helpers: connection profile, machine/repo inventory, binding.

Everything here is either read-only (``setup_status``) or gated by explicit
``--yes``/``--dry-run`` flags at the CLI layer. Setup actions deliberately stay
on the CLI — agent harnesses gate each mutating command behind their own
approval prompt — and ``setup_status`` must stay safe for agents and session
hooks to call unprompted: no writes, and the only network I/O is a single
swallowed health probe.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

import lab_tracker_client.hpc as hpc_capture
import lab_tracker_client.watch as watch_capture
from lab_tracker_client.client import (
    DEFAULT_BASE_URL,
    LabTracker,
    LTValidationError,
    connection_profile_path,
    load_connection_profile,
)
from lab_tracker_client.hooks import HOOK_BLOCK_BEGIN

JsonObject = dict[str, Any]

PROFILE_KEYS = ("base_url", "default_project_id", "access_token")
_HEALTH_PROBE_TIMEOUT_SECONDS = 2.0

_SCAFFOLD_FILES = (
    ".mcp.json",
    ".cursor/mcp.json",
    ".claude/settings.json",
    "scripts/lt.py",
    "AGENTS.lt.md",
    "lt_ids.json",
)


def save_connection_profile(
    *,
    base_url: str | None = None,
    default_project_id: str | None = None,
    access_token: str | None = None,
    dry_run: bool = False,
) -> JsonObject:
    """Merge the provided values into the profile file and harden permissions.

    Only explicitly provided values change; the access token is persisted only
    when passed (the CLI requires the separate ``--save-token`` consent for it).
    """

    path = connection_profile_path()
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    profile = load_connection_profile()
    updates = {
        "base_url": base_url,
        "default_project_id": default_project_id,
        "access_token": access_token,
    }
    for key, value in updates.items():
        if value is not None and value.strip():
            profile[key] = value.strip()
    proposed_text = json.dumps({key: profile[key] for key in sorted(profile)}, indent=2) + "\n"
    payload: JsonObject = {
        "command": "setup-connect",
        "path": str(path),
        "dry_run": dry_run,
        "stored_keys": sorted(profile),
        "has_token": "access_token" in profile,
        "diff": _text_diff(
            path,
            _redact_profile_text(existing_text),
            _redact_profile_text(proposed_text),
        ),
    }
    if dry_run:
        return payload
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        with suppress(OSError):
            path.parent.chmod(0o700)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    # Create the tmp file user-only BEFORE the token is written, so there is
    # no readable window; the mode travels with the atomic replace.
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(proposed_text)
    if sys.platform == "win32":
        _harden_profile_permissions(tmp_path)
    tmp_path.replace(path)
    payload["permissions_hardened"] = _harden_profile_permissions(path)
    return payload


def delete_connection_profile(*, dry_run: bool = False) -> JsonObject:
    path = connection_profile_path()
    payload: JsonObject = {
        "command": "setup-connect",
        "path": str(path),
        "dry_run": dry_run,
        "removed": path.exists(),
    }
    if not dry_run and path.exists():
        path.unlink()
    return payload


def _harden_profile_permissions(path: Path) -> bool:
    """Restrict the profile to the current user; best-effort, never raises.

    ``chmod`` is a no-op on Windows NTFS, so ACLs go through ``icacls`` there.
    """

    if sys.platform == "win32":
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        if not user:
            return False
        with suppress(Exception):
            completed = subprocess.run(  # noqa: S603 - fixed executable, no shell.
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return completed.returncode == 0
        return False
    with suppress(OSError):
        path.chmod(0o600)
        return True
    return False


def setup_status(target: str | Path = ".") -> JsonObject:
    """Read-only inventory of server, profile, repo scaffold, watch, hpc, hooks."""

    root = Path(target).expanduser().resolve()
    profile = load_connection_profile()
    base_url, base_url_source = _resolve_base_url(profile)
    return {
        "command": "setup-status",
        "target": str(root),
        "server": {
            "base_url": base_url,
            "source": base_url_source,
            "reachable": probe_health(base_url),
        },
        "profile": {
            "present": connection_profile_path().exists(),
            "path": str(connection_profile_path()),
            "base_url": profile.get("base_url"),
            "default_project_id": profile.get("default_project_id"),
            "has_token": bool(
                os.getenv("LAB_TRACKER_ACCESS_TOKEN") or profile.get("access_token")
            ),
        },
        "repo": _repo_status(root),
        "watch": _watch_status(root),
        "hpc": _hpc_status(root),
        "hooks": _hooks_status(root),
    }


def _resolve_base_url(profile: dict[str, str]) -> tuple[str, str]:
    from_env = os.getenv("LAB_TRACKER_BASE_URL") or os.getenv("LAB_TRACKER_MCP_BASE_URL")
    if from_env:
        return from_env, "env"
    if profile.get("base_url"):
        return profile["base_url"], "profile"
    return DEFAULT_BASE_URL, "default"


def probe_health(base_url: str) -> bool:
    with suppress(Exception):
        response = httpx.get(
            base_url.rstrip("/") + "/health",
            timeout=_HEALTH_PROBE_TIMEOUT_SECONDS,
        )
        return bool(response.status_code < 500)
    return False


def _repo_status(root: Path) -> JsonObject:
    from lab_tracker.cli import _doctor
    from lab_tracker.decision_context_constants import CLAUDE_BLOCK_BEGIN

    scaffold = {name: (root / name).exists() for name in _SCAFFOLD_FILES}
    claude_md = root / "CLAUDE.md"
    activation_present = False
    with suppress(OSError, UnicodeDecodeError):
        activation_present = CLAUDE_BLOCK_BEGIN in claude_md.read_text(encoding="utf-8")
    ids_present = (root / "lt_ids.json").exists()
    project_id = ""
    project_name = ""
    if ids_present:
        with suppress(Exception):
            payload = json.loads((root / "lt_ids.json").read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                project_id = str(payload.get("project_id") or "")
                project_name = str(payload.get("project_name") or "")
    conventions = []
    with suppress(Exception):
        conventions = [
            {
                "name": item.get("name"),
                "present": item.get("present"),
                "drifted": item.get("drifted"),
            }
            for item in _doctor(root).get("targets", [])
            if isinstance(item, dict)
        ]
    return {
        "scaffold": scaffold,
        "claude_activation_block_present": activation_present,
        "lt_ids": {
            "present": ids_present,
            "project_id_bound": bool(project_id),
            "project_name": project_name,
        },
        "conventions": conventions,
    }


def _watch_status(root: Path) -> JsonObject:
    try:
        config = watch_capture.load_config(start=root)
    except LTValidationError as exc:
        return {"config_present": False, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - status is a read-only inventory.
        return {"config_present": None, "detail": str(exc)}
    summary: JsonObject = {
        "config_present": True,
        "config_path": str(config.config_path),
        "project_id": config.project_id,
        "watch_count": len(config.watches),
    }
    with suppress(Exception):
        summary["outbox"] = watch_capture.outbox_status(config.outbox_path())
    return summary


def _hpc_status(root: Path) -> JsonObject:
    try:
        config = hpc_capture.load_config(start=root)
    except LTValidationError as exc:
        return {"config_present": False, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - status is a read-only inventory.
        return {"config_present": None, "detail": str(exc)}
    return {
        "config_present": True,
        "config_path": str(config.config_path),
        "cluster": config.cluster,
    }


def _hooks_status(root: Path) -> JsonObject:
    hook_path: Path | None = None
    with suppress(Exception):
        completed = subprocess.run(  # noqa: S603 - fixed executable, no shell.
            ["git", "rev-parse", "--git-path", "hooks/post-commit"],
            cwd=root,
            capture_output=True,
            timeout=10,
            text=True,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            candidate = Path(completed.stdout.strip())
            hook_path = candidate if candidate.is_absolute() else root / candidate
    if hook_path is None:
        return {"git_repo": False}
    managed_block_present = False
    with suppress(OSError, UnicodeDecodeError):
        managed_block_present = HOOK_BLOCK_BEGIN in hook_path.read_text(encoding="utf-8")
    return {
        "git_repo": True,
        "post_commit_path": str(hook_path),
        "post_commit_present": hook_path.exists(),
        "managed_block_present": managed_block_present,
    }


def bind_project(
    client: LabTracker,
    *,
    project_id: str | None = None,
    name: str | None = None,
    create: bool = False,
    ids_path: str | Path = "lt_ids.json",
    dry_run: bool = False,
) -> JsonObject:
    """Resolve a project and write its id into lt_ids.json with a diff preview."""

    if bool(project_id) == bool(name):
        raise LTValidationError("Pass exactly one of --project-id or --name.")
    created_project = False
    if project_id:
        record = _find_project_by_id(client, project_id)
        if record is None:
            raise LTValidationError(f"No project found with id {project_id}.")
    else:
        matches = [
            project
            for project in client.list_projects()
            if str(project.get("name") or "") == name
        ]
        if len(matches) > 1:
            raise LTValidationError(
                f"Multiple projects are named {name!r}; bind by --project-id instead."
            )
        if matches:
            record = matches[0]
        elif create:
            # Dry run must not touch the server: preview with a placeholder
            # id instead of creating the project.
            record = None if dry_run else client.upsert_project(name=str(name))
            created_project = True
        else:
            raise LTValidationError(
                f"No project named {name!r}. Pass --create to create it."
            )
    if record is not None:
        resolved_id = str(record.get("project_id") or record.id)
        resolved_name = str(record.get("name") or "")
    else:
        resolved_id = "<created-on-apply>"
        resolved_name = str(name)
    path = Path(ids_path)
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    ids_payload: dict[str, str] = {}
    if existing_text.strip():
        try:
            parsed = json.loads(existing_text)
        except json.JSONDecodeError as exc:
            raise LTValidationError(f"{path} is not valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise LTValidationError(f"{path} must contain a JSON object of string ids.")
        ids_payload = {str(key): str(value) for key, value in parsed.items()}
    ids_payload["project_id"] = resolved_id
    if resolved_name:
        ids_payload["project_name"] = resolved_name
    proposed_text = json.dumps(ids_payload, indent=2) + "\n"
    payload: JsonObject = {
        "command": "project-bind",
        "action": "would-bind" if dry_run else "bound",
        "project_id": resolved_id,
        "project_name": resolved_name,
        "created_project": created_project,
        "ids_path": str(path),
        "diff": _text_diff(path, existing_text, proposed_text),
    }
    if not dry_run:
        path.write_text(proposed_text, encoding="utf-8")
    return payload


def _find_project_by_id(client: LabTracker, project_id: str) -> Any:
    for project in client.list_projects():
        if str(project.get("project_id") or "") == project_id:
            return project
    return None


def _text_diff(path: Path, existing: str, proposed: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            existing.splitlines(),
            proposed.splitlines(),
            fromfile=f"{path} (current)",
            tofile=f"{path} (proposed)",
            lineterm="",
        )
    )


def _redact_profile_text(text: str) -> str:
    """Keep tokens out of diffs shown to agents and terminals.

    Both diff sides go through this, so a stored token can never surface on
    the ``-`` line of a later run. The regex fallback covers text the JSON
    parser cannot handle (e.g. a hand-mangled profile).
    """

    if not text:
        return text
    with suppress(Exception):
        payload = json.loads(text)
        if isinstance(payload, dict):
            if "access_token" in payload:
                payload["access_token"] = "***redacted***"
            return json.dumps(payload, indent=2) + "\n"
    return re.sub(
        r'("access_token"\s*:\s*")[^"]*(")',
        r"\1***redacted***\2",
        text,
    )
