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


def lab_tracker_create_project(
    name: str,
    description: str | None = None,
    status: str | None = None,
) -> JsonObject:
    """Create a Lab Tracker project through the API."""
    client = client_from_env()
    try:
        return client.create_project(name=name, description=description, status=status)
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
    """Create a Lab Tracker question through the API."""
    client = client_from_env()
    try:
        return client.create_question(
            project_id=project_id,
            text=text,
            question_type=question_type,
            hypothesis=hypothesis,
            status=status,
            parent_question_ids=parent_question_ids,
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
    """Create a text note, optionally targeting graph entities."""
    client = client_from_env()
    try:
        return client.create_note(
            project_id=project_id,
            raw_content=raw_content,
            transcribed_text=transcribed_text,
            targets=targets,
            metadata=metadata,
            status=status,
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
    """Create a Lab Tracker dataset through the API."""
    client = client_from_env()
    try:
        return client.create_dataset(
            project_id=project_id,
            primary_question_id=primary_question_id,
            secondary_question_ids=secondary_question_ids,
            commit_manifest=commit_manifest,
            commit_hash=commit_hash,
            status=status,
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
    """Create a Lab Tracker analysis through the API."""
    client = client_from_env()
    try:
        return client.create_analysis(
            project_id=project_id,
            dataset_ids=dataset_ids,
            method_hash=method_hash,
            code_version=code_version,
            environment_hash=environment_hash,
            status=status,
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
) -> JsonObject:
    """Create a Lab Tracker claim through the API."""
    client = client_from_env()
    try:
        return client.create_claim(
            project_id=project_id,
            statement=statement,
            confidence=confidence,
            status=status,
            supported_by_dataset_ids=supported_by_dataset_ids,
            supported_by_analysis_ids=supported_by_analysis_ids,
            answers_question_ids=answers_question_ids,
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
    """Create a Lab Tracker visualization through the API."""
    client = client_from_env()
    try:
        return client.create_visualization(
            analysis_id=analysis_id,
            viz_type=viz_type,
            file_path=file_path,
            caption=caption,
            related_claim_ids=related_claim_ids,
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
        return client.upload_visualization_file(
            viz_id=viz_id,
            file_path=file_path,
            content_type=content_type,
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
    lab_tracker_create_visualization,
    lab_tracker_upload_visualization_file,
)


def register_write_tools(server: Any) -> None:
    for tool in WRITE_TOOLS:
        server.tool()(tool)
