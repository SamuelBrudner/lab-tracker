"""Registration entrypoint for the API-backed Lab Tracker MCP server."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from lab_tracker.mcp_api_client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    SERVER_NAME,
    JsonObject,
    LabTrackerAPIClient,
    LabTrackerAPIError,
    MCPSettings,
    client_from_env,
)
from lab_tracker.mcp_tools import (
    register_read_tools,
    register_resources,
    register_write_tools,
)
from lab_tracker.mcp_tools.read import (
    lab_tracker_get_analysis_provenance,
    lab_tracker_get_dataset_provenance,
    lab_tracker_get_decision_context,
    lab_tracker_get_goal,
    lab_tracker_health,
    lab_tracker_list_analyses,
    lab_tracker_list_claims,
    lab_tracker_list_datasets,
    lab_tracker_list_goals,
    lab_tracker_list_node_goals,
    lab_tracker_list_notes,
    lab_tracker_list_projects,
    lab_tracker_list_questions,
    lab_tracker_list_sessions,
    lab_tracker_list_visualizations,
    lab_tracker_readiness,
    lab_tracker_search,
)
from lab_tracker.mcp_tools.resources import (
    lab_tracker_agent_consultation_policy,
    lab_tracker_quickstart,
    lab_tracker_surface,
)
from lab_tracker.mcp_tools.write import (
    lab_tracker_create_analysis,
    lab_tracker_create_claim,
    lab_tracker_create_dataset,
    lab_tracker_create_goal,
    lab_tracker_create_note,
    lab_tracker_create_project,
    lab_tracker_create_question,
    lab_tracker_create_visualization,
    lab_tracker_link_node_to_goal,
    lab_tracker_list_question_refactors,
    lab_tracker_refactor_question,
    lab_tracker_update_goal,
    lab_tracker_upload_visualization_file,
)

server = FastMCP(SERVER_NAME)
register_read_tools(server)
register_write_tools(server)
register_resources(server)


def main() -> None:
    server.run(transport="stdio")


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "JsonObject",
    "LabTrackerAPIClient",
    "LabTrackerAPIError",
    "MCPSettings",
    "SERVER_NAME",
    "client_from_env",
    "lab_tracker_agent_consultation_policy",
    "lab_tracker_create_analysis",
    "lab_tracker_create_claim",
    "lab_tracker_create_dataset",
    "lab_tracker_create_goal",
    "lab_tracker_create_note",
    "lab_tracker_create_project",
    "lab_tracker_create_question",
    "lab_tracker_create_visualization",
    "lab_tracker_get_analysis_provenance",
    "lab_tracker_get_dataset_provenance",
    "lab_tracker_get_decision_context",
    "lab_tracker_get_goal",
    "lab_tracker_health",
    "lab_tracker_list_analyses",
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
    "lab_tracker_quickstart",
    "lab_tracker_readiness",
    "lab_tracker_link_node_to_goal",
    "lab_tracker_refactor_question",
    "lab_tracker_search",
    "lab_tracker_surface",
    "lab_tracker_update_goal",
    "lab_tracker_upload_visualization_file",
    "main",
    "server",
]


if __name__ == "__main__":
    main()
