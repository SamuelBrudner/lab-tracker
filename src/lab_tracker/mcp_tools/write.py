"""Mutating MCP tool functions and registration."""

from __future__ import annotations

from typing import Any

from lab_tracker.mcp_api_client import (
    QUESTION_STATUS_TEXT,
    QUESTION_STATUS_VALUES,
    JsonObject,
    LabTrackerAPIError,
    NoteMetadataScalar,
    client_from_env,
)
from lab_tracker.mcp_evidence_bundle import record_evidence_bundle
from lab_tracker.mcp_tools.hints import next_action, with_next_action


def lab_tracker_create_project(
    name: str,
    description: str | None = None,
    status: str | None = None,
) -> JsonObject:
    """Create a project only when the user explicitly asks for a new scope."""
    client = client_from_env()
    try:
        return with_next_action(
            client.create_project(name=name, description=description, status=status),
            next_action(
                "lab_tracker_create_goal",
                "Create or select the goal/output this project should advance.",
            ),
        )
    finally:
        client.close()


def lab_tracker_create_question(
    project_id: str,
    text: str,
    question_type: str = "other",
    hypothesis: str | None = None,
    status: str | None = None,
    parent_question_ids: list[str] | None = None,
) -> JsonObject:
    """Create a question after project/goal scope is known."""
    client = client_from_env()
    try:
        return with_next_action(
            client.create_question(
                project_id=project_id,
                text=text,
                question_type=question_type,
                hypothesis=hypothesis,
                status=status,
                parent_question_ids=parent_question_ids,
            ),
            next_action(
                "lab_tracker_link_node_to_goal",
                "Link important questions to the goal they advance.",
            ),
        )
    finally:
        client.close()


def lab_tracker_refactor_question(
    question_id: str,
    replacement_text: str,
    replacement_question_type: str = "other",
    replacement_status: str = "staged",
    reason: str = "",
    replacement_hypothesis: str | None = None,
    replacement_parent_question_ids: list[str] | None = None,
    child_question_ids_to_reparent: list[str] | None = None,
    note_ids_to_retarget: list[str] | None = None,
) -> JsonObject:
    """Supersede a question with a replacement and optional child/note moves."""
    if replacement_status not in QUESTION_STATUS_VALUES:
        raise LabTrackerAPIError(
            f"Invalid replacement status {replacement_status!r}. "
            f"Allowed statuses: {QUESTION_STATUS_TEXT}."
        )
    client = client_from_env()
    try:
        return with_next_action(
            client.refactor_question(
                question_id=question_id,
                replacement_text=replacement_text,
                replacement_question_type=replacement_question_type,
                replacement_status=replacement_status,
                replacement_hypothesis=replacement_hypothesis,
                replacement_parent_question_ids=replacement_parent_question_ids,
                reason=reason,
                child_question_ids_to_reparent=child_question_ids_to_reparent,
                note_ids_to_retarget=note_ids_to_retarget,
            ),
            next_action(
                "lab_tracker_get_decision_context",
                "Reload context for the replacement question before follow-up writes.",
            ),
        )
    finally:
        client.close()


def lab_tracker_list_question_refactors(
    question_id: str,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List refactor history where a question is the source or replacement."""
    client = client_from_env()
    try:
        return client.list_question_refactors(
            question_id=question_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


def lab_tracker_create_note(
    project_id: str,
    raw_content: str,
    transcribed_text: str | None = None,
    targets: list[dict[str, str]] | None = None,
    metadata: dict[str, NoteMetadataScalar] | None = None,
    status: str | None = None,
) -> JsonObject:
    """Create a text note when the user asks to record source context."""
    client = client_from_env()
    try:
        return with_next_action(
            client.create_note(
                project_id=project_id,
                raw_content=raw_content,
                transcribed_text=transcribed_text,
                targets=targets,
                metadata=metadata,
                status=status,
            ),
            next_action(
                "lab_tracker_create_question",
                "If the note raises a durable research question, create or link it next.",
            ),
        )
    finally:
        client.close()


def lab_tracker_create_dataset(
    project_id: str,
    primary_question_id: str,
    secondary_question_ids: list[str] | None = None,
    commit_manifest: JsonObject | None = None,
    commit_hash: str | None = None,
    status: str | None = "staged",
) -> JsonObject:
    """Create a dataset before analyses, claims, and visualizations."""
    client = client_from_env()
    try:
        return with_next_action(
            client.create_dataset(
                project_id=project_id,
                primary_question_id=primary_question_id,
                secondary_question_ids=secondary_question_ids,
                commit_manifest=commit_manifest,
                commit_hash=commit_hash,
                status=status,
            ),
            next_action(
                "lab_tracker_create_analysis",
                "Record the analysis that uses this dataset before creating claims.",
            ),
        )
    finally:
        client.close()


def lab_tracker_create_analysis(
    project_id: str,
    dataset_ids: list[str],
    method_hash: str,
    code_version: str,
    environment_hash: str | None = None,
    status: str | None = "staged",
) -> JsonObject:
    """Create an analysis after datasets and before claims or figures."""
    client = client_from_env()
    try:
        return with_next_action(
            client.create_analysis(
                project_id=project_id,
                dataset_ids=dataset_ids,
                method_hash=method_hash,
                code_version=code_version,
                environment_hash=environment_hash,
                status=status,
            ),
            next_action(
                "lab_tracker_create_claim",
                "Summarize what this analysis supports or refutes as a claim.",
            ),
        )
    finally:
        client.close()


def lab_tracker_create_claim(
    project_id: str,
    statement: str,
    confidence: float,
    status: str | None = "proposed",
    supported_by_dataset_ids: list[str] | None = None,
    supported_by_analysis_ids: list[str] | None = None,
    answers_question_ids: list[str] | None = None,
    external_citations: list[JsonObject] | None = None,
) -> JsonObject:
    """Create a claim after linking supporting datasets or analyses."""
    client = client_from_env()
    try:
        return with_next_action(
            client.create_claim(
                project_id=project_id,
                statement=statement,
                confidence=confidence,
                status=status,
                supported_by_dataset_ids=supported_by_dataset_ids,
                supported_by_analysis_ids=supported_by_analysis_ids,
                answers_question_ids=answers_question_ids,
                external_citations=external_citations,
            ),
            next_action(
                "lab_tracker_create_visualization",
                "If a figure communicates this claim, register or upload it next.",
            ),
        )
    finally:
        client.close()


def lab_tracker_create_claim_edge(
    claim_id: str,
    target_claim_id: str,
    relation: str,
) -> JsonObject:
    """Create a typed claim-to-claim logic edge such as refutes or extends."""
    client = client_from_env()
    try:
        return client.create_claim_edge(
            claim_id=claim_id,
            target_claim_id=target_claim_id,
            relation=relation,
        )
    finally:
        client.close()


def lab_tracker_create_visualization(
    analysis_id: str,
    viz_type: str,
    file_path: str,
    caption: str | None = None,
    related_claim_ids: list[str] | None = None,
) -> JsonObject:
    """Register a visualization after its analysis and related claims exist."""
    client = client_from_env()
    try:
        return with_next_action(
            client.create_visualization(
                analysis_id=analysis_id,
                viz_type=viz_type,
                file_path=file_path,
                caption=caption,
                related_claim_ids=related_claim_ids,
            ),
            next_action(
                "lab_tracker_upload_visualization_file",
                "Upload the local figure asset if file_path is not already managed storage.",
            ),
        )
    finally:
        client.close()


def lab_tracker_create_goal(
    project_id: str,
    goal_type: str,
    title: str,
    summary: str | None = None,
    status: str | None = "planned",
    target_date: str | None = None,
    external_ref: str | None = None,
    attributes: JsonObject | None = None,
) -> JsonObject:
    """Create a goal/output before linking questions, datasets, or claims."""
    client = client_from_env()
    try:
        return with_next_action(
            client.create_goal(
                project_id=project_id,
                goal_type=goal_type,
                title=title,
                summary=summary,
                status=status,
                target_date=target_date,
                external_ref=external_ref,
                attributes=attributes,
            ),
            next_action(
                "lab_tracker_link_node_to_goal",
                "Link existing questions or evidence nodes that advance this goal.",
            ),
        )
    finally:
        client.close()


def lab_tracker_update_goal(
    goal_id: str,
    goal_type: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    status: str | None = None,
    target_date: str | None = None,
    external_ref: str | None = None,
    attributes: JsonObject | None = None,
) -> JsonObject:
    """Update a Lab Tracker goal/output."""
    client = client_from_env()
    try:
        return with_next_action(
            client.update_goal(
                goal_id=goal_id,
                goal_type=goal_type,
                title=title,
                summary=summary,
                status=status,
                target_date=target_date,
                external_ref=external_ref,
                attributes=attributes,
            ),
            next_action(
                "lab_tracker_next_questions",
                "After changing goal state, re-rank open questions on active goals.",
            ),
        )
    finally:
        client.close()


def lab_tracker_link_node_to_goal(
    goal_id: str,
    entity_type: str,
    entity_id: str,
    relation: str,
    link_status: str | None = "candidate",
    slot: str | None = None,
) -> JsonObject:
    """Tag an existing graph node in relation to a goal/output."""
    client = client_from_env()
    try:
        return with_next_action(
            client.link_node_to_goal(
                goal_id=goal_id,
                entity_type=entity_type,
                entity_id=entity_id,
                relation=relation,
                link_status=link_status,
                slot=slot,
            ),
            next_action(
                "lab_tracker_next_questions",
                "Use goal links to rank the next open questions.",
            ),
        )
    finally:
        client.close()


def lab_tracker_upload_visualization_file(
    viz_id: str,
    file_path: str,
    content_type: str | None = None,
) -> JsonObject:
    """Upload a local file into managed storage for a visualization node."""
    client = client_from_env()
    try:
        return with_next_action(
            client.upload_visualization_file(
                viz_id=viz_id,
                file_path=file_path,
                content_type=content_type,
            ),
            next_action(
                "lab_tracker_get_decision_context",
                "Reload context before using this visualization in writing or slides.",
            ),
        )
    finally:
        client.close()


def lab_tracker_record_evidence_bundle(
    project_id: str,
    primary_question_id: str | None = None,
    dataset: JsonObject | None = None,
    analysis: JsonObject | None = None,
    claim: JsonObject | None = None,
    visualization: JsonObject | None = None,
    source_note: JsonObject | None = None,
    dry_run: bool = True,
    idempotency_key: str | None = None,
) -> JsonObject:
    """Preview or record a dataset-analysis-claim-visualization evidence bundle.

    dry_run defaults to true. With dry_run=false, this tool still writes only
    through the existing strict create/upload API endpoints and returns created
    or reused stable IDs.
    """
    client = client_from_env()
    try:
        return with_next_action(
            record_evidence_bundle(
                client,
                project_id=project_id,
                primary_question_id=primary_question_id,
                dataset=dataset,
                analysis=analysis,
                claim=claim,
                visualization=visualization,
                source_note=source_note,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
            ),
            next_action(
                "lab_tracker_get_decision_context",
                "Reload decision context after recording the evidence bundle.",
            ),
        )
    finally:
        client.close()


WRITE_TOOLS = (
    lab_tracker_create_project,
    lab_tracker_create_question,
    lab_tracker_refactor_question,
    lab_tracker_list_question_refactors,
    lab_tracker_create_note,
    lab_tracker_create_dataset,
    lab_tracker_create_analysis,
    lab_tracker_create_claim,
    lab_tracker_create_claim_edge,
    lab_tracker_create_visualization,
    lab_tracker_create_goal,
    lab_tracker_update_goal,
    lab_tracker_link_node_to_goal,
    lab_tracker_upload_visualization_file,
    lab_tracker_record_evidence_bundle,
)


def register_write_tools(server: Any) -> None:
    for tool in WRITE_TOOLS:
        server.tool()(tool)
