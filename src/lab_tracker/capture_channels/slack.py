"""Inbound Slack capture: a slash command and a "Save to Lab Tracker" shortcut.

Slack signs every request with the app's signing secret (``v0`` HMAC-SHA256
over ``v0:<timestamp>:<raw body>``); nothing here needs a bot token or makes an
outbound call. A verified request from a mapped Slack user in a mapped channel
becomes a staged text note in the mapped project, authored by the mapped Lab
Tracker user -- the person acted in Slack and Slack's signature proves it. An
unmapped user or channel gets an ephemeral reply and nothing is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal, Protocol
from urllib.parse import parse_qs
from uuid import UUID

from lab_tracker.capture_channels.common import (
    CAPTURE_CHANNEL_KEY,
    CAPTURED_AT_KEY,
    EVIDENCE_ADAPTER_KEY,
    EVIDENCE_CAPTURE_KIND_KEY,
    EVIDENCE_SOURCE_EXTERNAL_ID_KEY,
    EVIDENCE_SOURCE_PROVIDER_KEY,
    EVIDENCE_SOURCE_URI_KEY,
    UserDirectory,
    bound_text,
    channel_principal,
    resolve_user_ref,
    stable_key,
)
from lab_tracker.capture_channels.settings import (
    parse_slack_channel_projects,
    parse_slack_id_map,
    parse_slack_workspace_url,
    parse_user_emails,
)
from lab_tracker.errors import ConflictError, NotFoundError, PermissionDeniedError
from lab_tracker.models import EntityOrigin, NoteMetadataScalar, NoteStatus

SLACK_CHANNEL: Final = "slack"
SLACK_ADAPTER: Final = "lab-tracker-slack"
SLACK_SIGNATURE_VERSION: Final = "v0"
# Slack's own guidance: refuse requests whose timestamp is over five minutes off.
MAX_SLACK_REQUEST_SKEW_SECONDS: Final = 300
MAX_SLACK_BODY_BYTES: Final = 64 * 1024
_SLACK_TS_RE = re.compile(r"[0-9]{1,12}\.[0-9]{1,9}\Z")
_SLACK_ID_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{1,63}\Z")


class SlackRequestRejected(Exception):
    """A request failed signature, freshness, size, or shape checks."""


@dataclass(frozen=True, slots=True)
class SlackConfig:
    """Parsed Slack channel configuration (see ``Settings.slack_*``)."""

    signing_secret: str
    workspace_url: str
    channel_projects: dict[str, UUID]
    users: dict[str, str]
    email_directory: dict[str, str]

    def __repr__(self) -> str:
        return "SlackConfig(<redacted>)"

    @classmethod
    def from_settings(cls, settings: Any) -> SlackConfig | None:
        """Return the configuration, or ``None`` when Slack capture is off."""

        secret = str(settings.slack_signing_secret or "").strip()
        if not secret:
            return None
        return cls(
            signing_secret=secret,
            workspace_url=parse_slack_workspace_url(
                settings.slack_workspace_url or "", variable="LAB_TRACKER_SLACK_WORKSPACE_URL"
            ),
            channel_projects=parse_slack_channel_projects(
                settings.slack_channel_projects or "",
                variable="LAB_TRACKER_SLACK_CHANNEL_PROJECTS",
            ),
            users=parse_slack_id_map(
                settings.slack_users or "", variable="LAB_TRACKER_SLACK_USERS", kind="user"
            ),
            email_directory=parse_user_emails(
                settings.capture_user_emails or "", variable="LAB_TRACKER_CAPTURE_USER_EMAILS"
            ),
        )

    def permalink(self, channel_id: str, message_ts: str | None) -> str | None:
        """``https://<workspace>.slack.com/archives/<channel>/p<ts without dot>``."""

        if not self.workspace_url or not message_ts:
            return None
        return f"{self.workspace_url}/archives/{channel_id}/p{message_ts.replace('.', '')}"


def slack_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Compute Slack's ``v0=<hex>`` request signature."""

    base = f"{SLACK_SIGNATURE_VERSION}:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"{SLACK_SIGNATURE_VERSION}={digest}"


def verify_slack_request(
    *,
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float,
) -> None:
    """Reject a stale, replayed, oversized, or tampered request (constant-time compare)."""

    if len(body) > MAX_SLACK_BODY_BYTES:
        raise SlackRequestRejected("Slack request body is too large.")
    if not timestamp or not timestamp.isascii() or not timestamp.isdigit():
        raise SlackRequestRejected("Slack request timestamp is missing.")
    if abs(now - int(timestamp)) > MAX_SLACK_REQUEST_SKEW_SECONDS:
        raise SlackRequestRejected("Slack request timestamp is outside the replay window.")
    if not signature or not signature.isascii():
        raise SlackRequestRejected("Slack request signature is missing.")
    expected = slack_signature(secret, timestamp, body)
    if not hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii")):
        raise SlackRequestRejected("Slack request signature does not match.")


@dataclass(frozen=True, slots=True)
class SlackCapture:
    """One verified capture request, normalized across the two entry points."""

    kind: Literal["command", "message"]
    team_id: str
    channel_id: str
    user_id: str
    text: str
    message_ts: str | None = None
    message_user_id: str | None = None
    trigger_id: str | None = None


@dataclass(frozen=True, slots=True)
class SlackReply:
    """An ephemeral reply only the acting user sees."""

    text: str
    stored: bool = False

    def body(self) -> dict[str, str]:
        return {"response_type": "ephemeral", "text": self.text}


def parse_slash_command(body: bytes) -> SlackCapture | None:
    """Parse a slash-command form body; ``None`` for Slack's SSL check."""

    form = _form(body)
    if form.get("ssl_check") == "1":
        return None
    return SlackCapture(
        kind="command",
        team_id=_slack_id(form.get("team_id"), "team"),
        channel_id=_slack_id(form.get("channel_id"), "channel"),
        user_id=_slack_id(form.get("user_id"), "user"),
        text=form.get("text", ""),
        trigger_id=_bounded_token(form.get("trigger_id")),
    )


def parse_interactivity(body: bytes) -> SlackCapture | SlackReply | None:
    """Parse an interactivity payload.

    Returns a capture for the ``message_action`` shortcut, a reply for a
    global shortcut (it carries no message to save), and ``None`` for every
    other interaction type, which is simply acknowledged.
    """

    raw_payload = _form(body).get("payload")
    if raw_payload is None:
        raise SlackRequestRejected("Slack interactivity request has no payload.")
    try:
        payload = json.loads(raw_payload)
    except (ValueError, RecursionError) as exc:
        raise SlackRequestRejected("Slack interactivity payload is not JSON.") from exc
    if not isinstance(payload, dict):
        raise SlackRequestRejected("Slack interactivity payload is not an object.")
    kind = payload.get("type")
    if kind == "shortcut":
        return SlackReply("Use Save to Lab Tracker from a message's ⋮ menu to save that message.")
    if kind != "message_action":
        return None
    team, channel = _object(payload.get("team")), _object(payload.get("channel"))
    user, message = _object(payload.get("user")), _object(payload.get("message"))
    message_ts = message.get("ts")
    if not isinstance(message_ts, str) or not _SLACK_TS_RE.fullmatch(message_ts):
        raise SlackRequestRejected("Slack message shortcut carries no message timestamp.")
    message_user = message.get("user")
    text = message.get("text")
    return SlackCapture(
        kind="message",
        team_id=_slack_id(team.get("id"), "team"),
        channel_id=_slack_id(channel.get("id"), "channel"),
        user_id=_slack_id(user.get("id"), "user"),
        text=text if isinstance(text, str) else "",
        message_ts=message_ts,
        message_user_id=(
            message_user
            if isinstance(message_user, str) and _SLACK_ID_RE.fullmatch(message_user)
            else None
        ),
        trigger_id=_bounded_token(payload.get("trigger_id")),
    )


class NoteCreator(Protocol):
    """The note-creating slice of ``LabTrackerAPI`` this channel uses."""

    def create_note_result(self, *args: Any, **kwargs: Any) -> Any: ...


def capture_slack_request(
    capture: SlackCapture,
    *,
    config: SlackConfig,
    users: UserDirectory,
    api: NoteCreator,
    request_timestamp: int,
) -> SlackReply:
    """Stage one Slack capture, or explain (ephemerally) why nothing was stored."""

    project_id = config.channel_projects.get(capture.channel_id)
    if project_id is None:
        return SlackReply(
            f"This channel ({capture.channel_id}) is not connected to a Lab Tracker "
            "project, so nothing was saved. Ask your Lab Tracker admin to map it."
        )
    user_ref = config.users.get(capture.user_id)
    user = (
        resolve_user_ref(users, user_ref, email_directory=config.email_directory)
        if user_ref is not None
        else None
    )
    if user is None:
        return SlackReply(
            f"Your Slack account ({capture.user_id}) is not linked to a Lab Tracker user, "
            "so nothing was saved. Ask your Lab Tracker admin to link it."
        )
    body = bound_text(_unescape_slack(capture.text))
    if not body.text:
        usage = (
            "Add some text after the command, for example: /lt Rig 2 fly 12 looks dehydrated."
            if capture.kind == "command"
            else "That message has no text to save."
        )
        return SlackReply(f"Nothing was saved. {usage}")

    try:
        result = api.create_note_result(
            project_id=project_id,
            raw_content=body.text,
            metadata=_note_metadata(
                capture,
                config=config,
                body_metadata=body.metadata(),
                request_timestamp=request_timestamp,
            ),
            client_capture_id=_client_capture_id(capture, request_timestamp=request_timestamp),
            status=NoteStatus.STAGED,
            actor=channel_principal(user, label=SLACK_CHANNEL),
            origin=EntityOrigin.USER,
            origin_provider=SLACK_CHANNEL,
        )
    except PermissionDeniedError:
        return SlackReply(
            "You are not a contributor on the Lab Tracker project this channel maps to, "
            "so nothing was saved."
        )
    except NotFoundError:
        return SlackReply(
            "The Lab Tracker project this channel maps to no longer exists; nothing was saved."
        )
    except ConflictError:
        return SlackReply("This was already saved to Lab Tracker.", stored=True)
    if getattr(result, "reused", False):
        return SlackReply("This was already saved to Lab Tracker.", stored=True)
    return SlackReply("Saved to Lab Tracker as a staged note for your next review.", stored=True)


def _note_metadata(
    capture: SlackCapture,
    *,
    config: SlackConfig,
    body_metadata: dict[str, str | int | bool],
    request_timestamp: int,
) -> dict[str, NoteMetadataScalar]:
    metadata: dict[str, NoteMetadataScalar] = {
        CAPTURE_CHANNEL_KEY: SLACK_CHANNEL,
        EVIDENCE_SOURCE_PROVIDER_KEY: SLACK_CHANNEL,
        EVIDENCE_ADAPTER_KEY: SLACK_ADAPTER,
        EVIDENCE_CAPTURE_KIND_KEY: "text",
        "slack_team_id": capture.team_id,
        "slack_channel_id": capture.channel_id,
        "slack_user_id": capture.user_id,
        "slack_capture_kind": "slash_command" if capture.kind == "command" else "message_shortcut",
    }
    if capture.message_ts is not None:
        metadata["slack_message_ts"] = capture.message_ts
        metadata[EVIDENCE_SOURCE_EXTERNAL_ID_KEY] = f"{capture.channel_id}:{capture.message_ts}"
        posted_at = datetime.fromtimestamp(float(capture.message_ts), timezone.utc)
        metadata[CAPTURED_AT_KEY] = posted_at.isoformat()
    else:
        metadata[CAPTURED_AT_KEY] = datetime.fromtimestamp(
            request_timestamp, timezone.utc
        ).isoformat()
    if capture.message_user_id is not None:
        metadata["slack_message_user_id"] = capture.message_user_id
    permalink = config.permalink(capture.channel_id, capture.message_ts)
    if permalink is not None:
        metadata["slack_permalink"] = permalink
        metadata[EVIDENCE_SOURCE_URI_KEY] = permalink
    metadata.update(body_metadata)
    return metadata


def _client_capture_id(capture: SlackCapture, *, request_timestamp: int) -> str:
    if capture.kind == "message" and capture.message_ts is not None:
        # One capture per (message, saver): each person's save is their own act.
        return stable_key(
            "slack", capture.team_id, capture.channel_id, capture.message_ts, capture.user_id
        )
    invocation = capture.trigger_id or f"{request_timestamp}:{capture.text}"
    return stable_key("slack-cmd", capture.team_id, capture.channel_id, capture.user_id, invocation)


def _form(body: bytes) -> dict[str, str]:
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SlackRequestRejected("Slack request body is not UTF-8.") from exc
    parsed = parse_qs(decoded, keep_blank_values=True, max_num_fields=64)
    return {key: values[0] for key, values in parsed.items() if values}


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _slack_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SLACK_ID_RE.fullmatch(value):
        raise SlackRequestRejected(f"Slack request has no valid {label} id.")
    return value


def _bounded_token(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 200 or not value.isprintable():
        return None
    return value


def _unescape_slack(text: str) -> str:
    """Undo Slack's three HTML escapes; mention/link markup is kept verbatim."""

    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
