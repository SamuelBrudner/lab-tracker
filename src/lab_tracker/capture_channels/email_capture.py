"""Email-to-capture: poll an IMAP inbox and stage each accepted message.

Sender spoofing is the main risk, so a message is accepted only when BOTH hold:

* it is addressed to a per-(user, project) capture address
  ``<local>+<token>@<domain>`` whose token is a truncated HMAC of
  ``user_id|project_id`` under a key derived from ``LAB_TRACKER_AUTH_SECRET_KEY``
  (no storage: the token is recomputed and compared in constant time), and
* its single ``From`` address is one the operator mapped to that same user in
  ``LAB_TRACKER_CAPTURE_USER_EMAILS``.

The verified person authors the capture. The body is bounded plain text with
quoted replies stripped conservatively; small images, PDFs, and CSVs become
separate staged file notes sharing the text note's ``capture_bundle_id``, and
anything else is recorded by name, size, and SHA-256 only. A message is marked
processed (``\\Seen``, then moved when a processed folder is configured) only
after its notes are stored or it was deliberately rejected; a transient
failure leaves it unseen for the next poll.
"""

from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import hmac
import imaplib
import logging
import re
import ssl
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timezone
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from functools import cached_property
from html.parser import HTMLParser
from typing import Any, Final, Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext, User
from lab_tracker.capture_channels.common import (
    CAPTURE_BUNDLE_ID_KEY,
    CAPTURE_CHANNEL_KEY,
    CAPTURED_AT_KEY,
    EVIDENCE_ADAPTER_KEY,
    EVIDENCE_CAPTURE_KIND_KEY,
    EVIDENCE_CONTENT_HASH_KEY,
    EVIDENCE_SOURCE_EXTERNAL_ID_KEY,
    EVIDENCE_SOURCE_PROVIDER_KEY,
    EVIDENCE_TITLE_KEY,
    MAX_TITLE_CHARS,
    UserDirectory,
    bound_text,
    bound_value,
    channel_principal,
    emails_for_user,
    resolve_user_ref,
    sha256_hex,
    stable_key,
)
from lab_tracker.capture_channels.settings import (
    CaptureAddress,
    parse_capture_address,
    parse_user_emails,
    read_password_file,
)
from lab_tracker.capture_channels.settings import (
    normalize_email as normalize_email_address,
)
from lab_tracker.errors import ConflictError, NotFoundError, PermissionDeniedError
from lab_tracker.models import EntityOrigin, NoteMetadataScalar, NoteStatus

_logger = logging.getLogger(__name__)

EMAIL_CHANNEL: Final = "email"
EMAIL_ADAPTER: Final = "lab-tracker-email-capture"
CAPTURE_TOKEN_CHARS: Final = 20  # 100 bits of base32
_TOKEN_KEY_LABEL: Final = b"lab-tracker/email-capture-address/v1"
MAX_MESSAGES_PER_POLL: Final = 25
MAX_MESSAGE_BYTES: Final = 25 * 1024 * 1024
MAX_STORED_ATTACHMENT_BYTES: Final = 10 * 1024 * 1024
MAX_STORED_ATTACHMENTS: Final = 10
MAX_POINTER_ATTACHMENTS: Final = 20
MAX_PROJECTS_PER_MESSAGE: Final = 3
MAX_CANDIDATE_PROJECTS: Final = 10_000
# Text scanned for quoted replies (the stored body is bounded to 8,000 chars).
MAX_BODY_SCAN_CHARS: Final = 64 * 1024
IMAP_TIMEOUT_SECONDS: Final = 30.0
STORED_ATTACHMENT_TYPES: Final = frozenset(
    {
        "application/pdf",
        "image/gif",
        "image/heic",
        "image/heif",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
        "text/csv",
    }
)
_RECIPIENT_HEADERS: Final = ("To", "Cc", "Delivered-To", "X-Original-To", "Envelope-To")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
_TOKEN_RE = re.compile(rf"[a-z2-7]{{{CAPTURE_TOKEN_CHARS}}}\Z")
_UNSAFE_FILENAME_CHARACTERS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


class EmailCaptureError(RuntimeError):
    """The mailbox could not be read; the poll is retried at the next interval."""


def capture_token(secret: str, user_id: UUID, project_id: UUID) -> str:
    """Truncated HMAC naming one (user, project) capture address."""

    key = hmac.new(secret.encode("utf-8"), _TOKEN_KEY_LABEL, hashlib.sha256).digest()
    mac = hmac.new(key, f"{user_id}|{project_id}".encode(), hashlib.sha256).digest()
    return base64.b32encode(mac).decode("ascii").lower()[:CAPTURE_TOKEN_CHARS]


@dataclass(frozen=True, slots=True)
class EmailCaptureConfig:
    """Parsed email-capture configuration (see ``Settings.email_capture_*``)."""

    address: CaptureAddress
    host: str
    port: int
    username: str
    folder: str
    processed_folder: str
    email_directory: dict[str, str]
    secret: str = field(repr=False)
    _password: str = field(repr=False)
    _password_file: str = field(repr=False)

    @classmethod
    def from_settings(cls, settings: Any) -> EmailCaptureConfig | None:
        """Return the configuration, or ``None`` when email capture is off."""

        address = parse_capture_address(
            settings.email_capture_address or "", variable="LAB_TRACKER_EMAIL_CAPTURE_ADDRESS"
        )
        if address is None:
            return None
        return cls(
            address=address,
            host=str(settings.email_capture_imap_host).strip(),
            port=int(settings.email_capture_imap_port),
            username=str(settings.email_capture_imap_username).strip(),
            folder=str(settings.email_capture_imap_folder),
            processed_folder=str(settings.email_capture_processed_folder or ""),
            email_directory=parse_user_emails(
                settings.capture_user_emails or "", variable="LAB_TRACKER_CAPTURE_USER_EMAILS"
            ),
            secret=str(settings.auth_secret_key),
            _password=str(settings.email_capture_imap_password or ""),
            _password_file=str(settings.email_capture_imap_password_file or "").strip(),
        )

    def password(self) -> str:
        """Read the password now, so a rotated password file needs no restart."""

        return read_password_file(self._password_file) if self._password_file else self._password

    def address_for(self, user_id: UUID, project_id: UUID) -> str:
        return self.address.with_token(capture_token(self.secret, user_id, project_id))


# --------------------------------------------------------------------------- parsing


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content_type: str
    payload: bytes = field(repr=False)

    @property
    def sha256(self) -> str:
        return sha256_hex(self.payload)

    def storable(self, *, max_bytes: int) -> bool:
        return self.content_type in STORED_ATTACHMENT_TYPES and 0 < len(self.payload) <= max_bytes


@dataclass(eq=False)
class ParsedEmail:
    """The envelope of one message; its body and attachments are read lazily.

    Only the headers are interpreted up front. ``body`` and ``attachments`` are
    extracted on first use, which :func:`stage_email` reaches only after the
    sender and the capture token have been verified, so an unverified message
    never costs more than a header parse.
    """

    message_key: str
    message_id: str | None
    sender: str | None
    recipients: tuple[str, ...]
    subject: str
    sent_at: str | None
    _message: EmailMessage | None = field(default=None, repr=False)

    @cached_property
    def body(self) -> str:
        if self._message is None:
            return ""
        try:
            return _message_text(self._message)
        except Exception:  # a malformed body must not wedge the mailbox
            _logger.warning("Email capture could not read a message body.")
            return ""

    @cached_property
    def attachments(self) -> tuple[Attachment, ...]:
        if self._message is None:
            return ()
        try:
            return tuple(_attachments(self._message))
        except Exception:
            _logger.warning("Email capture could not read a message's attachments.")
            return ()


def parse_email(raw: bytes) -> ParsedEmail:
    """Parse one RFC 5322 message's headers without trusting any of its claims yet."""

    message = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(message, EmailMessage)
    message_id = _header(message, "Message-ID")
    return ParsedEmail(
        message_key=bound_value(message_id, limit=300) if message_id else sha256_hex(raw),
        message_id=bound_value(message_id, limit=300) if message_id else None,
        sender=_single_sender(message),
        recipients=_recipients(message),
        subject=bound_value(_header(message, "Subject") or "", limit=MAX_TITLE_CHARS),
        sent_at=_sent_at(message),
        _message=message,
    )


def _single_sender(message: EmailMessage) -> str | None:
    """The one ``From`` address, or ``None`` when absent, repeated, or malformed.

    A ``From`` header with parse defects is refused outright: an input such as
    ``alice@lab.org <mallory@evil.com>`` is repaired by lenient parsers into
    ``alice@lab.org`` while DMARC aligns on ``evil.com``.
    """

    try:
        headers = message.get_all("From") or []
    except (IndexError, ValueError, TypeError):
        return None
    if len(headers) != 1:
        return None
    header = headers[0]
    addresses = getattr(header, "addresses", None)
    if getattr(header, "defects", ()) or not addresses or len(addresses) != 1:
        return None
    return normalize_email_address(str(addresses[0].addr_spec))


def _recipients(message: EmailMessage) -> tuple[str, ...]:
    found: list[str] = []
    for name in _RECIPIENT_HEADERS:
        try:
            values = message.get_all(name) or []
        except (IndexError, ValueError, TypeError):
            continue
        for value in values:
            addresses = getattr(value, "addresses", None)
            candidates = (
                [str(address.addr_spec) for address in addresses]
                if addresses is not None
                else [addr for _name, addr in getaddresses([str(value)])]
            )
            found.extend(
                normalized
                for candidate in candidates
                if (normalized := normalize_email_address(candidate)) is not None
            )
    return tuple(found)


def capture_tokens(recipients: Iterable[str], address: CaptureAddress) -> tuple[str, ...]:
    """Tokens of every ``<local>+<token>@<domain>`` recipient, in order, deduplicated."""

    tokens: list[str] = []
    for recipient in recipients:
        local, _, domain = recipient.partition("@")
        base, plus, token = local.partition("+")
        if (
            plus
            and base == address.local
            and domain == address.domain
            and _TOKEN_RE.fullmatch(token)
            and token not in tokens
        ):
            tokens.append(token)
    return tuple(tokens)


_ORIGINAL_MESSAGE_RE = re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.IGNORECASE)
_OUTLOOK_SEPARATOR_RE = re.compile(r"^\s*_{20,}\s*$")
_REPLY_HEADER_START_RE = re.compile(r"^\s*(on|am|le|el)\s.+", re.IGNORECASE)
_REPLY_HEADER_END_RE = re.compile(r"(wrote|schrieb|a écrit|escribió)\s*:\s*$", re.IGNORECASE)


def strip_quoted_reply(text: str) -> str:
    """Drop a trailing quoted reply, conservatively.

    Cut only (a) at an explicit ``-----Original Message-----`` marker, (b) at an
    Outlook underscore rule followed by a ``From:`` line, or (c) at an
    ``On … wrote:`` header when every non-blank line after it is ``>``-quoted.
    Trailing ``>`` lines are then removed. Interleaved replies are kept whole,
    and a message that would become empty is returned unstripped.
    """

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    count = len(lines)
    # One backward pass (linear): the next non-blank line at or after each index,
    # and whether every non-blank line from there on is ">"-quoted.
    next_nonblank = [count] * (count + 1)
    quoted_from = [False] * (count + 1)
    for index in range(count - 1, -1, -1):
        if lines[index].strip():
            next_nonblank[index] = index
            later = next_nonblank[index + 1]
            quoted_from[index] = lines[index].lstrip().startswith(">") and (
                later == count or quoted_from[later]
            )
        else:
            next_nonblank[index] = next_nonblank[index + 1]
            quoted_from[index] = quoted_from[index + 1]
    cut = count
    for index, line in enumerate(lines):
        if _ORIGINAL_MESSAGE_RE.match(line):
            cut = index
            break
        following = next_nonblank[index + 1] if index + 1 <= count else count
        if (
            _OUTLOOK_SEPARATOR_RE.match(line)
            and following < count
            and lines[following].strip().lower().startswith("from:")
        ):
            cut = index
            break
        if _REPLY_HEADER_START_RE.match(line):
            header_end = _reply_header_end(lines, index)
            if header_end is not None:
                rest = next_nonblank[header_end + 1]
                if rest < count and quoted_from[rest]:
                    cut = index
                    break
    kept = lines[:cut]
    while kept and (not kept[-1].strip() or kept[-1].lstrip().startswith(">")):
        kept.pop()
    stripped = "\n".join(kept).strip()
    return stripped or text.strip()


def _reply_header_end(lines: list[str], start: int) -> int | None:
    # Mail clients wrap the attribution line; allow it to span two lines.
    for offset in (0, 1):
        index = start + offset
        if index < len(lines) and _REPLY_HEADER_END_RE.search(lines[index]):
            return index
    return None


class _HtmlText(HTMLParser):
    _BLOCKS = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Crude, dependency-free HTML to text (only used when no text/plain part exists)."""

    parser = _HtmlText()
    with suppress(Exception):
        parser.feed(markup)
        parser.close()
    text = "".join(parser.parts)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.split("\n")))


def _message_text(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, KeyError, ValueError):
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
    if not isinstance(content, str):
        return ""
    # Bound the work before any scanning: the stored body is capped far lower.
    content = content[:MAX_BODY_SCAN_CHARS]
    if part.get_content_subtype() == "html":
        content = html_to_text(content)
    return strip_quoted_reply(content)


def _attachments(message: EmailMessage) -> Iterable[Attachment]:
    for index, part in enumerate(message.iter_attachments(), start=1):
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        yield Attachment(
            filename=_safe_filename(part.get_filename(), fallback=f"attachment-{index}"),
            content_type=part.get_content_type().lower(),
            payload=payload,
        )


def _safe_filename(raw: str | None, *, fallback: str) -> str:
    cleaned = _UNSAFE_FILENAME_CHARACTERS.sub("_", (raw or "").strip()).strip(". ")
    return (cleaned or fallback)[:200]


def _header(message: EmailMessage, name: str) -> str | None:
    try:
        value = message.get(name)
    except (IndexError, ValueError, TypeError):
        return None
    return str(value).strip() if value is not None else None


def _sent_at(message: EmailMessage) -> str | None:
    raw = _header(message, "Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- staging


class CaptureApi(Protocol):
    """The slice of ``LabTrackerAPI`` the email channel uses."""

    def accessible_project_ids(self, actor: AuthContext | None) -> set[UUID] | None: ...

    def list_projects(self) -> list[Any]: ...

    def find_note_by_client_capture_id(self, *args: Any, **kwargs: Any) -> Any: ...

    def create_note_result(self, *args: Any, **kwargs: Any) -> Any: ...

    def upload_note_raw_result(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass
class MessageOutcome:
    """What happened to one message; ``processed`` messages are flagged in IMAP."""

    status: str  # stored | duplicate | rejected | failed
    reason: str = ""
    notes_created: int = 0

    @property
    def processed(self) -> bool:
        return self.status in {"stored", "duplicate", "rejected"}


def stage_email(
    parsed: ParsedEmail,
    *,
    config: EmailCaptureConfig,
    users: UserDirectory,
    api: CaptureApi,
    max_attachment_bytes: int,
) -> MessageOutcome:
    """Verify one parsed message and stage it for its (user, project) pairs."""

    if parsed.sender is None:
        return MessageOutcome("rejected", "sender_missing_or_ambiguous")
    user_ref = config.email_directory.get(parsed.sender)
    user = resolve_user_ref(users, user_ref) if user_ref is not None else None
    if user is None:
        return MessageOutcome("rejected", "unknown_sender")
    tokens = capture_tokens(parsed.recipients, config.address)
    if not tokens:
        return MessageOutcome("rejected", "no_capture_address")
    principal = channel_principal(user, label=EMAIL_CHANNEL)
    projects = _matching_projects(tokens, user=user, principal=principal, config=config, api=api)
    if not projects:
        return MessageOutcome("rejected", "token_mismatch")
    created = 0
    duplicate = True
    for project_id in projects[:MAX_PROJECTS_PER_MESSAGE]:
        try:
            project_created = _stage_for_project(
                parsed,
                project_id=project_id,
                principal=principal,
                api=api,
                max_attachment_bytes=max_attachment_bytes,
            )
        except PermissionDeniedError:
            return MessageOutcome("rejected", "not_a_contributor", created)
        except (NotFoundError, ConflictError):
            return MessageOutcome("rejected", "not_stageable", created)
        created += project_created
        duplicate = duplicate and project_created == 0
    return MessageOutcome("duplicate" if duplicate else "stored", "", created)


def _matching_projects(
    tokens: tuple[str, ...],
    *,
    user: User,
    principal: AuthContext,
    config: EmailCaptureConfig,
    api: CaptureApi,
) -> list[UUID]:
    accessible = api.accessible_project_ids(principal)
    if accessible is None:
        candidates = [project.project_id for project in api.list_projects()]
    else:
        candidates = sorted(accessible, key=str)
    matched: list[UUID] = []
    for project_id in candidates[:MAX_CANDIDATE_PROJECTS]:
        expected = capture_token(config.secret, user.user_id, project_id)
        if any(hmac.compare_digest(expected, token) for token in tokens):
            matched.append(project_id)
    return matched


def _stage_for_project(
    parsed: ParsedEmail,
    *,
    project_id: UUID,
    principal: AuthContext,
    api: CaptureApi,
    max_attachment_bytes: int,
) -> int:
    """Stage the text note and its file notes; return how many notes were created."""

    stored = [a for a in parsed.attachments if a.storable(max_bytes=max_attachment_bytes)][
        :MAX_STORED_ATTACHMENTS
    ]
    pointers = [a for a in parsed.attachments if a not in stored][:MAX_POINTER_ATTACHMENTS]
    bundle_id = (
        stable_key("email-bundle", parsed.message_key, str(project_id), length=24)
        if stored
        else None
    )
    text_key = stable_key("email", parsed.message_key)
    base = _base_metadata(parsed, bundle_id=bundle_id)
    created = 0
    if api.find_note_by_client_capture_id(project_id, text_key, actor=principal) is None:
        body = bound_text(_note_text(parsed, pointers=pointers))
        metadata = {
            **base,
            EVIDENCE_CAPTURE_KIND_KEY: "text",
            "email_attachments_stored": len(stored),
            "email_attachments_pointer_only": len(pointers),
            **body.metadata(),
        }
        result = api.create_note_result(
            project_id=project_id,
            raw_content=body.text,
            metadata=metadata,
            client_capture_id=text_key,
            status=NoteStatus.STAGED,
            actor=principal,
            origin=EntityOrigin.USER,
            origin_provider=EMAIL_CHANNEL,
        )
        created += int(bool(getattr(result, "created", True)))
    for index, attachment in enumerate(stored, start=1):
        file_key = stable_key("email", parsed.message_key, f"attachment-{index}")
        if api.find_note_by_client_capture_id(project_id, file_key, actor=principal) is not None:
            continue
        result = api.upload_note_raw_result(
            project_id=project_id,
            content=attachment.payload,
            filename=attachment.filename,
            content_type=attachment.content_type,
            metadata={
                **base,
                EVIDENCE_CAPTURE_KIND_KEY: "file",
                EVIDENCE_CONTENT_HASH_KEY: attachment.sha256,
                EVIDENCE_TITLE_KEY: attachment.filename,
                "source_file_name": attachment.filename,
                "source_file_content_type": attachment.content_type,
                "source_file_size_bytes": len(attachment.payload),
            },
            client_capture_id=file_key,
            status=NoteStatus.STAGED,
            actor=principal,
            origin=EntityOrigin.USER,
            origin_provider=EMAIL_CHANNEL,
        )
        created += int(bool(getattr(result, "created", True)))
    return created


def _base_metadata(parsed: ParsedEmail, *, bundle_id: str | None) -> dict[str, NoteMetadataScalar]:
    metadata: dict[str, NoteMetadataScalar] = {
        CAPTURE_CHANNEL_KEY: EMAIL_CHANNEL,
        EVIDENCE_SOURCE_PROVIDER_KEY: EMAIL_CHANNEL,
        EVIDENCE_ADAPTER_KEY: EMAIL_ADAPTER,
        "email_from": parsed.sender or "",
    }
    if parsed.subject:
        metadata[EVIDENCE_TITLE_KEY] = parsed.subject
    if parsed.message_id:
        metadata["email_message_id"] = parsed.message_id
        metadata[EVIDENCE_SOURCE_EXTERNAL_ID_KEY] = parsed.message_id
    if parsed.sent_at:
        # The Date header is the sender's composition clock.
        metadata[CAPTURED_AT_KEY] = parsed.sent_at
    if bundle_id is not None:
        metadata[CAPTURE_BUNDLE_ID_KEY] = bundle_id
    return metadata


def _note_text(parsed: ParsedEmail, *, pointers: list[Attachment]) -> str:
    sections = [parsed.subject or "(no subject)"]
    if parsed.body:
        sections.append(parsed.body)
    if pointers:
        lines = ["Attachments recorded as pointers only (not stored in Lab Tracker):"]
        lines.extend(
            f"- {a.filename} ({a.content_type}, {len(a.payload)} bytes, sha256 {a.sha256})"
            for a in pointers
        )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


# --------------------------------------------------------------------------- IMAP


class ImapConnection(Protocol):
    """The ``imaplib.IMAP4`` subset the poller uses (a fake satisfies it in tests)."""

    capabilities: tuple[str, ...]

    def login(self, user: str, password: str) -> Any: ...

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[Any]]: ...

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]: ...

    def logout(self) -> Any: ...


ImapFactory = Callable[[str, int, float], ImapConnection]


def default_imap_factory(host: str, port: int, timeout: float) -> ImapConnection:
    """Open an implicit-TLS IMAP connection with certificate verification."""

    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)


@dataclass
class EmailPollResult:
    """Counts for one mailbox poll."""

    examined: int = 0
    stored: int = 0
    duplicate: int = 0
    rejected: int = 0
    failed: int = 0
    notes_created: int = 0
    left_for_next_poll: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)


def poll_mailbox(
    *,
    config: EmailCaptureConfig,
    imap_factory: ImapFactory,
    stage: Callable[[ParsedEmail], MessageOutcome],
    max_messages: int = MAX_MESSAGES_PER_POLL,
    expired: Callable[[], bool] = lambda: False,
) -> EmailPollResult:
    """Read unseen messages (bounded), stage each, and mark it processed only after.

    ``expired`` is the poller's wall-clock budget: once it reports true, the
    remaining unseen messages are left for the next poll.
    """

    result = EmailPollResult()
    try:
        connection = imap_factory(config.host, config.port, IMAP_TIMEOUT_SECONDS)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise EmailCaptureError("Could not connect to the capture mailbox.") from exc
    try:
        try:
            connection.login(config.username, config.password())
            status, _ = connection.select(_mailbox(config.folder))
            if status != "OK":
                raise EmailCaptureError("Could not open the capture mailbox folder.")
            status, data = connection.uid("SEARCH", "UNSEEN")
        except (OSError, imaplib.IMAP4.error) as exc:
            raise EmailCaptureError("Could not read the capture mailbox.") from exc
        if status != "OK":
            raise EmailCaptureError("Could not search the capture mailbox.")
        uids = [uid.decode("ascii") for uid in (data[0] or b"").split() if uid.isdigit()]
        for position, uid in enumerate(uids[:max_messages]):
            if expired():
                result.left_for_next_poll = len(uids[:max_messages]) - position
                break
            result.examined += 1
            outcome = _process_uid(connection, uid, config=config, stage=stage)
            _tally(result, outcome)
    finally:
        with suppress(Exception):
            connection.logout()
    return result


def _process_uid(
    connection: ImapConnection,
    uid: str,
    *,
    config: EmailCaptureConfig,
    stage: Callable[[ParsedEmail], MessageOutcome],
) -> MessageOutcome:
    try:
        status, data = connection.uid("FETCH", uid, "(RFC822.SIZE)")
        size = _fetched_size(data) if status == "OK" else None
        if size is None:
            return MessageOutcome("failed", "size_unavailable")
        if size > MAX_MESSAGE_BYTES:
            outcome = MessageOutcome("rejected", "message_too_large")
        else:
            status, data = connection.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = _fetched_body(data) if status == "OK" else None
            if raw is None:
                return MessageOutcome("failed", "fetch_failed")
            try:
                parsed = parse_email(raw)
            except Exception:  # a malformed message must never wedge the mailbox
                _logger.warning("Email capture could not parse message uid %s.", uid)
                parsed = None
            outcome = MessageOutcome("rejected", "unparsable") if parsed is None else stage(parsed)
        if outcome.processed:
            _mark_processed(connection, uid, processed_folder=config.processed_folder)
        return outcome
    except (OSError, imaplib.IMAP4.error):
        return MessageOutcome("failed", "imap_error")


def _mark_processed(connection: ImapConnection, uid: str, *, processed_folder: str) -> None:
    connection.uid("STORE", uid, "+FLAGS", "(\\Seen)")
    if not processed_folder:
        return
    folder = _mailbox(processed_folder)
    if "MOVE" in {str(cap).upper() for cap in getattr(connection, "capabilities", ())}:
        status, _ = connection.uid("MOVE", uid, folder)
        if status == "OK":
            return
    status, _ = connection.uid("COPY", uid, folder)
    if status == "OK":
        # Expunge is left to the server or operator so this never removes
        # another client's deleted-but-unexpunged messages.
        connection.uid("STORE", uid, "+FLAGS", "(\\Deleted)")


def _tally(result: EmailPollResult, outcome: MessageOutcome) -> None:
    if outcome.status == "stored":
        result.stored += 1
    elif outcome.status == "duplicate":
        result.duplicate += 1
    elif outcome.status == "rejected":
        result.rejected += 1
        reasons = result.rejection_reasons
        reasons[outcome.reason] = reasons.get(outcome.reason, 0) + 1
    else:
        result.failed += 1
    result.notes_created += outcome.notes_created


def _fetched_size(data: list[Any]) -> int | None:
    for item in data:
        chunk = item[0] if isinstance(item, tuple) else item
        if isinstance(chunk, bytes) and (match := _SIZE_RE.search(chunk)):
            return int(match.group(1))
    return None


def _fetched_body(data: list[Any]) -> bytes | None:
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _mailbox(name: str) -> str:
    return f'"{name}"' if any(character.isspace() for character in name) else name


__all__ = [
    "EmailCaptureConfig",
    "EmailCaptureError",
    "capture_token",
    "capture_tokens",
    "emails_for_user",
    "parse_email",
    "poll_mailbox",
    "stage_email",
    "strip_quoted_reply",
]
