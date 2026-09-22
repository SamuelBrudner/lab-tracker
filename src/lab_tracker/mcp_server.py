"""Registration entrypoint for the API-backed Lab Tracker MCP server."""

from __future__ import annotations

import functools
import inspect
import ipaddress
import os
import re
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import anyio.to_thread
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from lab_tracker.auth import LPAT_TOKEN_PREFIX
from lab_tracker.decision_context_constants import MCP_SERVER_INSTRUCTIONS
from lab_tracker.mcp_api_client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    SERVER_NAME,
    JsonObject,
    LabTrackerAPIAuthError,
    LabTrackerAPIClient,
    LabTrackerAPIError,
    LabTrackerAPIPermissionError,
    LabTrackerAPIUnavailableError,
    LabTrackerAPIValidationError,
    MCPSettings,
    client_from_env,
    lab_tracker_api_error,
)
from lab_tracker.mcp_tools import (
    register_hosted_write_tools,
    register_read_tools,
    register_resources,
    register_write_tools,
)
from lab_tracker.mcp_tools.read import (
    lab_tracker_describe_schema,
    lab_tracker_export_goal_artifact,
    lab_tracker_export_question_subtree,
    lab_tracker_get_analysis_provenance,
    lab_tracker_get_claim_provenance,
    lab_tracker_get_dataset_provenance,
    lab_tracker_get_decision_context,
    lab_tracker_get_goal,
    lab_tracker_get_graph_neighborhood,
    lab_tracker_graph_overview,
    lab_tracker_health,
    lab_tracker_list_analyses,
    lab_tracker_list_claim_edges,
    lab_tracker_list_claims,
    lab_tracker_list_datasets,
    lab_tracker_list_goals,
    lab_tracker_list_node_goals,
    lab_tracker_list_notes,
    lab_tracker_list_projects,
    lab_tracker_list_question_refactors,
    lab_tracker_list_questions,
    lab_tracker_list_sessions,
    lab_tracker_list_visualizations,
    lab_tracker_next_questions,
    lab_tracker_publication_readiness,
    lab_tracker_readiness,
    lab_tracker_search,
    lab_tracker_search_graph,
)
from lab_tracker.mcp_tools.resources import (
    lab_tracker_agent_consultation_policy,
    lab_tracker_code_conventions,
    lab_tracker_quickstart,
    lab_tracker_surface,
)
from lab_tracker.mcp_tools.write import (
    lab_tracker_create_analysis,
    lab_tracker_create_claim,
    lab_tracker_create_claim_edge,
    lab_tracker_create_dataset,
    lab_tracker_create_goal,
    lab_tracker_create_note,
    lab_tracker_create_project,
    lab_tracker_create_question,
    lab_tracker_create_visualization,
    lab_tracker_link_node_to_goal,
    lab_tracker_record_evidence_bundle,
    lab_tracker_refactor_question,
    lab_tracker_update_goal,
    lab_tracker_upload_visualization_file,
)

MCPTransport = Literal["stdio", "streamable-http"]
_VALID_TRANSPORTS: set[str] = {"stdio", "streamable-http"}

ALLOW_WRITES_ENV = "LAB_TRACKER_MCP_ALLOW_WRITES"
ALLOWED_HOSTS_ENV = "LAB_TRACKER_MCP_ALLOWED_HOSTS"
ALLOWED_ORIGINS_ENV = "LAB_TRACKER_MCP_ALLOWED_ORIGINS"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})
_ALLOWED_HOST_PATTERN = (
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::(?:[0-9]{1,5}|\*))?"
)
_ALLOWED_HOST_RE = re.compile(_ALLOWED_HOST_PATTERN)
_ALLOWED_ORIGIN_RE = re.compile(rf"https?://{_ALLOWED_HOST_PATTERN}")


@dataclass(frozen=True)
class MCPServerRuntimeSettings:
    transport: MCPTransport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    inbound_token: str | None = field(default=None, repr=False)
    # Hosted (streamable-http) only: stdio always serves the full local tool set.
    allow_writes: bool = False
    # Host/Origin (DNS-rebinding) validation is opt-in: empty means the reverse
    # proxy owns Host/Origin policy and the inbound bearer defeats rebinding.
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> MCPServerRuntimeSettings:
        transport = os.getenv("LAB_TRACKER_MCP_TRANSPORT", "stdio").strip().lower()
        if transport not in _VALID_TRANSPORTS:
            raise SystemExit(
                "LAB_TRACKER_MCP_TRANSPORT must be 'stdio' or 'streamable-http'."
            )
        path = os.getenv("LAB_TRACKER_MCP_PATH", "/mcp").strip() or "/mcp"
        if not path.startswith("/"):
            path = f"/{path}"
        inbound_token = os.getenv("LAB_TRACKER_MCP_INBOUND_TOKEN")
        if transport == "streamable-http":
            inbound_token = _validated_inbound_token(inbound_token)
        allowed_hosts = _env_allowlist(
            ALLOWED_HOSTS_ENV,
            pattern=_ALLOWED_HOST_RE,
            example="mcp.lab.internal or 127.0.0.1:*",
        )
        allowed_origins = _env_allowlist(
            ALLOWED_ORIGINS_ENV,
            pattern=_ALLOWED_ORIGIN_RE,
            example="https://github.com or http://localhost:*",
        )
        if allowed_origins and not allowed_hosts:
            raise SystemExit(
                f"{ALLOWED_ORIGINS_ENV} requires {ALLOWED_HOSTS_ENV}: Host/Origin "
                "validation is enabled only by a Host allowlist."
            )
        return cls(
            transport=transport,  # type: ignore[arg-type]
            host=os.getenv("LAB_TRACKER_MCP_HOST", "127.0.0.1").strip()
            or "127.0.0.1",
            port=_env_int("LAB_TRACKER_MCP_PORT", default=8000),
            path=path,
            inbound_token=inbound_token,
            allow_writes=_env_bool(ALLOW_WRITES_ENV),
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )


class MCPInboundBearerAuthMiddleware:
    """Fail-closed bearer authentication for a hosted MCP ASGI application."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = _validated_inbound_token(token).encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        authorization_values = [
            value for name, value in scope.get("headers", []) if name.lower() == b"authorization"
        ]
        if not self._authorized(authorization_values):
            response = Response(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        # Do not expose the transport credential to FastMCP or application logs.
        downstream_scope = dict(scope)
        downstream_scope["headers"] = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() != b"authorization"
        ]
        await self.app(downstream_scope, receive, send)

    def _authorized(self, values: list[bytes]) -> bool:
        if len(values) != 1:
            return False
        scheme, separator, credential = values[0].partition(b" ")
        return (
            separator == b" "
            and scheme.lower() == b"bearer"
            and secrets.compare_digest(credential, self._token)
        )


def _offload_blocking_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run a synchronous tool in a worker thread instead of on the event loop.

    FastMCP awaits async tools but calls sync ones inline on its event loop, and
    every Lab Tracker tool does blocking HTTP I/O, so one slow API call would
    stall every other client of a hosted server. ``functools.wraps`` keeps the
    name, docstring and (through ``__wrapped__``) the signature FastMCP derives
    the tool schema from, so the registered contract and results are unchanged.
    """

    if inspect.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    async def run_in_worker_thread(*args: Any, **kwargs: Any) -> Any:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return run_in_worker_thread


class LabTrackerFastMCP(FastMCP):
    """FastMCP server that keeps blocking tool calls off the event loop."""

    def add_tool(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().add_tool(_offload_blocking_tool(fn), *args, **kwargs)


def build_server(settings: MCPServerRuntimeSettings | None = None) -> FastMCP:
    settings = settings or MCPServerRuntimeSettings()
    kwargs: dict[str, object] = {}
    if settings.transport == "streamable-http":
        kwargs = {
            "host": settings.host,
            "port": settings.port,
            "streamable_http_path": settings.path,
            "json_response": True,
            "stateless_http": True,
            # Validate Host/Origin only against an operator-configured
            # allowlist, whatever the bind address. Left to itself FastMCP
            # enables a loopback-only allowlist for loopback binds, which
            # rejects every request through a Host-preserving reverse proxy
            # (Caddy, `tailscale serve`, nginx, Traefik); the inbound bearer
            # already defeats DNS rebinding.
            "transport_security": TransportSecuritySettings(
                enable_dns_rebinding_protection=bool(settings.allowed_hosts),
                allowed_hosts=list(settings.allowed_hosts),
                allowed_origins=list(settings.allowed_origins),
            ),
        }
    runtime_server = LabTrackerFastMCP(
        SERVER_NAME,
        instructions=MCP_SERVER_INSTRUCTIONS,
        **kwargs,
    )
    register_read_tools(runtime_server)
    if settings.transport == "stdio":
        register_write_tools(runtime_server)
    elif settings.allow_writes:
        # Hosted writes are an explicit opt-in, and even then no tool may read
        # files from the MCP host.
        register_hosted_write_tools(runtime_server)
    register_resources(runtime_server)
    return runtime_server


def build_streamable_http_app(
    runtime_server: FastMCP, settings: MCPServerRuntimeSettings
) -> ASGIApp:
    if settings.transport != "streamable-http":
        raise ValueError("Streamable HTTP app requires streamable-http transport settings.")
    token = _validated_inbound_token(settings.inbound_token)
    return MCPInboundBearerAuthMiddleware(runtime_server.streamable_http_app(), token)


server = build_server()


def main() -> None:
    runtime_settings = MCPServerRuntimeSettings.from_env()
    api_settings = MCPSettings.from_env()
    hosted = runtime_settings.transport == "streamable-http"
    _ensure_hosted_tokens_are_distinct(runtime_settings, api_settings)
    _ensure_mcp_target_safe(api_settings, hosted=hosted)
    if hosted and runtime_settings.allow_writes:
        print(
            f"WARNING: {ALLOW_WRITES_ENV} is enabled: this hosted Lab Tracker MCP "
            "endpoint serves write tools to every holder of the inbound bearer, and "
            "the API credential is not required to be read-only.",
            file=sys.stderr,
            flush=True,
        )
    elif hosted:
        _ensure_hosted_api_credential_is_read_only(api_settings)
    runtime_server = build_server(runtime_settings)
    if runtime_settings.transport == "streamable-http":
        _run_streamable_http(runtime_server, runtime_settings)
    else:
        runtime_server.run(transport=runtime_settings.transport)


def _run_streamable_http(
    runtime_server: FastMCP, settings: MCPServerRuntimeSettings
) -> None:
    import uvicorn

    app = build_streamable_http_app(runtime_server, settings)
    uvicorn.run(app, host=settings.host, port=settings.port)


def _ensure_hosted_tokens_are_distinct(
    runtime_settings: MCPServerRuntimeSettings, api_settings: MCPSettings
) -> None:
    if runtime_settings.transport != "streamable-http":
        return
    inbound_token = _validated_inbound_token(runtime_settings.inbound_token)
    api_key = (api_settings.api_key or "").strip()
    if api_key and secrets.compare_digest(inbound_token.encode(), api_key.encode()):
        raise SystemExit(
            "LAB_TRACKER_MCP_INBOUND_TOKEN must be distinct from "
            "LAB_TRACKER_MCP_API_KEY / LT_MCP_READONLY_TOKEN."
        )


def _ensure_mcp_target_safe(
    settings: MCPSettings | None = None, *, hosted: bool = False
) -> None:
    """Refuse to serve an API target that does not enforce authentication.

    Hosted (streamable-http) servers always probe and fail closed: they boot only
    once ``/readiness`` confirms ``auth.enabled``. Local stdio servers skip
    loopback targets and, for remote targets, stay fail-soft on probe errors (a
    local agent proceeds without graph context) but warn loudly on stderr.
    """

    settings = settings or MCPSettings.from_env()
    if not hosted and _is_loopback_url(settings.base_url):
        return
    client = LabTrackerAPIClient(settings)
    try:
        payload = client.readiness()
    except LabTrackerAPIError as exc:
        if hosted:
            raise SystemExit(
                "Refusing to start hosted Lab Tracker MCP: the startup safety probe "
                f"could not confirm that the API at {settings.base_url} enforces "
                f"authentication (GET /readiness failed: {exc}). Fix the API target "
                "or credentials; the server will start once the probe succeeds."
            ) from exc
        if isinstance(exc, (LabTrackerAPIAuthError, LabTrackerAPIPermissionError)):
            # A swallowed auth failure makes the resulting 401/403 undiagnosable:
            # health stays green while every authenticated tool fails (GH #79).
            _warn_startup_auth_probe_failed(settings, exc)
        else:
            _warn_startup_target_probe_failed(settings, exc)
        return
    finally:
        client.close()
    auth = payload.get("auth")
    auth_enabled = auth.get("enabled") if isinstance(auth, dict) else None
    if auth_enabled is False:
        location = "hosted" if hosted else "a non-loopback"
        raise SystemExit(
            f"Refusing to start Lab Tracker MCP against {location} auth-disabled "
            f"API target: {settings.base_url}"
        )
    if hosted and auth_enabled is not True:
        raise SystemExit(
            "Refusing to start hosted Lab Tracker MCP: GET /readiness at "
            f"{settings.base_url} did not report auth.enabled=true, so the startup "
            "safety probe could not confirm that the API enforces authentication."
        )


def _ensure_hosted_api_credential_is_read_only(settings: MCPSettings) -> None:
    """Refuse to host a read-only surface with a credential that can write.

    The hosted endpoint is read-only by contract, enforced by the API credential
    it holds. Only a personal access token can be read-only, and the API exposes
    no introspection route to service tokens, so the check asks the API's own
    service-token policy (see ``LabTrackerAPIClient.credential_can_write``) and
    fails closed on anything but an explicit refusal.
    """

    api_key = (settings.api_key or "").strip()
    if not api_key.startswith(LPAT_TOKEN_PREFIX):
        raise SystemExit(
            "Refusing to start hosted Lab Tracker MCP: the hosted endpoint is read-only "
            "and must reach the API with a read-only personal access token. Set "
            "LAB_TRACKER_MCP_API_KEY (compose: LT_MCP_READONLY_TOKEN) to an lpat_ token "
            "minted with read_only=true; username/password logins and other credentials "
            f"cannot be verified read-only. To serve write tools instead, set "
            f"{ALLOW_WRITES_ENV}=true."
        )
    client = LabTrackerAPIClient(settings)
    try:
        can_write = client.credential_can_write()
    except LabTrackerAPIError as exc:
        raise SystemExit(
            "Refusing to start hosted Lab Tracker MCP: could not verify that "
            f"LAB_TRACKER_MCP_API_KEY is read-only against {settings.base_url}: {exc}"
        ) from exc
    finally:
        client.close()
    if can_write:
        raise SystemExit(
            "Refusing to start hosted Lab Tracker MCP: LAB_TRACKER_MCP_API_KEY can write "
            f"(the API at {settings.base_url} did not refuse a write probe with it). "
            "Mint an lpat_ token with read_only=true for the hosted endpoint, or set "
            f"{ALLOW_WRITES_ENV}=true to serve write tools deliberately."
        )


def _warn_startup_target_probe_failed(
    settings: MCPSettings, exc: LabTrackerAPIError
) -> None:
    status = f"HTTP {exc.status_code}" if exc.status_code is not None else "no response"
    print(
        f"WARNING: Lab Tracker MCP startup safety probe ({status}) could not confirm "
        f"that the API at {settings.base_url} enforces authentication: {exc}. The "
        "server is starting anyway; tools will report Lab Tracker as unavailable "
        "until the API answers. Relaunch the MCP server once the API is reachable so "
        "the probe can run.",
        file=sys.stderr,
        flush=True,
    )


def _warn_startup_auth_probe_failed(
    settings: MCPSettings, exc: LabTrackerAPIAuthError | LabTrackerAPIPermissionError
) -> None:
    status = exc.status_code or 401
    auth_mode = (
        "static LAB_TRACKER_MCP_API_KEY (LPAT)"
        if (settings.api_key or "").strip()
        else "LAB_TRACKER_MCP_USERNAME/PASSWORD login"
        if (settings.username or "").strip()
        else "no credentials"
    )
    print(
        f"WARNING: Lab Tracker MCP startup auth probe failed (HTTP {status}) against "
        f"{settings.base_url} using {auth_mode}. The server is starting anyway, but "
        "every authenticated tool will fail until this is fixed — check "
        "LAB_TRACKER_MCP_API_KEY / credentials, then relaunch the MCP server.",
        file=sys.stderr,
        flush=True,
    )


def _is_loopback_url(value: str) -> bool:
    host = (urlparse(value).hostname or "").strip().lower()
    if host in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _env_bool(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUE_ENV_VALUES:
        return True
    if value in _FALSE_ENV_VALUES:
        return False
    raise SystemExit(f"{name} must be true or false.")


def _env_allowlist(
    name: str,
    *,
    pattern: re.Pattern[str],
    example: str,
) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return ()
    entries = tuple(entry.strip() for entry in raw.split(","))
    invalid = [entry for entry in entries if pattern.fullmatch(entry) is None]
    if not entries or invalid:
        raise SystemExit(
            f"{name} must be a comma-separated list such as {example} "
            f"(':*' matches any port); invalid entries: {invalid!r}."
        )
    return entries


def _env_int(name: str, *, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer.") from exc
    if value <= 0 or value > 65535:
        raise SystemExit(f"{name} must be a TCP port from 1 to 65535.")
    return value


def _validated_inbound_token(value: str | None) -> str:
    if value is None or not value:
        raise SystemExit(
            "LAB_TRACKER_MCP_INBOUND_TOKEN is required for streamable-http transport."
        )
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SystemExit(
            "LAB_TRACKER_MCP_INBOUND_TOKEN must contain only visible ASCII characters."
        ) from exc
    if len(encoded) < 32 or len(encoded) > 512 or any(
        byte < 0x21 or byte > 0x7E for byte in encoded
    ):
        raise SystemExit(
            "LAB_TRACKER_MCP_INBOUND_TOKEN must be 32-512 visible ASCII characters "
            "with no whitespace."
        )
    return value


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "JsonObject",
    "LabTrackerAPIAuthError",
    "LabTrackerAPIClient",
    "LabTrackerAPIError",
    "LabTrackerAPIPermissionError",
    "LabTrackerAPIUnavailableError",
    "LabTrackerAPIValidationError",
    "MCPSettings",
    "MCPInboundBearerAuthMiddleware",
    "MCPServerRuntimeSettings",
    "ALLOW_WRITES_ENV",
    "ALLOWED_HOSTS_ENV",
    "ALLOWED_ORIGINS_ENV",
    "MCP_SERVER_INSTRUCTIONS",
    "MCPTransport",
    "SERVER_NAME",
    "build_server",
    "build_streamable_http_app",
    "client_from_env",
    "lab_tracker_api_error",
    "lab_tracker_agent_consultation_policy",
    "lab_tracker_code_conventions",
    "lab_tracker_create_analysis",
    "lab_tracker_create_claim",
    "lab_tracker_create_claim_edge",
    "lab_tracker_create_dataset",
    "lab_tracker_create_goal",
    "lab_tracker_create_note",
    "lab_tracker_create_project",
    "lab_tracker_create_question",
    "lab_tracker_create_visualization",
    "lab_tracker_describe_schema",
    "lab_tracker_export_goal_artifact",
    "lab_tracker_export_question_subtree",
    "lab_tracker_get_analysis_provenance",
    "lab_tracker_get_claim_provenance",
    "lab_tracker_get_dataset_provenance",
    "lab_tracker_get_decision_context",
    "lab_tracker_get_graph_neighborhood",
    "lab_tracker_get_goal",
    "lab_tracker_graph_overview",
    "lab_tracker_health",
    "lab_tracker_list_analyses",
    "lab_tracker_list_claim_edges",
    "lab_tracker_list_claims",
    "lab_tracker_list_datasets",
    "lab_tracker_list_goals",
    "lab_tracker_list_node_goals",
    "lab_tracker_list_notes",
    "lab_tracker_list_projects",
    "lab_tracker_list_question_refactors",
    "lab_tracker_list_questions",
    "lab_tracker_list_sessions",
    "lab_tracker_list_visualizations",
    "lab_tracker_next_questions",
    "lab_tracker_publication_readiness",
    "lab_tracker_quickstart",
    "lab_tracker_readiness",
    "lab_tracker_link_node_to_goal",
    "lab_tracker_record_evidence_bundle",
    "lab_tracker_refactor_question",
    "lab_tracker_search",
    "lab_tracker_search_graph",
    "lab_tracker_surface",
    "lab_tracker_update_goal",
    "lab_tracker_upload_visualization_file",
    "main",
    "server",
]


if __name__ == "__main__":
    main()
