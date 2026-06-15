"""Read-only MCP tool functions and registration."""

from __future__ import annotations

from typing import Any

from lab_tracker.mcp_api_client import JsonObject, client_from_env


def lab_tracker_health() -> JsonObject:
    """Check the Lab Tracker API health endpoint."""
    client = client_from_env()
    try:
        return client.health()
    finally:
        client.close()


def lab_tracker_readiness() -> JsonObject:
    """Check database and storage readiness for Lab Tracker."""
    client = client_from_env()
    try:
        return client.readiness()
    finally:
        client.close()


def lab_tracker_describe_schema(entity_type: str | None = None) -> JsonObject:
    """Describe Lab Tracker fields, allowed enum values, and status lifecycles."""
    client = client_from_env()
    try:
        return client.describe_schema(entity_type=entity_type)
    finally:
        client.close()


def lab_tracker_list_projects(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker projects through the API."""
    client = client_from_env()
    try:
        return client.list_projects(status=status, limit=limit, offset=offset)
    finally:
        client.close()


def lab_tracker_list_questions(
    project_id: str | None = None,
    status: str | None = None,
    question_type: str | None = None,
    search: str | None = None,
    parent_question_id: str | None = None,
    ancestor_question_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List/search questions; use parent or ancestor filters for hierarchy traversal."""
    client = client_from_env()
    try:
        return client.list_questions(
            project_id=project_id,
            status=status,
            question_type=question_type,
            search=search,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_list_notes(
    project_id: str | None = None,
    status: str | None = None,
    target_entity_type: str | None = None,
    target_entity_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker notes through the API."""
    client = client_from_env()
    try:
        return client.list_notes(
            project_id=project_id,
            status=status,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_search(
    query: str,
    project_id: str | None = None,
    goal_id: str | None = None,
    include: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> JsonObject:
    """Search Lab Tracker questions and notes through the API."""
    client = client_from_env()
    try:
        return client.search(
            query,
            project_id=project_id,
            goal_id=goal_id,
            include=include,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_list_sessions(
    project_id: str | None = None,
    status: str | None = None,
    session_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker sessions through the API."""
    client = client_from_env()
    try:
        return client.list_sessions(
            project_id=project_id,
            status=status,
            session_type=session_type,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_list_datasets(
    project_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker datasets through the API."""
    client = client_from_env()
    try:
        return client.list_datasets(
            project_id=project_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_list_analyses(
    project_id: str | None = None,
    dataset_id: str | None = None,
    question_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker analyses through the API."""
    client = client_from_env()
    try:
        return client.list_analyses(
            project_id=project_id,
            dataset_id=dataset_id,
            question_id=question_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_list_claims(
    project_id: str | None = None,
    status: str | None = None,
    dataset_id: str | None = None,
    analysis_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker claims through the API."""
    client = client_from_env()
    try:
        return client.list_claims(
            project_id=project_id,
            status=status,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_list_visualizations(
    project_id: str | None = None,
    analysis_id: str | None = None,
    claim_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker visualizations through the API."""
    client = client_from_env()
    try:
        return client.list_visualizations(
            project_id=project_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_list_goals(
    project_id: str,
    goal_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker goals for a project."""
    client = client_from_env()
    try:
        return client.list_goals(
            project_id=project_id,
            goal_type=goal_type,
            status=status,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_get_goal(goal_id: str) -> JsonObject:
    """Get a Lab Tracker goal with its node links."""
    client = client_from_env()
    try:
        return client.get_goal(goal_id)
    finally:
        client.close()


def lab_tracker_list_node_goals(
    project_id: str,
    entity_type: str,
    entity_id: str,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List goals linked to one project graph node."""
    client = client_from_env()
    try:
        return client.list_node_goals(
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_get_dataset_provenance(dataset_id: str) -> JsonObject:
    """Get dataset provenance JSON-LD through the API."""
    client = client_from_env()
    try:
        return client.get_dataset_provenance(dataset_id)
    finally:
        client.close()


def lab_tracker_get_analysis_provenance(analysis_id: str) -> JsonObject:
    """Get analysis provenance JSON-LD through the API."""
    client = client_from_env()
    try:
        return client.get_analysis_provenance(analysis_id)
    finally:
        client.close()


def lab_tracker_get_decision_context(
    task_kind: str,
    query: str,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_id: str | None = None,
    analysis_id: str | None = None,
    claim_id: str | None = None,
    visualization_id: str | None = None,
    limit: int = 20,
) -> JsonObject:
    """Build bounded graph context before research-facing assistant decisions."""
    client = client_from_env()
    try:
        return client.get_decision_context(
            task_kind=task_kind,
            query=query,
            project_id=project_id,
            question_id=question_id,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            visualization_id=visualization_id,
            limit=limit,
        )
    finally:
        client.close()

READ_TOOLS = (
    lab_tracker_health,
    lab_tracker_readiness,
    lab_tracker_describe_schema,
    lab_tracker_list_projects,
    lab_tracker_list_questions,
    lab_tracker_list_notes,
    lab_tracker_search,
    lab_tracker_list_sessions,
    lab_tracker_list_datasets,
    lab_tracker_list_analyses,
    lab_tracker_list_claims,
    lab_tracker_list_visualizations,
    lab_tracker_list_goals,
    lab_tracker_get_goal,
    lab_tracker_list_node_goals,
    lab_tracker_get_dataset_provenance,
    lab_tracker_get_analysis_provenance,
    lab_tracker_get_decision_context,
)


def register_read_tools(server: Any) -> None:
    for tool in READ_TOOLS:
        server.tool()(tool)
