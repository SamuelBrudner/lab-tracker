"""Deliver an assigned proposal and authenticate a reply to that exact revision."""

from __future__ import annotations

import base64
import binascii
import hmac
import re
from email.message import EmailMessage
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lab_tracker.auth import AuthContext, Role
from lab_tracker.config import Settings
from lab_tracker.db_models import ReviewEmailOutboxModel, UserModel
from lab_tracker.errors import AuthError, ConflictError, NotFoundError, ValidationError
from lab_tracker.models import ReviewEmailDelivery, ReviewEmailDeliveryStatus, utc_now
from lab_tracker.proposal_email_content import proposal_detail_lines
from lab_tracker.services.proposal_email_decisions import (
    apply_proposal_email_decision,
    correctable_fields,
    parse_review_reply,
    require_current_proposal,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

if TYPE_CHECKING:
    from lab_tracker.api import LabTrackerAPI

_TOKEN = re.compile(r"[a-z2-7]{52}")
_IN_FLIGHT = (
    ReviewEmailDeliveryStatus.SENDING,
    ReviewEmailDeliveryStatus.RETRYABLE,
    ReviewEmailDeliveryStatus.ACCEPTED,
)


def _token_for(delivery_id: UUID, secret: str) -> str:
    mac = hmac.digest(secret.encode(), b"proposal-reply-v1\0" + delivery_id.bytes, "sha256")
    return base64.b32encode(delivery_id.bytes + mac[:16]).decode().rstrip("=").lower()


def _delivery_id(token: str, secret: str) -> UUID | None:
    if not _TOKEN.fullmatch(token):
        return None
    try:
        delivery_id = UUID(bytes=base64.b32decode(token.upper() + "====")[:16])
    except (ValueError, binascii.Error):
        return None
    return delivery_id if hmac.compare_digest(_token_for(delivery_id, secret), token) else None


def _mailbox_owner(session: Session, delivery: ReviewEmailDelivery) -> AuthContext:
    # Verification is both proof of the current login and consent to receive
    # research content. Re-enrolling cannot resurrect earlier reply addresses.
    user = session.scalar(
        select(UserModel)
        .where(
            UserModel.user_id == delivery.recipient_user_id,
            UserModel.username == delivery.destination_email,
            UserModel.review_mailbox_address == UserModel.username,
            UserModel.review_mailbox_verified_at <= delivery.created_at,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise AuthError("This review mailbox is no longer enrolled.")
    return AuthContext(user_id=user.user_id, role=Role(user.role), principal_label="email review")


def _subject(delivery: ReviewEmailDelivery) -> str:
    # Distinct, content-free subjects keep reviewers' proposals apart in inboxes
    # that group mail by subject. Field corrections stay in the same thread.
    if delivery.operation_id is None:
        raise ConflictError("This delivery has no proposal.")
    return f"Lab Tracker proposal for review [{delivery.operation_id.hex[:8]}]"


def _thread_headers(
    delivery: ReviewEmailDelivery, settings: Settings, *, original_id: UUID
) -> EmailMessage:
    local, domain = settings.review_email_reply_address.split("@", 1)
    token = _token_for(original_id, settings.auth_secret_key)
    message = EmailMessage()
    message["To"] = delivery.destination_email
    message["Reply-To"] = f"{local}+{token}@{domain}"
    message["Message-ID"] = f"<proposal-{delivery.delivery_id.hex}@{domain}>"
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    if delivery.parent_delivery_id:
        parent = f"<proposal-{delivery.parent_delivery_id.hex}@{domain}>"
        message["In-Reply-To"] = parent
        message["References"] = parent
    return message


def build_reviewer_message(
    session: Session,
    api: LabTrackerAPI,
    delivery: ReviewEmailDelivery,
    settings: Settings,
    *,
    review_url: str,
) -> EmailMessage:
    if delivery.event_type in {"reply_ack", "reply_help"} and delivery.parent_delivery_id:
        original = api.review_emails.get(delivery.parent_delivery_id)
        if (
            original.event_type != "proposal"
            or delivery.recipient_user_id != original.recipient_user_id
            or delivery.destination_email != original.destination_email
            or delivery.change_set_id != original.change_set_id
        ):
            raise AuthError("This response does not match the original delivery.")
        _mailbox_owner(session, original)
        message = _thread_headers(delivery, settings, original_id=original.delivery_id)
        message["Subject"] = f"Re: {_subject(original)}"
        if delivery.event_type == "reply_help" and not original.replied_at:
            body = (
                "Your reply did not change the proposal. To approve it, reply with just Approve. "
                "To edit it, use a field listed in the original email, for example "
                "Correct text: followed by the replacement. To request a revision, reply with "
                "Correct: followed by your feedback."
            )
        elif delivery.event_type == "reply_ack" and original.reply_outcome == "approved":
            body = "Your approval was recorded for this proposal."
        elif (
            delivery.event_type == "reply_ack" and original.reply_outcome == "correction_requested"
        ):
            body = "Your correction was recorded. The proposal has been set aside for revision."
        else:
            raise ConflictError("This response has been superseded.")
        message.set_content(body + "\n\nFull review: " + review_url)
        return message
    if (
        delivery.event_type != "proposal"
        or not delivery.change_set_id
        or not delivery.operation_id
        or not delivery.operation_revision
        or not delivery.reply_expires_at
        or delivery.reply_expires_at <= utc_now()
        or delivery.replied_at is not None
    ):
        raise ConflictError("This delivery is no longer awaiting review.")
    actor = _mailbox_owner(session, delivery)
    draft = api.get_graph_change_set(delivery.change_set_id)
    proposal = require_current_proposal(
        api.graph_drafts.review, draft, delivery.operation_id, delivery.operation_revision, actor
    )
    project = api.projects.get_project(draft.project_id)
    message = _thread_headers(delivery, settings, original_id=delivery.delivery_id)
    message["Subject"] = _subject(delivery)

    lines = ["Revised proposal", project.name] if delivery.parent_delivery_id else [project.name]
    lines.extend(proposal_detail_lines(draft, proposal, api.questions.repository))
    if proposal.rationale:
        lines.extend(("", f"Reason for the proposal: {proposal.rationale}"))
    fields = correctable_fields(proposal)
    lines.extend(
        (
            "",
            "Reply above this message with one line:",
            "Approve",
            "Correct: describe what needs to change",
        )
    )
    if fields:
        lines.extend(
            (
                f"To replace a field, use: Correct {fields[0]}: your replacement",
                f"Editable fields: {', '.join(fields)}",
                "You will receive the revised proposal for approval.",
            )
        )
    lines.extend(
        (
            "",
            "Approval accepts this proposal in the draft.",
            f"Reply by {delivery.reply_expires_at:%Y-%m-%d %H:%M UTC}.",
            f"Full review: {review_url}",
        )
    )
    message.set_content("\n".join(lines))
    return message


def receive_proposal_reply(
    session: Session, *, token: str, sender_email: str, reply_text: str, settings: Settings
) -> str:
    """Handle a sender-authenticated reply in a fresh session, before IMAP acknowledgement."""
    from lab_tracker.api import LabTrackerAPI

    if not (
        settings.review_email_enabled
        and settings.review_email_transport == "smtp"
        and settings.review_email_reply_address
        and settings.review_email_authserv_id
    ):
        return "ignored"
    delivery_id = _delivery_id(token, settings.auth_secret_key)
    if delivery_id is None:
        return "ignored"
    api = LabTrackerAPI(repository=SQLAlchemyLabTrackerRepository(session), settings=settings)
    try:
        with api.graph_drafts.review.application_transaction():
            delivery = api.review_emails.get(delivery_id)
            if (
                delivery.event_type != "proposal"
                or sender_email != delivery.destination_email
                or not delivery.operation_id
                or not delivery.change_set_id
                or not delivery.operation_revision
            ):
                raise AuthError("This reply does not match the delivery.")
            if (
                delivery.replied_at
                or not delivery.reply_expires_at
                or delivery.reply_expires_at <= utc_now()
                or delivery.status not in _IN_FLIGHT
            ):
                raise ConflictError("This reply has expired or has already been handled.")
            # Validate eligibility before offering help, and serialize attempts
            # for this draft, including simultaneous malformed replies.
            draft = api.graph_drafts.review.records.get_graph_change_set_for_update(
                delivery.change_set_id
            )
            # SQLite obtains its writer reservation on the draft above. Read
            # current consent and permissions after that reservation on all backends.
            actor = _mailbox_owner(session, delivery)
            now = utc_now()
            try:
                # The ordinary editor can save validation diagnostics before it
                # declines an approval. Keep those writes out of a rejected reply.
                with api.graph_drafts.review.repository.savepoint():
                    command = parse_review_reply(reply_text)
                    outcome = apply_proposal_email_decision(
                        api.graph_drafts.review,
                        change_set_id=delivery.change_set_id,
                        operation_id=delivery.operation_id,
                        expected_revision=delivery.operation_revision,
                        actor=actor,
                        command=command,
                    )
            except ValidationError:
                require_current_proposal(
                    api.graph_drafts.review,
                    draft,
                    delivery.operation_id,
                    delivery.operation_revision,
                    actor,
                )
                api.review_emails.enqueue_response(delivery, "reply_help")
                return "ignored"
            # Consumption, audit and any resulting email share the draft transaction.
            claimed = session.execute(
                update(ReviewEmailOutboxModel)
                .where(
                    ReviewEmailOutboxModel.delivery_id == delivery_id,
                    ReviewEmailOutboxModel.status.in_(_IN_FLIGHT),
                    ReviewEmailOutboxModel.replied_at.is_(None),
                    ReviewEmailOutboxModel.reply_expires_at > now,
                )
                .values(replied_at=now, reply_outcome=outcome)
            )
            if getattr(claimed, "rowcount", 0) != 1:
                raise ConflictError("This reply has expired or has already been handled.")
            if outcome == "corrected":
                draft = api.get_graph_change_set(delivery.change_set_id)
                revised = next(
                    op for op in draft.operations if op.operation_id == delivery.operation_id
                )
                api.review_emails.enqueue_proposal(
                    draft,
                    revised,
                    delivery.destination_email,
                    previous_delivery_id=delivery.delivery_id,
                )
            else:
                api.review_emails.enqueue_response(delivery, "reply_ack")
        return outcome
    except (AuthError, ConflictError, NotFoundError, ValidationError):
        return "ignored"
