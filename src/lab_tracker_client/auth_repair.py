"""Read-only credential diagnosis and human-operated protected-profile recovery.

Never mint credentials, log response bodies, change a host registration, or claim
that an already-running MCP process was verified by this terminal process.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from lab_tracker.instance_url import (
    BASE_URL_ENV,
    DEFAULT_BASE_URL,
    LEGACY_MCP_BASE_URL_ENV,
    normalize_instance_base_url,
)
from lab_tracker.mcp_api_client import MCPSettings
from lab_tracker_client import auth
from lab_tracker_client.client import connection_profile_path
from lab_tracker_client.connection_diagnostics import ConnectionTrace
from lab_tracker_client.setup import ConnectionProfileSecurityError, save_connection_profile
from lab_tracker_client.transport import request_within_deadline

JsonObject = dict[str, Any]
_TOKEN_KEYS = ("LAB_TRACKER_MCP_API_KEY", "LAB_TRACKER_MCP_TOKEN")
_URL_KEYS = (BASE_URL_ENV, LEGACY_MCP_BASE_URL_ENV)
_NETWORK_GUIDANCE = (
    "This terminal's network context may differ from the MCP host. Compare the "
    "instance URLs and available lab_tracker_health result; obtain normal sandbox "
    "approval for any read-only probe outside the sandbox. Do not infer a server "
    "outage from this failure or disable TLS verification."
)


def _entries(target: str | Path, home: Path) -> list[tuple[str, Path, str, dict[str, str]]]:
    """Keep registration values private; only names and source facts are rendered."""
    entries = []
    for source in auth._config_sources(Path(target).resolve(), home):
        config = auth._load_config(source.path)
        if config is None:
            continue
        maps = [config.get(key) for key in ("mcpServers", "servers", "mcp_servers")]
        projects = config.get("projects", {})
        if isinstance(projects, dict):
            project = projects.get(str(Path(target).resolve()), {})
            if isinstance(project, dict):
                maps.append(project.get("mcpServers"))
        for server_map in maps:
            if not isinstance(server_map, dict):
                continue
            for name, entry in server_map.items():
                if not isinstance(entry, dict) or not auth._is_lab_tracker_entry(name, entry):
                    continue
                raw_env = entry.get("env", {})
                env = (
                    {k: v for k, v in raw_env.items() if isinstance(v, str)}
                    if isinstance(raw_env, dict)
                    else {}
                )
                entries.append((source.surface, source.path, str(name), env))
    return entries


def _source(settings: MCPSettings, environment: Mapping[str, str]) -> str:
    for key in _TOKEN_KEYS:
        if environment.get(key):
            return key
    if settings.username:
        return "deprecated_username_password"
    return "profile" if settings.api_key else "missing"


def _read_profile(path: Path) -> dict[str, str]:
    # A malformed profile is never silently overwritten by the repair command.
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or any(not isinstance(v, str) for v in payload.values()):
        raise ValueError("Invalid connection profile.")
    return {k: v.strip() for k, v in payload.items()}


def _registration_report(
    entry: tuple[str, Path, str, dict[str, str]],
    home: Path,
) -> JsonObject:
    surface, config, server, env = entry
    directory = env.get("LAB_TRACKER_CONFIG_DIR")
    path = (
        Path(directory).expanduser() / "config.json"
        if directory
        else home / ".lab-tracker/config.json"
    )
    result: JsonObject = {
        "host": surface,
        "server": server,
        "path": str(config),
        "profile_path": str(path),
        "overrides": sorted(k for k in env if k.startswith("LAB_TRACKER_")),
    }
    try:
        settings = MCPSettings.from_sources(env, _read_profile(path))
        result.update(base_url=settings.base_url, credential_source=_source(settings, env))
    except (OSError, ValueError):
        result["configuration_valid"] = False
    return result


def verify_credential(
    base_url: str,
    token: str | None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> JsonObject:
    """Verify an auth-enforced project read; LPATs cannot access /auth/*.

    The anonymous control must fail with 401 before a bearer success can prove
    authentication. No login, token introspection, refresh, or graph writes.
    """
    if not token:
        return {"status": "missing", "authenticated_read_ok": False}
    trace = ConnectionTrace(base_url)
    try:
        with httpx.Client(timeout=4, transport=transport, follow_redirects=False) as client:
            for authenticated in (False, True):
                response = request_within_deadline(
                    client,
                    "GET",
                    base_url + "/projects?limit=1&offset=0",
                    deadline_seconds=4,
                    headers={"Authorization": f"Bearer {token}"} if authenticated else {},
                    extensions={"trace": trace},
                )
                code = response.status_code
                if not authenticated:
                    if code == 401:
                        continue
                    return {
                        "status": "unavailable" if code >= 500 else "authentication_unverified",
                        "status_code": code,
                        "authenticated_read_ok": False,
                    }
                if code != 200:
                    status = (
                        "rejected"
                        if code == 401
                        else "insufficient_access"
                        if code == 403
                        else "unavailable"
                        if code >= 500
                        else "unexpected_response"
                    )
                    return {"status": status, "status_code": code, "authenticated_read_ok": False}
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ValueError("Not a Lab Tracker envelope.")
    except httpx.TransportError as exc:
        return {
            "status": "connectivity_error",
            "authenticated_read_ok": False,
            **trace.diagnose(exc),
            "network_context_guidance": _NETWORK_GUIDANCE,
        }
    except (ValueError, TypeError, AttributeError):
        return {"status": "unexpected_response", "authenticated_read_ok": False}
    return {"status": "valid", "authenticated_read_ok": True}


def _restart(hosts: list[str]) -> str:
    names = ", ".join(dict.fromkeys(hosts)) or "the app reporting the MCP failure"
    return (
        f"Fully quit and reopen {names} (Cmd/Ctrl+Q for desktop apps; restart the "
        "MCP session for a CLI host). A window reload is insufficient. This command "
        "cannot reload or verify that live host. Then retry the originally blocked "
        "MCP operation; only its successful authenticated response confirms recovery."
    )


def repair(
    target: str | Path = ".",
    *,
    host: str | None = None,
    diagnose: bool = False,
    home: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    confirm: Callable[[str], str] | None = None,
    secret_prompt: Callable[[str], str] | None = None,
) -> JsonObject:
    """Guide a person; noninteractive invocations perform diagnosis only."""
    home = home or Path.home()
    entries = _entries(target, home)
    environment = dict(os.environ)
    path = connection_profile_path()
    payload: JsonObject = {
        "command": "auth-repair",
        "ok": False,
        "profile_changed": False,
        "live_mcp_verified": False,
        "recovery_complete": False,
        "registrations": [_registration_report(entry, home) for entry in entries],
    }
    selected = [entry for entry in entries if entry[0] == host] if host else []
    if host and len(selected) != 1:
        return {
            **payload,
            "status": "select_host",
            "next_step": (
                "Choose --host from a single registration's host field; resolve ambiguous "
                "registrations with lt auth doctor before proceeding."
            ),
        }
    if selected:
        environment = selected[0][3]
        config_dir = environment.get("LAB_TRACKER_CONFIG_DIR")
        path = (
            Path(config_dir).expanduser() / "config.json"
            if config_dir
            else home / ".lab-tracker/config.json"
        )
        payload["host"] = host
        payload["environment_note"] = (
            "Probing the saved registration and profile. The running host's inherited "
            "environment and loaded credential cannot be inspected from this terminal."
        )
    payload["profile_path"] = str(path)
    payload["restart"] = _restart([host] if host else [entry[0] for entry in entries])
    if not path.is_absolute():
        return {
            **payload,
            "status": "invalid_profile",
            "next_step": "Use an absolute LAB_TRACKER_CONFIG_DIR.",
        }
    try:
        profile = _read_profile(path)
        if profile.get("base_url"):
            normalize_instance_base_url(profile["base_url"])
        settings = MCPSettings.from_sources(environment, profile)
    except (ValueError, OSError):
        return {
            **payload,
            "status": "invalid_configuration",
            "next_step": (
                "Check the selected connection profile and instance URL. No changes were saved."
            ),
        }
    source = _source(settings, environment)
    payload.update(
        base_url=settings.base_url,
        credential_source=source,
        url_source=next((key for key in _URL_KEYS if environment.get(key)), "profile_or_default"),
        agents_url=settings.base_url + "/app/agents",
        precedence=(
            "MCP: LAB_TRACKER_MCP_API_KEY, then LAB_TRACKER_MCP_TOKEN, then matching "
            "profile (unless username login overrides it). CLI uses LAB_TRACKER_ACCESS_TOKEN "
            "instead. LAB_TRACKER_BASE_URL wins over LAB_TRACKER_MCP_BASE_URL; a different "
            "instance never inherits the profile token. Host env overrides are separate "
            "from this terminal; select --host to check a saved registration."
        ),
        cli_token_source=(
            "LAB_TRACKER_ACCESS_TOKEN"
            if os.getenv("LAB_TRACKER_ACCESS_TOKEN")
            else "environment_username"
            if os.getenv("LAB_TRACKER_USERNAME") or os.getenv("LAB_TRACKER_MCP_USERNAME")
            else "profile_or_missing"
        ),
    )
    result = verify_credential(settings.base_url, settings.api_key, transport=transport)
    payload["verification"] = result
    payload["status"] = result["status"]
    if not host:
        overrides = [
            report
            for report in payload["registrations"]
            if any(
                key in report["overrides"]
                for key in (
                    *_TOKEN_KEYS,
                    *_URL_KEYS,
                    "LAB_TRACKER_MCP_USERNAME",
                    "LAB_TRACKER_CONFIG_DIR",
                )
            )
        ]
        if overrides:
            if not diagnose and sys.stdin.isatty() and sys.stderr.isatty():
                print(
                    "Select the host reporting the failure: "
                    + ", ".join(dict.fromkeys(report["host"] for report in overrides)),
                    file=sys.stderr,
                )
                try:
                    selected_host = input("Host (empty cancels): ").strip()
                except (EOFError, KeyboardInterrupt):
                    selected_host = ""
                if not selected_host:
                    return {**payload, "status": "cancelled"}
                return repair(
                    target,
                    host=selected_host,
                    home=home,
                    transport=transport,
                    confirm=confirm,
                    secret_prompt=secret_prompt,
                )
            return {
                **payload,
                "status": "select_host",
                "next_step": (
                    "Host registrations contain connection overrides. Before rotating a token, "
                    "rerun lt auth repair --host <host> for the failing registration listed "
                    "above; this terminal's credential may differ from that host."
                ),
            }
    if result["authenticated_read_ok"]:
        return {
            **payload,
            "ok": True,
            "status": "restart_required",
            "next_step": payload["restart"],
        }

    # A working saved token wins over rotation when an env/host override is stale.
    same_instance = (
        normalize_instance_base_url(profile.get("base_url") or DEFAULT_BASE_URL)
        == settings.base_url
    )
    if source != "profile" and same_instance and profile.get("access_token"):
        saved = verify_credential(settings.base_url, profile["access_token"], transport=transport)
        payload["saved_profile_verification"] = saved
        if saved["authenticated_read_ok"]:
            return {
                **payload,
                "status": "override_conflict",
                "next_step": (
                    "The saved profile credential works. Remove the overriding credential "
                    "from the selected host registration or launching environment, so the "
                    "protected profile can be used; do not rotate it. " + payload["restart"]
                ),
            }
    if result["status"] == "insufficient_access":
        return {
            **payload,
            "next_step": (
                "HTTP 403 means access is insufficient. Ask the instance/project owner to "
                "review the required permission or token scope; do not rotate a valid "
                "credential or grant broader scope merely to repair read access."
            ),
        }
    if result["status"] not in {"missing", "rejected"}:
        return {
            **payload,
            "next_step": _NETWORK_GUIDANCE
            if result["status"] in {"connectivity_error", "unavailable"}
            else "Check the instance's API/auth configuration before replacing credentials.",
        }
    if source in _TOKEN_KEYS or source == "deprecated_username_password":
        return {
            **payload,
            "status": "override_conflict",
            "next_step": (
                "An environment/host credential overrides the protected profile. Remove "
                "that override, then rerun lt auth repair (with --host for that registration). "
                "Never paste a replacement into desktop settings or chat."
            ),
        }
    if not same_instance and profile:
        return {
            **payload,
            "status": "profile_instance_mismatch",
            "next_step": (
                "The selected instance differs from the saved profile. Correct the URL or "
                "select its intended LAB_TRACKER_CONFIG_DIR, then rerun; bindings were preserved."
            ),
        }
    payload["next_step"] = (
        f"Sign in to {payload['agents_url']} and create a token with only the access "
        "needed. Run lt auth repair in your own interactive terminal to enter it "
        "at the hidden prompt and approve saving it. Never relay it through an agent."
    )
    if diagnose or (confirm is None and not (sys.stdin.isatty() and sys.stderr.isatty())):
        return payload
    confirm = confirm or input
    secret_prompt = secret_prompt or getpass.getpass
    try:
        print(payload["next_step"], file=sys.stderr)
        if confirm(f"Save a replacement ONLY in {path}? [y/N] ").strip().lower() not in {
            "y",
            "yes",
        }:
            return {**payload, "status": "cancelled"}
        # Fail closed if getpass would echo through a fallback stream.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = secret_prompt("Replacement token (hidden; empty cancels): ").strip()
        if not token:
            return {**payload, "status": "cancelled"}
        replacement = verify_credential(settings.base_url, token, transport=transport)
        payload["replacement_verification"] = replacement
        if not replacement["authenticated_read_ok"]:
            return {
                **payload,
                "status": replacement["status"],
                "next_step": "Replacement failed verification; nothing was saved.",
            }
        # Do not clobber a profile changed during browser approval/token entry.
        if _read_profile(path) != profile:
            return {
                **payload,
                "status": "profile_changed_during_repair",
                "next_step": "Profile changed; rerun diagnosis. Nothing was saved.",
            }
        save_connection_profile(base_url=settings.base_url, access_token=token, profile_path=path)
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        return {
            **payload,
            "status": "cancelled",
            "next_step": "Cancelled; no profile changes were saved.",
        }
    except (OSError, ValueError, ConnectionProfileSecurityError):
        return {
            **payload,
            "status": "save_failed",
            "next_step": "Could not privately save the profile; no changes were saved.",
        }
    return {
        **payload,
        "ok": True,
        "profile_changed": True,
        "status": "restart_required",
        "next_step": payload["restart"],
    }
