"""Read-only MCP tool functions and registration."""

from __future__ import annotations

from typing import Any

import httpx

from lab_tracker.mcp_api_client import (
    JsonObject,
    LabTrackerAPIError,
    client_from_env,
    lab_tracker_unavailable,
)
from lab_tracker.mcp_tools.hints import next_action, with_next_action


def _read_tool(
    tool_name: str,
    call: Any,
    *,
    hint: JsonObject,
) -> JsonObject:
    client = client_from_env()
    try:
        return with_next_action(call(client), hint)
    except (LabTrackerAPIError, httpx.HTTPError) as exc:
        return lab_tracker_unavailable(tool_name, detail=str(exc))
    finally:
        client.close()


def lab_tracker_health() -> JsonObject:
    """Check Lab Tracker API health; fail softly if the service is unavailable."""
    return _read_tool(
        "lab_tracker_health",
        lambda client: client.health(),
        hint=next_action(
            "lab_tracker_readiness",
            "If health is OK, check database and storage readiness next.",
        ),
    )


def lab_tracker_readiness() -> JsonObject:
    """Check Lab Tracker database and storage readiness."""
    return _read_tool(
        "lab_tracker_readiness",
        lambda client: client.readiness(),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "For research-facing work, load graph context after readiness passes.",
        ),
    )


def lab_tracker_describe_schema(entity_type: str | None = None) -> JsonObject:
    """Describe fields/enums before create_* calls; use after context lookup."""
    return _read_tool(
        "lab_tracker_describe_schema",
        lambda client: client.describe_schema(entity_type=entity_type),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "Resolve project and entity IDs before writing new records.",
        ),
    )


def lab_tracker_list_projects(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List visible projects when scoping a follow-up Lab Tracker read."""
    return _read_tool(
        "lab_tracker_list_projects",
        lambda client: client.list_projects(status=status, limit=limit, offset=offset),
        hint=next_action(
            "lab_tracker_list_goals",
            "After choosing a project, inspect active goals or call decision context.",
        ),
    )


def lab_tracker_list_questions(
    project_id: str | None = None,
    status: str | None = None,
    question_type: str | None = None,
    search: str | None = None,
    created_by: str | None = None,
    parent_question_id: str | None = None,
    ancestor_question_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List/search questions when inspecting known project/question scope.

    Use lab_tracker_get_decision_context first for research-facing decisions.
    Parent and ancestor filters traverse the question hierarchy.
    """
    return _read_tool(
        "lab_tracker_list_questions",
        lambda client: client.list_questions(
            project_id=project_id,
            status=status,
            question_type=question_type,
            search=search,
            created_by=created_by,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "Load bounded graph context before using a question to plan analysis or writing.",
        ),
    )


def lab_tracker_list_notes(
    project_id: str | None = None,
    status: str | None = None,
    created_by: str | None = None,
    target_entity_type: str | None = None,
    target_entity_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List notes for known scope; use decision context first for research choices."""
    return _read_tool(
        "lab_tracker_list_notes",
        lambda client: client.list_notes(
            project_id=project_id,
            status=status,
            created_by=created_by,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "Use notes as source context through decision context before writing outputs.",
        ),
    )


def lab_tracker_search(
    query: str,
    project_id: str | None = None,
    goal_id: str | None = None,
    include: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> JsonObject:
    """Search questions and notes when the project or anchor IDs are not known."""
    return _read_tool(
        "lab_tracker_search",
        lambda client: client.search(
            query,
            project_id=project_id,
            goal_id=goal_id,
            include=include,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "Turn search hits into bounded decision context before research-facing work.",
        ),
    )


def lab_tracker_list_sessions(
    project_id: str | None = None,
    status: str | None = None,
    session_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List acquisition/experiment sessions for a known project scope."""
    return _read_tool(
        "lab_tracker_list_sessions",
        lambda client: client.list_sessions(
            project_id=project_id,
            status=status,
            session_type=session_type,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_list_datasets",
            "After sessions, inspect datasets or load decision context for the task.",
        ),
    )


def lab_tracker_list_datasets(
    project_id: str | None = None,
    status: str | None = None,
    created_by: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List datasets; create-order is dataset -> analysis -> claim -> visualization."""
    return _read_tool(
        "lab_tracker_list_datasets",
        lambda client: client.list_datasets(
            project_id=project_id,
            status=status,
            created_by=created_by,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_list_analyses",
            "Find analyses that used candidate datasets before creating new analyses.",
        ),
    )


def lab_tracker_list_analyses(
    project_id: str | None = None,
    dataset_id: str | None = None,
    question_id: str | None = None,
    status: str | None = None,
    created_by: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List analyses; use after datasets and before claims/visualizations."""
    return _read_tool(
        "lab_tracker_list_analyses",
        lambda client: client.list_analyses(
            project_id=project_id,
            dataset_id=dataset_id,
            question_id=question_id,
            status=status,
            created_by=created_by,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_list_claims",
            "Check claims supported by candidate analyses before writing new claims.",
        ),
    )


def lab_tracker_list_claims(
    project_id: str | None = None,
    status: str | None = None,
    dataset_id: str | None = None,
    analysis_id: str | None = None,
    created_by: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List claims for known evidence; claims come after datasets and analyses."""
    return _read_tool(
        "lab_tracker_list_claims",
        lambda client: client.list_claims(
            project_id=project_id,
            status=status,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            created_by=created_by,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_list_visualizations",
            "Inspect visualizations tied to claims before creating new figures.",
        ),
    )


def lab_tracker_list_claim_edges(
    claim_id: str,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List typed outgoing logic edges for a claim."""
    return _read_tool(
        "lab_tracker_list_claim_edges",
        lambda client: client.list_claim_edges(
            claim_id=claim_id,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_create_claim_edge",
            "Add a typed relation when one claim extends or refutes another.",
        ),
    )


def lab_tracker_list_visualizations(
    project_id: str | None = None,
    analysis_id: str | None = None,
    claim_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List visualizations after resolving related analyses or claims."""
    return _read_tool(
        "lab_tracker_list_visualizations",
        lambda client: client.list_visualizations(
            project_id=project_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "Use returned figures as evidence context for plots, slides, or writing.",
        ),
    )


def lab_tracker_list_goals(
    project_id: str | None = None,
    goal_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List goals/outputs when deciding what research objective to advance."""
    return _read_tool(
        "lab_tracker_list_goals",
        lambda client: client.list_goals(
            project_id=project_id,
            goal_type=goal_type,
            status=status,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_next_questions",
            "Rank open questions on active goals to choose an obvious next move.",
        ),
    )


def lab_tracker_get_goal(goal_id: str) -> JsonObject:
    """Get one goal with node links before advancing or updating it."""
    return _read_tool(
        "lab_tracker_get_goal",
        lambda client: client.get_goal(goal_id),
        hint=next_action(
            "lab_tracker_next_questions",
            "Use goal links to rank open questions before choosing follow-up work.",
        ),
    )


def lab_tracker_publication_readiness(project_id: str) -> JsonObject:
    """Check ARA-Seal L1 structural readiness for one project."""
    return _read_tool(
        "lab_tracker_publication_readiness",
        lambda client: client.publication_readiness(project_id),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "If readiness is blocked, inspect the flagged graph nodes before writing.",
        ),
    )


def lab_tracker_list_node_goals(
    project_id: str,
    entity_type: str,
    entity_id: str,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List goals linked to one project graph node."""
    return _read_tool(
        "lab_tracker_list_node_goals",
        lambda client: client.list_node_goals(
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            limit=limit,
            offset=offset,
        ),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "Load decision context for the linked goal/node before research-facing work.",
        ),
    )


def lab_tracker_get_dataset_provenance(dataset_id: str) -> JsonObject:
    """Get dataset provenance JSON-LD before reusing evidence."""
    return _read_tool(
        "lab_tracker_get_dataset_provenance",
        lambda client: client.get_dataset_provenance(dataset_id),
        hint=next_action(
            "lab_tracker_list_analyses",
            "Find analyses derived from this dataset before creating new claims.",
        ),
    )


def lab_tracker_get_analysis_provenance(analysis_id: str) -> JsonObject:
    """Get analysis provenance JSON-LD before reusing derived evidence."""
    return _read_tool(
        "lab_tracker_get_analysis_provenance",
        lambda client: client.get_analysis_provenance(analysis_id),
        hint=next_action(
            "lab_tracker_list_claims",
            "Find claims supported by this analysis before writing new claims.",
        ),
    )


def lab_tracker_get_claim_provenance(claim_id: str) -> JsonObject:
    """Get claim-centric provenance JSON-LD with analysis/dataset/question ancestry."""
    return _read_tool(
        "lab_tracker_get_claim_provenance",
        lambda client: client.get_claim_provenance(claim_id),
        hint=next_action(
            "lab_tracker_export_goal_artifact",
            "Use the surrounding goal artifact when compiling publication evidence.",
        ),
    )


def lab_tracker_export_goal_artifact(
    goal_id: str,
    layer: str | None = None,
) -> JsonObject:
    """Compile a goal into an Ara artifact; pass layer logic/src/trace/evidence for one layer."""
    return _read_tool(
        "lab_tracker_export_goal_artifact",
        lambda client: client.export_goal_artifact(goal_id, layer=layer),
        hint=next_action(
            "lab_tracker_get_claim_provenance",
            "Inspect claim provenance when a layer binding needs ancestry details.",
        ),
    )


def lab_tracker_export_question_subtree(
    question_id: str,
    layer: str | None = None,
) -> JsonObject:
    """Compile a question subtree into layered Ara JSON-LD."""
    return _read_tool(
        "lab_tracker_export_question_subtree",
        lambda client: client.export_question_subtree(question_id, layer=layer),
        hint=next_action(
            "lab_tracker_get_claim_provenance",
            "Inspect claim provenance for any claims in the compiled subtree.",
        ),
    )


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
    """CALL THIS FIRST before research-facing decisions.

    Returns bounded graph context plus resolved project scope, anchor IDs,
    candidate entity IDs, evidence links, and guidance for subsequent create
    calls. Use for what to plot, which analysis/control to run, figures,
    summaries, slides, and manuscript/grant/abstract text. Allowed task_kind
    values: plot, analysis, slides, experiment_plan, summary, research_writing.
    """
    return _read_tool(
        "lab_tracker_get_decision_context",
        lambda client: client.get_decision_context(
            task_kind=task_kind,
            query=query,
            project_id=project_id,
            question_id=question_id,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            visualization_id=visualization_id,
            limit=limit,
        ),
        hint=next_action(
            "lab_tracker_next_questions",
            "If the user asks what to advance next, rank active-goal questions.",
        ),
    )


def lab_tracker_next_questions(
    project_id: str | None = None,
    limit: int = 5,
) -> JsonObject:
    """Rank open active/staged questions on planned/in-progress goals.

    Call this when the user asks what research thread to advance or when a
    fresh session needs an obvious entry action. The ranking favors direct
    goal-question links, active questions, and questions with hypotheses.
    """
    return _read_tool(
        "lab_tracker_next_questions",
        lambda client: client.next_questions(project_id=project_id, limit=limit),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "Load graph context for the selected ranked question.",
        ),
    )

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
    lab_tracker_list_claim_edges,
    lab_tracker_list_visualizations,
    lab_tracker_list_goals,
    lab_tracker_get_goal,
    lab_tracker_publication_readiness,
    lab_tracker_list_node_goals,
    lab_tracker_get_dataset_provenance,
    lab_tracker_get_analysis_provenance,
    lab_tracker_get_claim_provenance,
    lab_tracker_export_goal_artifact,
    lab_tracker_export_question_subtree,
    lab_tracker_get_decision_context,
    lab_tracker_next_questions,
)


def register_read_tools(server: Any) -> None:
    for tool in READ_TOOLS:
        server.tool()(tool)
