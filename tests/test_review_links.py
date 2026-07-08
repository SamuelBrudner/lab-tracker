"""Unit tests for the stateless signed daily-review deep links."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import lab_tracker.review_links as review_links
from lab_tracker.review_links import sign_review_link, verify_review_link

SECRET = "a-strong-non-placeholder-secret-value"


def test_sign_verify_roundtrip() -> None:
    change_set_id = uuid4()
    token = sign_review_link(SECRET, change_set_id, ttl_hours=72)
    assert verify_review_link(SECRET, token) == change_set_id


def test_verify_rejects_wrong_secret() -> None:
    token = sign_review_link(SECRET, uuid4(), ttl_hours=72)
    assert verify_review_link("a-different-secret-value", token) is None


def test_verify_rejects_tampered_token() -> None:
    change_set_id = uuid4()
    token = sign_review_link(SECRET, change_set_id, ttl_hours=72)
    payload, signature = token.split(".")
    # Re-point the payload at a different change set while keeping the signature.
    forged_payload = sign_review_link(SECRET, uuid4(), ttl_hours=72).split(".")[0]
    assert verify_review_link(SECRET, f"{forged_payload}.{signature}") is None
    assert verify_review_link(SECRET, token + "x") is None


@pytest.mark.parametrize(
    "bad",
    ["", "garbage", "only-one-part", "a.b.c", "..", ".", "é.YWJj", "é.def", "é.a.b"],
)
def test_verify_rejects_malformed(bad: str) -> None:
    # Non-ASCII segments must return None, never raise: the public /r/{token}
    # route feeds attacker-controlled input straight into verify.
    assert verify_review_link(SECRET, bad) is None


def test_verify_rejects_expired_but_accepts_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    change_set_id = uuid4()
    base = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(review_links, "utc_now", lambda: base)
    token = sign_review_link(SECRET, change_set_id, ttl_hours=1)

    # Still inside the window: re-clickable, not single-use.
    monkeypatch.setattr(review_links, "utc_now", lambda: base + timedelta(minutes=30))
    assert verify_review_link(SECRET, token) == change_set_id
    assert verify_review_link(SECRET, token) == change_set_id

    # Past the TTL: bounces.
    monkeypatch.setattr(review_links, "utc_now", lambda: base + timedelta(hours=2))
    assert verify_review_link(SECRET, token) is None


def test_sign_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError):
        sign_review_link("", uuid4(), ttl_hours=72)
    with pytest.raises(ValueError):
        sign_review_link(SECRET, uuid4(), ttl_hours=0)
