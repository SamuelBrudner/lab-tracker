"""Unit tests for lpat_ personal access tokens."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from lab_tracker.auth import (
    LPAT_TOKEN_PREFIX,
    PERSONAL_ACCESS_TOKEN_MAX_TTL,
    AuthService,
    PersonalAccessTokenService,
    Role,
    _as_utc,
    service_principal_can_access,
    utc_now,
)
from lab_tracker.db_models import PersonalAccessTokenModel
from lab_tracker.errors import NotFoundError, ValidationError


@pytest.fixture()
def session_factory(migrated_sqlite_database_url: str) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        migrated_sqlite_database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        yield factory
    finally:
        engine.dispose()


def _services(
    session_factory: sessionmaker[Session],
) -> tuple[AuthService, PersonalAccessTokenService]:
    return AuthService(session_factory=session_factory), PersonalAccessTokenService(
        session_factory=session_factory
    )


def test_issue_token_returns_secret_once_and_hashes_storage(session_factory):
    auth_service, pat_service = _services(session_factory)
    user = auth_service.register_user("viewer", "secret", Role.VIEWER)

    issued = pat_service.issue_token(
        user,
        label="Copilot",
        role=Role.ADMIN,
        read_only=True,
        expires_at=utc_now() + timedelta(days=7),
    )

    assert issued.secret.startswith(LPAT_TOKEN_PREFIX)
    assert issued.token.role == Role.VIEWER
    assert issued.token.read_only is True
    with session_factory() as session:
        row = session.scalar(select(PersonalAccessTokenModel))
        assert row is not None
        assert row.token_hash != issued.secret
        assert len(row.token_hash) == 64


def test_issue_token_rejects_invalid_ttl_and_unknown_user(session_factory):
    auth_service, pat_service = _services(session_factory)
    user = auth_service.register_user("admin", "secret", Role.ADMIN)

    with pytest.raises(ValidationError, match="future"):
        pat_service.issue_token(
            user,
            label="expired",
            role=Role.VIEWER,
            expires_at=utc_now() - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError, match="maximum"):
        pat_service.issue_token(
            user,
            label="too long",
            role=Role.VIEWER,
            expires_at=utc_now() + PERSONAL_ACCESS_TOKEN_MAX_TTL + timedelta(seconds=5),
        )
    with pytest.raises(NotFoundError):
        pat_service.issue_token(
            user.__class__(
                user_id=uuid4(),
                username="missing",
                password_hash="x",
                role=Role.ADMIN,
            ),
            label="missing",
            role=Role.ADMIN,
            expires_at=utc_now() + timedelta(days=1),
        )


def test_verify_token_returns_capped_role_and_throttles_last_used(session_factory):
    auth_service, pat_service = _services(session_factory)
    user = auth_service.register_user("editor", "secret", Role.EDITOR)
    issued = pat_service.issue_token(
        user,
        label="Notebook",
        role=Role.EDITOR,
        read_only=False,
        expires_at=utc_now() + timedelta(days=1),
    )

    principal = pat_service.verify_token(issued.secret)

    assert principal is not None
    assert principal.user_id == user.user_id
    assert principal.role == Role.EDITOR
    assert principal.read_only is False

    recent_last_used_at = utc_now() - timedelta(minutes=1)
    with session_factory() as session:
        row = session.get(PersonalAccessTokenModel, str(issued.token.token_id))
        assert row is not None
        row.last_used_at = recent_last_used_at
        session.commit()

    assert pat_service.verify_token(issued.secret) is not None
    with session_factory() as session:
        row = session.get(PersonalAccessTokenModel, str(issued.token.token_id))
        assert row is not None
        assert _as_utc(row.last_used_at) == recent_last_used_at


def test_verify_token_rejects_revoked_expired_unknown_and_malformed(session_factory):
    auth_service, pat_service = _services(session_factory)
    user = auth_service.register_user("admin", "secret", Role.ADMIN)
    issued = pat_service.issue_token(
        user,
        label="Copilot",
        role=Role.VIEWER,
        expires_at=utc_now() + timedelta(days=1),
    )

    assert pat_service.verify_token(None) is None
    assert pat_service.verify_token("") is None
    assert pat_service.verify_token("not-a-token") is None
    assert pat_service.verify_token(f"{LPAT_TOKEN_PREFIX}missing") is None

    pat_service.revoke_token(user.user_id, issued.token.token_id)
    assert pat_service.verify_token(issued.secret) is None

    expired = pat_service.issue_token(
        user,
        label="Expired soon",
        role=Role.VIEWER,
        expires_at=utc_now() + timedelta(minutes=1),
    )
    with session_factory() as session:
        row = session.get(PersonalAccessTokenModel, str(expired.token.token_id))
        assert row is not None
        row.expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
    assert pat_service.verify_token(expired.secret) is None


def test_list_and_revoke_are_user_scoped(session_factory):
    auth_service, pat_service = _services(session_factory)
    owner = auth_service.register_user("owner", "secret", Role.ADMIN)
    stranger = auth_service.register_user("stranger", "secret", Role.ADMIN)
    issued = pat_service.issue_token(
        owner,
        label="Owner token",
        role=Role.ADMIN,
        expires_at=utc_now() + timedelta(days=1),
    )

    assert [token.token_id for token in pat_service.list_tokens(owner.user_id)] == [
        issued.token.token_id
    ]
    assert pat_service.list_tokens(stranger.user_id) == []
    with pytest.raises(NotFoundError):
        pat_service.revoke_token(stranger.user_id, issued.token.token_id)


def test_service_principal_policy_is_read_only_by_default():
    assert service_principal_can_access(
        "GET", "/projects", read_only=True, role=Role.VIEWER
    )
    assert service_principal_can_access(
        "GET", "/auth/me", read_only=True, role=Role.ADMIN
    )
    assert not service_principal_can_access(
        "POST", "/projects", read_only=True, role=Role.ADMIN
    )
    assert service_principal_can_access(
        "POST", "/batches/run-due", read_only=True, role=Role.ADMIN
    )
    assert not service_principal_can_access(
        "POST", "/batches/run-due", read_only=True, role=Role.VIEWER
    )
    assert not service_principal_can_access(
        "POST", "/projects", read_only=False, role=Role.VIEWER
    )
    assert service_principal_can_access(
        "POST", "/projects", read_only=False, role=Role.EDITOR
    )


def uuid_from(value: object) -> UUID:
    return UUID(str(value))
