"""Registration entrypoint for the API-backed Lab Tracker MCP server."""

from __future__ import annotations

import ipaddress
import os
import sys
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

from lab_tracker.decision_context_constants import MCP_SERVER_INSTRUCTIONS
from lab_tracker.mcp_api_client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    SERVER_NAME,
    JsonObject,
    LabTrackerAPIAuthError,
    LabTrackerAPIClient,
    LabTrackerAPIError,
    LabTrackerAPIUnavailableError,
    LabTrackerAPIValidationError,
    MCPSettings,
    client_from_env,
    lab_tracker_api_error,
)
from lab_tracker.mcp_tools import (
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
    lab_tracker_get_experiment,
    lab_tracker_get_goal,
    lab_tracker_health,
    lab_tracker_list_analyses,
    lab_tracker_list_claim_edges,
    lab_tracker_list_claims,
    lab_tracker_list_datasets,
    lab_tracker_list_experiment_datasets,
    lab_tracker_list_experiment_sessions,
    lab_tracker_list_experiments,
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
    lab_tracker_create_experiment,
    lab_tracker_create_goal,
    lab_tracker_create_note,
    lab_tracker_create_project,
    lab_tracker_create_question,
    lab_tracker_create_visualization,
    lab_tracker_link_node_to_goal,
    lab_tracker_record_evidence_bundle,
    lab_tracker_refactor_question,
    lab_tracker_update_experiment,
    lab_tracker_update_goal,
    lab_tracker_upload_visualization_file,
)

MCPTransport = Literal["stdio", "streamable-http"]
_VALID_TRANSPORTS: set[str] = {"stdio", "streamable-http"}


@dataclass(frozen=True)
class MCPServerRuntimeSettings:
    transport: MCPTransport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"

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
        return cls(
            transport=transport,  # type: ignore[arg-type]
            host=os.getenv("LAB_TRACKER_MCP_HOST", "127.0.0.1").strip()
            or "127.0.0.1",
            port=_env_int("LAB_TRACKER_MCP_PORT", default=8000),
            path=path,
        )


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
        }
    runtime_server = FastMCP(
        SERVER_NAME,
        instructions=MCP_SERVER_INSTRUCTIONS,
        **kwargs,
    )
    register_read_tools(runtime_server)
    register_write_tools(runtime_server)
    register_resources(runtime_server)
    return runtime_server


server = build_server()


def main() -> None:
    runtime_settings = MCPServerRuntimeSettings.from_env()
    _ensure_mcp_target_safe()
    runtime_server = build_server(runtime_settings)
    runtime_server.run(transport=runtime_settings.transport)


def _ensure_mcp_target_safe(settings: MCPSettings | None = None) -> None:
    settings = settings or MCPSettings.from_env()
    if _is_loopback_url(settings.base_url):
        return
    client = LabTrackerAPIClient(settings)
    try:
        payload = client.readiness()
    except LabTrackerAPIAuthError as exc:
        # The startup probe is fail-soft (the server still boots), but a swallowed
        # auth failure makes the resulting 401 undiagnosable: health stays green
        # while every authenticated tool fails. Emit a loud stderr warning so it
        # lands in the MCP server log (GH #79).
        _warn_startup_auth_probe_failed(settings, exc)
        return
    except LabTrackerAPIError:
        return
    finally:
        client.close()
    auth = payload.get("auth")
    if isinstance(auth, dict) and auth.get("enabled") is False:
        raise SystemExit(
            "Refusing to start Lab Tracker MCP against a non-loopback auth-disabled "
            f"API target: {settings.base_url}"
        )


def _warn_startup_auth_probe_failed(
    settings: MCPSettings, exc: LabTrackerAPIAuthError
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


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "JsonObject",
    "LabTrackerAPIAuthError",
    "LabTrackerAPIClient",
    "LabTrackerAPIError",
    "LabTrackerAPIUnavailableError",
    "LabTrackerAPIValidationError",
    "MCPSettings",
    "MCPServerRuntimeSettings",
    "MCP_SERVER_INSTRUCTIONS",
    "MCPTransport",
    "SERVER_NAME",
    "build_server",
    "client_from_env",
    "lab_tracker_api_error",
    "lab_tracker_agent_consultation_policy",
    "lab_tracker_code_conventions",
    "lab_tracker_create_analysis",
    "lab_tracker_create_claim",
    "lab_tracker_create_claim_edge",
    "lab_tracker_create_dataset",
    "lab_tracker_create_experiment",
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
    "lab_tracker_get_experiment",
    "lab_tracker_get_goal",
    "lab_tracker_health",
    "lab_tracker_list_analyses",
    "lab_tracker_list_claim_edges",
    "lab_tracker_list_claims",
    "lab_tracker_list_datasets",
    "lab_tracker_list_experiment_datasets",
    "lab_tracker_list_experiment_sessions",
    "lab_tracker_list_experiments",
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
    "lab_tracker_surface",
    "lab_tracker_update_goal",
    "lab_tracker_update_experiment",
    "lab_tracker_upload_visualization_file",
    "main",
    "server",
]


if __name__ == "__main__":
    main()
