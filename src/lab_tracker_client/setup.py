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
    access_token_from_env,
    connection_profile_path,
    load_connection_profile,
)
from lab_tracker_client.hooks import HOOK_BLOCK_BEGIN, hook_path_for_repo

JsonObject = dict[str, Any]

PROFILE_KEYS = (
    "base_url",
    "default_project_id",
    "access_token",
    "capture_host_label",
    "default_capture_context_id",
)
_HEALTH_PROBE_TIMEOUT_SECONDS = 2.0

_SCAFFOLD_FILES = (
    ".mcp.json",
    ".cursor/mcp.json",
    ".gemini/settings.json",
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
    capture_host_label: str | None = None,
    default_capture_context_id: str | None = None,
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
        "capture_host_label": capture_host_label,
        "default_capture_context_id": default_capture_context_id,
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


def setup_status(target: str | Path = ".", *, brief: bool = False) -> JsonObject:
    """Read-only inventory of server, profile, repo scaffold, watch, hpc, hooks.

    ``brief`` reduces the payload for session hooks: one line when healthy, a
    short advisory plus the suggestions when anything is missing or drifted.
    Suggestions are non-imperative — they name what a command does; a person
    decides whether it runs.
    """

    root = Path(target).expanduser().resolve()
    profile = load_connection_profile()
    base_url, base_url_source = _resolve_base_url(profile)
    payload: JsonObject = {
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
            "capture_host_label": profile.get("capture_host_label"),
            "default_capture_context_id": profile.get("default_capture_context_id"),
            "has_token": bool(access_token_from_env() or profile.get("access_token")),
        },
        "repo": _repo_status(root),
        "watch": _watch_status(root),
        "hpc": _hpc_status(root),
        "hooks": _hooks_status(root),
        "skills": _skills_status(),
        "installation": _installation_status(),
    }
    payload["identity"] = identity_status(
        base_url,
        reachable=bool(payload["server"]["reachable"]),
    )
    _add_identity_warnings(payload)
    payload["suggestions"] = _suggestions(payload)
    if not brief:
        return payload
    suggestions = payload["suggestions"]
    warnings = payload.get("warnings") or []
    if warnings:
        brief_line = f"lab-tracker: warning -- {warnings[0]}"
    elif suggestions:
        brief_line = (
            f"lab-tracker: {len(suggestions)} setup suggestion(s) — {suggestions[0]}"
        )
    else:
        brief_line = "lab-tracker: capture is configured; server reachable."
        if not payload["server"]["reachable"]:
            brief_line = "lab-tracker: capture is configured; server currently unreachable."
    return {
        "command": "setup-status",
        "brief": brief_line,
        "suggestions": suggestions,
        "warnings": warnings,
    }


def identity_status(base_url: str, *, reachable: bool | None = None) -> JsonObject:
    """Resolve the current Lab Tracker user without making status commands fragile."""

    if reachable is False:
        return {
            "available": False,
            "base_url": base_url,
            "reason": "server_unreachable",
            "warnings": [],
        }
    client = LabTracker.from_env()
    try:
        response = client.whoami()
    except Exception as exc:  # noqa: BLE001 - status must fail soft.
        return {
            "available": False,
            "base_url": base_url,
            "error": str(exc),
            "warnings": [],
        }
    finally:
        client.close()
    data = response.get("data") if isinstance(response, dict) else None
    meta = response.get("meta") if isinstance(response, dict) else None
    data = data if isinstance(data, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    payload: JsonObject = {
        "available": True,
        "base_url": base_url,
        "resolved_user": {
            "user_id": data.get("user_id"),
            "username": data.get("username"),
            "role": data.get("role"),
        },
        "auth_enabled": meta.get("auth_enabled"),
        "principal_type": meta.get("principal_type"),
        "is_interactive": meta.get("is_interactive"),
        "warnings": [],
    }
    service_token = meta.get("service_token")
    if isinstance(service_token, dict):
        payload["service_token"] = service_token
    if meta.get("auth_enabled") is False:
        payload["warnings"] = [
            "Authentication is disabled on this Lab Tracker server; status can "
            "resolve only the local tester identity, and scripted accept/commit "
            "calls are not structurally distinguishable from a person."
        ]
    return payload


def with_identity_status(payload: JsonObject) -> JsonObject:
    """Attach the same fail-soft identity block used by setup status."""

    profile = load_connection_profile()
    base_url, _source = _resolve_base_url(profile)
    payload["identity"] = identity_status(base_url, reachable=probe_health(base_url))
    _add_identity_warnings(payload)
    return payload


def _add_identity_warnings(payload: JsonObject) -> None:
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        return
    warnings = [
        str(item)
        for item in identity.get("warnings", [])
        if str(item or "").strip()
    ]
    if not warnings:
        return
    existing = [
        str(item)
        for item in payload.get("warnings", [])
        if str(item or "").strip()
    ]
    payload["warnings"] = existing + warnings


def _suggestions(status: JsonObject) -> list[str]:
    suggestions: list[str] = []
    repo = status["repo"]
    hooks = status["hooks"]
    watch = status["watch"]
    skills = status["skills"]
    installation = status.get("installation") or {}
    # Only when nothing configured the URL at all: an env- or profile-pinned
    # server that is temporarily down is an outage, not a setup gap.
    if not status["server"]["reachable"] and status["server"]["source"] == "default":
        suggestions.append(
            f"No Lab Tracker API responded at {status['server']['base_url']}; "
            "`lab-tracker serve` starts a local instance and `lt setup connect` "
            "records a lab server URL (`--yes` applies)."
        )
    if status["server"]["source"] == "default" and not status["profile"]["present"]:
        suggestions.append(
            "`lt setup connect --base-url <url>` records the intended Lab Tracker "
            "URL before MCP configs bake the default localhost "
            "(`--dry-run` previews, `--yes` applies)."
        )
    if not repo["scaffold"][".mcp.json"]:
        suggestions.append(
            "`lt setup init` scaffolds the integration files here "
            "(`--dry-run` previews)."
        )
    elif any(item.get("drifted") for item in repo["conventions"]):
        suggestions.append(
            "Managed blocks differ from the installed package; `lt update` "
            "refreshes them (`--dry-run` previews)."
        )
    if not repo["lt_ids"]["project_id_bound"]:
        suggestions.append(
            "`lt project bind` records the project id in lt_ids.json, "
            "creating the file when missing (`--yes` applies)."
        )
    if not watch.get("config_present") or not watch.get("watch_count"):
        suggestions.append(_watch_setup_suggestion(status))
    if hooks.get("git_repo") and not hooks.get("managed_block_present"):
        suggestions.append(
            "`lt hooks install` enrolls this repository's commits "
            "(`--yes` applies)."
        )
    if hooks.get("managed_block_present") and hooks.get("lt_path_exists") is False:
        suggestions.append(
            "The commit hook points at a missing lt executable; "
            "`lt hooks install --yes` re-records the current path."
        )
    if not skills.get("installed"):
        suggestions.append(
            "One or more Lab Tracker agent skills are missing; "
            "`lt update --install-skills` installs them for Claude and Codex."
        )
    elif skills.get("up_to_date") is False:
        suggestions.append(
            "One or more installed Lab Tracker agent skills are stale; "
            "`lt update --install-skills` refreshes it."
        )
    # Installation problems (lt/lt-mcp not on PATH, a fragile venv install) are
    # already phrased as non-imperative suggestions; append them so they reach
    # `--brief` and the SessionStart hook, which surface only `suggestions`.
    suggestions.extend(installation.get("problems") or [])
    return suggestions


def _skills_status() -> JsonObject:
    from lab_tracker.cli import _full_skill_markdown, _full_skill_paths, _setup_skill_paths
    from lab_tracker.setup_guide import (
        setup_skill_markdown,
        skill_content_without_version_line,
    )

    expected = [
        *((path, "lab-tracker", _full_skill_markdown()) for path in _full_skill_paths()),
        *((path, "lab-tracker-setup", setup_skill_markdown()) for path in _setup_skill_paths()),
    ]
    targets: list[JsonObject] = []
    for path, kind, generated in expected:
        target: JsonObject = {
            "kind": kind,
            "path": str(path),
            "installed": path.exists(),
            "up_to_date": False,
            "version_in_sync": False,
        }
        if path.exists():
            with suppress(Exception):
                installed = path.read_text(encoding="utf-8")
                target["up_to_date"] = skill_content_without_version_line(
                    installed
                ) == skill_content_without_version_line(generated)
                target["version_in_sync"] = installed == generated
        targets.append(target)
    primary_path = _setup_skill_paths()[0]
    summary: JsonObject = {
        "path": str(primary_path),
        "targets": targets,
        "installed": all(bool(target["installed"]) for target in targets),
        "up_to_date": all(bool(target["up_to_date"]) for target in targets),
        "version_in_sync": all(bool(target["version_in_sync"]) for target in targets),
    }
    return summary


def _installation_status() -> JsonObject:
    """Read-only report on whether lt/lt-mcp resolve and from a stable location."""

    from lab_tracker_client.executables import installation_report

    try:
        return installation_report()
    except Exception:  # noqa: BLE001 - status is a read-only inventory; never raise.
        return {"problems": []}


def _resolve_base_url(profile: dict[str, str]) -> tuple[str, str]:
    from_env = os.getenv("LAB_TRACKER_BASE_URL") or os.getenv("LAB_TRACKER_MCP_BASE_URL")
    if from_env:
        return from_env, "env"
    if profile.get("base_url"):
        return profile["base_url"], "profile"
    return DEFAULT_BASE_URL, "default"


def resolved_base_url_for_setup() -> tuple[str, str]:
    """Return the URL/source that setup scaffolding should bake into MCP files."""

    return _resolve_base_url(load_connection_profile())


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
        return {
            "config_present": False,
            "detail": str(exc),
            "candidate_roots": _watch_candidate_roots(root),
        }
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


def _watch_candidate_roots(root: Path) -> list[JsonObject]:
    candidates: list[JsonObject] = []
    for name in ("results", "outputs", "figures", "plots", "reports", "artifacts"):
        path = root / name
        if not path.is_dir():
            continue
        broad = name in {"artifacts", "outputs"}
        candidates.append(
            {
                "path": str(path),
                "name": name,
                "broad": broad,
                "guidance": (
                    "Prefer a run- or analysis-specific subfolder."
                    if broad
                    else "Reasonable candidate; add include globs for expected outputs."
                ),
            }
        )
    return candidates


def _watch_setup_suggestion(status: JsonObject) -> str:
    hooks = status["hooks"]
    candidates = status["watch"].get("candidate_roots") or []
    candidate_names = [
        str(item.get("name") or item.get("path"))
        for item in candidates
        if isinstance(item, dict)
    ]
    candidate_hint = ""
    if candidate_names:
        candidate_hint = (
            " Candidate roots found: "
            + ", ".join(candidate_names[:3])
            + "; prefer a narrow run-specific folder or include globs."
        )
    if hooks.get("managed_block_present") and hooks.get("lt_path_exists") is not False:
        return (
            "Commit snapshots are active; skip watch setup unless there is a "
            "narrow results folder to capture. `lt watch add <folder> --include "
            "<glob>` registers one (`--dry-run` previews)."
            + candidate_hint
        )
    return (
        "`lt watch add <folder> --include <glob>` registers a narrow results "
        "folder for capture (`--dry-run` previews)."
        + candidate_hint
    )


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
    core_hooks_path: str | None = None
    with suppress(Exception):
        completed = subprocess.run(  # noqa: S603 - fixed executable, no shell.
            ["git", "config", "--get", "core.hooksPath"],
            cwd=root,
            capture_output=True,
            timeout=10,
            text=True,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            core_hooks_path = completed.stdout.strip()
    with suppress(Exception):
        _repo_root, hook_path = hook_path_for_repo(root)
    if hook_path is None:
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
    content = ""
    with suppress(OSError, UnicodeDecodeError):
        content = hook_path.read_text(encoding="utf-8")
        managed_block_present = HOOK_BLOCK_BEGIN in content
    summary: JsonObject = {
        "git_repo": True,
        "post_commit_path": str(hook_path),
        "core_hooks_path": core_hooks_path,
        "post_commit_present": hook_path.exists(),
        "managed_block_present": managed_block_present,
    }
    if managed_block_present:
        # The venv-moved failure mode: the block's baked lt path no longer
        # exists, so the hook dies silently on every commit.
        from lab_tracker_client.hooks import _LT_LINE_PATTERN

        match = _LT_LINE_PATTERN.search(content)
        if match:
            summary["lt_path"] = match.group("path")
            with suppress(OSError):
                summary["lt_path_exists"] = Path(match.group("path")).exists()
    return summary


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
    verification_warning: str | None = None
    if project_id:
        try:
            record = _find_project_by_id(client, project_id)
        except Exception as exc:  # noqa: BLE001 - explicit ids can be written offline.
            record = None
            verification_warning = _project_id_verification_warning(exc)
        if record is None and verification_warning is None:
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
    elif project_id:
        resolved_id = project_id
        resolved_name = ""
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
    if not resolved_name and project_id:
        resolved_name = ids_payload.get("project_name", "")
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
    if verification_warning:
        payload["warnings"] = [verification_warning]
    if not dry_run:
        path.write_text(proposed_text, encoding="utf-8")
    return payload


def _project_id_verification_warning(exc: Exception) -> str:
    return (
        "Could not verify the project id through the Lab Tracker API "
        f"({exc}). The explicit id was recorded anyway. To verify names and "
        "permissions, set LAB_TRACKER_ACCESS_TOKEN or run "
        "`lt setup connect --save-token --yes` with a token from the web app."
    )


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
