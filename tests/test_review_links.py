"""Focused tests for signed daily-review delivery links."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from lab_tracker.review_links import (
    InvalidReviewLinkToken,
    sign_review_link,
    verify_review_link,
)

SECRET = "a-strong-non-placeholder-secret-value"
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def test_sign_verify_roundtrip_returns_all_bound_claims() -> None:
    change_set_id = uuid4()
    recipient_user_id = uuid4()
    delivery_id = uuid4()

    token = sign_review_link(
        SECRET,
        change_set_id,
        recipient_user_id=recipient_user_id,
        delivery_id=delivery_id,
        ttl_minutes=24 * 60,
        now=NOW,
    )
    claims = verify_review_link(
        SECRET,
        token,
        expected_recipient_user_id=recipient_user_id,
        expected_delivery_id=delivery_id,
        now=NOW + timedelta(hours=23),
    )

    assert claims.change_set_id == change_set_id
    assert claims.recipient_user_id == recipient_user_id
    assert claims.delivery_id == delivery_id
    assert claims.version == 1
    assert claims.expires_at == NOW + timedelta(hours=24)


def test_recipient_and_delivery_are_part_of_the_signed_binding() -> None:
    change_set_id = uuid4()
    recipient_user_id = uuid4()
    delivery_id = uuid4()
    token = sign_review_link(
        SECRET,
        change_set_id,
        recipient_user_id=recipient_user_id,
        delivery_id=delivery_id,
        now=NOW,
    )

    with pytest.raises(InvalidReviewLinkToken):
        verify_review_link(
            SECRET,
            token,
            expected_recipient_user_id=uuid4(),
            now=NOW,
        )
    with pytest.raises(InvalidReviewLinkToken):
        verify_review_link(
            SECRET,
            token,
            expected_delivery_id=uuid4(),
            now=NOW,
        )


def test_domain_separation_rejects_signature_without_review_link_context() -> None:
    token = sign_review_link(
        SECRET,
        uuid4(),
        recipient_user_id=uuid4(),
        delivery_id=uuid4(),
        now=NOW,
    )
    payload_segment, _ = token.split(".")
    legacy_signature = hmac.new(
        SECRET.encode(),
        payload_segment.encode(),
        hashlib.sha256,
    ).digest()
    legacy_signature_segment = base64.urlsafe_b64encode(legacy_signature).rstrip(b"=").decode()

    with pytest.raises(InvalidReviewLinkToken):
        verify_review_link(
            SECRET,
            f"{payload_segment}.{legacy_signature_segment}",
            now=NOW,
        )


def test_wrong_secret_and_tampering_raise_specific_exception() -> None:
    token = sign_review_link(
        SECRET,
        uuid4(),
        recipient_user_id=uuid4(),
        delivery_id=uuid4(),
        now=NOW,
    )
    payload_segment, signature_segment = token.split(".")
    other_payload = sign_review_link(
        SECRET,
        uuid4(),
        recipient_user_id=uuid4(),
        delivery_id=uuid4(),
        now=NOW,
    ).split(".")[0]

    with pytest.raises(InvalidReviewLinkToken):
        verify_review_link("a-different-secret-value", token, now=NOW)
    with pytest.raises(InvalidReviewLinkToken):
        verify_review_link(
            SECRET,
            f"{other_payload}.{signature_segment}",
            now=NOW,
        )
    with pytest.raises(InvalidReviewLinkToken):
        verify_review_link(SECRET, f"{payload_segment}.{signature_segment}x", now=NOW)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "garbage",
        "a.b.c",
        ".",
        "a.",
        ".b",
        "é.YWJj",
        "YWJj.é",
        "====.====",
    ],
)
def test_malformed_tokens_raise_specific_exception(token: str) -> None:
    with pytest.raises(InvalidReviewLinkToken):
        verify_review_link(SECRET, token, now=NOW)


def test_token_is_reclickable_until_expiry_then_fails_closed() -> None:
    token = sign_review_link(
        SECRET,
        uuid4(),
        recipient_user_id=uuid4(),
        delivery_id=uuid4(),
        ttl_minutes=1,
        now=NOW,
    )

    assert verify_review_link(SECRET, token, now=NOW + timedelta(seconds=59))
    assert verify_review_link(SECRET, token, now=NOW + timedelta(seconds=59))
    with pytest.raises(InvalidReviewLinkToken):
        verify_review_link(SECRET, token, now=NOW + timedelta(minutes=1))


def test_sign_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        sign_review_link(
            "",
            uuid4(),
            recipient_user_id=uuid4(),
            delivery_id=uuid4(),
            now=NOW,
        )
    with pytest.raises(ValueError):
        sign_review_link(
            SECRET,
            uuid4(),
            recipient_user_id=uuid4(),
            delivery_id=uuid4(),
            ttl_minutes=0,
            now=NOW,
        )
    with pytest.raises(TypeError):
        sign_review_link(
            SECRET,
            uuid4(),
            recipient_user_id="not-a-uuid",  # type: ignore[arg-type]
            delivery_id=uuid4(),
            now=NOW,
        )
    with pytest.raises(ValueError):
        sign_review_link(
            SECRET,
            uuid4(),
            recipient_user_id=uuid4(),
            delivery_id=uuid4(),
            now=NOW.replace(tzinfo=None),
        )
