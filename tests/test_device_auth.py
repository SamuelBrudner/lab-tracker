"""Unit tests for the DeviceAuthService persistence layer (lab-tracker-bbd)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from lab_tracker.auth import (
    DEVICE_TOKEN_PREFIX,
    ENROLLMENT_OFFER_PREFIX,
    AuthError,
    DeviceAuthService,
    Role,
    _as_utc,
    utc_now,
)
from lab_tracker.db_models import DeviceEnrollmentModel, DeviceTokenModel, UserModel
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


def _create_user(session_factory: sessionmaker[Session]) -> str:
    user_id = str(uuid4())
    with session_factory() as session:
        session.add(
            UserModel(
                user_id=user_id,
                username=f"user-{user_id[:8]}",
                password_hash="pbkdf2$1$salt$hash",
                role=Role.ADMIN.value,
                created_at=utc_now(),
            )
        )
        session.commit()
    return user_id


def test_create_enrollment_returns_short_lived_offer(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    user_id = _create_user(session_factory)

    offer = service.create_enrollment(uuid_from(user_id), ttl_minutes=5)

    assert offer.offer_token.startswith(ENROLLMENT_OFFER_PREFIX)
    assert offer.expires_at - utc_now() <= timedelta(minutes=5, seconds=2)
    assert offer.expires_at - utc_now() >= timedelta(minutes=4, seconds=55)
    assert offer.user_id == uuid_from(user_id)


def test_create_enrollment_rejects_invalid_inputs(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    user_id = _create_user(session_factory)

    with pytest.raises(ValidationError, match="TTL"):
        service.create_enrollment(uuid_from(user_id), ttl_minutes=0)
    with pytest.raises(NotFoundError):
        service.create_enrollment(uuid_from(str(uuid4())))


def test_consume_enrollment_issues_device_token_and_marks_offer_consumed(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    user_id = _create_user(session_factory)
    offer = service.create_enrollment(uuid_from(user_id))

    issued = service.consume_enrollment(offer.offer_token, label="iPhone 14")

    assert issued.secret.startswith(DEVICE_TOKEN_PREFIX)
    assert issued.device_token.label == "iPhone 14"
    assert issued.device_token.user_id == uuid_from(user_id)
    assert issued.device_token.revoked is False

    # The enrollment row is now consumed and linked to the device token.
    with session_factory() as session:
        row = session.get(DeviceEnrollmentModel, str(offer.enrollment_id))
        assert row is not None
        assert row.consumed_at is not None
        assert row.consumed_device_token_id == str(issued.device_token.device_token_id)


def test_consume_enrollment_rejects_expired_offer(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    user_id = _create_user(session_factory)
    offer = service.create_enrollment(uuid_from(user_id), ttl_minutes=1)

    with session_factory() as session:
        row = session.get(DeviceEnrollmentModel, str(offer.enrollment_id))
        row.expires_at = utc_now() - timedelta(seconds=1)
        session.commit()

    with pytest.raises(AuthError, match="expired"):
        service.consume_enrollment(offer.offer_token, label="iPhone")


def test_consume_enrollment_is_single_use(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    user_id = _create_user(session_factory)
    offer = service.create_enrollment(uuid_from(user_id))

    service.consume_enrollment(offer.offer_token, label="First")
    with pytest.raises(AuthError, match="already been consumed"):
        service.consume_enrollment(offer.offer_token, label="Second")


def test_consume_enrollment_rejects_unknown_or_malformed_offers(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    _create_user(session_factory)

    with pytest.raises(AuthError, match="Unrecognized"):
        service.consume_enrollment("not-a-pair-token", label="x")
    with pytest.raises(AuthError, match="invalid"):
        service.consume_enrollment(f"{ENROLLMENT_OFFER_PREFIX}does-not-exist", label="x")
    with pytest.raises(AuthError, match="empty"):
        service.consume_enrollment("", label="x")
    with pytest.raises(ValidationError):
        service.consume_enrollment(f"{ENROLLMENT_OFFER_PREFIX}xx", label="")


def test_list_devices_returns_all_including_revoked(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    user_id = _create_user(session_factory)
    issued_a = service.consume_enrollment(
        service.create_enrollment(uuid_from(user_id)).offer_token,
        label="A",
    )
    issued_b = service.consume_enrollment(
        service.create_enrollment(uuid_from(user_id)).offer_token,
        label="B",
    )
    service.revoke_device(uuid_from(user_id), issued_a.device_token.device_token_id)

    devices = service.list_devices(uuid_from(user_id))
    by_label = {device.label: device for device in devices}
    assert set(by_label) == {"A", "B"}
    assert by_label["A"].revoked is True
    assert by_label["B"].revoked is False
    assert issued_b.device_token.device_token_id == by_label["B"].device_token_id


def test_revoke_device_rejects_other_users_tokens(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    owner_id = _create_user(session_factory)
    stranger_id = _create_user(session_factory)
    issued = service.consume_enrollment(
        service.create_enrollment(uuid_from(owner_id)).offer_token,
        label="owner-phone",
    )

    with pytest.raises(NotFoundError):
        service.revoke_device(uuid_from(stranger_id), issued.device_token.device_token_id)


def test_verify_device_token_returns_principal_for_valid_token(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    user_id = _create_user(session_factory)
    issued = service.consume_enrollment(
        service.create_enrollment(uuid_from(user_id)).offer_token,
        label="iPhone",
    )

    principal = service.verify_device_token(issued.secret)

    assert principal is not None
    assert principal.user_id == uuid_from(user_id)
    assert principal.device_token_id == issued.device_token.device_token_id
    assert principal.label == "iPhone"

    devices_after = service.list_devices(uuid_from(user_id))
    matching = next(d for d in devices_after if d.device_token_id == principal.device_token_id)
    assert matching.last_used_at is not None


def test_verify_device_token_throttles_recent_last_used_writes(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    user_id = _create_user(session_factory)
    issued = service.consume_enrollment(
        service.create_enrollment(uuid_from(user_id)).offer_token,
        label="iPhone",
    )
    recent_last_used_at = utc_now() - timedelta(minutes=1)
    with session_factory() as session:
        row = session.get(DeviceTokenModel, str(issued.device_token.device_token_id))
        assert row is not None
        row.last_used_at = recent_last_used_at
        session.commit()

    principal = service.verify_device_token(issued.secret)

    assert principal is not None
    with session_factory() as session:
        row = session.get(DeviceTokenModel, str(issued.device_token.device_token_id))
        assert row is not None
        assert _as_utc(row.last_used_at) == recent_last_used_at


def test_verify_device_token_rejects_revoked_unknown_and_malformed(session_factory):
    service = DeviceAuthService(session_factory=session_factory)
    user_id = _create_user(session_factory)
    issued = service.consume_enrollment(
        service.create_enrollment(uuid_from(user_id)).offer_token,
        label="iPhone",
    )

    assert service.verify_device_token(None) is None
    assert service.verify_device_token("") is None
    assert service.verify_device_token("not-a-device-token") is None
    assert service.verify_device_token(f"{DEVICE_TOKEN_PREFIX}does-not-exist") is None

    service.revoke_device(uuid_from(user_id), issued.device_token.device_token_id)
    assert service.verify_device_token(issued.secret) is None


def uuid_from(value):
    from uuid import UUID

    if isinstance(value, UUID):
        return value
    return UUID(str(value))
