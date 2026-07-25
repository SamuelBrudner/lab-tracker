"""Pure retry and starter-question policies for graph-draft generation."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from lab_tracker.errors import ValidationError
from lab_tracker.graph_drafting import GraphDraftingError
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeSet,
    GraphDraftMode,
    GraphDraftPurpose,
    GraphDraftSemanticType,
)

DEFAULT_GENERATION_LEASE_SECONDS = 5 * 60
STARTER_QUESTION_MAX_OPERATIONS = 12
STARTER_QUESTIONS_PROMPT_VERSION = "starter-questions-v1"
NOTE_DRAFT_IDEMPOTENCY_VERSION = "v1"


def starter_question_contract() -> dict[str, Any]:
    """Return the canonical trusted contract for every starter draft attempt."""

    return {
        "type": GraphDraftPurpose.STARTER_QUESTIONS.value,
        "entity_type": EntityType.QUESTION.value,
        "operation": GraphChangeOp.CREATE.value,
        "semantic_type": GraphDraftSemanticType.SUGGEST_NEW_QUESTION.value,
        "question_status": "staged",
        "max_operations": STARTER_QUESTION_MAX_OPERATIONS,
        "human_commit_required": True,
    }


def resolved_note_idempotency_key(
    *,
    purpose: GraphDraftPurpose,
    idempotency_key: str | None,
) -> str | None:
    cleaned = idempotency_key.strip() if idempotency_key else None
    if idempotency_key is not None and not cleaned:
        raise ValidationError("idempotency_key must not be empty.")
    if cleaned is not None and len(cleaned) > 200:
        raise ValidationError("idempotency_key must be 200 characters or fewer.")
    if cleaned is None and purpose == GraphDraftPurpose.STARTER_QUESTIONS:
        return f"starter-questions:{NOTE_DRAFT_IDEMPOTENCY_VERSION}"
    return cleaned


def note_draft_batch_key(
    *,
    note_id: UUID,
    mode: GraphDraftMode,
    purpose: GraphDraftPurpose,
    idempotency_key: str | None,
) -> str | None:
    if idempotency_key is None:
        return None
    payload = {
        "version": NOTE_DRAFT_IDEMPOTENCY_VERSION,
        "note_id": str(note_id),
        "mode": mode.value,
        "purpose": purpose.value,
        "idempotency_key": idempotency_key,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"note:{digest[:48]}"


def note_draft_request_fingerprint(
    *,
    note_id: UUID,
    mode: GraphDraftMode,
    purpose: GraphDraftPurpose,
    user_hint: str | None,
) -> str:
    payload = {
        "note_id": str(note_id),
        "mode": mode.value,
        "purpose": purpose.value,
        "user_hint": user_hint,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def ensure_matching_note_draft_request(
    change_set: GraphChangeSet,
    *,
    request_fingerprint: str,
) -> None:
    idempotency = (
        change_set.context_packet.get("idempotency")
        if change_set.context_packet
        else None
    )
    persisted_fingerprint = (
        idempotency.get("request_fingerprint")
        if isinstance(idempotency, dict)
        else None
    )
    if persisted_fingerprint != request_fingerprint:
        raise ValidationError(
            "idempotency_key was already used with conflicting graph-draft fields."
        )


def enforce_starter_question_contract(change_set: GraphChangeSet) -> None:
    operations = change_set.operations
    if not operations:
        raise GraphDraftingError(
            "Starter-question drafting must propose at least one staged question."
        )
    if len(operations) > STARTER_QUESTION_MAX_OPERATIONS:
        raise GraphDraftingError(
            "Starter-question drafting proposed too many questions; "
            f"the maximum is {STARTER_QUESTION_MAX_OPERATIONS}."
        )
    client_refs: set[str] = set()
    for operation in operations:
        if (
            operation.op != GraphChangeOp.CREATE
            or operation.entity_type != EntityType.QUESTION
            or operation.semantic_type
            != GraphDraftSemanticType.SUGGEST_NEW_QUESTION
            or operation.target_entity_id is not None
        ):
            raise GraphDraftingError(
                "Starter-question drafts may only create staged question operations."
            )
        if str(operation.payload.get("project_id") or "") != str(
            change_set.project_id
        ):
            raise GraphDraftingError(
                "Starter-question operations must target the source project."
            )
        status = str(operation.payload.get("status") or "staged")
        if status != "staged":
            raise GraphDraftingError(
                "Starter-question operations must use staged question status."
            )
        operation.payload["status"] = "staged"
        if operation.client_ref:
            if operation.client_ref in client_refs:
                raise GraphDraftingError(
                    "Starter-question operation client_ref values must be unique."
                )
            client_refs.add(operation.client_ref)


def finish_generation_lease(change_set: GraphChangeSet) -> None:
    change_set.context_packet["generation_lease_expires_at"] = None
    change_set.context_packet["generation_finished_at"] = (
        change_set.updated_at.isoformat()
    )
