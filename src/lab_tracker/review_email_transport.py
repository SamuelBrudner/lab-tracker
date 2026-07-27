"""Privacy-preserving email transport for ready graph-draft reviews.

The transport intentionally accepts no project, note, proposal, or review-count
content.  A delivery can therefore contain only a fixed cue and an authenticated
review link.  Durable retry and deduplication remain the caller's responsibility;
the stable ``Message-ID`` gives each retry of the same logical delivery the same
SMTP identity.
"""

from __future__ import annotations

import hashlib
import smtplib
import ssl
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from enum import Enum
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

DEFAULT_SMTP_TIMEOUT_SECONDS = 10.0
MAX_SMTP_TIMEOUT_SECONDS = 30.0

REVIEW_READY_SUBJECT = "A Lab Tracker review is ready"
REVIEW_READY_BODY = """A Lab Tracker review is ready.

Open Lab Tracker to review it:
{review_url}

Delivery reference: {dedupe_marker}

Sign-in is required. This email contains no research details.
"""
REVIEW_EMAIL_TEST_SUBJECT = "Lab Tracker email alerts are working"
REVIEW_EMAIL_TEST_BODY = """This is a Lab Tracker email-alert test.

Open Lab Tracker:
{review_url}

Delivery reference: {dedupe_marker}

No graph record was created. This email contains no research details.
"""

RetryableReviewEmailErrorCode = Literal[
    "connection_failed",
    "temporary_rejection",
    "timeout",
    "transport_unavailable",
]
PermanentReviewEmailErrorCode = Literal[
    "authentication_rejected",
    "invalid_configuration",
    "invalid_message",
    "permanent_rejection",
    "tls_unavailable",
]

_SAFE_ERROR_MESSAGES: dict[str, str] = {
    "authentication_rejected": "The email provider rejected its configured credentials.",
    "connection_failed": "The email provider could not be reached.",
    "invalid_configuration": "The email provider configuration is invalid.",
    "invalid_message": "The review-ready email could not be constructed.",
    "permanent_rejection": "The email provider permanently rejected the delivery.",
    "temporary_rejection": "The email provider temporarily rejected the delivery.",
    "timeout": "The email provider timed out.",
    "tls_unavailable": "The email provider could not establish the configured TLS mode.",
    "transport_unavailable": "The email provider is temporarily unavailable.",
}


class SMTPTLSMode(str, Enum):
    """Supported SMTP connection-security modes."""

    NONE = "none"
    STARTTLS = "starttls"
    IMPLICIT = "implicit"


@dataclass(frozen=True)
class SMTPSettings:
    """Validated connection details for the SMTP provider."""

    host: str
    port: int
    sender_email: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    tls_mode: SMTPTLSMode = SMTPTLSMode.STARTTLS
    timeout_seconds: float = DEFAULT_SMTP_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        host = self.host.strip()
        sender_email = self.sender_email.strip()
        username = self.username.strip() if self.username is not None else None
        password = self.password if self.password else None
        try:
            tls_mode = SMTPTLSMode(self.tls_mode)
        except ValueError as exc:
            raise ValueError("tls_mode must be none, starttls, or implicit.") from exc

        if not host or "\r" in host or "\n" in host:
            raise ValueError("host must be a non-blank single-line value.")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535.")
        _parse_email_address(sender_email, field_name="sender_email")
        if username is not None and password is None:
            raise ValueError("password is required when username is configured.")
        if password is not None and username is None:
            raise ValueError("username is required when password is configured.")
        if not 0 < self.timeout_seconds <= MAX_SMTP_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be greater than zero and no more than "
                f"{MAX_SMTP_TIMEOUT_SECONDS:g}."
            )

        object.__setattr__(self, "host", host)
        object.__setattr__(self, "sender_email", sender_email)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "password", password)
        object.__setattr__(self, "tls_mode", tls_mode)


@dataclass(frozen=True)
class ReviewReadyEmail:
    """The complete, deliberately contentless input to one email delivery."""

    recipient_email: str
    review_url: str
    idempotency_key: str
    event_type: Literal["review_ready", "test"] = "review_ready"

    def __post_init__(self) -> None:
        recipient_email = self.recipient_email.strip()
        review_url = self.review_url.strip()
        idempotency_key = self.idempotency_key.strip()

        _parse_email_address(recipient_email, field_name="recipient_email")
        parsed_url = urlsplit(review_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or "\r" in review_url
            or "\n" in review_url
        ):
            raise ValueError("review_url must be an absolute HTTP(S) URL without credentials.")
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency_key must contain between 1 and 200 characters.")
        if "\r" in idempotency_key or "\n" in idempotency_key:
            raise ValueError("idempotency_key must be a single-line value.")
        if self.event_type not in {"review_ready", "test"}:
            raise ValueError("event_type must be review_ready or test.")

        object.__setattr__(self, "recipient_email", recipient_email)
        object.__setattr__(self, "review_url", review_url)
        object.__setattr__(self, "idempotency_key", idempotency_key)


@dataclass(frozen=True)
class ReviewEmailDeliveryResult:
    """Provider-neutral receipt for one accepted delivery."""

    provider: str
    idempotency_key: str
    message_id: str
    dedupe_marker: str


@dataclass(frozen=True)
class RenderedReviewEmail:
    """Fixed outbound content shared by SMTP and external mailbox adapters."""

    subject: str
    text_content: str
    dedupe_marker: str


@runtime_checkable
class ReviewEmailProvider(Protocol):
    """Port implemented by review-ready email providers."""

    def send_review_ready(self, delivery: ReviewReadyEmail) -> ReviewEmailDeliveryResult:
        """Submit one contentless review-ready cue."""


class ReviewEmailDeliveryError(RuntimeError):
    """Sanitized provider failure safe to log or persist."""

    retryable: bool

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_SAFE_ERROR_MESSAGES[code])


class ReviewEmailRetryableError(ReviewEmailDeliveryError):
    """A transient delivery failure that a durable worker may retry."""

    retryable = True

    def __init__(self, code: RetryableReviewEmailErrorCode) -> None:
        super().__init__(code)


class ReviewEmailPermanentError(ReviewEmailDeliveryError):
    """A delivery failure that requires configuration or recipient repair."""

    retryable = False

    def __init__(self, code: PermanentReviewEmailErrorCode) -> None:
        super().__init__(code)


def deterministic_message_id(delivery: ReviewReadyEmail) -> str:
    """Return a stable opaque Message-ID for one recipient-scoped delivery."""

    return f"<review-ready-{_delivery_digest(delivery)}@lab-tracker.invalid>"


def delivery_dedupe_marker(delivery: ReviewReadyEmail) -> str:
    """Return an opaque body marker for crash-safe sent-mail deduplication."""

    return f"LT-{_delivery_digest(delivery)[:32].upper()}"


def render_review_email_content(delivery: ReviewReadyEmail) -> RenderedReviewEmail:
    """Render the one contentless subject/body contract for every transport."""

    is_test = delivery.event_type == "test"
    subject = REVIEW_EMAIL_TEST_SUBJECT if is_test else REVIEW_READY_SUBJECT
    body = REVIEW_EMAIL_TEST_BODY if is_test else REVIEW_READY_BODY
    dedupe_marker = delivery_dedupe_marker(delivery)
    return RenderedReviewEmail(
        subject=subject,
        text_content=body.format(
            review_url=delivery.review_url,
            dedupe_marker=dedupe_marker,
        ),
        dedupe_marker=dedupe_marker,
    )


def _delivery_digest(delivery: ReviewReadyEmail) -> str:
    address = _parse_email_address(delivery.recipient_email, field_name="recipient_email")
    normalized_recipient = f"{address.username}@{address.domain.lower()}"
    return hashlib.sha256(
        f"{delivery.idempotency_key}\0{normalized_recipient}".encode()
    ).hexdigest()


def build_review_ready_message(
    delivery: ReviewReadyEmail,
    *,
    sender_email: str,
) -> EmailMessage:
    """Build the fixed plain-text cue without accepting research content."""

    _parse_email_address(sender_email, field_name="sender_email")
    rendered = render_review_email_content(delivery)
    message = EmailMessage()
    message["From"] = sender_email
    message["To"] = delivery.recipient_email
    message["Subject"] = rendered.subject
    message["Message-ID"] = deterministic_message_id(delivery)
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content(rendered.text_content)
    return message


class SMTPReviewEmailProvider:
    """SMTP implementation with explicit TLS behavior and bounded I/O time."""

    def __init__(self, settings: SMTPSettings) -> None:
        self._settings = settings

    def send_review_ready(self, delivery: ReviewReadyEmail) -> ReviewEmailDeliveryResult:
        try:
            message = build_review_ready_message(
                delivery,
                sender_email=self._settings.sender_email,
            )
            refused = self._send(message, recipient_email=delivery.recipient_email)
            if refused:
                code = next(iter(refused.values()))[0]
                _raise_for_smtp_status(code)
        except ReviewEmailDeliveryError:
            raise
        except smtplib.SMTPAuthenticationError:
            raise ReviewEmailPermanentError("authentication_rejected") from None
        except smtplib.SMTPNotSupportedError:
            raise ReviewEmailPermanentError("tls_unavailable") from None
        except smtplib.SMTPRecipientsRefused as exc:
            status = next(iter(exc.recipients.values()), (500, b""))[0]
            _raise_for_smtp_status(status)
        except smtplib.SMTPResponseException as exc:
            _raise_for_smtp_status(exc.smtp_code)
        except ssl.SSLCertVerificationError:
            raise ReviewEmailPermanentError("tls_unavailable") from None
        except ssl.SSLError:
            raise ReviewEmailPermanentError("tls_unavailable") from None
        except TimeoutError:
            raise ReviewEmailRetryableError("timeout") from None
        except (ConnectionError, OSError):
            raise ReviewEmailRetryableError("connection_failed") from None
        except (TypeError, UnicodeError, ValueError):
            raise ReviewEmailPermanentError("invalid_message") from None
        except smtplib.SMTPServerDisconnected:
            raise ReviewEmailRetryableError("connection_failed") from None
        except smtplib.SMTPException:
            raise ReviewEmailRetryableError("transport_unavailable") from None
        except Exception:
            # SMTP implementations and test doubles can surface provider-specific
            # exceptions.  Never let their potentially credential-bearing text
            # cross the transport boundary.
            raise ReviewEmailRetryableError("transport_unavailable") from None

        return ReviewEmailDeliveryResult(
            provider="smtp",
            idempotency_key=delivery.idempotency_key,
            message_id=str(message["Message-ID"]),
            dedupe_marker=delivery_dedupe_marker(delivery),
        )

    def _send(
        self,
        message: EmailMessage,
        *,
        recipient_email: str,
    ) -> dict[str, tuple[int, bytes]]:
        settings = self._settings
        context = ssl.create_default_context()
        client: smtplib.SMTP
        if settings.tls_mode is SMTPTLSMode.IMPLICIT:
            client = smtplib.SMTP_SSL(
                settings.host,
                settings.port,
                timeout=settings.timeout_seconds,
                context=context,
            )
        else:
            client = smtplib.SMTP(
                settings.host,
                settings.port,
                timeout=settings.timeout_seconds,
            )

        with client:
            if settings.tls_mode is SMTPTLSMode.STARTTLS:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            if settings.username is not None:
                client.login(settings.username, settings.password or "")
            return client.send_message(
                message,
                from_addr=settings.sender_email,
                to_addrs=[recipient_email],
            )


def _parse_email_address(value: str, *, field_name: str) -> Address:
    try:
        address = Address(addr_spec=value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be one plain email address.") from exc
    if not address.username or not address.domain:
        raise ValueError(f"{field_name} must be one plain email address.")
    return address


def _raise_for_smtp_status(status: int) -> None:
    if 400 <= status <= 499:
        raise ReviewEmailRetryableError("temporary_rejection") from None
    raise ReviewEmailPermanentError("permanent_rejection") from None
