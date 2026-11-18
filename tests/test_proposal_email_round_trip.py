"""Exercise enrollment, proposal generation, SMTP worker and a reply through IMAP."""

from datetime import timedelta
from email.message import EmailMessage
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import update
from test_review_email_delivery import _BatchDraftClient, _project_and_note
from test_review_mailbox import _challenge, _confirm, _email_user
from test_server_capture_channels import FakeImap, _poll_email

from lab_tracker.app_parts.runtime import _background_api, _process_one_review_email
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import ReviewEmailOutboxModel
from lab_tracker.models import GraphChangeOperationStatus, utc_now

SCIENTIST = "researcher@example.edu"


@pytest.fixture(
    params=[
        pytest.param(("client", "admin_auth_headers"), id="sqlite"),
        pytest.param(
            ("postgres_client", "postgres_admin_auth_headers"),
            id="postgres",
            marks=pytest.mark.postgres,
        ),
    ]
)
def assigned_mail(request):
    client_fixture, admin_fixture = request.param
    client = request.getfixturevalue(client_fixture)
    admin_auth_headers = request.getfixturevalue(admin_fixture)
    state = client.app.state
    settings = state.settings
    settings.review_email_enabled = True
    settings.review_email_transport = "smtp"
    settings.review_email_reply_address = "review@lab.example"
    settings.review_email_authserv_id = "mx.lab.example"
    settings.email_capture_imap_host = "imap.lab.example"
    settings.email_capture_imap_username = "review"
    settings.email_capture_imap_password = "fixture"
    settings.base_url = "https://lab.example"
    provider = state.review_email_provider = Mock()
    user_id, headers = _email_user(client, SCIENTIST)
    project_id, _ = _project_and_note(client, admin_auth_headers)
    added = client.post(
        f"/projects/{project_id}/members",
        headers=admin_auth_headers,
        json={"username": SCIENTIST, "role": "contributor"},
    )
    assert added.status_code == 201, added.text
    assert _challenge(client, headers).status_code == 200
    assert (
        _confirm(client, headers, provider.send_verification_code.call_args.args[1]).status_code
        == 200
    )
    note = client.post(
        "/notes",
        headers=headers,
        json={
            "project_id": project_id,
            "raw_content": "Recorded during today's session",
            "status": "staged",
        },
    )
    assert note.status_code == 201
    with state.db_session_factory() as session:
        api = _background_api(client.app, session)
        run = api.run_graph_draft_batch_for_project(
            UUID(project_id),
            draft_client=_BatchDraftClient(project_id),
            actor=AuthContext(user_id=user_id, role=Role.EDITOR),
        )
        assert run.change_set_id
        draft = api.get_graph_change_set(run.change_set_id)
        invitations = api.review_emails.list()
        assert {d.operation_id for d in invitations} == {op.operation_id for op in draft.operations}
        assert all(d.destination_email == SCIENTIST for d in invitations)
        api.review_emails.enqueue_ready_review(draft)
        assert len(api.review_emails.list()) == len(invitations)
    return client, headers, draft.change_set_id, draft.operations[0].operation_id


def _send(client):
    provider = client.app.state.review_email_provider
    provider.send_message.reset_mock()
    while _process_one_review_email(client.app):
        pass
    return [call.args[0] for call in provider.send_message.call_args_list]


def _reply(client, proposal, text, *, sender=SCIENTIST):
    message = EmailMessage()
    message["To"] = proposal["Reply-To"]
    message["From"] = sender
    message["Subject"] = "Re: Lab Tracker proposal for your review"
    message["In-Reply-To"] = proposal["Message-ID"]
    message["Authentication-Results"] = "mx.lab.example; dmarc=pass header.from=example.edu"
    message.set_content(text + "\n\nOn Wednesday, Lab Tracker wrote:\n> Approve\n")
    mailbox = FakeImap({"1": message.as_bytes()})
    report = _poll_email(client, mailbox)
    assert not report["errors"], report
    return report["counts"]


def test_correct_review_then_approve_by_reply(assigned_mail):
    client, _, draft_id, operation_id = assigned_mail
    sent = _send(client)
    [original] = [mail for mail in sent if "Private proposed question" in mail.get_content()]
    assert original["To"] == SCIENTIST
    assert "Correct text:" in original.get_content()
    assert len(str(original["Reply-To"]).split("@")[0]) <= 64
    assert _reply(client, original, "Approve", sender="another@example.edu")["rejected"] == 1
    assert _reply(client, original, "Correct unknown: A revision")["rejected"] == 1
    assert _reply(client, original, "Yes, probably")["rejected"] == 1
    [help_email] = _send(client)
    assert help_email["Reply-To"] == original["Reply-To"]
    assert help_email["In-Reply-To"] == original["Message-ID"]
    assert "did not change the proposal" in help_email.get_content()
    assert (
        _reply(client, original, "Correct text: Which trial used this condition?")[
            "reviews_corrected"
        ]
        == 1
    )
    assert _reply(client, original, "Approve")["rejected"] == 1
    [updated] = [
        mail for mail in _send(client) if "Which trial used this condition?" in mail.get_content()
    ]
    assert updated["Subject"] == original["Subject"]
    assert updated["Reply-To"] != original["Reply-To"]
    assert updated["In-Reply-To"] == original["Message-ID"]
    assert _reply(client, updated, "Approve")["reviews_approved"] == 1
    assert _reply(client, updated, "Approve")["rejected"] == 1
    [receipt] = _send(client)
    assert receipt["In-Reply-To"] == updated["Message-ID"]
    assert "approval was recorded" in receipt.get_content()
    with client.app.state.db_session_factory() as session:
        api = _background_api(client.app, session)
        draft = api.get_graph_change_set(draft_id)
        operation = next(op for op in draft.operations if op.operation_id == operation_id)
        assert operation.status == GraphChangeOperationStatus.ACCEPTED
        assert operation.accepted_by_user_id == draft.review_assignee_user_id
        assert operation.payload["text"] == "Which trial used this condition?"
        assert operation.result_entity_id is None
        assert sorted(d.reply_outcome for d in api.review_emails.list() if d.replied_at) == [
            "approved",
            "corrected",
        ]


def test_revoking_enrollment_invalidates_old_reply(assigned_mail):
    client, headers, _, _ = assigned_mail
    sent = _send(client)
    assert client.delete("/auth/review-mailbox", headers=headers).status_code == 200
    assert _reply(client, sent[0], "Approve")["rejected"] == 1
    assert _challenge(client, headers).status_code == 200
    provider = client.app.state.review_email_provider
    assert (
        _confirm(client, headers, provider.send_verification_code.call_args.args[1]).status_code
        == 200
    )
    assert _reply(client, sent[0], "Approve")["rejected"] == 1


def test_expired_proposal_is_not_sent(assigned_mail):
    client, _, _, _ = assigned_mail
    with client.app.state.db_session_factory() as session:
        session.execute(
            update(ReviewEmailOutboxModel).values(reply_expires_at=utc_now() - timedelta(seconds=1))
        )
        session.commit()
    assert _send(client) == []


def test_freeform_correction_is_returned_for_revision(assigned_mail):
    client, _, draft_id, operation_id = assigned_mail
    original = next(
        mail for mail in _send(client) if "Private proposed question" in mail.get_content()
    )
    reply = "Correct: Link the recording to the recovery question instead."
    assert _reply(client, original, reply)["reviews_correction_requested"] == 1
    [receipt] = _send(client)
    assert "set aside for revision" in receipt.get_content()
    with client.app.state.db_session_factory() as session:
        draft = _background_api(client.app, session).get_graph_change_set(draft_id)
        proposal = next(op for op in draft.operations if op.operation_id == operation_id)
        assert proposal.review_note == "Link the recording to the recovery question instead."
        assert proposal.deferred_at is not None


def test_revoking_before_dispatch_cancels_queued_proposals(assigned_mail):
    client, headers, _, _ = assigned_mail
    assert client.delete("/auth/review-mailbox", headers=headers).status_code == 200
    assert _send(client) == []
