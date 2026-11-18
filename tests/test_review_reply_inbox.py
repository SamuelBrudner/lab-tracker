from email.message import EmailMessage
from unittest.mock import Mock

import pytest

from lab_tracker.capture_channels.email_capture import parse_email
from lab_tracker.capture_channels.review_replies import route_review_reply
from lab_tracker.capture_channels.settings import CaptureAddress

ALIAS = CaptureAddress("review", "lab.example")
TOKEN = "t" * 52
GATEWAY = "mx.lab.example"
PASS = f"{GATEWAY}; spf=pass; dmarc=pass (p=reject) header.from=uni.example"


def message(body: str = "Approve") -> EmailMessage:
    mail = EmailMessage()
    mail["From"] = '"A. Scientist" <A@uni.example>'
    mail["To"] = ALIAS.with_token(TOKEN)
    mail["Subject"] = "Re: Lab Tracker proposal"
    mail["In-Reply-To"] = "<proposal@lab.example>"
    mail["Authentication-Results"] = PASS
    mail.set_content(body)
    return mail


@pytest.mark.parametrize(
    ("body", "authored", "decision"),
    [
        ("Approve\n\nOn Tue, Lab Tracker wrote:\n> Proposal", "Approve", "approved"),
        (
            "Correct text: Measure the first sample.\n\n-- \nA. Scientist",
            "Correct text: Measure the first sample.",
            "corrected",
        ),
        (
            "Correct: Split the observations.\n\n> Proposal",
            "Correct: Split the observations.",
            "correction_requested",
        ),
        ("Approve\n\n-----Original Message-----\nReply Approve", "Approve", "approved"),
        ("Approve\n\nSent from my iPhone\n\n> Original", "Approve", "approved"),
        ("Approve\n\nOn Tue, Lab Tracker\n<team@lab.example> wrote:\n> ...", "Approve", "approved"),
    ],
)
def test_review_reply_supplies_only_top_posted_text(body: str, authored: str, decision: str):
    mail = message(body)
    mail.add_alternative("<blockquote>ignore this alternative</blockquote>", subtype="html")
    receive = Mock(return_value=decision)
    outcome = route_review_reply(parse_email(mail.as_bytes()), ALIAS, GATEWAY, receive)
    assert outcome and outcome.status == decision and outcome.processed
    receive.assert_called_once_with(TOKEN, "a@uni.example", authored)


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("To", "review@lab.example"),
        ("To", f"review+{TOKEN}z@lab.example"),
        ("To", "review+mistyped@lab.example, capture+aaa@lab.example"),
        ("Cc", "colleague@uni.example"),
        ("X-Original-To", f"review+{'u' * 52}@lab.example"),
        ("From", "A@uni.example, B@uni.example"),
        ("In-Reply-To", ""),
        ("Subject", "Re: Fwd: Proposal"),
        ("Auto-Submitted", "auto-replied"),
        ("List-Id", "<list.example>"),
        ("Precedence", "bulk"),
        ("Return-Path", "<>"),
        ("Resent-From", "A@uni.example"),
        ("Authentication-Results", "mx.other.example; dmarc=pass header.from=uni.example"),
        ("Authentication-Results", f"{GATEWAY}; dmarc=pass header.from=other.example"),
        ("Authentication-Results", f"{GATEWAY}; dmarc=fail header.from=uni.example"),
        ("Authentication-Results", f"{GATEWAY}; spf=pass smtp.mailfrom=uni.example"),
        ("Authentication-Results", f"{PASS}; dmarc=fail header.from=uni.example"),
        (
            "Authentication-Results",
            f"{GATEWAY}; spf=fail (hint; dmarc=pass header.from=uni.example )",
        ),
        (
            "Authentication-Results",
            f'{GATEWAY}; spf=fail reason="oops; dmarc=pass header.from=uni.example "',
        ),
    ],
)
def test_unverified_forwarded_automatic_and_misdirected_messages_are_rejected(header, value):
    mail = message()
    del mail[header]
    mail[header] = value
    receive = Mock()
    outcome = route_review_reply(parse_email(mail.as_bytes()), ALIAS, GATEWAY, receive)
    assert outcome and outcome.status == "rejected" and outcome.processed
    receive.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        "> Approve",
        "\nOn Tue, Lab Tracker wrote:\n> Approve",
        "Approve\n> project\nPlease don't use that sample",
        "Approve\nOn Tue, Lab Tracker wrote:\n> old text\nWait, actually correct this",
        "Approve\n\n---------- Forwarded message ---------\nFrom: A",
        "-- \nApprove",
    ],
)
def test_quoted_ambiguous_and_forwarded_bodies_are_rejected(body: str):
    receive = Mock()
    outcome = route_review_reply(parse_email(message(body).as_bytes()), ALIAS, GATEWAY, receive)
    assert outcome and outcome.status == "rejected"
    receive.assert_not_called()


def test_untrusted_header_cannot_override_receivers_failure():
    mail = message()
    del mail["Authentication-Results"]
    mail["Authentication-Results"] = f"{GATEWAY}; dmarc=fail header.from=uni.example"
    mail["Authentication-Results"] = PASS
    receive = Mock()
    outcome = route_review_reply(parse_email(mail.as_bytes()), ALIAS, GATEWAY, receive)
    assert outcome and outcome.status == "rejected"
    receive.assert_not_called()


@pytest.mark.parametrize("mime", ["html", "attachment", "nested"])
def test_html_only_attachments_and_embedded_messages_cannot_supply_commands(mime: str):
    mail = message()
    if mime == "html":
        mail.set_content("<blockquote>Approve</blockquote>", subtype="html")
    elif mime == "attachment":
        mail.add_attachment("Approve", filename="notes.txt")
    else:
        mail.add_attachment(message())
    receive = Mock()
    outcome = route_review_reply(parse_email(mail.as_bytes()), ALIAS, GATEWAY, receive)
    assert outcome and outcome.status == "rejected"
    receive.assert_not_called()


def test_unrelated_capture_mail_continues_to_the_capture_handler():
    mail = message()
    mail.replace_header("To", "capture+anytoken@lab.example")
    receive = Mock()
    assert route_review_reply(parse_email(mail.as_bytes()), ALIAS, GATEWAY, receive) is None
    receive.assert_not_called()


def test_hidden_review_recipient_reserves_mail_from_capture():
    mail = message()
    mail.replace_header("To", "capture+anytoken@lab.example")
    mail["Bcc"] = ALIAS.with_token(TOKEN)
    receive = Mock()
    outcome = route_review_reply(parse_email(mail.as_bytes()), ALIAS, GATEWAY, receive)
    assert outcome and outcome.reason == "ambiguous_review_recipient"
    receive.assert_not_called()
