"""Route person-authored replies from the trusted IMAP inbox to proposal review."""

from __future__ import annotations

import re
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import quote

from lab_tracker.capture_channels.email_capture import (
    MAX_BODY_SCAN_CHARS,
    MessageOutcome,
    ParsedEmail,
    recipient_addresses,
    single_sender_address,
)
from lab_tracker.capture_channels.settings import CaptureAddress
from lab_tracker.errors import ValidationError
from lab_tracker.services.review_email_service import normalize_review_email

_TOKEN = re.compile(r"[a-z2-7]{52}")
_FORWARD = re.compile(r"^\s*(?:begin forwarded message:|-{2,}\s*forwarded message\s*-{2,})", re.I)
_ATTRIBUTION = re.compile(r"^\s*(?:on|am|le|el)\s.+(?:wrote|schrieb|a écrit|escribió):\s*$", re.I)
_ORIGINAL = re.compile(r"^\s*-{2,}\s*original message\s*-{2,}\s*$", re.I)
_MOBILE_SIGNATURES = {"sent from my iphone", "sent from my ipad", "sent from my android"}
_AUTOMATED_HEADERS = {
    "list-id",
    "list-unsubscribe",
    "x-autoreply",
    "x-autorespond",
    "x-autoreply-from",
    "x-loop",
    "x-ms-exchange-inbox-rules-loop",
}


def route_review_reply(
    parsed: ParsedEmail,
    address: CaptureAddress,
    authserv_id: str,
    receive: Callable[[str, str, str], str],
) -> MessageOutcome | None:
    """Reserve bare and tagged review aliases; unrelated mail can continue to capture."""

    base = f"{address.local}@{address.domain}"
    message = parsed.message
    forwarded = recipient_addresses(message, ("Bcc", "Resent-To", "Resent-Cc", "Resent-Bcc"))
    aliases = {
        recipient
        for recipient in (*parsed.recipients, *forwarded)
        if recipient == base
        or (recipient.startswith(address.local + "+") and recipient.endswith("@" + address.domain))
    }
    if not aliases:
        return None

    direct = recipient_addresses(message, ("To",))
    if (
        len(direct) != 1
        or direct[0] not in aliases
        or aliases - {direct[0], base}
        or len(message.get_all("To", [])) != 1
        or bool(getattr(message["To"], "defects", ()))
        or message["Cc"] is not None
        or message["Bcc"] is not None
    ):
        return MessageOutcome("rejected", "ambiguous_review_recipient")
    token = direct[0].partition("@")[0].partition("+")[2]
    if not _TOKEN.fullmatch(token):
        return MessageOutcome("rejected", "invalid_review_address")

    sender = _review_sender(message)
    if sender is None or not _dmarc_pass(message, authserv_id, sender):
        return MessageOutcome("rejected", "unauthenticated_review_sender")
    if not _personal_reply(message):
        return MessageOutcome("rejected", "not_a_personal_reply")
    text = _authored_text(message)
    if not text:
        return MessageOutcome("rejected", "no_review_command")
    decision = receive(token, sender, text)
    if decision == "ignored":
        return MessageOutcome("rejected", "review_ignored")
    if decision not in {"approved", "corrected", "correction_requested"}:
        raise ValueError("Unexpected proposal review outcome.")
    return MessageOutcome(decision)


def _review_sender(message: EmailMessage) -> str | None:
    address = single_sender_address(message)
    if address is None:
        return None
    # HeaderRegistry's addr_spec sometimes drops required local-part quotes.
    # Re-quote the parsed local part, then canonicalize exactly as enrollment does.
    local = quote(address.username.lower())
    try:
        return normalize_review_email(f'"{local}"@{address.domain}')
    except ValidationError:
        return None


def _dmarc_pass(message: EmailMessage, authserv_id: str, sender: str) -> bool:
    # Use the raw, topmost Authentication-Results. The configured receiving MX
    # must remove externally supplied results claiming its identity (RFC 8601 §5).
    raw = next(
        (value for name, value in message.raw_items() if name.lower() == "authentication-results"),
        "",
    )
    if len(raw) > 8192:
        return False
    fields = _auth_fields(raw.lower())
    if not fields or fields[0].split() not in ([authserv_id], [authserv_id, "1"]):
        return False
    dmarc = [f.strip() for f in fields[1:] if re.match(r"\s*dmarc\s*=", f)]
    if len(dmarc) != 1 or not re.match(r"dmarc\s*=\s*pass(?:\s|$)", dmarc[0]):
        return False
    domains = re.findall(r"(?:^|\s)header\.from\s*=\s*([^\s]+)", dmarc[0])
    return domains == [sender.rpartition("@")[2]]


def _auth_fields(raw: str) -> list[str]:
    """Mask comments and quoted values so their text cannot become a DMARC result."""

    visible: list[str] = []
    depth = 0
    quoted = escaped = False
    for char in raw:
        if escaped:
            escaped = False
        elif (depth or quoted) and char == "\\":
            escaped = True
        elif not depth and char == '"':
            quoted = not quoted
            visible.append('"')
        elif quoted:
            continue
        elif char == "(":
            depth += 1
            visible.append(" ")
        elif char == ")":
            if not depth:
                return []
            depth -= 1
        elif not depth:
            visible.append(char)
    return [] if depth or quoted else "".join(visible).split(";")


def _personal_reply(message: EmailMessage) -> bool:
    if any(k.lower() in _AUTOMATED_HEADERS or k.lower().startswith("resent-") for k in message):
        return False
    if any(str(v).strip().lower() != "no" for v in message.get_all("Auto-Submitted", [])):
        return False
    if str(message.get("Precedence", "")).strip().lower() in {"bulk", "list", "junk", "auto_reply"}:
        return False
    if str(message.get("Return-Path", "")).strip() == "<>":
        return False
    if re.search(r"(?:^|\s)fwd?:", str(message.get("Subject", "")), re.I):
        return False
    parents = message.get_all("In-Reply-To", [])
    if len(parents) != 1 or not re.fullmatch(r"<[^<>\s@]+@[^<>\s@]+>", str(parents[0]).strip()):
        return False
    return all(
        part.get_content_maintype() != "message"
        and part.get_content_type() != "multipart/report"
        and part.get_content_disposition() != "attachment"
        for part in message.walk()
    )


def _authored_text(message: EmailMessage) -> str:
    # HTML flattening loses quote boundaries. Require the normal text/plain alternative.
    part = message.get_body(preferencelist=("plain",))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError, KeyError):
        return ""
    if not isinstance(text, str) or len(text) > MAX_BODY_SCAN_CHARS:
        return ""
    lines = text.splitlines()
    if any(_FORWARD.match(line) for line in lines):
        return ""
    for index, line in enumerate(lines):
        if line == "-- " or _ORIGINAL.match(line):
            break
        if (
            re.match(r"^_{5,}\s*$", line)
            and len(lines) > index + 1
            and lines[index + 1].lstrip().lower().startswith("from:")
        ):
            break
        if _ATTRIBUTION.match(line) or (
            index + 1 < len(lines) and _ATTRIBUTION.match(line + " " + lines[index + 1])
        ):
            # A wrapped attribution can include the second line. Substantive
            # unquoted text below a quote signals an interleaved reply.
            start = index + (1 if _ATTRIBUTION.match(line) else 2)
            if any(t.strip() and not t.lstrip().startswith(">") for t in lines[start:]):
                return ""
            break
        if line.lstrip().startswith(">"):
            if any(t.strip() and not t.lstrip().startswith(">") for t in lines[index:]):
                return ""
            break
    else:
        index = len(lines)
    authored = "\n".join(lines[:index]).strip()
    before, separator, last = authored.rpartition("\n")
    if separator and last.strip().lower() in _MOBILE_SIGNATURES:
        return before.strip()
    return authored
