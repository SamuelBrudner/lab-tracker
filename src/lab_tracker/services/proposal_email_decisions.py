"""Human decisions on individual proposals delivered to a verified mailbox."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from lab_tracker.auth import AuthContext, PrincipalType
from lab_tracker.errors import ConflictError, PermissionDeniedError, ValidationError
from lab_tracker.models import (
    DEFERRED_AT_KEY,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    GraphDraftPurpose,
)

if TYPE_CHECKING:
    from lab_tracker.services.graph_draft_review import GraphDraftReviewCoordinator


ReplyOutcome = Literal["approved", "corrected", "correction_requested"]
MAX_REPLY_LENGTH = 4000

# Identity, references, paths, status and other structured fields need their
# ordinary typed editors. Any proposal can still receive a free-text correction.
_PROSE_FIELDS = frozenset(
    {
        "caption",
        "choice",
        "description",
        "failure_mode",
        "falsification_criteria",
        "hypothesis",
        "lesson",
        "name",
        "rationale",
        "raw_content",
        "refuting_outcome",
        "statement",
        "summary",
        "terminal_reason",
        "text",
        "title",
        "tooling_context",
        "transcribed_text",
        "trigger",
        "verification_plan",
    }
)
_CORRECTION = re.compile(
    r"correct(?:[ \t]+([a-z][a-z0-9_]*))?[ \t]*:[ \t]*(.+)", re.IGNORECASE | re.ASCII
)


@dataclass(frozen=True)
class EmailReviewCommand:
    action: Literal["approve", "correct"]
    field: str | None = None
    text: str = ""


def reply_line(text: str) -> str:
    """Require one line of author-written reply text."""
    line = text.strip()
    if (
        not line
        or len(line) > MAX_REPLY_LENGTH
        or len(line.splitlines()) != 1
        or any(ord(char) < 32 and char != "\t" for char in line)
    ):
        raise ValidationError("Reply with one line of up to 4000 characters.")
    return line


def parse_review_reply(text: str) -> EmailReviewCommand:
    """Parse only the scientist's new, unquoted text supplied by the mail adapter."""
    line = reply_line(text)
    if line.lower() == "approve":
        return EmailReviewCommand("approve")
    match = _CORRECTION.fullmatch(line)
    if match:
        field, replacement = match.groups()
        return EmailReviewCommand("correct", field.lower() if field else None, replacement)
    raise ValidationError(
        "Reply with Approve, Correct <field>: <replacement>, or Correct: <feedback>."
    )


def correctable_fields(operation: GraphChangeOperation) -> tuple[str, ...]:
    return tuple(
        sorted(field for field in _PROSE_FIELDS if isinstance(operation.payload.get(field), str))
    )


def operation_fingerprint(operation: GraphChangeOperation) -> str:
    """Bind an invitation to the exact proposal, including subsequent review edits."""
    snapshot = operation.model_dump(mode="json", exclude={"deferred_at", "reject_reason"})
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def require_assigned_email_review(
    review: GraphDraftReviewCoordinator, draft: GraphChangeSet, actor: AuthContext
) -> None:
    if actor.principal_type != PrincipalType.USER:
        raise PermissionDeniedError("Email review requires the verified mailbox owner.")
    if draft.review_assignee_user_id != actor.user_id:
        raise PermissionDeniedError("This draft is no longer assigned to this reviewer.")
    review.authorization.require_contributor(draft.project_id, actor=actor)
    if (
        draft.draft_mode != GraphDraftMode.GRAPH_BATCH
        or draft.purpose != GraphDraftPurpose.GENERAL
        or draft.status != GraphChangeSetStatus.READY
        or draft.review_metadata.get("review_triage_carryover_to")
    ):
        raise ConflictError("This draft is no longer awaiting email review.")


def require_current_proposal(
    review: GraphDraftReviewCoordinator,
    draft: GraphChangeSet,
    operation_id: UUID,
    expected_revision: str,
    actor: AuthContext,
) -> GraphChangeOperation:
    """Authorize delivery or review of this precise version to the assigned scientist."""
    require_assigned_email_review(review, draft, actor)
    operation = review._find_graph_operation(draft, operation_id)
    if (
        operation.status != GraphChangeOperationStatus.PROPOSED
        or DEFERRED_AT_KEY in operation.error_metadata
        or operation_fingerprint(operation) != expected_revision
    ):
        raise ConflictError("This proposal has changed since the email was sent.")
    return operation


def apply_proposal_email_decision(
    review: GraphDraftReviewCoordinator,
    *,
    change_set_id: UUID,
    operation_id: UUID,
    expected_revision: str,
    actor: AuthContext,
    command: EmailReviewCommand,
) -> ReplyOutcome:
    """Apply one authenticated reply inside the caller's delivery-consumption transaction."""
    with review.application_transaction():
        draft = review.records.get_graph_change_set_for_update(change_set_id)
        operation = require_current_proposal(review, draft, operation_id, expected_revision, actor)
        if command.action == "approve":
            result = review.update_graph_change_operation(
                change_set_id, operation_id, status=GraphChangeOperationStatus.ACCEPTED, actor=actor
            )
            updated = review._find_graph_operation(result, operation_id)
            if updated.status != GraphChangeOperationStatus.ACCEPTED:
                raise ValidationError("This proposal needs correction before approval.")
            return "approved"
        if command.action != "correct" or not command.text.strip():
            raise ValidationError("Supply a correction.")
        if command.field is None:
            review.update_graph_change_operation(
                change_set_id, operation_id, review_note=command.text, deferred=True, actor=actor
            )
            return "correction_requested"
        if command.field not in correctable_fields(operation):
            raise ValidationError(
                "Choose a listed proposal field or reply with Correct: <feedback>."
            )
        if command.text == operation.payload[command.field]:
            raise ValidationError("The correction matches the current proposal.")
        payload = {**operation.payload, command.field: command.text}
        # The ordinary editor permits saving invalid work-in-progress. A mailed
        # correction acknowledges a successful edit, so validate before saving.
        review.patch_validator.validate_operation(operation, payload)
        review.update_graph_change_operation(
            change_set_id, operation_id, payload=payload, actor=actor
        )
        return "corrected"
