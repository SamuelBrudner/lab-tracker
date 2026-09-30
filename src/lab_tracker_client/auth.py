"""Read-only audit of lab-tracker MCP auth configs across host surfaces (GH #80).

Lab Tracker MCP is registered in several independent config surfaces that drift
apart — Codex, the claude-code CLI (``~/.claude.json``), Claude Desktop, and
repo-local ``.mcp.json`` / ``.cursor`` / ``.gemini`` / VS Code files. When an
LPAT or URL migration is applied to one surface but not another, the stale one
keeps using old connection settings while ``health`` can remain misleadingly
green elsewhere.

``auth_doctor`` enumerates every registration it can find and reports, per
surface: the effective auth mode (``api_key`` LPAT vs deprecated
``username_password`` vs ``none``), the base URL, and a warning when the
deprecated username/password env is present. For a Claude Desktop entry it also
reports the registered ``command`` (whether it is absolute and whether it exists),
warns when an absolute ``command`` is not an existing file, and adds a neutral
note when the command is not absolute. It is strictly read-only and fail-soft:
unreadable or malformed files are skipped, never raised.
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

JsonObject = dict[str, Any]

AUTH_API_KEY = "api_key"
AUTH_USERNAME_PASSWORD = "username_password"
AUTH_NONE = "none"

# The env keys the MCP client reads (mirrors MCPSettings.from_env precedence:
# api_key wins over username/password).
_API_KEY_ENV = ("LAB_TRACKER_MCP_API_KEY", "LAB_TRACKER_MCP_TOKEN")
_USERNAME_ENV = "LAB_TRACKER_MCP_USERNAME"
_PASSWORD_ENV = "LAB_TRACKER_MCP_PASSWORD"
_BASE_URL_ENVS = ("LAB_TRACKER_BASE_URL", "LAB_TRACKER_MCP_BASE_URL")

_MIGRATE_HINT = (
    "migrate to an LPAT: set LAB_TRACKER_MCP_API_KEY and remove "
    "LAB_TRACKER_MCP_USERNAME / LAB_TRACKER_MCP_PASSWORD."
)
_DESKTOP_RELAUNCH_NOTE = (
    "Claude Desktop only re-reads MCP env on a full quit-and-reopen (Cmd/Ctrl+Q), "
    "not a window reload — restart it after changing credentials."
)
# A precaution, not a prediction: which PATH the app searches is the app's business,
# so this never claims a bare command fails.
_DESKTOP_NON_ABSOLUTE_NOTE = (
    "Claude Desktop entry '{server}' uses the command '{command}', which is not an "
    "absolute path. If the app cannot find it, set the command to the absolute path "
    "of the executable."
)
_DESKTOP_MISSING_COMMAND_WARNING = (
    "The registered command '{command}' is an absolute path that is not an existing "
    "file; correct it to the executable that starts the server, then fully quit and "
    "reopen Claude Desktop."
)


@dataclass
class AuthRegistration:
    surface: str
    path: str
    server: str
    auth_mode: str
    base_url: str | None = None
    warning: str | None = None
    scope: str | None = None
    command: str | None = None
    command_is_absolute: bool | None = None
    command_exists: bool | None = None

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "surface": self.surface,
            "path": self.path,
            "server": self.server,
            "auth_mode": self.auth_mode,
            "base_url": self.base_url,
        }
        if self.scope:
            payload["scope"] = self.scope
        if self.command is not None:
            payload["command"] = self.command
            payload["command_is_absolute"] = self.command_is_absolute
            payload["command_exists"] = self.command_exists
        if self.warning:
            payload["warning"] = self.warning
        return payload


@dataclass(frozen=True)
class _CommandFacts:
    """What the doctor can see about a registered ``command`` from this machine.

    ``exists`` is whether an absolute command is an existing file, or whether a
    non-absolute one resolves from this process's working directory and ``PATH``.
    The latter says nothing about the search path a GUI app uses.
    """

    command: str
    is_absolute: bool
    exists: bool


@dataclass
class _ConfigSource:
    surface: str
    path: Path
    is_desktop: bool = False


def auth_doctor(target: str | Path = ".", *, home: str | Path | None = None) -> JsonObject:
    """Enumerate lab-tracker MCP registrations across every known surface."""

    root = Path(target).expanduser().resolve()
    home_dir = Path(home).expanduser() if home is not None else Path.home()

    registrations: list[AuthRegistration] = []
    desktop_registrations: list[AuthRegistration] = []
    for source in _config_sources(root, home_dir):
        config = _load_config(source.path)
        if config is None:
            continue
        found = _registrations_from_config(source, config)
        if source.is_desktop:
            desktop_registrations.extend(found)
        registrations.extend(found)

    deprecated = [r for r in registrations if r.auth_mode == AUTH_USERNAME_PASSWORD]
    warnings = [r for r in registrations if r.warning]
    notes: list[str] = []
    if desktop_registrations:
        notes.append(_DESKTOP_RELAUNCH_NOTE)
        notes.extend(_desktop_command_notes(desktop_registrations))

    return {
        "command": "auth-doctor",
        "registrations": [r.to_dict() for r in registrations],
        "deprecated_count": len(deprecated),
        "warning_count": len(warnings),
        "notes": notes,
    }


def _config_sources(root: Path, home_dir: Path) -> list[_ConfigSource]:
    sources = [
        # claude-code CLI (user + per-project registrations live in one file).
        _ConfigSource("claude-code", home_dir / ".claude.json"),
        # Codex stores MCP registrations in TOML under `mcp_servers`.
        _ConfigSource("codex", home_dir / ".codex" / "config.toml"),
        # repo-local surfaces using the mcpServers schema.
        _ConfigSource("repo:.mcp.json", root / ".mcp.json"),
        _ConfigSource("repo:.cursor/mcp.json", root / ".cursor" / "mcp.json"),
        _ConfigSource("repo:.gemini/settings.json", root / ".gemini" / "settings.json"),
        # repo-local surfaces using the VS Code / Copilot `servers` schema.
        _ConfigSource("repo:.vscode/mcp.json", root / ".vscode" / "mcp.json"),
        # Older checkouts used this root-level Visual Studio config. Keep
        # scanning it so auth diagnostics still find credentials that need
        # migration, even though new checkouts use `.vscode/mcp.json`.
        _ConfigSource("repo:mcp.visualstudio.json", root / "mcp.visualstudio.json"),
    ]
    for path in _desktop_config_paths(home_dir):
        sources.append(_ConfigSource("claude-desktop", path, is_desktop=True))
    return sources


def _desktop_config_paths(home_dir: Path) -> list[Path]:
    """Candidate Claude Desktop config paths across macOS/Linux/Windows."""

    candidates = [
        home_dir / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        home_dir / ".config" / "Claude" / "claude_desktop_config.json",
    ]
    appdata = os.getenv("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    # De-dup while preserving order (paths can coincide on some setups).
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _load_config(path: Path) -> JsonObject | None:
    if not path.exists():
        return None
    with suppress(Exception):  # read-only + fail-soft: never raise on a bad file.
        text = path.read_text(encoding="utf-8")
        payload = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
        if isinstance(payload, dict):
            return payload
    return None


def _registrations_from_config(
    source: _ConfigSource, config: JsonObject
) -> list[AuthRegistration]:
    registrations: list[AuthRegistration] = []
    # Top-level server maps: `mcpServers` (Claude/Cursor/Gemini) and `servers`
    # (VS Code / Copilot).
    for key in ("mcpServers", "servers", "mcp_servers"):
        registrations.extend(
            _registrations_from_server_map(source, config.get(key), scope=None)
        )
    # claude-code stores per-project registrations under `projects[path]`.
    projects = config.get("projects")
    if isinstance(projects, dict):
        for project_path, project_config in projects.items():
            if not isinstance(project_config, dict):
                continue
            registrations.extend(
                _registrations_from_server_map(
                    source,
                    project_config.get("mcpServers"),
                    scope=str(project_path),
                )
            )
    return registrations


def _registrations_from_server_map(
    source: _ConfigSource, server_map: object, *, scope: str | None
) -> list[AuthRegistration]:
    if not isinstance(server_map, dict):
        return []
    registrations: list[AuthRegistration] = []
    for name, entry in server_map.items():
        if not isinstance(entry, dict) or not _is_lab_tracker_entry(name, entry):
            continue
        env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
        auth_mode, warning = _classify_auth(env)
        facts = _command_facts(entry) if source.is_desktop else None
        registrations.append(
            AuthRegistration(
                surface=source.surface,
                path=str(source.path),
                server=str(name),
                auth_mode=auth_mode,
                base_url=_first_clean(env, _BASE_URL_ENVS),
                warning=_join_warnings(warning, _missing_command_warning(facts)),
                scope=scope,
                command=facts.command if facts else None,
                command_is_absolute=facts.is_absolute if facts else None,
                command_exists=facts.exists if facts else None,
            )
        )
    return registrations


def _command_facts(entry: JsonObject) -> _CommandFacts | None:
    command = _clean(entry.get("command"))
    if command is None:
        return None
    if Path(command).is_absolute():
        return _CommandFacts(command, is_absolute=True, exists=_is_existing_file(command))
    return _CommandFacts(command, is_absolute=False, exists=_resolves_from_here(command))


def _is_existing_file(command: str) -> bool:
    try:
        return Path(command).is_file()
    except (OSError, ValueError):  # fail-soft: an unreadable path is "not a file we can see".
        return False


def _resolves_from_here(command: str) -> bool:
    try:
        return shutil.which(command) is not None
    except (OSError, ValueError):  # fail-soft, e.g. an embedded NUL in the command.
        return False


def _missing_command_warning(facts: _CommandFacts | None) -> str | None:
    if facts is None or not facts.is_absolute or facts.exists:
        return None
    return _DESKTOP_MISSING_COMMAND_WARNING.format(command=facts.command)


def _join_warnings(*warnings: str | None) -> str | None:
    return " ".join(warning for warning in warnings if warning) or None


def _desktop_command_notes(registrations: list[AuthRegistration]) -> list[str]:
    notes = [
        _DESKTOP_NON_ABSOLUTE_NOTE.format(server=r.server, command=r.command)
        for r in registrations
        if r.command is not None and r.command_is_absolute is False
    ]
    return list(dict.fromkeys(notes))  # the same entry can sit in several candidate files.


def _is_lab_tracker_entry(name: str, entry: JsonObject) -> bool:
    if "lab-tracker" in str(name).lower() or "lab_tracker" in str(name).lower():
        return True
    command = str(entry.get("command") or "").lower()
    if command in {"lt-mcp", "lab-tracker-mcp"} or "lt-mcp" in command:
        return True
    env = entry.get("env")
    return isinstance(env, dict) and any(
        str(k).startswith("LAB_TRACKER_MCP_") for k in env
    )


def _classify_auth(env: JsonObject) -> tuple[str, str | None]:
    """Effective auth mode + a warning, matching MCPSettings.from_env precedence."""

    has_api_key = any(_present(env.get(key)) for key in _API_KEY_ENV)
    has_user = _present(env.get(_USERNAME_ENV))
    has_pass = _present(env.get(_PASSWORD_ENV))

    if has_api_key:
        warning = None
        if has_user or has_pass:
            warning = (
                "Sets deprecated LAB_TRACKER_MCP_USERNAME/PASSWORD alongside the "
                "LPAT; the API key takes precedence, so remove the username/password "
                "env to avoid confusion."
            )
        return AUTH_API_KEY, warning
    if has_user and has_pass:
        return (
            AUTH_USERNAME_PASSWORD,
            f"Uses deprecated username/password login (no LPAT); {_MIGRATE_HINT}",
        )
    if has_user or has_pass:
        return (
            AUTH_USERNAME_PASSWORD,
            f"Incomplete deprecated username/password credentials; {_MIGRATE_HINT}",
        )
    return AUTH_NONE, None


def _present(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _clean(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _first_clean(env: JsonObject, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = _clean(env.get(name))
        if value:
            return value
    return None


def render_report(payload: JsonObject) -> str:
    """Human-readable summary written to stderr by the CLI."""

    lines: list[str] = []
    registrations = payload.get("registrations") or []
    if not registrations:
        lines.append("lab-tracker auth: no MCP registrations found across known surfaces.")
        return "\n".join(lines)
    for reg in registrations:
        scope = f" ({reg['scope']})" if reg.get("scope") else ""
        lines.append(
            f"[{reg['auth_mode']}] {reg['surface']}{scope} -> "
            f"{reg.get('base_url') or '(no base_url)'}"
        )
        if reg.get("warning"):
            lines.append(f"    WARN: {reg['warning']}")
    if payload.get("deprecated_count"):
        lines.append(
            f"{payload['deprecated_count']} registration(s) still on deprecated "
            "username/password auth."
        )
    for note in payload.get("notes") or []:
        lines.append(f"note: {note}")
    return "\n".join(lines)
