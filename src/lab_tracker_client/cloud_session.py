"""Recognize a cloud agent session and the server addresses it cannot reach.

A cloud session (Claude Code on the web, ``claude --cloud``, a cloud scheduled
task, a Codex cloud task, or another vendor's hosted agent) runs in a fresh VM
cloned from the repository. Nothing installed or saved on the person's computer
is there, the VM's loopback is the VM itself, and the VM is discarded later.
``lt setup status`` uses this module to swap computer-only suggestions for the
cloud environment settings a person changes instead (``docs/cloud-agents.md``).
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from lab_tracker.client_release import client_install_command

JsonObject = dict[str, Any]

CLOUD_SESSION_ENV = "LAB_TRACKER_CLOUD_SESSION"
# Anthropic documents this as ``true`` in every Claude Code cloud session VM and
# never ``true`` locally.
CLAUDE_CODE_REMOTE_ENV = "CLAUDE_CODE_REMOTE"
# OpenAI documents no variable that marks a Codex cloud task, so the environment
# declares one (``LAB_TRACKER_CLOUD_SESSION=codex``). Codex's harness sets these
# on the commands its agent runs, locally and wherever else it runs.
CODEX_HARNESS_ENVS = ("CODEX_THREAD_ID", "CODEX_CI")
# Codex's command sandbox sets this to ``1`` when it runs a command without network.
CODEX_NETWORK_DISABLED_ENV = "CODEX_SANDBOX_NETWORK_DISABLED"
CLAUDE_CODE = "claude-code"
CODEX = "codex"
CLOUD_AGENTS_DOC = "docs/cloud-agents.md in the Lab Tracker repository"
_OFF_VALUES = frozenset({"0", "false", "no", "off"})

LOOPBACK = "loopback"
PRIVATE_NETWORK = "private_network"
_UNREACHABLE_REASONS = {
    LOOPBACK: "a loopback address, which inside a cloud VM is the VM itself",
    PRIVATE_NETWORK: "a private-network address",
}
# Where a cloud VM can join the network a private address belongs to.
_PRIVATE_NETWORK_ROUTES = {
    CODEX: ", which a Codex cloud environment reaches only through its Tailscale VPN setting",
    CLAUDE_CODE: (
        ", and an Anthropic-hosted Claude Code environment is on no lab network: a "
        "self-hosted environment or Remote Control is the way to reach one"
    ),
}
_PRIVATE_NETWORK_ROUTE = ", which a cloud VM reaches only through a network its environment joins"
# Names that never resolve on the public internet: mDNS, the special-use and
# ICANN-reserved private suffixes, and two undelegated router conventions.
_PRIVATE_SUFFIXES = (".local", ".internal", ".home.arpa", ".lan", ".localdomain")
# Claude Code expands ``${VAR}`` and ``${VAR:-default}`` in ``.mcp.json`` values.
_CLAUDE_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def cloud_session(environ: Mapping[str, str] | None = None) -> JsonObject:
    """Whether this process runs in a cloud agent session, which variable said so, and the agent.

    An explicit ``LAB_TRACKER_CLOUD_SESSION`` wins: ``0``/``false``/``no``/``off``
    declares a machine that is not one (a self-hosted runner on the lab network),
    any other value declares one (a Codex cloud task, another vendor's hosted
    agent). ``host`` names the agent when something shows it: ``claude-code``,
    ``codex`` (the value ``codex`` or Codex's harness variables), else ``None``.
    """

    env = os.environ if environ is None else environ
    declared = env.get(CLOUD_SESSION_ENV, "").strip().lower()
    if declared in _OFF_VALUES:
        return {"detected": False, "signal": CLOUD_SESSION_ENV, "host": None}
    if declared:
        host = _declared_host(declared, env)
        return {"detected": True, "signal": CLOUD_SESSION_ENV, "host": host}
    if env.get(CLAUDE_CODE_REMOTE_ENV, "").strip().lower() == "true":
        return {"detected": True, "signal": CLAUDE_CODE_REMOTE_ENV, "host": CLAUDE_CODE}
    return {"detected": False, "signal": None, "host": None}


def _declared_host(declared: str, env: Mapping[str, str]) -> str | None:
    if declared == CODEX or any(env.get(name) for name in CODEX_HARNESS_ENVS):
        return CODEX
    if env.get(CLAUDE_CODE_REMOTE_ENV, "").strip().lower() == "true":
        return CLAUDE_CODE
    return None


def codex_network_disabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether Codex runs this command in its sandbox with network access off."""

    env = os.environ if environ is None else environ
    return env.get(CODEX_NETWORK_DISABLED_ENV, "").strip() == "1"


def unreachable_from_cloud(base_url: str) -> str | None:
    """Why a cloud VM cannot reach ``base_url``, or ``None`` when its address shows no reason.

    Only the address is judged. A public name the environment's network access
    refuses is still ``None`` here, and so is a value with no parseable host;
    the health probe is what shows either.
    """

    try:
        host = (urlsplit(base_url).hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if not host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return _hostname_reason(host)
    # An unspecified address (0.0.0.0, ::) connects to the local host.
    if address.is_loopback or address.is_unspecified:
        return LOOPBACK
    return None if address.is_global else PRIVATE_NETWORK


def _hostname_reason(host: str) -> str | None:
    if host == "localhost" or host.endswith(".localhost"):
        return LOOPBACK
    # A bare name resolves only on a local network or search domain.
    if "." not in host or host.endswith(_PRIVATE_SUFFIXES):
        return PRIVATE_NETWORK
    return None


def unreachable_reason_text(reason: str, host: str | None = None) -> str:
    """The phrase ``lt setup status`` uses for a reason from :func:`unreachable_from_cloud`."""

    if reason != PRIVATE_NETWORK:
        return _UNREACHABLE_REASONS[reason]
    return _UNREACHABLE_REASONS[reason] + _PRIVATE_NETWORK_ROUTES.get(
        host or "", _PRIVATE_NETWORK_ROUTE
    )


def script_name(host: str | None) -> str:
    """What the cloud environment calls the script that prepares its VM."""

    return {
        CLAUDE_CODE: "setup script",
        CODEX: "install script (setup script in Codex Cloud (Legacy))",
    }.get(host or "", "setup or install script")


def expand_claude_variables(value: str, environ: Mapping[str, str]) -> str:
    """``value`` with ``${VAR}`` and ``${VAR:-default}`` expanded as Claude Code does."""

    return _CLAUDE_VARIABLE.sub(
        lambda match: environ.get(match[1]) or (match[2] or ""),
        value,
    )


def setup_script_install(revision: str | None) -> str:
    """The setup-script install line for ``revision``, as a suggestion quotes it."""

    install = client_install_command(revision)
    if install is None:
        return "the server-pinned `uv tool install` command from the web app's Setup page"
    return f"`{install}`"


def cloud_update_steps(revision: str | None, host: str | None = None) -> str:
    """Where a client update belongs in a cloud session: the setup script, not the VM."""

    return (
        f"in a cloud session the client comes from the environment's {script_name(host)}, "
        f"so replacing its install line with {setup_script_install(revision)} updates every "
        "later session; `lt maintain` or a reinstall in this VM lasts only this session"
    )
