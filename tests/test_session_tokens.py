"""Unit tests for session JWT revocation claims and absolute lifetime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import lab_tracker.auth as auth_module
from lab_tracker.auth import (
    AuthService,
    Role,
    TokenService,
    User,
    _b64url_encode,
    _b64url_encode_json,
    resolve_session_user,
)
from lab_tracker.errors import AuthError, ValidationError

_SIGNED_IN_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, datetime]:
    current = {"value": _SIGNED_IN_AT}
    monkeypatch.setattr(auth_module, "utc_now", lambda: current["value"])
    return current


def _user(*, session_epoch: int = 0) -> User:
    return User(
        user_id=uuid4(),
        username="sam",
        password_hash="unused",
        role=Role.EDITOR,
        session_epoch=session_epoch,
    )


def test_issued_token_carries_session_epoch_and_original_auth_time(clock) -> None:
    service = TokenService("secret", ttl_minutes=60, max_session_age_hours=1)
    user = _user(session_epoch=3)
    first = service.issue_access_token(user)
    clock["value"] = _SIGNED_IN_AT + timedelta(minutes=30)

    refreshed = service.issue_access_token(
        user, auth_time=service.verify_access_token(first.token).auth_time
    )
    claims = service.verify_access_token(refreshed.token)

    assert claims.session_epoch == 3
    assert claims.auth_time == _SIGNED_IN_AT
    # A fresh 60-minute token would run to +90 min; the session ends at +60.
    assert refreshed.expires_at == _SIGNED_IN_AT + timedelta(hours=1)


def test_issuing_past_the_absolute_lifetime_is_refused(clock) -> None:
    service = TokenService("secret", ttl_minutes=60, max_session_age_hours=2)
    clock["value"] = _SIGNED_IN_AT + timedelta(hours=2)

    with pytest.raises(AuthError, match="maximum lifetime"):
        service.issue_access_token(_user(), auth_time=_SIGNED_IN_AT)


def test_verification_enforces_a_lowered_absolute_lifetime(clock) -> None:
    long_lived = TokenService("secret", ttl_minutes=600, max_session_age_hours=24)
    token = long_lived.issue_access_token(_user()).token
    clock["value"] = _SIGNED_IN_AT + timedelta(hours=3)

    with pytest.raises(AuthError, match="maximum lifetime"):
        TokenService("secret", ttl_minutes=60, max_session_age_hours=2).verify_access_token(token)


def test_tokens_without_session_claims_are_rejected(clock) -> None:
    service = TokenService("secret", ttl_minutes=60)
    header = _b64url_encode_json({"alg": "HS256", "typ": "JWT"})
    payload = _b64url_encode_json(
        {
            "sub": str(uuid4()),
            "role": "editor",
            "iat": int(_SIGNED_IN_AT.timestamp()),
            "exp": int((_SIGNED_IN_AT + timedelta(minutes=30)).timestamp()),
        }
    )
    signature = _b64url_encode(service._sign(f"{header}.{payload}".encode()))

    with pytest.raises(AuthError, match="Invalid token"):
        service.verify_access_token(f"{header}.{payload}.{signature}")


def test_token_service_rejects_an_invalid_max_session_age() -> None:
    with pytest.raises(ValidationError, match="max_session_age_hours"):
        TokenService("secret", ttl_minutes=60, max_session_age_hours=0)
    with pytest.raises(ValidationError, match="max_session_age_hours"):
        TokenService("secret", ttl_minutes=180, max_session_age_hours=2)


def test_resolve_session_user_rejects_a_bumped_session_epoch(clock) -> None:
    auth_service = AuthService()
    token_service = TokenService("secret", ttl_minutes=60)
    user = auth_service.register_user("sam", "secret", Role.EDITOR)
    token = token_service.issue_access_token(user).token
    claims, resolved = resolve_session_user(
        token, token_service=token_service, auth_service=auth_service
    )
    assert resolved.user_id == user.user_id
    assert claims.session_epoch == 0

    auth_service.revoke_sessions(user.user_id)

    with pytest.raises(AuthError, match="Session has been revoked"):
        resolve_session_user(token, token_service=token_service, auth_service=auth_service)


def test_resolve_session_user_rejects_an_unknown_user(clock) -> None:
    token_service = TokenService("secret", ttl_minutes=60)
    token = token_service.issue_access_token(_user()).token

    with pytest.raises(AuthError, match="Invalid token"):
        resolve_session_user(token, token_service=token_service, auth_service=AuthService())
