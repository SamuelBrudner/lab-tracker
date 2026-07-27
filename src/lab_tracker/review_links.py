"""Signed, capability-free links to a review delivery.

The token identifies the review, its intended recipient, and the delivery row,
but does not authorize access to any of them. Routes consuming these tokens
must still apply Lab Tracker's normal authentication and authorization checks.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from lab_tracker.models import utc_now

REVIEW_LINK_TOKEN_VERSION = 1
DEFAULT_REVIEW_LINK_TTL_MINUTES = 72 * 60

_SIGNATURE_CONTEXT = b"lab-tracker/review-link-token/v1\x00"
_INVALID_TOKEN_MESSAGE = "Review link token is invalid or expired."
_EXPECTED_PAYLOAD_KEYS = {
    "change_set_id",
    "delivery_id",
    "exp",
    "recipient_user_id",
    "v",
}
_MAX_TOKEN_LENGTH = 2048


class InvalidReviewLinkToken(ValueError):
    """Raised when a review-link token cannot be trusted."""


@dataclass(frozen=True)
class ReviewLinkClaims:
    """Trusted claims recovered from a verified review-link token."""

    change_set_id: UUID
    recipient_user_id: UUID
    delivery_id: UUID
    version: int
    expires_at: datetime


def sign_review_link(
    secret: str,
    change_set_id: UUID,
    *,
    recipient_user_id: UUID,
    delivery_id: UUID,
    ttl_minutes: int = DEFAULT_REVIEW_LINK_TTL_MINUTES,
    now: datetime | None = None,
) -> str:
    """Return a signed, short-lived token bound to one review delivery."""

    _validate_secret(secret)
    if type(ttl_minutes) is not int or ttl_minutes < 1:
        raise ValueError("ttl_minutes must be an integer of at least 1.")
    issued_at = _normalize_datetime(now if now is not None else utc_now())
    expires_at = issued_at + timedelta(minutes=ttl_minutes)
    payload = {
        "change_set_id": str(_require_uuid(change_set_id, "change_set_id")),
        "delivery_id": str(_require_uuid(delivery_id, "delivery_id")),
        "exp": int(expires_at.timestamp()),
        "recipient_user_id": str(_require_uuid(recipient_user_id, "recipient_user_id")),
        "v": REVIEW_LINK_TOKEN_VERSION,
    }
    payload_bytes = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload_segment = _b64url_encode(payload_bytes)
    signature_segment = _b64url_encode(_sign(secret, payload_segment))
    return f"{payload_segment}.{signature_segment}"


def verify_review_link(
    secret: str,
    token: str,
    *,
    expected_recipient_user_id: UUID | None = None,
    expected_delivery_id: UUID | None = None,
    now: datetime | None = None,
) -> ReviewLinkClaims:
    """Verify ``token`` and return its claims, or raise ``InvalidReviewLinkToken``.

    The optional expected identifiers let an authenticated consume path bind
    the token to the current user and the delivery row it loaded.
    """

    _validate_secret(secret)
    checked_at = _normalize_datetime(now if now is not None else utc_now())
    try:
        payload_segment, signature_segment = _split_token(token)
        expected_signature = _sign(secret, payload_segment)
        provided_signature = _b64url_decode(signature_segment)
        if not hmac.compare_digest(expected_signature, provided_signature):
            raise InvalidReviewLinkToken(_INVALID_TOKEN_MESSAGE)

        payload = _decode_payload(payload_segment)
        claims = _claims_from_payload(payload)
        if claims.expires_at <= checked_at:
            raise InvalidReviewLinkToken(_INVALID_TOKEN_MESSAGE)
        if (
            expected_recipient_user_id is not None
            and claims.recipient_user_id != expected_recipient_user_id
        ):
            raise InvalidReviewLinkToken(_INVALID_TOKEN_MESSAGE)
        if expected_delivery_id is not None and claims.delivery_id != expected_delivery_id:
            raise InvalidReviewLinkToken(_INVALID_TOKEN_MESSAGE)
        return claims
    except InvalidReviewLinkToken:
        raise
    except (
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        OSError,
        OverflowError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise InvalidReviewLinkToken(_INVALID_TOKEN_MESSAGE) from exc


def _validate_secret(secret: str) -> None:
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("secret must not be empty.")


def _require_uuid(value: UUID, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID.")
    return value


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _split_token(token: str) -> tuple[str, str]:
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LENGTH:
        raise InvalidReviewLinkToken(_INVALID_TOKEN_MESSAGE)
    try:
        token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InvalidReviewLinkToken(_INVALID_TOKEN_MESSAGE) from exc
    parts = token.split(".")
    if len(parts) != 2 or not all(parts):
        raise InvalidReviewLinkToken(_INVALID_TOKEN_MESSAGE)
    return parts[0], parts[1]


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = b"=" * (-len(segment) % 4)
    raw = base64.b64decode(
        segment.encode("ascii") + padding,
        altchars=b"-_",
        validate=True,
    )
    if _b64url_encode(raw) != segment:
        raise ValueError("Non-canonical base64url segment.")
    return raw


def _sign(secret: str, payload_segment: str) -> bytes:
    signed_data = _SIGNATURE_CONTEXT + payload_segment.encode("ascii")
    return hmac.new(secret.encode("utf-8"), signed_data, hashlib.sha256).digest()


def _decode_payload(payload_segment: str) -> dict[str, Any]:
    decoded = json.loads(_b64url_decode(payload_segment).decode("utf-8"))
    if not isinstance(decoded, dict) or set(decoded) != _EXPECTED_PAYLOAD_KEYS:
        raise ValueError("Unexpected review-link payload.")
    return decoded


def _claims_from_payload(payload: dict[str, Any]) -> ReviewLinkClaims:
    version = payload["v"]
    expires_at_epoch = payload["exp"]
    if type(version) is not int or version != REVIEW_LINK_TOKEN_VERSION:
        raise ValueError("Unsupported review-link token version.")
    if type(expires_at_epoch) is not int:
        raise ValueError("Invalid review-link expiry.")
    return ReviewLinkClaims(
        change_set_id=_canonical_uuid(payload["change_set_id"]),
        recipient_user_id=_canonical_uuid(payload["recipient_user_id"]),
        delivery_id=_canonical_uuid(payload["delivery_id"]),
        version=version,
        expires_at=datetime.fromtimestamp(expires_at_epoch, tz=timezone.utc),
    )


def _canonical_uuid(value: Any) -> UUID:
    if not isinstance(value, str):
        raise ValueError("Invalid UUID claim.")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("UUID claim is not canonical.")
    return parsed
