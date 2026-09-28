"""Per-operation review verdict helpers: deferral stamps and structured reject reasons.

These sit beside the review coordinator so that every durable stamp written
into ``GraphChangeOperation.error_metadata`` for a human verdict comes from one
place, keyed by the shared constants in ``lab_tracker.models``.
"""

from __future__ import annotations

from typing import Any

from lab_tracker.auth import AuthContext
from lab_tracker.errors import PermissionDeniedError, ValidationError
from lab_tracker.models import (
    DEFERRED_AT_KEY,
    DEFERRED_BY_KEY,
    REJECT_REASON_KEY,
    REVIEW_NOTE_KEY,
    REVIEWED_AT_KEY,
    REVIEWED_BY_KEY,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphOperationRejectReason,
    utc_now,
)
from lab_tracker.patching import PatchValue, is_provided
from lab_tracker.services.shared import actor_user_id

# A rejection supersedes any deferral and restates its reason from scratch.
_CLEARED_ON_REJECTION = frozenset({DEFERRED_AT_KEY, DEFERRED_BY_KEY, REJECT_REASON_KEY})


def apply_deferral(
    operation: GraphChangeOperation,
    deferred: PatchValue[bool | None],
    actor: AuthContext | None,
) -> None:
    """Stamp or clear the deferral verdict; a repeat defer is a no-op."""

    if not is_provided(deferred) or deferred is None:
        return
    if not deferred:
        operation.error_metadata = {
            key: value
            for key, value in operation.error_metadata.items()
            if key not in {DEFERRED_AT_KEY, DEFERRED_BY_KEY}
        }
        return
    if operation.status != GraphChangeOperationStatus.PROPOSED:
        raise ValidationError("Only proposed operations can be deferred.")
    if DEFERRED_AT_KEY in operation.error_metadata:
        return
    operation.error_metadata = {
        **operation.error_metadata,
        DEFERRED_AT_KEY: utc_now().isoformat(),
        DEFERRED_BY_KEY: actor_user_id(actor),
    }


def resolve_reject_reason(
    operation: GraphChangeOperation,
    reject_reason: PatchValue[GraphOperationRejectReason | None],
) -> GraphOperationRejectReason | None:
    """The reason this update leaves on the operation: the provided one, else current."""

    if not is_provided(reject_reason):
        return operation.reject_reason
    if reject_reason is not None and operation.status != GraphChangeOperationStatus.REJECTED:
        raise ValidationError("reject_reason requires status rejected.")
    return reject_reason


def rejection_audit_metadata(
    operation: GraphChangeOperation,
    *,
    reason: GraphOperationRejectReason | None,
    actor: AuthContext | None,
) -> dict[str, Any]:
    """Error metadata for a (re)stated rejection: reviewer stamps plus the reason."""

    metadata = {
        key: value
        for key, value in operation.error_metadata.items()
        if key not in _CLEARED_ON_REJECTION
    }
    metadata.update(
        {
            REVIEWED_AT_KEY: utc_now().isoformat(),
            REVIEWED_BY_KEY: actor_user_id(actor),
            REVIEW_NOTE_KEY: operation.review_note,
        }
    )
    if reason is not None:
        metadata[REJECT_REASON_KEY] = reason.value
    return metadata


def ensure_non_interactive_only_accepts(
    actor: AuthContext | None,
    operation: GraphChangeOperation,
    *,
    payload: PatchValue[dict[str, Any] | None],
    status: PatchValue[GraphChangeOperationStatus | None],
    review_note: PatchValue[str | None],
    deferred: PatchValue[bool | None],
    reject_reason: PatchValue[GraphOperationRejectReason | None],
) -> None:
    """Editing, rejecting, and deferring are a person's verdicts.

    A delegated principal (the drafting pass or a graph_curate token) may only
    move a still-proposed, undeferred proposal to ``accepted``; a person's
    rejection or deferral is never overridden, and whether the accept is
    admitted is decided afterwards against the project's grant.
    """

    if actor is None or actor.is_interactive:
        return
    if (
        any(is_provided(value) for value in (payload, review_note, deferred, reject_reason))
        or not is_provided(status)
        or status is not GraphChangeOperationStatus.ACCEPTED
    ):
        raise PermissionDeniedError(
            "Delegated curation may only accept proposals; editing, rejecting, and "
            "deferring require an interactive human session."
        )
    if (
        operation.status != GraphChangeOperationStatus.PROPOSED
        or DEFERRED_AT_KEY in operation.error_metadata
    ):
        raise PermissionDeniedError(
            "Delegated curation may only accept a proposal that is still proposed and "
            "not deferred; a person's verdict stands."
        )
