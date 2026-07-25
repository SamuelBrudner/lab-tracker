from __future__ import annotations

import smtplib
from email.message import EmailMessage

import pytest

import lab_tracker.review_email_transport as transport
from lab_tracker.review_email_transport import (
    MAX_SMTP_TIMEOUT_SECONDS,
    REVIEW_READY_BODY,
    REVIEW_READY_SUBJECT,
    ReviewEmailPermanentError,
    ReviewEmailProvider,
    ReviewEmailRetryableError,
    ReviewReadyEmail,
    SMTPReviewEmailProvider,
    SMTPSettings,
    SMTPTLSMode,
    build_review_ready_message,
    delivery_dedupe_marker,
    deterministic_message_id,
    render_review_email_content,
)


def _delivery(
    *,
    recipient_email: str = "reviewer@example.test",
    idempotency_key: str = "ready:change-set-123:reviewer-456",
) -> ReviewReadyEmail:
    return ReviewReadyEmail(
        recipient_email=recipient_email,
        review_url="https://lab-tracker.example.test/r/signed-token",
        idempotency_key=idempotency_key,
    )


class _FakeSMTP:
    instances: list[_FakeSMTP] = []
    refused: dict[str, tuple[int, bytes]] = {}
    error: Exception | None = None

    def __init__(self, host: str, port: int, **kwargs: object) -> None:
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.calls: list[tuple[str, object]] = []
        self.sent_message: EmailMessage | None = None
        type(self).instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        self.calls.append(("enter", None))
        return self

    def __exit__(self, *args: object) -> None:
        self.calls.append(("exit", None))

    def ehlo(self) -> None:
        self.calls.append(("ehlo", None))

    def starttls(self, *, context: object) -> None:
        self.calls.append(("starttls", context))

    def login(self, username: str, password: str) -> None:
        self.calls.append(("login", (username, password)))
        if self.error is not None:
            raise self.error

    def send_message(
        self,
        message: EmailMessage,
        *,
        from_addr: str,
        to_addrs: list[str],
    ) -> dict[str, tuple[int, bytes]]:
        self.calls.append(("send_message", (from_addr, to_addrs)))
        self.sent_message = message
        if self.error is not None:
            raise self.error
        return dict(self.refused)


class _FakeSMTPSSL(_FakeSMTP):
    instances: list[_FakeSMTPSSL] = []


@pytest.fixture(autouse=True)
def _reset_fake_smtp() -> None:
    _FakeSMTP.instances = []
    _FakeSMTP.refused = {}
    _FakeSMTP.error = None
    _FakeSMTPSSL.instances = []
    _FakeSMTPSSL.refused = {}
    _FakeSMTPSSL.error = None


def test_fixed_message_contains_only_contentless_cue_and_review_link() -> None:
    delivery = _delivery()

    message = build_review_ready_message(
        delivery,
        sender_email="notifications@example.test",
    )

    assert message["Subject"] == REVIEW_READY_SUBJECT
    marker = delivery_dedupe_marker(delivery)
    assert (
        message.get_content().strip()
        == REVIEW_READY_BODY.format(
            review_url=delivery.review_url,
            dedupe_marker=marker,
        ).strip()
    )
    assert message.get_content().count(marker) == 1
    rendered = message.as_string()
    assert "project" not in rendered.lower()
    assert "proposal" not in rendered.lower()
    assert "science" not in rendered.lower()
    assert "note" not in rendered.lower()
    assert "count" not in rendered.lower()
    assert message.get_content_type() == "text/plain"


def test_message_id_is_stable_for_retry_and_scoped_to_recipient() -> None:
    first = _delivery()
    retry = _delivery()
    other_recipient = _delivery(recipient_email="other@example.test")

    assert deterministic_message_id(first) == deterministic_message_id(retry)
    assert deterministic_message_id(first) != deterministic_message_id(other_recipient)
    assert first.idempotency_key not in deterministic_message_id(first)


def test_dedupe_marker_is_stable_opaque_and_recipient_scoped() -> None:
    first = _delivery()
    retry = _delivery()
    other_recipient = _delivery(recipient_email="other@example.test")

    marker = delivery_dedupe_marker(first)

    assert marker == delivery_dedupe_marker(retry)
    assert marker != delivery_dedupe_marker(other_recipient)
    assert marker.startswith("LT-")
    assert len(marker) == 35
    assert first.idempotency_key not in marker
    assert "change-set" not in marker
    assert "reviewer" not in marker


def test_rendered_contract_is_the_same_content_used_by_smtp() -> None:
    delivery = ReviewReadyEmail(
        recipient_email="reviewer@example.test",
        review_url="https://lab-tracker.example.test/app/",
        idempotency_key="review-email-test:v1:opaque-id",
        event_type="test",
    )

    rendered = render_review_email_content(delivery)
    message = build_review_ready_message(
        delivery,
        sender_email="notifications@example.test",
    )

    assert message["Subject"] == rendered.subject
    assert message.get_content().strip() == rendered.text_content.strip()
    assert rendered.dedupe_marker in rendered.text_content
    assert delivery.idempotency_key not in rendered.text_content


@pytest.mark.parametrize(
    ("tls_mode", "smtp_type", "expected_starttls"),
    [
        (SMTPTLSMode.NONE, _FakeSMTP, False),
        (SMTPTLSMode.STARTTLS, _FakeSMTP, True),
        (SMTPTLSMode.IMPLICIT, _FakeSMTPSSL, False),
    ],
)
def test_smtp_provider_uses_selected_tls_mode_and_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tls_mode: SMTPTLSMode,
    smtp_type: type[_FakeSMTP],
    expected_starttls: bool,
) -> None:
    monkeypatch.setattr(transport.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(transport.smtplib, "SMTP_SSL", _FakeSMTPSSL)
    settings = SMTPSettings(
        host="smtp.example.test",
        port=465 if tls_mode is SMTPTLSMode.IMPLICIT else 587,
        sender_email="notifications@example.test",
        username="smtp-user",
        password="smtp-password",
        tls_mode=tls_mode,
        timeout_seconds=12.5,
    )
    provider = SMTPReviewEmailProvider(settings)

    result = provider.send_review_ready(_delivery())

    instance = smtp_type.instances[-1]
    assert instance.kwargs["timeout"] == 12.5
    assert ("login", ("smtp-user", "smtp-password")) in instance.calls
    assert any(call[0] == "starttls" for call in instance.calls) is expected_starttls
    assert instance.sent_message is not None
    assert result.provider == "smtp"
    assert result.idempotency_key == "ready:change-set-123:reviewer-456"
    assert result.message_id == instance.sent_message["Message-ID"]
    assert result.dedupe_marker in instance.sent_message.get_content()
    assert isinstance(provider, ReviewEmailProvider)


@pytest.mark.parametrize("timeout", [0, -1, MAX_SMTP_TIMEOUT_SECONDS + 0.01])
def test_smtp_timeout_must_stay_within_bound(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        SMTPSettings(
            host="smtp.example.test",
            port=587,
            sender_email="notifications@example.test",
            timeout_seconds=timeout,
        )


def test_smtp_settings_repr_does_not_expose_password() -> None:
    settings = SMTPSettings(
        host="smtp.example.test",
        port=587,
        sender_email="notifications@example.test",
        username="smtp-user",
        password="smtp-password-must-stay-secret",
    )

    assert "smtp-password-must-stay-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("upstream_error", "expected_type", "expected_code"),
    [
        (
            TimeoutError("smtp-password reviewer@example.test"),
            ReviewEmailRetryableError,
            "timeout",
        ),
        (
            OSError("smtp-password reviewer@example.test"),
            ReviewEmailRetryableError,
            "connection_failed",
        ),
        (
            smtplib.SMTPDataError(451, b"smtp-password reviewer@example.test"),
            ReviewEmailRetryableError,
            "temporary_rejection",
        ),
        (
            smtplib.SMTPDataError(550, b"smtp-password reviewer@example.test"),
            ReviewEmailPermanentError,
            "permanent_rejection",
        ),
        (
            smtplib.SMTPAuthenticationError(
                535,
                b"smtp-password reviewer@example.test",
            ),
            ReviewEmailPermanentError,
            "authentication_rejected",
        ),
        (
            smtplib.SMTPRecipientsRefused(
                {
                    "reviewer@example.test": (
                        450,
                        b"smtp-password reviewer@example.test",
                    )
                }
            ),
            ReviewEmailRetryableError,
            "temporary_rejection",
        ),
        (
            smtplib.SMTPRecipientsRefused(
                {
                    "reviewer@example.test": (
                        550,
                        b"smtp-password reviewer@example.test",
                    )
                }
            ),
            ReviewEmailPermanentError,
            "permanent_rejection",
        ),
        (
            RuntimeError("smtp-password reviewer@example.test"),
            ReviewEmailRetryableError,
            "transport_unavailable",
        ),
    ],
)
def test_provider_errors_are_typed_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    upstream_error: Exception,
    expected_type: type[ReviewEmailRetryableError | ReviewEmailPermanentError],
    expected_code: str,
) -> None:
    monkeypatch.setattr(transport.smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.error = upstream_error
    provider = SMTPReviewEmailProvider(
        SMTPSettings(
            host="smtp.example.test",
            port=587,
            sender_email="notifications@example.test",
            username="smtp-user",
            password="smtp-password",
            tls_mode=SMTPTLSMode.NONE,
        )
    )

    with pytest.raises(expected_type) as exc_info:
        provider.send_review_ready(_delivery())

    error = exc_info.value
    assert error.code == expected_code
    assert error.retryable is issubclass(expected_type, ReviewEmailRetryableError)
    assert "smtp-password" not in str(error)
    assert "reviewer@example.test" not in str(error)
    assert "smtp.example.test" not in str(error)
    assert error.__cause__ is None


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [
        (421, ReviewEmailRetryableError),
        (450, ReviewEmailRetryableError),
        (550, ReviewEmailPermanentError),
    ],
)
def test_refused_recipient_is_classified_by_smtp_status(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_type: type[ReviewEmailRetryableError | ReviewEmailPermanentError],
) -> None:
    monkeypatch.setattr(transport.smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.refused = {"reviewer@example.test": (status, b"provider detail must not escape")}
    provider = SMTPReviewEmailProvider(
        SMTPSettings(
            host="smtp.example.test",
            port=25,
            sender_email="notifications@example.test",
            tls_mode=SMTPTLSMode.NONE,
        )
    )

    with pytest.raises(expected_type) as exc_info:
        provider.send_review_ready(_delivery())

    assert "provider detail" not in str(exc_info.value)


def test_delivery_input_rejects_header_injection_and_credentialed_url() -> None:
    with pytest.raises(ValueError, match="recipient_email"):
        _delivery(recipient_email="reviewer@example.test\nBcc: observer@example.test")

    with pytest.raises(ValueError, match="without credentials"):
        ReviewReadyEmail(
            recipient_email="reviewer@example.test",
            review_url="https://user:password@lab-tracker.example.test/review",
            idempotency_key="delivery-key",
        )
