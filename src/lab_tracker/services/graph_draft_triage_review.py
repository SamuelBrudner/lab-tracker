"""Atomic triage commands owned by the graph-draft review coordinator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.graph_draft_triage import (
    TRIAGE_CLARIFICATIONS_KEY,
    clarification_id,
    review_triage,
)
from lab_tracker.models import (
    AcceptanceMode,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphDraftPurpose,
    GraphOperationRejectReason,
    utc_now,
)
from lab_tracker.patching import NOT_PROVIDED
from lab_tracker.services.shared import actor_user_id

if TYPE_CHECKING:
    from lab_tracker.services.graph_draft_review import GraphDraftReviewCoordinator


def decide_graph_draft_group(
    review: GraphDraftReviewCoordinator,
    change_set_id: UUID,
    *,
    operation_ids: list[UUID],
    decision: str,
    expected_revision: str | None = None,
    reject_reason: GraphOperationRejectReason | None = None,
    edits: dict[UUID, dict[str, Any]] | None = None,
    actor: AuthContext | None = None,
) -> GraphChangeSet:
    """One explicit, atomic verdict on one current triage group."""
    with review.application_transaction():
        change_set = review.records.get_graph_change_set_for_update(change_set_id)
        review.authorization.require_interactive(actor, action="Reviewing a triage decision")
        review._ensure_graph_change_set_editable(change_set, actor=actor)
        if change_set.purpose == GraphDraftPurpose.MEMBER_CHECKPOINT_ALIGNMENT:
            raise ValidationError("Member onboarding proposals require individual review.")
        if decision not in {"accepted", "rejected", "deferred", "resume"}:
            raise ValidationError("Unknown triage decision.")
        selected = {str(operation_id) for operation_id in operation_ids}
        if not selected or len(selected) != len(operation_ids):
            raise ValidationError("Select unique operations from one triage group.")
        field = "deferred_operation_ids" if decision == "resume" else "pending_operation_ids"
        group = next(
            (
                group
                for group in review_triage(change_set)["groups"]
                if set(group[field]) == selected
            ),
            None,
        )
        if group is None:
            raise ValidationError("This triage group changed. Reload the review before deciding.")
        if expected_revision is not None and group["revision"] != expected_revision:
            raise ValidationError("This decision was edited elsewhere. Reload before deciding.")
        if decision == "rejected" and reject_reason is None:
            raise ValidationError("Choose a reason for rejecting this decision.")
        if decision != "rejected" and reject_reason is not None:
            raise ValidationError("A rejection reason requires a rejected decision.")
        now = utc_now().isoformat()
        change_set.review_metadata = {
            **change_set.review_metadata,
            "review_triage_used": True,
            "first_triage_decision_at": change_set.review_metadata.get("first_triage_decision_at")
            or now,
            "last_triage_decision_at": now,
        }
        review.records.save_graph_change_set(change_set)
        edits = edits or {}
        if any(str(operation_id) not in selected for operation_id in edits):
            raise ValidationError("Edits must belong to the selected triage group.")
        for edit in edits.values():
            if set(edit) - {"payload", "review_note"}:
                raise ValidationError("Group edits may only change payload and review_note.")
        if decision == "accepted":
            targets_by_note: dict[str, list[dict[str, Any]]] = {}
            for operation in change_set.operations:
                if str(operation.operation_id) not in selected:
                    continue
                payload = edits.get(operation.operation_id, {}).get("payload", operation.payload)
                if (
                    operation.entity_type.value == "note"
                    and operation.target_entity_id
                    and "targets" in payload
                ):
                    key = str(operation.target_entity_id)
                    if key in targets_by_note and targets_by_note[key] != payload["targets"]:
                        raise ValidationError(
                            "These capture edits replace links with different target lists. "
                            "Correct them to the same combined targets before keeping the group."
                        )
                    targets_by_note[key] = payload["targets"]
        for operation_id in operation_ids:
            edit = edits.get(operation_id, {})
            change_set = review._update_graph_change_operation(
                change_set_id,
                operation_id,
                payload=edit.get("payload", NOT_PROVIDED),
                review_note=edit.get("review_note", NOT_PROVIDED),
                status=(
                    GraphChangeOperationStatus(decision)
                    if decision in {"accepted", "rejected"}
                    else NOT_PROVIDED
                ),
                deferred=(
                    decision == "deferred" if decision in {"deferred", "resume"} else NOT_PROVIDED
                ),
                reject_reason=reject_reason if decision == "rejected" else NOT_PROVIDED,
                acceptance_mode=AcceptanceMode.BULK_ACCEPTED,
                actor=actor,
            )
            operation = review._find_graph_operation(change_set, operation_id)
            if decision == "accepted" and operation.status != GraphChangeOperationStatus.ACCEPTED:
                raise ValidationError(
                    operation.error_metadata.get("message")
                    or "This group needs correction before acceptance."
                )
        return change_set


def decide_graph_draft_clarification(
    review: GraphDraftReviewCoordinator,
    change_set_id: UUID,
    *,
    question: str,
    decision: str,
    answer: str | None = None,
    actor: AuthContext | None = None,
) -> GraphChangeSet:
    with review.application_transaction():
        change_set = review.records.get_graph_change_set_for_update(change_set_id)
        review.authorization.require_interactive(actor, action="Answering a review clarification")
        review._ensure_graph_change_set_editable(change_set, actor=actor)
        if question not in change_set.clarification_requests:
            raise ValidationError("This clarification changed. Reload the review.")
        if decision not in {"answered", "deferred", "resume"}:
            raise ValidationError("Unknown clarification decision.")
        if decision == "answered" and not (answer and answer.strip()):
            raise ValidationError("An answer must not be blank.")
        answers = dict(change_set.review_metadata.get(TRIAGE_CLARIFICATIONS_KEY, {}))
        answers[clarification_id(question)] = {
            "status": decision,
            "answer": answer.strip() if answer else "",
            "reviewed_at": utc_now().isoformat(),
            "reviewed_by": actor_user_id(actor),
        }
        change_set.review_metadata = {
            **change_set.review_metadata,
            "review_triage_used": True,
            TRIAGE_CLARIFICATIONS_KEY: answers,
            "first_triage_decision_at": change_set.review_metadata.get("first_triage_decision_at")
            or utc_now().isoformat(),
            "last_triage_decision_at": utc_now().isoformat(),
        }
        change_set.updated_at = utc_now()
        review.records.save_graph_change_set(change_set)
        return change_set
