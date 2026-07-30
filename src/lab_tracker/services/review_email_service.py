"""Durable review-ready email outbox orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta
from email.headerregistry import Address
from uuid import UUID, uuid4

from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    ReviewEmailDelivery,
    ReviewEmailDeliveryStatus,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext

REVIEW_READY_EVENT = "review_ready"
TEST_EVENT = "test"
REVIEW_EMAIL_IDEMPOTENCY_VERSION = "v1"
DEFAULT_CLAIM_LEASE_SECONDS = 300
DEFAULT_MAX_ATTEMPTS = 8
MAX_RETRY_DELAY_SECONDS = 60 * 60


def normalize_review_email(value: str) -> str:
    """Return a normalized, header-safe mailbox or raise a validation error."""

    cleaned = str(value or "").strip()
    if len(cleaned) > 254 or "\r" in cleaned or "\n" in cleaned:
        raise ValidationError("notification_email must be one valid email address.")
    try:
        address = Address(addr_spec=cleaned)
    except Exception as exc:
        raise ValidationError("notification_email must be one valid email address.") from exc
    if not address.username or not address.domain:
        raise ValidationError("notification_email must be one valid email address.")
    return f"{address.username}@{address.domain.lower()}"


class ReviewEmailService(BaseService):
    """Own outbox enqueue, leasing, retry, and provider-acceptance state."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        delivery_enabled: bool = False,
    ) -> None:
        super().__init__(context)
        self.max_attempts = max(1, max_attempts)
        self.delivery_enabled = bool(delivery_enabled)

    def enqueue_ready_review(
        self,
        change_set: GraphChangeSet,
    ) -> ReviewEmailDelivery | None:
        """Queue one opted-in assigned batch review without performing I/O."""

        if (
            not self.delivery_enabled
            or change_set.draft_mode != GraphDraftMode.GRAPH_BATCH
            or change_set.status != GraphChangeSetStatus.READY
            or change_set.review_assignee_user_id is None
            or not any(
                operation.status == GraphChangeOperationStatus.PROPOSED
                for operation in change_set.operations
            )
        ):
            return None
        settings = self.repository.get_graph_draft_batch_settings_by_project(
            change_set.project_id,
            user_id=change_set.review_assignee_user_id,
        )
        if (
            settings is None
            or not settings.email_notifications_enabled
            or not settings.notification_email
            or settings.notification_email_confirmed_at is None
        ):
            return None
        destination_email = normalize_review_email(settings.notification_email)
        idempotency_key = (
            f"review-ready:{REVIEW_EMAIL_IDEMPOTENCY_VERSION}:"
            f"{change_set.change_set_id}:{change_set.review_assignee_user_id}"
        )
        existing = self.repository.review_email_outbox.get_by_idempotency_key(
            idempotency_key
        )
        if existing is not None:
            return existing
        now = utc_now()
        delivery = ReviewEmailDelivery(
            delivery_id=uuid4(),
            change_set_id=change_set.change_set_id,
            recipient_user_id=change_set.review_assignee_user_id,
            event_type=REVIEW_READY_EVENT,
            destination_email=destination_email,
            idempotency_key=idempotency_key,
            status=ReviewEmailDeliveryStatus.PENDING,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        self.repository.review_email_outbox.save(delivery)
        return delivery

    def enqueue_test(
        self,
        destination_email: str,
        *,
        recipient_user_id: UUID | None = None,
    ) -> ReviewEmailDelivery:
        """Queue a fixed test cue without creating any graph record."""

        if not self.delivery_enabled:
            raise ValidationError("Review email delivery is not enabled.")
        now = utc_now()
        delivery_id = uuid4()
        delivery = ReviewEmailDelivery(
            delivery_id=delivery_id,
            change_set_id=None,
            recipient_user_id=recipient_user_id,
            event_type=TEST_EVENT,
            destination_email=normalize_review_email(destination_email),
            idempotency_key=(
                f"review-email-test:{REVIEW_EMAIL_IDEMPOTENCY_VERSION}:{delivery_id}"
            ),
            status=ReviewEmailDeliveryStatus.PENDING,
            next_attempt_at=now,
            created_at=now,
            updated_at=now,
        )
        with self.unit_of_work():
            self.repository.review_email_outbox.save(delivery)
        return delivery

    def claim_next(
        self,
        *,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> ReviewEmailDelivery | None:
        """Lease the next due delivery for one provider attempt."""

        if not self.delivery_enabled:
            return None
        claimed_at = now or utc_now()
        while True:
            claim_token = uuid4()
            with self.unit_of_work():
                delivery = self.repository.review_email_outbox.claim_next(
                    now=claimed_at,
                    lease_until=claimed_at + timedelta(seconds=max(1, lease_seconds)),
                    claim_token=claim_token,
                )
            if delivery is None:
                return None
            if (
                delivery.event_type != REVIEW_READY_EVENT
                or self._preference_still_allows(delivery)
            ):
                return delivery
            self.cancel(
                delivery.delivery_id,
                claim_token=claim_token,
                reason="Review email preference is no longer enabled for this address.",
            )

    def mark_accepted(
        self,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        provider_message_id: str | None,
        accepted_at: datetime | None = None,
    ) -> ReviewEmailDelivery:
        delivery = self._claimed_delivery(delivery_id, claim_token=claim_token)
        when = accepted_at or utc_now()
        delivery.status = ReviewEmailDeliveryStatus.ACCEPTED
        delivery.provider_message_id = (provider_message_id or "").strip() or None
        delivery.accepted_at = when
        delivery.next_attempt_at = None
        delivery.claimed_at = None
        delivery.lease_expires_at = None
        delivery.claim_token = None
        delivery.last_error = None
        delivery.updated_at = when
        with self.unit_of_work():
            self.repository.review_email_outbox.save(delivery)
        return delivery

    def mark_failed(
        self,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        error_message: str,
        retryable: bool,
        failed_at: datetime | None = None,
    ) -> ReviewEmailDelivery:
        delivery = self._claimed_delivery(delivery_id, claim_token=claim_token)
        when = failed_at or utc_now()
        delivery.last_error = _sanitize_error(error_message)
        terminal = not retryable or delivery.attempt_count >= self.max_attempts
        delivery.status = (
            ReviewEmailDeliveryStatus.FAILED
            if terminal
            else ReviewEmailDeliveryStatus.RETRYABLE
        )
        delivery.next_attempt_at = (
            None
            if terminal
            else when
            + timedelta(
                seconds=min(
                    MAX_RETRY_DELAY_SECONDS,
                    30 * (2 ** max(0, delivery.attempt_count - 1)),
                )
            )
        )
        delivery.claimed_at = None
        delivery.lease_expires_at = None
        delivery.claim_token = None
        delivery.updated_at = when
        with self.unit_of_work():
            self.repository.review_email_outbox.save(delivery)
        return delivery

    def cancel(
        self,
        delivery_id: UUID,
        *,
        claim_token: UUID,
        reason: str,
    ) -> ReviewEmailDelivery:
        delivery = self._claimed_delivery(delivery_id, claim_token=claim_token)
        delivery.status = ReviewEmailDeliveryStatus.FAILED
        delivery.last_error = _sanitize_error(reason)
        delivery.next_attempt_at = None
        delivery.claimed_at = None
        delivery.lease_expires_at = None
        delivery.claim_token = None
        delivery.updated_at = utc_now()
        with self.unit_of_work():
            self.repository.review_email_outbox.save(delivery)
        return delivery

    def get(self, delivery_id: UUID) -> ReviewEmailDelivery:
        delivery = self.repository.review_email_outbox.get(delivery_id)
        if delivery is None:
            raise NotFoundError("Review email delivery does not exist.")
        return delivery

    def list(self) -> list[ReviewEmailDelivery]:
        return self.repository.review_email_outbox.list()

    def _claimed_delivery(
        self,
        delivery_id: UUID,
        *,
        claim_token: UUID,
    ) -> ReviewEmailDelivery:
        delivery = self.get(delivery_id)
        if (
            delivery.status != ReviewEmailDeliveryStatus.SENDING
            or delivery.claim_token != claim_token
        ):
            raise ValidationError("Review email delivery lease is no longer active.")
        return delivery

    def _preference_still_allows(self, delivery: ReviewEmailDelivery) -> bool:
        if delivery.recipient_user_id is None or delivery.change_set_id is None:
            return False
        change_set = self.repository.graph_change_sets.get(delivery.change_set_id)
        if change_set is None:
            return False
        settings = self.repository.get_graph_draft_batch_settings_by_project(
            change_set.project_id,
            user_id=delivery.recipient_user_id,
        )
        if (
            settings is None
            or not settings.email_notifications_enabled
            or not settings.notification_email
            or settings.notification_email_confirmed_at is None
        ):
            return False
        return normalize_review_email(settings.notification_email) == normalize_review_email(
            delivery.destination_email
        )


def _sanitize_error(message: str) -> str:
    cleaned = " ".join(str(message or "Email delivery failed.").split())
    if not cleaned:
        return "Email delivery failed."
    return cleaned[:500]
