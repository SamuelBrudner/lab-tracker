"""Stateless, signed, short-TTL deep links into a specific daily-review edition.

These links are what the daily-review *cue* carries (a push notification or the
email cue). They are deliberately **capability-free**: following one only lands
the recipient on ``/app/batches/<change_set_id>`` inside the app, where Lab
Tracker's normal authentication still gates every read and every accept/reject.
The signature (HMAC-SHA256 over ``change_set_id | exp`` with the app auth secret)
exists so a forged or tampered link is rejected, and the short TTL bounds how
long a leaked cue stays live -- not to grant access.

They are intentionally **re-clickable within the TTL** rather than single-use:
an email client's link-preview prefetch, or the recipient opening the mail twice,
must not burn the link. Because the link grants no capability, single-use would
add fragility without adding a security guarantee. See
``docs/daily-review-egress-defaults.md``.

The token format mirrors the app's other hand-rolled tokens (see
``lab_tracker.auth.TokenService``): two base64url segments ``<payload>.<sig>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from uuid import UUID

from lab_tracker.models import utc_now


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _sign(secret: str, data: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest()


def sign_review_link(
    secret: str,
    change_set_id: UUID,
    *,
    ttl_hours: int = 72,
) -> str:
    """Return a signed token that resolves to ``change_set_id`` until it expires.

    ``secret`` is the app auth secret key. ``ttl_hours`` bounds the link's life.
    """
    if not secret or not secret.strip():
        raise ValueError("secret must not be empty.")
    if ttl_hours < 1:
        raise ValueError("ttl_hours must be at least 1.")
    expires_at = utc_now() + timedelta(hours=ttl_hours)
    payload = {"cs": str(change_set_id), "exp": int(expires_at.timestamp())}
    payload_segment = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature_segment = _b64url_encode(_sign(secret, payload_segment.encode("ascii")))
    return f"{payload_segment}.{signature_segment}"


def verify_review_link(secret: str, token: str) -> UUID | None:
    """Return the change-set id for a valid, unexpired token, else ``None``.

    Returns ``None`` (never raises) for any malformed, tampered, or expired
    token so the consume route can uniformly bounce to "open your Read".
    """
    if not secret or not token:
        return None
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_segment, signature_segment = parts
    try:
        # A genuine token's segments are base64url (ASCII); a non-ASCII segment
        # is by definition forged. UnicodeEncodeError is a ValueError subclass,
        # so encoding must stay inside this guard or the public /r/ route 500s
        # on a crafted token instead of bouncing.
        expected = _sign(secret, payload_segment.encode("ascii"))
        provided = _b64url_decode(signature_segment)
        if not hmac.compare_digest(expected, provided):
            return None
        payload = json.loads(_b64url_decode(payload_segment))
        change_set_id = UUID(str(payload["cs"]))
        expires_at = int(payload["exp"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
    if expires_at <= int(utc_now().timestamp()):
        return None
    return change_set_id
