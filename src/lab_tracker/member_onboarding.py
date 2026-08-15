"""Trusted conventions shared by member-onboarding orchestration and note capture."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Final
from uuid import UUID

from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeSet,
    GraphDraftSemanticType,
    Note,
    Question,
    QuestionStatus,
)

CHECKPOINT_ROLE_KEY: Final = "member_onboarding_role"
CHECKPOINT_ROLE: Final = "checkpoint"
ONBOARDING_METADATA_PREFIX: Final = "member_onboarding_"
CHECKPOINT_CLIENT_KEY_PREFIX: Final = "member-checkpoint:"
SCHEDULED_DRAFT_POLICY_KEY: Final = "scheduled_graph_draft_policy"
SCHEDULED_DRAFT_EXCLUDE: Final = "exclude"

CHECKPOINT_SCHEMA_VERSION_KEY: Final = "member_onboarding_schema_version"
CHECKPOINT_AS_OF_KEY: Final = "member_onboarding_as_of"
CHECKPOINT_COVERAGE_KEY: Final = "member_onboarding_historical_coverage"
CHECKPOINT_PAYLOAD_HASH_KEY: Final = "member_onboarding_payload_sha256"
CHECKPOINT_INPUT_HASH_KEY: Final = "member_onboarding_input_sha256"
CHECKPOINT_CURRENT_OUTPUT_KEY: Final = (
    "member_onboarding_current_output_or_decision"
)
CHECKPOINT_LIVE_QUESTIONS_KEY: Final = "member_onboarding_live_questions_json"
CHECKPOINT_RECENT_CONTEXT_KEY: Final = (
    "member_onboarding_strongest_recent_context"
)
CHECKPOINT_NEXT_MOVE_KEY: Final = "member_onboarding_next_move"
CHECKPOINT_SOURCE_HASH_KEY: Final = "member_onboarding_source_text_sha256"
ALIGNMENT_MODE_KEY: Final = "member_onboarding_alignment_mode"
ALIGNMENT_PAYLOAD_HASH_KEY: Final = "member_onboarding_alignment_payload_sha256"
ALIGNMENT_RESOLVED_AT_KEY: Final = "member_onboarding_alignment_resolved_at"
ALIGNMENT_RESOLUTIONS_KEY: Final = "member_onboarding_question_resolutions_json"
ALIGNMENT_CHANGE_SET_ID_KEY: Final = "member_onboarding_alignment_change_set_id"
ALIGNMENT_RESOLUTION_KEY: Final = "member_onboarding_alignment_resolution"
FIRST_CAPTURE_NOTE_ID_KEY: Final = "member_onboarding_first_capture_note_id"
FIRST_CAPTURE_AT_KEY: Final = "member_onboarding_first_capture_at"
COMPLETED_AT_KEY: Final = "member_onboarding_completed_at"

CHECKPOINT_SCHEMA_VERSION: Final = "1"
CHECKPOINT_COVERAGE: Final = "selective"
CHECKPOINT_CONTENT_LIMIT: Final = 64_000
CHECKPOINT_PROMPT_VERSION: Final = "member-checkpoint-alignment-v1"
_LIVE_QUESTION_REF = re.compile(r"^live_question_([0-2])$")


def checkpoint_client_capture_id(actor_user_id: UUID) -> str:
    return f"member-checkpoint:v1:{actor_user_id}"


def is_member_checkpoint(note: Note) -> bool:
    return note.metadata.get(CHECKPOINT_ROLE_KEY) == CHECKPOINT_ROLE


def has_reserved_note_metadata(metadata: dict[str, object] | None) -> bool:
    for key in metadata or {}:
        normalized = str(key).strip()
        if normalized.startswith(ONBOARDING_METADATA_PREFIX):
            return True
        if normalized == SCHEDULED_DRAFT_POLICY_KEY:
            return True
    return False


def has_reserved_capture_key(client_capture_id: str | None) -> bool:
    return bool(
        client_capture_id
        and client_capture_id.strip().startswith(CHECKPOINT_CLIENT_KEY_PREFIX)
    )


def validate_member_alignment_operations(
    change_set: GraphChangeSet,
    checkpoint: Note,
    *,
    get_question: Callable[[UUID], Question],
) -> None:
    """Enforce the complete question-only onboarding draft contract."""

    if not is_member_checkpoint(checkpoint):
        raise ValidationError("Onboarding alignment source must be a member checkpoint.")
    if checkpoint.metadata.get(ALIGNMENT_MODE_KEY) == "manual":
        raise ValidationError(
            "An AI onboarding draft cannot modify a manually aligned checkpoint."
        )
    if change_set.source_note_id != checkpoint.note_id:
        raise ValidationError("Onboarding alignment must target its source checkpoint.")
    try:
        live_questions = json.loads(checkpoint.metadata[CHECKPOINT_LIVE_QUESTIONS_KEY])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("Member checkpoint live questions are invalid.") from exc
    if not isinstance(live_questions, list) or not 1 <= len(live_questions) <= 3:
        raise ValidationError("Member checkpoint must contain one to three live questions.")
    if len(change_set.operations) != len(live_questions):
        raise ValidationError(
            "Onboarding alignment requires exactly one proposal per live question."
        )
    seen_indexes: set[int] = set()
    for operation in change_set.operations:
        match = _LIVE_QUESTION_REF.fullmatch(operation.client_ref or "")
        if match is None:
            raise ValidationError(
                "Onboarding operations require client_ref live_question_<index>."
            )
        index = int(match.group(1))
        if index >= len(live_questions) or index in seen_indexes:
            raise ValidationError(
                "Onboarding operations must map uniquely to every live question."
            )
        seen_indexes.add(index)
        source_note_ids = {
            UUID(str(note_id))
            for source_ref in operation.source_refs
            for note_id in source_ref.get("source_note_ids", [])
        }
        if source_note_ids != {checkpoint.note_id}:
            raise ValidationError(
                "Onboarding operations must cite only the checkpoint source note."
            )
        if (
            operation.op == GraphChangeOp.CREATE
            and operation.entity_type == EntityType.QUESTION
            and operation.semantic_type == GraphDraftSemanticType.SUGGEST_NEW_QUESTION
        ):
            if set(operation.payload) != {
                "project_id",
                "text",
                "question_type",
                "status",
            }:
                raise ValidationError(
                    "Onboarding question proposals may only set project_id, text, "
                    "question_type, and status."
                )
            if str(operation.payload.get("project_id")) != str(checkpoint.project_id):
                raise ValidationError("Onboarding questions must stay in the checkpoint project.")
            if operation.payload.get("question_type") != "other":
                raise ValidationError("Onboarding questions must use question_type other.")
            if operation.payload.get("status") != "staged":
                raise ValidationError("Onboarding questions must remain staged.")
            if not str(operation.payload.get("text") or "").strip():
                raise ValidationError("Onboarding question text must not be empty.")
            continue
        if (
            operation.op == GraphChangeOp.UPDATE
            and operation.entity_type == EntityType.NOTE
            and operation.semantic_type == GraphDraftSemanticType.LINK_NOTE_TO_QUESTION
            and operation.target_entity_id == checkpoint.note_id
        ):
            if set(operation.payload) != {"targets"}:
                raise ValidationError("Onboarding note links may only add one question target.")
            targets = operation.payload.get("targets")
            if not isinstance(targets, list) or len(targets) != 1:
                raise ValidationError("Onboarding note links require one question target.")
            target = targets[0]
            if not isinstance(target, dict) or target.get("entity_type") != "question":
                raise ValidationError("Onboarding note links require a question target.")
            try:
                question = get_question(UUID(str(target.get("entity_id"))))
            except (TypeError, ValueError) as exc:
                raise ValidationError("Onboarding question target is invalid.") from exc
            if question.project_id != checkpoint.project_id:
                raise ValidationError("Onboarding question target must share the project.")
            if question.status not in {QuestionStatus.ACTIVE, QuestionStatus.STAGED}:
                raise ValidationError(
                    "Onboarding may link only active or staged questions."
                )
            continue
        raise ValidationError(
            "Onboarding alignment may only create staged questions or add question links."
        )
    if seen_indexes != set(range(len(live_questions))):
        raise ValidationError(
            "Onboarding alignment requires one proposal per live question."
        )
