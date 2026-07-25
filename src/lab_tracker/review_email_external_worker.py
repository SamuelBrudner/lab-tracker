"""Local JSON bridge for an external mailbox delivery worker.

This bridge never sends mail. It leases durable outbox rows and lets a mailbox
owner (for example, a Codex automation using Outlook) report provider
acceptance or a sanitized failure.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app_parts.runtime import _review_email_url
from lab_tracker.config import Settings, get_settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.review_email_transport import (
    ReviewReadyEmail,
    render_review_email_content,
)
from lab_tracker.services.review_email_service import normalize_review_email
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

MAX_PROVIDER_MESSAGE_ID_LENGTH = 500
MAX_AUTH_SECRET_FILE_BYTES = 4096


@contextmanager
def _api(settings: Settings) -> Iterator[LabTrackerAPI]:
    engine = get_engine(settings)
    session_factory = get_session_factory(engine=engine)
    try:
        with session_factory() as session:
            yield LabTrackerAPI(
                repository=SQLAlchemyLabTrackerRepository(session),
                settings=settings,
                surface="background",
            )
    finally:
        engine.dispose()


def claim(settings: Settings) -> dict[str, object]:
    _require_external_transport(settings)
    with _api(settings) as api:
        delivery = api.review_emails.claim_next(
            lease_seconds=settings.review_email_claim_lease_seconds
        )
        if delivery is None:
            return {"delivery": None}
        if delivery.claim_token is None:
            raise RuntimeError("Claimed review email is missing its lease token.")
        if delivery.event_type not in {"review_ready", "test"}:
            raise RuntimeError("Claimed review email has an unsupported event type.")
        destination_email = normalize_review_email(delivery.destination_email)
        review_url = _review_email_url(settings, delivery)
        rendered = render_review_email_content(
            ReviewReadyEmail(
                recipient_email=destination_email,
                review_url=review_url,
                idempotency_key=delivery.idempotency_key,
                event_type=delivery.event_type,
            )
        )
        return {
            "delivery": {
                "delivery_id": str(delivery.delivery_id),
                "claim_token": str(delivery.claim_token),
                "to": destination_email,
                "subject": rendered.subject,
                "text_content": rendered.text_content,
                "dedupe_marker": rendered.dedupe_marker,
            }
        }


def enqueue_test(
    settings: Settings,
    *,
    destination_email: str,
) -> dict[str, object]:
    """Queue one content-free transport test without creating graph records."""

    _require_external_transport(settings)
    with _api(settings) as api:
        delivery = api.review_emails.enqueue_test(destination_email)
        if delivery.event_type != "test" or delivery.change_set_id is not None:
            raise RuntimeError("Review email test did not preserve its no-graph invariant.")
        return {
            "delivery_id": str(delivery.delivery_id),
            "event_type": delivery.event_type,
            "status": delivery.status.value,
        }


def mark_accepted(
    settings: Settings,
    *,
    delivery_id: UUID,
    claim_token: UUID,
    provider_message_id: str | None,
) -> dict[str, object]:
    _require_external_transport(settings)
    with _api(settings) as api:
        delivery = api.review_emails.mark_accepted(
            delivery_id,
            claim_token=claim_token,
            provider_message_id=_safe_provider_message_id(provider_message_id),
        )
        return {
            "delivery_id": str(delivery.delivery_id),
            "status": delivery.status.value,
        }


def mark_failed(
    settings: Settings,
    *,
    delivery_id: UUID,
    claim_token: UUID,
    retryable: bool,
    error_code: str,
) -> dict[str, object]:
    _require_external_transport(settings)
    with _api(settings) as api:
        delivery = api.review_emails.mark_failed(
            delivery_id,
            claim_token=claim_token,
            error_message=_safe_external_error(error_code),
            retryable=retryable,
        )
        return {
            "delivery_id": str(delivery.delivery_id),
            "status": delivery.status.value,
        }


def _require_external_transport(settings: Settings) -> None:
    if not settings.review_email_enabled:
        raise RuntimeError("Review email delivery is disabled.")
    if settings.review_email_transport != "external":
        raise RuntimeError("Review email transport is not external.")


def _safe_external_error(code: str) -> str:
    normalized = str(code or "").strip().lower().replace(" ", "_")
    allowed = {
        "authentication_rejected",
        "connection_failed",
        "permanent_rejection",
        "temporary_rejection",
        "timeout",
        "transport_unavailable",
    }
    return (
        f"External email provider error: {normalized}."
        if normalized in allowed
        else "External email provider error."
    )


def _safe_provider_message_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if (
        len(normalized) > MAX_PROVIDER_MESSAGE_ID_LENGTH
        or "\r" in normalized
        or "\n" in normalized
        or not normalized.isprintable()
    ):
        raise ValueError("provider_message_id must be a short printable single-line value.")
    return normalized


def _worker_settings() -> Settings:
    """Load settings, optionally reusing the app's durable runtime secret.

    A one-shot Compose control container can mount the primary app's
    ``runtime-env`` directory read-only. This escape hatch lets it mint the same
    signed review links without copying the auth secret into Compose or another
    env file.
    """

    configured_secret = os.environ.get("LAB_TRACKER_AUTH_SECRET_KEY", "").strip()
    secret_file = os.environ.get("LAB_TRACKER_AUTH_SECRET_KEY_FILE", "").strip()
    if configured_secret or not secret_file:
        return get_settings()
    try:
        with Path(secret_file).open(encoding="utf-8") as handle:
            secret = handle.read(MAX_AUTH_SECRET_FILE_BYTES + 1).strip()
    except OSError:
        raise RuntimeError(
            "The external email worker could not read its configured auth-secret file."
        ) from None
    if not secret or len(secret.encode()) > MAX_AUTH_SECRET_FILE_BYTES:
        raise RuntimeError("The external email worker auth-secret file is empty or too large.")
    return Settings(auth_secret_key=secret)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lab_tracker.review_email_external_worker")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("claim")
    test = commands.add_parser("test")
    test.add_argument("--to", dest="destination_email", required=True)
    accepted = commands.add_parser("accepted")
    accepted.add_argument("--delivery-id", type=UUID, required=True)
    accepted.add_argument("--claim-token", type=UUID, required=True)
    accepted.add_argument("--provider-message-id")
    failed = commands.add_parser("failed")
    failed.add_argument("--delivery-id", type=UUID, required=True)
    failed.add_argument("--claim-token", type=UUID, required=True)
    failed.add_argument("--retryable", action="store_true")
    failed.add_argument("--error-code", default="transport_unavailable")
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = _worker_settings()
    if args.command == "claim":
        result = claim(settings)
    elif args.command == "test":
        result = enqueue_test(settings, destination_email=args.destination_email)
    elif args.command == "accepted":
        result = mark_accepted(
            settings,
            delivery_id=args.delivery_id,
            claim_token=args.claim_token,
            provider_message_id=args.provider_message_id,
        )
    elif args.command == "failed":
        result = mark_failed(
            settings,
            delivery_id=args.delivery_id,
            claim_token=args.claim_token,
            retryable=args.retryable,
            error_code=args.error_code,
        )
    else:  # pragma: no cover - argparse guarantees a known required subcommand
        raise RuntimeError("Unknown review email worker command.")
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
