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
from lab_tracker.graph_draft_triage import TRIAGE_CLARIFICATIONS_KEY, clarification_id
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
def mail_client(request):
    client_fixture, admin_fixture = request.param
    return request.getfixturevalue(client_fixture), request.getfixturevalue(admin_fixture)


@pytest.fixture
def assigned_mail(mail_client):
    return _prepare_assigned_mail(*mail_client, draft_client_factory=_BatchDraftClient)


def _prepare_assigned_mail(
    client, admin_auth_headers, *, draft_client_factory, bench_session=False
):
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
    targets = []
    if bench_session:
        response = client.post(
            "/sessions",
            headers=headers,
            json={
                "project_id": project_id,
                "session_type": "operational",
            },
        )
        assert response.status_code == 201, response.text
        targets.append(
            {"entity_type": "session", "entity_id": response.json()["data"]["session_id"]}
        )
        question = client.post(
            "/questions",
            headers=admin_auth_headers,
            json={
                "project_id": project_id,
                "text": "Does recovery change with trial number?",
                "question_type": "descriptive",
                "status": "active",
            },
        )
        assert question.status_code == 201, question.text
    for index in range(2 if bench_session else 1):
        note = client.post(
            "/notes",
            headers=headers,
            json={
                "project_id": project_id,
                "raw_content": f"Recorded during today's session ({index})",
                "status": "staged",
                "targets": targets,
            },
        )
        assert note.status_code == 201, note.text
    with state.db_session_factory() as session:
        api = _background_api(client.app, session)
        run = api.run_graph_draft_batch_for_project(
            UUID(project_id),
            draft_client=draft_client_factory(project_id),
            actor=AuthContext(user_id=user_id, role=Role.EDITOR),
        )
        assert run.change_set_id
        draft = api.get_graph_change_set(run.change_set_id)
        invitations = api.review_emails.list()
        assert {d.operation_id for d in invitations if d.event_type == "proposal"} == {
            op.operation_id for op in draft.operations
        }
        assert all(d.destination_email == SCIENTIST for d in invitations)
        api.review_emails.enqueue_ready_review(draft)
        assert len(api.review_emails.list()) == len(invitations)
    first_operation = draft.operations[0].operation_id if draft.operations else None
    return client, headers, draft.change_set_id, first_operation


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


_INTENT = "Which question was today's recording intended to address?"
_PLACEMENT = "Which rig was used for today's recording?"


class _ClarificationOnlyClient(_BatchDraftClient):
    def draft_from_batch(self, **kwargs):
        return {
            "summary": "Clarification needed before making a proposal.",
            "uncertain_fields": [],
            "clarification_requests": [_INTENT, _PLACEMENT, _INTENT],
            "operations": [],
        }


@pytest.fixture
def clarification_mail(mail_client):
    client, headers, draft_id, operation_id = _prepare_assigned_mail(
        *mail_client, draft_client_factory=_ClarificationOnlyClient
    )
    assert operation_id is None
    messages = _send(client)
    assert len(messages) == 2
    return (
        client,
        headers,
        draft_id,
        {
            prompt: next(message for message in messages if prompt in message.get_content())
            for prompt in (_INTENT, _PLACEMENT)
        },
    )


def test_clarification_only_batch_answers_and_defers_from_inbox(clarification_mail):
    client, _, draft_id, prompts = clarification_mail
    intent = prompts[_INTENT]
    assert _reply(client, intent, "Approve")["rejected"] == 1
    [help_email] = _send(client)
    assert "Reply with Answer:" in help_email.get_content()
    assert (
        _reply(client, intent, "Answer: Calibration", sender="another@example.edu")["rejected"] == 1
    )
    assert _reply(client, intent, "Answer: Calibration")["reviews_clarification_answered"] == 1
    assert _reply(client, intent, "Answer: Assess recovery")["rejected"] == 1
    assert _reply(client, prompts[_PLACEMENT], "Later")["reviews_clarification_deferred"] == 1
    receipts = _send(client)
    assert {message["In-Reply-To"] for message in receipts} == {
        message["Message-ID"] for message in prompts.values()
    }
    with client.app.state.db_session_factory() as session:
        draft = _background_api(client.app, session).get_graph_change_set(draft_id)
        answers = draft.review_metadata[TRIAGE_CLARIFICATIONS_KEY]
        assert answers[clarification_id(_INTENT)]["answer"] == "Calibration"
        assert answers[clarification_id(_INTENT)]["reviewed_by"] == str(
            draft.review_assignee_user_id
        )
        assert answers[clarification_id(_PLACEMENT)]["status"] == "deferred"
        assert draft.operations == []


def test_mail_cannot_override_web_answer_or_resume(clarification_mail):
    client, headers, draft_id, prompts = clarification_mail
    endpoint = f"/graph-drafts/{draft_id}/triage-clarification"
    answer = client.post(
        endpoint,
        headers=headers,
        json={"question": _INTENT, "decision": "answered", "answer": "Q2"},
    )
    assert answer.status_code == 200, answer.text
    assert _reply(client, prompts[_INTENT], "Answer: Q3")["rejected"] == 1
    resumed = client.post(
        endpoint, headers=headers, json={"question": _INTENT, "decision": "resume"}
    )
    assert resumed.status_code == 200, resumed.text
    assert _reply(client, prompts[_INTENT], "Answer: Q3")["rejected"] == 1
    [new_prompt] = _send(client)
    assert _INTENT in new_prompt.get_content()
    assert new_prompt["Reply-To"] != prompts[_INTENT]["Reply-To"]
    assert _reply(client, new_prompt, "Answer: Q2")["reviews_clarification_answered"] == 1


class _ProposalAndClarificationClient(_BatchDraftClient):
    def draft_from_batch(self, **kwargs):
        result = super().draft_from_batch(**kwargs)
        result["clarification_requests"] = [_INTENT]
        return result


def test_partial_commit_carries_unanswered_clarification_to_new_mail(mail_client):
    client, _, draft_id, _ = _prepare_assigned_mail(
        *mail_client, draft_client_factory=_ProposalAndClarificationClient
    )
    sent = _send(client)
    question = next(message for message in sent if _INTENT in message.get_content())
    proposal = next(
        message for message in sent if "Private proposed question" in message.get_content()
    )
    assert _reply(client, proposal, "Approve")["reviews_approved"] == 1
    response = client.post(
        f"/graph-drafts/{draft_id}/commit",
        headers=mail_client[1],
        json={"message": "Keep the approved proposal"},
    )
    assert response.status_code == 200, response.text
    carryover_id = response.json()["data"]["review_metadata"]["review_triage_carryover_to"]
    assert _reply(client, question, "Answer: Calibration")["rejected"] == 1
    [new_prompt] = [mail for mail in _send(client) if _INTENT in mail.get_content()]
    assert new_prompt["Reply-To"] != question["Reply-To"]
    assert _reply(client, new_prompt, "Answer: Calibration")["reviews_clarification_answered"] == 1
    with client.app.state.db_session_factory() as session:
        draft = _background_api(client.app, session).get_graph_change_set(UUID(carryover_id))
        assert (
            draft.review_metadata[TRIAGE_CLARIFICATIONS_KEY][clarification_id(_INTENT)]["answer"]
            == "Calibration"
        )


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("Question 1", "question"), ("Calibration", "calibration"), ("Exploratory", "exploratory")],
)
def test_session_intent_is_confirmed_once_from_the_verified_inbox(mail_client, answer, expected):
    client, headers, draft_id, _ = _prepare_assigned_mail(
        *mail_client, draft_client_factory=_ClarificationOnlyClient, bench_session=True
    )
    [prompt] = [
        message for message in _send(client) if "Which question, if any" in message.get_content()
    ]
    assert "Question 1. Does recovery change" in prompt.get_content()
    assert _reply(client, prompt, f"Answer: {answer}")["reviews_clarification_answered"] == 1
    with client.app.state.db_session_factory() as session:
        api = _background_api(client.app, session)
        draft = api.get_graph_change_set(draft_id)
        packets = draft.review_metadata["work_intent_prompts"]
        assert len(packets) == 1
        session_id = next(iter(packets.values()))["session_id"]
    read = client.get(f"/work-intent/session/{session_id}", headers=headers)
    assert read.status_code == 200, read.text
    intent = read.json()["data"]["intent"]
    assert intent["kind"] == expected
    assert intent["source"] == "confirmed_in_review"
    assert intent["recorded_by_user_id"] == str(draft.review_assignee_user_id)
    assert intent["review_draft_id"] == str(draft_id)
    assert bool(intent["question_id"]) == (expected == "question")
    assert _reply(client, prompt, "Answer: Maintenance")["rejected"] == 1
    with client.app.state.db_session_factory() as session:
        api = _background_api(client.app, session)
        capture = api.get_note(draft.source_note_ids[0])
        packet = api.build_batch_graph_context([capture])
        item = packet["batch_notes"][0]
        assert item["work_intent"]["kind"] == expected
        assert item["work_intent"]["source"] == "confirmed_in_review"
        assert item["work_intent"]["work_id"] == session_id


def test_later_capture_does_not_repeat_a_pending_session_purpose_prompt(mail_client):
    client, headers, draft_id, _ = _prepare_assigned_mail(
        *mail_client, draft_client_factory=_ClarificationOnlyClient, bench_session=True
    )
    [orig] = [
        message for message in _send(client) if "Which question, if any" in message.get_content()
    ]
    with client.app.state.db_session_factory() as session:
        draft = _background_api(client.app, session).get_graph_change_set(draft_id)
        session_id = next(iter(draft.review_metadata["work_intent_prompts"].values()))["session_id"]
    capture = client.post(
        "/notes",
        headers=headers,
        json={
            "project_id": str(draft.project_id),
            "status": "staged",
            "raw_content": "Additional recording from the same bench session",
            "targets": [{"entity_type": "session", "entity_id": session_id}],
        },
    )
    assert capture.status_code == 201, capture.text
    with client.app.state.db_session_factory() as session:
        api = _background_api(client.app, session)
        assert draft.review_assignee_user_id is not None
        run = api.run_graph_draft_batch_for_project(
            draft.project_id,
            actor=AuthContext(draft.review_assignee_user_id, Role.EDITOR),
            draft_client=_ClarificationOnlyClient(str(draft.project_id)),
        )
        assert run.change_set_id and run.change_set_id != draft_id
        followup = api.get_graph_change_set(run.change_set_id)
        assert "work_intent_prompts" not in followup.review_metadata
    assert not [
        message for message in _send(client) if "Which question, if any" in message.get_content()
    ]
    assert _reply(client, orig, "Answer: Calibration")["reviews_clarification_answered"] == 1
