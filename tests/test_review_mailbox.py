from datetime import timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import update

from lab_tracker.auth import Role
from lab_tracker.capture_channels.email_capture import parse_email
from lab_tracker.capture_channels.review_replies import route_review_reply
from lab_tracker.capture_channels.settings import CaptureAddress
from lab_tracker.db_models import UserModel
from lab_tracker.models import utc_now
from lab_tracker.review_mailbox import get_verified_review_mailbox

MAILBOX = "/auth/review-mailbox"


@pytest.fixture
def mailbox_sender(client):
    settings = client.app.state.settings
    settings.review_email_enabled = True
    settings.review_email_transport = "smtp"
    settings.review_email_reply_address = "review@lab.example"
    settings.review_email_authserv_id = "mx.lab.example"
    sender = Mock()
    client.app.state.review_email_provider = sender
    return sender


def _email_user(client, login):
    user = client.app.state.auth_service.register_user(login, "secret", Role.EDITOR)
    response = client.post("/auth/login", json={"username": login, "password": "secret"})
    assert response.status_code == 200
    return user.user_id, {"Authorization": "Bearer " + response.json()["data"]["access_token"]}


def _challenge(client, headers, **payload):
    return client.post(MAILBOX + "/challenge", headers=headers, json=payload)


def _confirm(client, headers, code):
    return client.post(MAILBOX + "/confirm", headers=headers, json={"code": code})


def test_verify_only_own_login_email_and_opt_out(client, mailbox_sender, viewer_user):
    address = "scientist@example.edu"
    user_id, headers = _email_user(client, address)
    assert client.get(MAILBOX, headers=headers).json()["data"] == {
        "available": True,
        "login_email": address,
        "enabled": False,
        "pending_email": None,
    }
    assert _challenge(client, headers, login_email="unrelated@example.edu").status_code == 422
    assert _challenge(client, headers).status_code == 200
    recipient, code = mailbox_sender.send_verification_code.call_args.args
    assert recipient == address
    assert _challenge(client, headers).status_code == 429
    assert _confirm(client, viewer_user.headers, code).status_code == 422
    assert _confirm(client, headers, "incorrect").status_code == 422
    assert code not in client.get(MAILBOX, headers=headers).text
    with client.app.state.db_session_factory() as session:
        assert get_verified_review_mailbox(session, user_id) is None
        assert session.get(UserModel, user_id).review_mailbox_challenge_hash != code
    assert _confirm(client, headers, code).json()["data"]["enabled"]
    assert _confirm(client, headers, code).status_code == 422
    with client.app.state.db_session_factory() as session:
        assert get_verified_review_mailbox(session, user_id) == address
    assert not client.delete(MAILBOX, headers=headers).json()["data"]["enabled"]
    with client.app.state.db_session_factory() as session:
        assert get_verified_review_mailbox(session, user_id) is None


def test_legacy_login_adopts_only_a_unique_proved_email(client, mailbox_sender, viewer_user):
    address = "new@example.edu"
    headers = viewer_user.headers
    taken = "existing@example.edu"
    _email_user(client, taken)
    assert _challenge(client, headers, login_email=taken).status_code == 200
    assert (
        _confirm(
            client, headers, mailbox_sender.send_verification_code.call_args.args[1]
        ).status_code
        == 409
    )
    assert client.delete(MAILBOX, headers=headers).status_code == 200
    assert _challenge(client, headers, login_email=address).status_code == 200
    assert (
        client.get("/auth/me", headers=headers).json()["data"]["username"] == viewer_user.username
    )
    assert (
        _confirm(
            client, headers, mailbox_sender.send_verification_code.call_args.args[1]
        ).status_code
        == 200
    )
    login = client.post("/auth/login", json={"username": address, "password": "secret"})
    assert login.json()["data"]["user"]["user_id"] == viewer_user.user_id
    assert (
        client.post(
            "/auth/login", json={"username": viewer_user.username, "password": "secret"}
        ).status_code
        == 401
    )


def test_expiry_resend_and_changed_username_invalidate_proof(client, mailbox_sender):
    user_id, headers = _email_user(client, "original@example.edu")
    assert _challenge(client, headers).status_code == 200
    original = mailbox_sender.send_verification_code.call_args.args[1]
    with client.app.state.db_session_factory() as session:
        session.execute(
            update(UserModel)
            .where(UserModel.user_id == user_id)
            .values(review_mailbox_challenge_expires_at=utc_now() - timedelta(seconds=1))
        )
        session.commit()
    assert _confirm(client, headers, original).status_code == 422
    assert _challenge(client, headers).status_code == 200
    fresh = mailbox_sender.send_verification_code.call_args.args[1]
    assert _confirm(client, headers, original).status_code == 422
    assert _confirm(client, headers, fresh).status_code == 200
    with client.app.state.db_session_factory() as session:
        session.execute(
            update(UserModel)
            .where(UserModel.user_id == user_id)
            .values(username="changed@example.edu")
        )
        session.commit()
        assert get_verified_review_mailbox(session, user_id) is None


def test_unconfigured_host_cannot_enroll(client, viewer_user):
    assert client.get(MAILBOX).status_code == 401
    assert not client.get(MAILBOX, headers=viewer_user.headers).json()["data"]["available"]
    assert _challenge(client, viewer_user.headers, login_email="a@example.edu").status_code == 422


@pytest.mark.parametrize("local", ['"bench scientist"', '"bench..scientist"'])
def test_quoted_login_mailbox_can_enroll_and_reply_without_changing_capture(
    client, mailbox_sender, local
):
    address = f"{local}@example.edu"
    _, headers = _email_user(client, address)
    assert _challenge(client, headers).status_code == 200
    recipient, code = mailbox_sender.send_verification_code.call_args.args
    assert recipient == address and _confirm(client, headers, code).json()["data"]["enabled"]
    token = "a" * 52
    raw = (
        f"From: Scientist <{address}>\r\nTo: review+{token}@lab.example\r\n"
        "Authentication-Results: mx.lab.example; dmarc=pass header.from=example.edu\r\n"
        "Subject: Re: proposal\r\nIn-Reply-To: <proposal@lab.example>\r\n\r\nApprove\r\n"
    )
    parsed = parse_email(raw.encode())
    assert parsed.sender is None  # The capture directory only supports simple mailboxes.
    receive = Mock(return_value="approved")
    result = route_review_reply(
        parsed, CaptureAddress("review", "lab.example"), "mx.lab.example", receive
    )
    assert result.status == "approved"
    receive.assert_called_once_with(token, address, "Approve")
