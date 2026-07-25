"""Mutating MCP tool functions and registration."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.types import ToolAnnotations

from lab_tracker.mcp_api_client import (
    QUESTION_STATUS_TEXT,
    QUESTION_STATUS_VALUES,
    JsonObject,
    LabTrackerAPIError,
    LabTrackerAPIUnavailableError,
    NoteMetadataScalar,
    client_from_env,
    lab_tracker_api_error,
    lab_tracker_unavailable,
)
from lab_tracker.mcp_evidence_bundle import record_evidence_bundle
from lab_tracker.mcp_tools.hints import next_action, with_next_action


def _write_tool(
    tool_name: str,
    call: Any,
    *,
    hint: JsonObject | None = None,
) -> JsonObject:
    client = None
    try:
        client = client_from_env()
        payload = call(client)
        return with_next_action(payload, hint) if hint is not None else payload
    except (LabTrackerAPIUnavailableError, httpx.HTTPError) as exc:
        return lab_tracker_unavailable(tool_name, detail=str(exc))
    except LabTrackerAPIError as exc:
        return lab_tracker_api_error(tool_name, exc)
    finally:
        if client is not None:
            client.close()


def _tool_title(tool: Any) -> str:
    name = str(getattr(tool, "__name__", "lab_tracker_tool"))
    return "Lab Tracker " + name.removeprefix("lab_tracker_").replace("_", " ").title()


def _write_tool_annotations(tool: Any) -> ToolAnnotations:
    name = str(getattr(tool, "__name__", ""))
    return ToolAnnotations(
        title=_tool_title(tool),
        readOnlyHint=False,
        destructiveHint=name
        in {
            "lab_tracker_refactor_question",
            "lab_tracker_update_experiment",
            "lab_tracker_update_goal",
        },
        openWorldHint=True,
    )


def lab_tracker_create_project(
    name: str,
    description: str | None = None,
    status: str | None = None,
) -> JsonObject:
    """Create a project only when the user explicitly asks for a new scope."""
    return _write_tool(
        "lab_tracker_create_project",
        lambda client: client.create_project(
            name=name,
            description=description,
            status=status,
        ),
        hint=next_action(
            "lab_tracker_list_goals",
            "Select or (if the user asks) create the goal/output this project should advance.",
        ),
    )


def lab_tracker_create_question(
    project_id: str,
    text: str,
    question_type: str = "other",
    hypothesis: str | None = None,
    status: str | None = None,
    parent_question_ids: list[str] | None = None,
) -> JsonObject:
    """Create a question after project/goal scope is known."""
    return _write_tool(
        "lab_tracker_create_question",
        lambda client: client.create_question(
            project_id=project_id,
            text=text,
            question_type=question_type,
            hypothesis=hypothesis,
            status=status,
            parent_question_ids=parent_question_ids,
        ),
        hint=next_action(
            "lab_tracker_link_node_to_goal",
            "Link important questions to the goal they advance.",
        ),
    )


def lab_tracker_create_experiment(
    project_id: str,
    name: str,
    primary_question_id: str,
    description: str | None = None,
) -> JsonObject:
    """Create a lightweight Experiment grouping only when the user explicitly asks."""
    return _write_tool(
        "lab_tracker_create_experiment",
        lambda client: client.create_experiment(
            project_id=project_id,
            name=name,
            primary_question_id=primary_question_id,
            description=description,
        ),
        hint=next_action(
            "lab_tracker_get_experiment",
            "Read the created Experiment before linking Sessions or Datasets.",
        ),
    )


def lab_tracker_update_experiment(
    experiment_id: str,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> JsonObject:
    """Update or advance an Experiment lifecycle only when the user explicitly asks."""
    return _write_tool(
        "lab_tracker_update_experiment",
        lambda client: client.update_experiment(
            experiment_id=experiment_id,
            name=name,
            description=description,
            status=status,
        ),
        hint=next_action(
            "lab_tracker_get_experiment",
            "Read back the Experiment and its lifecycle state.",
        ),
    )


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
    """Supersede a question with a replacement and optional child/note moves.

    Destructive: this supersedes a canonical question and re-points its children/notes.
    Only call it when the user explicitly asks; confirm before superseding.
    """
    return _write_tool(
        "lab_tracker_refactor_question",
        lambda client: _refactor_question(
            client,
            question_id=question_id,
            replacement_text=replacement_text,
            replacement_question_type=replacement_question_type,
            replacement_status=replacement_status,
            reason=reason,
            replacement_hypothesis=replacement_hypothesis,
            replacement_parent_question_ids=replacement_parent_question_ids,
            child_question_ids_to_reparent=child_question_ids_to_reparent,
            note_ids_to_retarget=note_ids_to_retarget,
        ),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "Reload context for the replacement question before follow-up writes.",
        ),
    )


def _refactor_question(
    client: Any,
    *,
    question_id: str,
    replacement_text: str,
    replacement_question_type: str,
    replacement_status: str,
    reason: str,
    replacement_hypothesis: str | None,
    replacement_parent_question_ids: list[str] | None,
    child_question_ids_to_reparent: list[str] | None,
    note_ids_to_retarget: list[str] | None,
) -> JsonObject:
    if replacement_status not in QUESTION_STATUS_VALUES:
        raise LabTrackerAPIError(
            f"Invalid replacement status {replacement_status!r}. "
            f"Allowed statuses: {QUESTION_STATUS_TEXT}.",
        )
    return client.refactor_question(
        question_id=question_id,
        replacement_text=replacement_text,
        replacement_question_type=replacement_question_type,
        replacement_status=replacement_status,
        replacement_hypothesis=replacement_hypothesis,
        replacement_parent_question_ids=replacement_parent_question_ids,
        reason=reason,
        child_question_ids_to_reparent=child_question_ids_to_reparent,
        note_ids_to_retarget=note_ids_to_retarget,
    )


def lab_tracker_create_note(
    project_id: str,
    raw_content: str,
    transcribed_text: str | None = None,
    targets: list[dict[str, str]] | None = None,
    metadata: dict[str, NoteMetadataScalar] | None = None,
    status: str | None = None,
) -> JsonObject:
    """Create a text note when the user asks to record source context."""
    return _write_tool(
        "lab_tracker_create_note",
        lambda client: client.create_note(
            project_id=project_id,
            raw_content=raw_content,
            transcribed_text=transcribed_text,
            targets=targets,
            metadata=metadata,
            status=status,
        ),
        hint=next_action(
            "lab_tracker_create_question",
            "If the note raises a durable research question, create or link it next.",
        ),
    )


def lab_tracker_create_dataset(
    project_id: str,
    primary_question_id: str,
    secondary_question_ids: list[str] | None = None,
    commit_manifest: JsonObject | None = None,
    commit_hash: str | None = None,
    status: str | None = "staged",
) -> JsonObject:
    """Create a dataset before analyses, claims, and visualizations.

    Only when the user asks. This commits a canonical record immediately; the staged
    status is a lifecycle state, not a review gate that a human still has to accept.
    """
    return _write_tool(
        "lab_tracker_create_dataset",
        lambda client: client.create_dataset(
            project_id=project_id,
            primary_question_id=primary_question_id,
            secondary_question_ids=secondary_question_ids,
            commit_manifest=commit_manifest,
            commit_hash=commit_hash,
            status=status,
        ),
        hint=next_action(
            "lab_tracker_create_analysis",
            "Record the analysis that uses this dataset before creating claims.",
        ),
    )


def lab_tracker_create_analysis(
    project_id: str,
    dataset_ids: list[str],
    method_hash: str,
    code_version: str,
    environment_hash: str | None = None,
    status: str | None = "staged",
) -> JsonObject:
    """Create an analysis after datasets and before claims or figures.

    Only when the user asks. This commits a canonical record immediately; the staged
    status is a lifecycle state, not a review gate.
    """
    return _write_tool(
        "lab_tracker_create_analysis",
        lambda client: client.create_analysis(
            project_id=project_id,
            dataset_ids=dataset_ids,
            method_hash=method_hash,
            code_version=code_version,
            environment_hash=environment_hash,
            status=status,
        ),
        hint=next_action(
            "lab_tracker_create_claim",
            "Summarize what this analysis supports or refutes as a claim.",
        ),
    )


def lab_tracker_create_claim(
    project_id: str,
    statement: str,
    confidence: float,
    status: str | None = "proposed",
    falsification_criteria: str | None = None,
    verification_plan: str | None = None,
    refuting_outcome: str | None = None,
    supported_by_dataset_ids: list[str] | None = None,
    supported_by_analysis_ids: list[str] | None = None,
    answers_question_ids: list[str] | None = None,
    external_citations: list[JsonObject] | None = None,
) -> JsonObject:
    """Create a claim after linking supporting datasets or analyses.

    Only when the user asks. This commits a canonical record immediately; the proposed
    status is a lifecycle state, not a review gate.
    """
    return _write_tool(
        "lab_tracker_create_claim",
        lambda client: client.create_claim(
            project_id=project_id,
            statement=statement,
            confidence=confidence,
            status=status,
            falsification_criteria=falsification_criteria,
            verification_plan=verification_plan,
            refuting_outcome=refuting_outcome,
            supported_by_dataset_ids=supported_by_dataset_ids,
            supported_by_analysis_ids=supported_by_analysis_ids,
            answers_question_ids=answers_question_ids,
            external_citations=external_citations,
        ),
        hint=next_action(
            "lab_tracker_create_visualization",
            "If a figure communicates this claim, register or upload it next.",
        ),
    )


def lab_tracker_create_claim_edge(
    claim_id: str,
    target_claim_id: str,
    relation: str,
) -> JsonObject:
    """Create a typed claim-to-claim logic edge such as refutes or extends.

    Create only when the user states the relationship; refutes/contradicts in particular
    is a durable, strong epistemic claim in the graph.
    """
    return _write_tool(
        "lab_tracker_create_claim_edge",
        lambda client: client.create_claim_edge(
            claim_id=claim_id,
            target_claim_id=target_claim_id,
            relation=relation,
        ),
    )


def lab_tracker_create_visualization(
    analysis_id: str,
    viz_type: str,
    file_path: str,
    caption: str | None = None,
    related_claim_ids: list[str] | None = None,
) -> JsonObject:
    """Register a visualization after its analysis and related claims exist.

    Only when the user asks; this commits a canonical record immediately.
    """
    return _write_tool(
        "lab_tracker_create_visualization",
        lambda client: client.create_visualization(
            analysis_id=analysis_id,
            viz_type=viz_type,
            file_path=file_path,
            caption=caption,
            related_claim_ids=related_claim_ids,
        ),
        hint=next_action(
            "lab_tracker_upload_visualization_file",
            "Upload the local figure asset if file_path is not already managed storage.",
        ),
    )


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
    """Create a goal/output before linking questions, datasets, or claims.

    Only when the user asks; this commits a canonical record immediately.
    """
    return _write_tool(
        "lab_tracker_create_goal",
        lambda client: client.create_goal(
            project_id=project_id,
            goal_type=goal_type,
            title=title,
            summary=summary,
            status=status,
            target_date=target_date,
            external_ref=external_ref,
            attributes=attributes,
        ),
        hint=next_action(
            "lab_tracker_link_node_to_goal",
            "Link existing questions or evidence nodes that advance this goal.",
        ),
    )


def lab_tracker_update_goal(
    goal_id: str,
    goal_type: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    status: str | None = None,
    target_date: str | None = None,
    external_ref: str | None = None,
    attributes: JsonObject | None = None,
    clear_target_date: bool = False,
    clear_external_ref: bool = False,
) -> JsonObject:
    """Update a Lab Tracker goal/output.

    Destructive: overwrites the canonical goal's fields in place. Only call it when the
    user explicitly asks; confirm before overwriting. Set a clear flag to remove the
    corresponding nullable value; omitted values are left unchanged.
    """
    return _write_tool(
        "lab_tracker_update_goal",
        lambda client: client.update_goal(
            goal_id=goal_id,
            goal_type=goal_type,
            title=title,
            summary=summary,
            status=status,
            target_date=target_date,
            external_ref=external_ref,
            attributes=attributes,
            clear_target_date=clear_target_date,
            clear_external_ref=clear_external_ref,
        ),
        hint=next_action(
            "lab_tracker_next_questions",
            "After changing goal state, re-rank open questions on active goals.",
        ),
    )


def lab_tracker_link_node_to_goal(
    goal_id: str,
    entity_type: str,
    entity_id: str,
    relation: str,
    link_status: str | None = "candidate",
    slot: str | None = None,
) -> JsonObject:
    """Tag an existing graph node in relation to a goal/output."""
    return _write_tool(
        "lab_tracker_link_node_to_goal",
        lambda client: client.link_node_to_goal(
            goal_id=goal_id,
            entity_type=entity_type,
            entity_id=entity_id,
            relation=relation,
            link_status=link_status,
            slot=slot,
        ),
        hint=next_action(
            "lab_tracker_next_questions",
            "Use goal links to rank the next open questions.",
        ),
    )


def lab_tracker_upload_visualization_file(
    viz_id: str,
    file_path: str,
    content_type: str | None = None,
) -> JsonObject:
    """Upload a local file into managed storage for a visualization node."""
    return _write_tool(
        "lab_tracker_upload_visualization_file",
        lambda client: client.upload_visualization_file(
            viz_id=viz_id,
            file_path=file_path,
            content_type=content_type,
        ),
        hint=next_action(
            "lab_tracker_get_decision_context",
            "Reload context before using this visualization in writing or slides.",
        ),
    )


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
    """Preview or atomically record an evidence bundle; defaults to dry-run.

    dry_run defaults to true and only previews. Pass dry_run=false to commit ONLY when
    the user has explicitly asked you to record the bundle — the created dataset, analysis,
    claim, visualization, and source note are canonical graph records, not proposals for
    later review. A non-blank idempotency_key is required whenever dry_run=false; an
    identical replay reuses the first result and conflicting key reuse is rejected.

    Component inputs remain flat MCP objects. Supply dataset_id, analysis_id, claim_id,
    viz_id/visualization_id, or note_id to reuse an existing component; otherwise supply
    that component's create fields. With dry_run=false the graph records are committed
    through one atomic bundle endpoint. A requested local visualization upload is a
    client-side follow-up after that commit; an attachment failure is reported explicitly
    and does not roll the graph records back.
    """
    return _write_tool(
        "lab_tracker_record_evidence_bundle",
        lambda client: record_evidence_bundle(
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
        hint=next_action(
            "lab_tracker_get_decision_context",
            "Reload decision context after recording the evidence bundle.",
        ),
    )


WRITE_TOOLS = (
    lab_tracker_create_project,
    lab_tracker_create_question,
    lab_tracker_create_experiment,
    lab_tracker_update_experiment,
    lab_tracker_refactor_question,
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
        title = _tool_title(tool)
        server.tool(title=title, annotations=_write_tool_annotations(tool))(tool)
