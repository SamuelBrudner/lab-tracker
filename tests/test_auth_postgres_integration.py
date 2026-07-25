from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from lab_tracker.auth import AuthService, InvitationTokenService, Role
from lab_tracker.db import Base
from lab_tracker.errors import AuthError, ConflictError

POSTGRES_URL = os.getenv("LAB_TRACKER_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="LAB_TRACKER_TEST_POSTGRES_URL is required for Postgres integration.",
)


@pytest.fixture
def postgres_auth_services():
    url = make_url(POSTGRES_URL)
    if url.get_backend_name() != "postgresql" or url.database != "lab_tracker_invite_test":
        pytest.fail(
            "Postgres auth integration requires a disposable database named "
            "lab_tracker_invite_test."
        )
    engine = create_engine(POSTGRES_URL, future=True, pool_pre_ping=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield AuthService(factory), InvitationTokenService(
            "postgres-invitation-integration-secret",
            session_factory=factory,
        )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_postgres_invitation_atomicity_and_concurrency(
    postgres_auth_services,
    monkeypatch,
) -> None:
    auth_service, invitation_service = postgres_auth_services
    issued = invitation_service.issue_invitation(
        email="rollback@example.org",
        role=Role.ADMIN,
    )
    monkeypatch.setattr(
        invitation_service,
        "_claim_persistent_invitation",
        lambda _session, **_kwargs: False,
    )

    with pytest.raises(AuthError, match="no longer available"):
        auth_service.register_invited_user(
            invitation_token_service=invitation_service,
            invite_token=issued.token,
            username="rollback@example.org",
            password="long-enough-secret",
            password_confirmation="long-enough-secret",
        )

    assert auth_service.get_user("rollback@example.org") is None
    rollback_invitation = next(
        invitation
        for invitation in invitation_service.list_invitations()
        if invitation.email == "rollback@example.org"
    )
    assert rollback_invitation.status == "pending"

    monkeypatch.undo()
    race_invitation = invitation_service.issue_invitation(
        email="race@example.org",
        role=Role.ADMIN,
    )
    barrier = threading.Barrier(2)

    def accept() -> tuple[str, str]:
        barrier.wait(timeout=10)
        try:
            user = auth_service.register_invited_user(
                invitation_token_service=invitation_service,
                invite_token=race_invitation.token,
                username="race@example.org",
                password="long-enough-secret",
                password_confirmation="long-enough-secret",
            )
        except (AuthError, ConflictError) as exc:
            return ("error", str(exc))
        return ("accepted", str(user.user_id))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: accept(), range(2)))

    accepted = [value for kind, value in results if kind == "accepted"]
    errors = [value for kind, value in results if kind == "error"]
    assert len(accepted) == 1
    assert len(errors) == 1
    user = auth_service.get_user("race@example.org")
    invitation = next(
        item
        for item in invitation_service.list_invitations()
        if item.email == "race@example.org"
    )
    assert user is not None
    assert invitation.status == "consumed"
    assert str(invitation.consumed_by_user_id) == accepted[0]
