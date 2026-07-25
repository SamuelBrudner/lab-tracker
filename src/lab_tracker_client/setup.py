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
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
from lab_tracker_client.hooks import HOOK_BLOCK_BEGIN, hook_path_for_repo

JsonObject = dict[str, Any]

PROFILE_KEYS = ("base_url", "default_project_id", "access_token")
_HEALTH_PROBE_TIMEOUT_SECONDS = 2.0
_DEFAULT_MCP_VERIFY_TIMEOUT_SECONDS = 15.0
_FULL_GIT_REVISION = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

_SCAFFOLD_FILES = (
    ".mcp.json",
    ".cursor/mcp.json",
    ".gemini/settings.json",
    ".claude/settings.json",
    "scripts/lt.py",
    "AGENTS.lt.md",
    "lt_ids.json",
)


class ConnectionProfileSecurityError(RuntimeError):
    """Raised when a connection profile cannot be persisted privately."""


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
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        # The temporary file must be proven private while it is still empty.
        # On Windows, mode bits do not constrain NTFS ACLs, so no credential
        # bytes may be written until icacls succeeds.
        if not _harden_profile_permissions(tmp_path):
            raise ConnectionProfileSecurityError(
                "Could not secure the temporary Lab Tracker connection profile; "
                "no changes were saved."
            )
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(proposed_text)
            handle.flush()
            os.fsync(handle.fileno())
        # Re-apply and verify the restriction before the atomic replace. The
        # secured temporary file's permissions travel with the same-directory
        # rename, while the existing profile remains untouched on any failure.
        if not _harden_profile_permissions(tmp_path):
            raise ConnectionProfileSecurityError(
                "Could not verify private Lab Tracker connection profile "
                "permissions; no changes were saved."
            )
        tmp_path.replace(path)
    finally:
        # Also runs for KeyboardInterrupt/SystemExit after credential bytes are
        # written. After a successful replace the temporary pathname is already
        # absent, so this is safe on every exit path.
        with suppress(OSError):
            tmp_path.unlink()
    payload["permissions_hardened"] = True
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
            "has_token": bool(
                os.getenv("LAB_TRACKER_ACCESS_TOKEN") or profile.get("access_token")
            ),
        },
        "repo": _repo_status(root),
        "watch": _watch_status(root),
        "hpc": _hpc_status(root),
        "hooks": _hooks_status(root),
        "skills": _skills_status(),
    }
    payload["suggestions"] = _suggestions(payload)
    if not brief:
        return payload
    suggestions = payload["suggestions"]
    if suggestions:
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
    }


def _suggestions(status: JsonObject) -> list[str]:
    suggestions: list[str] = []
    repo = status["repo"]
    hooks = status["hooks"]
    watch = status["watch"]
    skills = status["skills"]
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
    skill_targets = [
        target
        for target in skills.get("targets", [])
        if isinstance(target, dict)
    ]
    missing_skill_targets = [
        str(target.get("name") or target.get("path") or "unknown")
        for target in skill_targets
        if not target.get("installed")
    ]
    stale_skill_targets = [
        str(target.get("name") or target.get("path") or "unknown")
        for target in skill_targets
        if target.get("installed") and target.get("up_to_date") is not True
    ]
    if missing_skill_targets:
        names = ", ".join(missing_skill_targets)
        suggestions.append(
            f"The lab-tracker-setup skill is missing from: {names}; "
            "`lt setup init --install-skills` installs it."
        )
    elif stale_skill_targets:
        suggestions.append(
            "One or more installed lab-tracker-setup skills are stale; "
            "`lt update --install-skills` refreshes them."
        )
    elif not skill_targets and skills.get("installed") and skills.get("up_to_date") is False:
        # Backward-compatible fallback for status payloads produced before
        # multi-agent skill targets were exposed.
        suggestions.append(
            "The installed lab-tracker-setup skill is stale; "
            "`lt update --install-skills` refreshes it."
        )
    return suggestions


def _skills_status() -> JsonObject:
    from lab_tracker.cli import _setup_skill_targets
    from lab_tracker.setup_guide import (
        setup_skill_markdown,
        skill_content_without_version_line,
    )

    generated = setup_skill_markdown()
    targets: list[JsonObject] = []
    for name, path in _setup_skill_targets():
        target: JsonObject = {
            "name": name,
            "path": str(path),
            "installed": path.exists(),
        }
        if target["installed"]:
            with suppress(Exception):
                installed = path.read_text(encoding="utf-8")
                # Staleness is a CONTENT verdict, mirroring doctor: a package
                # bump with unchanged skill text must not cry wolf in every
                # session's SessionStart hook. The raw version line stays
                # informational.
                target["up_to_date"] = skill_content_without_version_line(
                    installed
                ) == skill_content_without_version_line(generated)
                target["version_in_sync"] = installed == generated
        targets.append(target)

    # Keep the original scalar fields as the primary-target view so existing
    # consumers do not need to change. The new target list and aggregate fields
    # expose the complete Claude+Codex state.
    primary = targets[0]
    summary: JsonObject = {
        key: primary[key]
        for key in ("path", "installed", "up_to_date", "version_in_sync")
        if key in primary
    }
    summary["targets"] = targets
    summary["all_installed"] = all(bool(target["installed"]) for target in targets)
    summary["all_up_to_date"] = all(
        target.get("up_to_date") is True for target in targets
    )
    summary["all_version_in_sync"] = all(
        target.get("version_in_sync") is True for target in targets
    )
    return summary


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


def installed_source_revision() -> str | None:
    """Return the immutable VCS revision recorded by a direct-URL install.

    The guided setup installs Lab Tracker from an exact Git revision. Python
    installers preserve the resolved commit in ``direct_url.json`` (PEP 610),
    which gives both the tool environment and a consumer project's environment
    a local, offline compatibility check.
    """

    with suppress(Exception):
        direct_url_text = importlib.metadata.distribution("lab-tracker").read_text(
            "direct_url.json"
        )
        if not direct_url_text:
            return None
        direct_url = json.loads(direct_url_text)
        vcs_info = direct_url.get("vcs_info")
        if not isinstance(vcs_info, dict):
            return None
        revision = str(vcs_info.get("commit_id") or "").strip().lower()
        return revision if _FULL_GIT_REVISION.fullmatch(revision) else None
    return None


def verify_client_revision(expected_revision: str) -> JsonObject:
    """Fail-closed compatibility check for the installed client package."""

    expected = _validated_source_revision(expected_revision)
    installed = installed_source_revision()
    compatible = installed == expected
    payload: JsonObject = {
        "command": "setup-verify-client",
        "expected_revision": expected,
        "installed_revision": installed,
        "compatible": compatible,
        "ok": compatible,
    }
    if not installed:
        payload["error"] = (
            "The installed lab-tracker package has no immutable Git revision metadata. "
            "Install the exact requirement shown by this server, then retry."
        )
    elif not compatible:
        payload["error"] = (
            "The installed lab-tracker client does not match this server's source revision."
        )
    return payload


def verify_mcp_launch(
    *,
    expected_revision: str,
    command: str = "lt-mcp",
    timeout_seconds: float = _DEFAULT_MCP_VERIFY_TIMEOUT_SECONDS,
) -> JsonObject:
    """Launch MCP over stdio and exercise health plus an authenticated read.

    This checks the executable that Codex will launch, not merely a config
    listing. The project-list call is deliberately read-only but authentication
    and authorization aware, so a saved LPAT/profile failure is caught here.
    """

    compatibility = verify_client_revision(expected_revision)
    if timeout_seconds <= 0:
        raise LTValidationError("MCP verification timeout must be greater than zero.")
    executable = _resolve_mcp_executable(command)
    if not executable:
        return {
            "command": "setup-verify-mcp",
            "executable": command,
            "client_compatibility": compatibility,
            "launched": False,
            "initialized": False,
            "health_ok": False,
            "authenticated_read_ok": False,
            "ok": False,
            "error": (
                f"Could not find {command!r} on PATH. Install the exact tool "
                "requirement shown by this server, then retry."
            ),
        }

    env = dict(os.environ)
    env["LAB_TRACKER_MCP_TRANSPORT"] = "stdio"
    deadline = time.monotonic() + timeout_seconds
    try:
        health_completed, health_initialize, health_response = _run_mcp_tool_probe(
            executable,
            tool_name="lab_tracker_health",
            arguments={},
            timeout_seconds=_remaining_mcp_timeout(deadline),
            env=env,
        )
        projects_completed, projects_initialize, projects_response = (
            _run_mcp_tool_probe(
                executable,
                tool_name="lab_tracker_list_projects",
                arguments={"limit": 1, "offset": 0},
                timeout_seconds=_remaining_mcp_timeout(deadline),
                env=env,
            )
        )
    except subprocess.TimeoutExpired:
        return {
            "command": "setup-verify-mcp",
            "executable": executable,
            "client_compatibility": compatibility,
            "launched": True,
            "initialized": False,
            "health_ok": False,
            "authenticated_read_ok": False,
            "ok": False,
            "error": f"MCP verification timed out after {timeout_seconds:g} seconds.",
        }
    except OSError as exc:
        return {
            "command": "setup-verify-mcp",
            "executable": executable,
            "client_compatibility": compatibility,
            "launched": False,
            "initialized": False,
            "health_ok": False,
            "authenticated_read_ok": False,
            "ok": False,
            "error": f"Could not launch MCP: {_redact_mcp_diagnostic(str(exc))}",
        }

    health_initialize_result = _mcp_result(health_initialize)
    projects_initialize_result = _mcp_result(projects_initialize)
    health_content = _mcp_structured_content(health_response)
    projects_content = _mcp_structured_content(projects_response)
    initialized = bool(
        isinstance(health_initialize_result.get("serverInfo"), dict)
        and isinstance(projects_initialize_result.get("serverInfo"), dict)
    )
    health_ok = bool(
        _mcp_tool_call_succeeded(health_response)
        and health_content is not None
        and not health_content.get("error")
    )
    authenticated_read_ok = bool(
        _mcp_tool_call_succeeded(projects_response)
        and projects_content is not None
        and not projects_content.get("error")
    )
    return_codes = (
        health_completed.returncode,
        projects_completed.returncode,
    )
    compatible = compatibility["compatible"] is True
    ok = bool(
        all(return_code == 0 for return_code in return_codes)
        and initialized
        and health_ok
        and authenticated_read_ok
        and compatible
    )
    payload = {
        "command": "setup-verify-mcp",
        "executable": executable,
        "client_compatibility": compatibility,
        "launched": True,
        "initialized": initialized,
        "health_ok": health_ok,
        "authenticated_read_ok": authenticated_read_ok,
        "server_info": (
            health_initialize_result.get("serverInfo") if initialized else None
        ),
        "health": health_content,
        "project_read": projects_content,
        "ok": ok,
    }
    if not ok:
        problems: list[str] = []
        if not compatible:
            problems.append("client revision does not match the server")
        failed_return_codes = [
            return_code for return_code in return_codes if return_code != 0
        ]
        if failed_return_codes:
            problems.append(
                "MCP process exited with status "
                + ", ".join(str(value) for value in failed_return_codes)
            )
        if not initialized:
            problems.append("MCP initialize did not complete")
        if not health_ok:
            problems.append("Lab Tracker health call failed")
        if not authenticated_read_ok:
            problems.append("authenticated project read failed")
        diagnostic = _redact_mcp_diagnostic(
            "\n".join(
                (
                    health_completed.stderr,
                    projects_completed.stderr,
                )
            )
        )
        payload["error"] = "; ".join(problems) or "MCP verification failed."
        if diagnostic:
            payload["diagnostic"] = diagnostic
    return payload


def _run_mcp_tool_probe(
    executable: str,
    *,
    tool_name: str,
    arguments: JsonObject,
    timeout_seconds: float,
    env: dict[str, str],
) -> tuple[subprocess.CompletedProcess[str], JsonObject | None, JsonObject | None]:
    """Run one initialized tool call in a fresh bounded stdio session.

    FastMCP can finish shutting down on stdin EOF before a later concurrent
    request flushes its response. One tool call per short-lived process avoids
    that race while still testing the exact executable and saved profile Codex
    will use.
    """

    messages = (
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {
                    "name": "lt-setup-verify",
                    "version": "1",
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        },
    )
    stdin_text = "".join(
        json.dumps(message, separators=(",", ":")) + "\n"
        for message in messages
    )
    completed = subprocess.run(  # noqa: S603 - resolved executable, no shell.
        [executable],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        env=env,
    )
    responses = _mcp_responses_by_id(completed.stdout)
    return completed, responses.get(1), responses.get(2)


def _resolve_mcp_executable(command: str) -> str | None:
    """Prefer the lt-mcp entrypoint from this client's Python environment."""

    if command == "lt-mcp":
        scripts_dir = Path(sys.executable).resolve().parent
        companion_names = (
            ("lt-mcp.exe", "lt-mcp")
            if sys.platform == "win32"
            else ("lt-mcp",)
        )
        for name in companion_names:
            companion = scripts_dir / name
            if companion.is_file():
                return str(companion)
    return shutil.which(command)


def _remaining_mcp_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(cmd="lt-mcp", timeout=0)
    return remaining


def _validated_source_revision(value: str) -> str:
    revision = str(value or "").strip().lower()
    if not _FULL_GIT_REVISION.fullmatch(revision):
        raise LTValidationError(
            "Expected a full 40-character Git source revision from the Lab Tracker "
            "Setup page; refusing an unpinned compatibility check."
        )
    return revision


def _mcp_responses_by_id(stdout: str) -> dict[int, JsonObject]:
    responses: dict[int, JsonObject] = {}
    for line in stdout.splitlines():
        with suppress(json.JSONDecodeError, TypeError, ValueError):
            message = json.loads(line)
            if not isinstance(message, dict):
                continue
            response_id = int(message.get("id"))
            responses[response_id] = message
    return responses


def _mcp_result(response: JsonObject | None) -> JsonObject:
    if not isinstance(response, dict):
        return {}
    result = response.get("result")
    return result if isinstance(result, dict) else {}


def _mcp_structured_content(response: JsonObject | None) -> JsonObject | None:
    result = _mcp_result(response)
    content = result.get("structuredContent")
    return content if isinstance(content, dict) else None


def _mcp_tool_call_succeeded(response: JsonObject | None) -> bool:
    result = _mcp_result(response)
    return bool(result) and result.get("isError") is not True


def _redact_mcp_diagnostic(value: str, *, limit: int = 1000) -> str:
    redacted = re.sub(
        r"\b(?:lpat|linv|ldev|lpair)_[A-Za-z0-9._~-]+",
        "***redacted***",
        value or "",
        flags=re.IGNORECASE,
    )
    return redacted.strip()[-limit:]


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
