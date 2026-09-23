"""Session link codes and the per-checkout active session.

A session's link code is the base32 form of its UUID (26 characters from
``A-Z2-7``), the same code the server prints on a session and that a bench
phone scans. Consumers can name it in three places so captures arrive already
linked to the session:

* ``lt session use <code-or-uuid>`` records an *active session* for the
  current checkout (``.lab-tracker/session.json``) for a bounded time; figure
  saves and watch scans made while it is active carry a session target.
* ``LAB_TRACKER_SESSION_ID`` overrides it for one shell or job.
* A watched folder or file whose name contains the code (for example
  ``session001_LT-<code>/``) attaches to that session without any setup.

Everything here is fail-soft: an unreadable or expired context simply yields
no session, never an exception in a consumer script.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import uuid
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lab_tracker_client.client import LTValidationError
from lab_tracker_client.outbox import write_json_atomic

JsonObject = dict[str, Any]

ACTIVE_SESSION_RELATIVE_PATH = Path(".lab-tracker") / "session.json"
ACTIVE_SESSION_ENV = "LAB_TRACKER_SESSION_ID"
ACTIVE_SESSION_CONTEXT_ENV = "LAB_TRACKER_SESSION_CONTEXT"
DEFAULT_ACTIVE_SESSION_HOURS = 12.0
_LINK_CODE_LENGTH = 26
# A bare 26-character base32 token, optionally prefixed ``LT-``, that is not
# part of a longer alphanumeric run. Base32 uses no 0/1/8/9, so ordinary hex
# ids and timestamps never match.
_LINK_CODE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:LT-)?([A-Za-z2-7]{26})(?![A-Za-z0-9])"
)


def encode_session_link_code(session_id: str | uuid.UUID) -> str:
    """Return the link code (base32, no padding) for a session UUID."""

    resolved = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    return base64.b32encode(resolved.bytes).decode("ascii").rstrip("=")


def decode_session_link_code(link_code: str) -> str:
    """Return the session UUID string for a link code, or raise ``LTValidationError``."""

    normalized = re.sub(r"[\s-]+", "", str(link_code or ""))
    if normalized.upper().startswith("LT") and len(normalized) == _LINK_CODE_LENGTH + 2:
        normalized = normalized[2:]
    normalized = normalized.upper()
    if len(normalized) != _LINK_CODE_LENGTH:
        raise LTValidationError("Session link codes are 26 base32 characters.")
    try:
        decoded = base64.b32decode(normalized + "======", casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise LTValidationError("Session link code has invalid characters.") from exc
    if len(decoded) != 16:
        raise LTValidationError("Session link code has an invalid length.")
    return str(uuid.UUID(bytes=decoded))


def looks_like_link_code(value: str) -> bool:
    text = str(value or "").strip()
    return bool(_LINK_CODE_TOKEN.fullmatch(text))


def session_id_from_reference(value: str | None) -> str | None:
    """Accept a session UUID or its link code; return the session id.

    A link code is decoded to its UUID; a UUID is normalized; any other
    non-blank value passes through unchanged so callers that already hold a
    server-issued id (or a test id) keep working. ``None``/blank returns
    ``None`` so optional arguments pass straight through.
    """

    text = str(value or "").strip()
    if not text:
        return None
    with suppress(ValueError):
        return str(uuid.UUID(text))
    if looks_like_link_code(text):
        return decode_session_link_code(text)
    return text


def strict_session_id(value: str | None) -> str:
    """A session UUID or link code, or ``LTValidationError``."""

    text = str(value or "").strip()
    if not text:
        raise LTValidationError("A session UUID or link code is required.")
    with suppress(ValueError):
        return str(uuid.UUID(text))
    return decode_session_link_code(text)


def find_session_link_code(text: str) -> tuple[str, str] | None:
    """Find the first session link code in free text (a path, a title).

    Returns ``(link_code, session_id)`` or ``None``. Only tokens that decode to
    a UUID count, so a random 26-letter word cannot claim a session.
    """

    for match in _LINK_CODE_TOKEN.finditer(str(text or "")):
        code = match.group(1).upper()
        with suppress(LTValidationError):
            return code, decode_session_link_code(code)
    return None


def _checkout_root(start: str | Path | None) -> Path:
    from lab_tracker_client import git_capture

    origin = Path(start or Path.cwd()).expanduser()
    with suppress(Exception):
        return git_capture.repo_toplevel(origin)
    return origin.resolve()


def active_session_path(start: str | Path | None = None) -> Path:
    override = os.getenv(ACTIVE_SESSION_CONTEXT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return _checkout_root(start) / ACTIVE_SESSION_RELATIVE_PATH


def set_active_session(
    session_ref: str,
    *,
    project_id: str | None = None,
    hours: float = DEFAULT_ACTIVE_SESSION_HOURS,
    start: str | Path | None = None,
    dry_run: bool = False,
) -> JsonObject:
    """Record the session captures from this checkout should attach to."""

    session_id = strict_session_id(session_ref)
    if hours <= 0:
        raise LTValidationError("--hours must be greater than zero.")
    now = datetime.now(timezone.utc)
    payload: JsonObject = {
        "version": 1,
        "session_id": session_id,
        "link_code": encode_session_link_code(session_id),
        "set_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=hours)).isoformat(),
    }
    if project_id:
        payload["project_id"] = str(project_id)
    path = active_session_path(start)
    result: JsonObject = {
        "command": "session-use",
        "action": "would-set" if dry_run else "set",
        "path": str(path),
        "dry_run": dry_run,
        **payload,
    }
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, payload)
    return result


def clear_active_session(*, start: str | Path | None = None, dry_run: bool = False) -> JsonObject:
    path = active_session_path(start)
    existed = path.exists()
    if existed and not dry_run:
        path.unlink()
    return {
        "command": "session-clear",
        "action": ("would-clear" if dry_run else "cleared") if existed else "absent",
        "path": str(path),
        "dry_run": dry_run,
    }


def read_active_session(start: str | Path | None = None) -> JsonObject | None:
    """Return the active session mapping, or ``None`` when unset or expired.

    ``LAB_TRACKER_SESSION_ID`` (a UUID or link code) wins over the checkout
    file so one shell or scheduler job can pin a session explicitly.
    """

    env_value = os.getenv(ACTIVE_SESSION_ENV)
    if env_value:
        with suppress(LTValidationError):
            session_id = strict_session_id(env_value)
            return {
                "session_id": session_id,
                "link_code": encode_session_link_code(session_id),
                "source": "env",
            }
        return None
    with suppress(Exception):
        payload = json.loads(active_session_path(start).read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return None
        session_id = strict_session_id(str(payload.get("session_id") or ""))
        expires_at = str(payload.get("expires_at") or "")
        if expires_at and _parse_iso(expires_at) <= datetime.now(timezone.utc):
            return None
        return {
            "session_id": session_id,
            "link_code": encode_session_link_code(session_id),
            "project_id": str(payload.get("project_id") or "") or None,
            "expires_at": expires_at or None,
            "source": "checkout",
        }
    return None


def active_session_status(start: str | Path | None = None) -> JsonObject:
    path = active_session_path(start)
    active = read_active_session(start)
    return {
        "command": "session-status",
        "path": str(path),
        "present": path.exists() or bool(os.getenv(ACTIVE_SESSION_ENV)),
        "active": active is not None,
        **(active or {}),
    }


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


__all__ = [
    "ACTIVE_SESSION_ENV",
    "DEFAULT_ACTIVE_SESSION_HOURS",
    "active_session_path",
    "active_session_status",
    "clear_active_session",
    "decode_session_link_code",
    "encode_session_link_code",
    "find_session_link_code",
    "looks_like_link_code",
    "read_active_session",
    "session_id_from_reference",
    "set_active_session",
    "strict_session_id",
]
