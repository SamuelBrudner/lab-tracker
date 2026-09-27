"""Session link codes and the per-checkout active session.

A session's link code is the base32 form of its UUID (26 characters from
``A-Z2-7``), the same code the server prints on a session and that a bench
phone scans. Consumers can name it in three places so captures arrive already
linked to the session:

* ``lt session use <code-or-uuid>`` looks the session up on the server and
  records it, with the project it belongs to, as the *active session* for the
  current checkout (``.lab-tracker/session.json``) for a bounded time; figure
  saves and watch scans made while it is active carry a session target when
  they are filed into that same project. A context recorded without its
  project (by an older client) is unverified and targets nothing.
* ``LAB_TRACKER_SESSION_ID`` overrides it for one shell or job.
* A watched folder or file whose name contains the ``LT-``-prefixed code
  (for example ``session001_LT-<code>/``) attaches to that session without
  any setup.

Reading is fail-soft: an unreadable or expired context simply yields no
session, never an exception in a consumer script. Recording is not: ``lt
session use`` fails loudly when the session does not exist or the server
cannot be reached.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from lab_tracker import models as _models
from lab_tracker_client.client import LTAPIError, LTValidationError
from lab_tracker_client.outbox import write_json_atomic

JsonObject = dict[str, Any]

ACTIVE_SESSION_RELATIVE_PATH = Path(".lab-tracker") / "session.json"
ACTIVE_SESSION_ENV = "LAB_TRACKER_SESSION_ID"
ACTIVE_SESSION_CONTEXT_ENV = "LAB_TRACKER_SESSION_CONTEXT"
DEFAULT_ACTIVE_SESSION_HOURS = 12.0
# How read_active_session labels where the session came from.
ACTIVE_SESSION_SOURCE_ENV = "env"
ACTIVE_SESSION_SOURCE_CHECKOUT = "checkout"
UNVERIFIED_SESSION_HINT = (
    "lab-tracker: the active session {session_id} was recorded without its project, "
    "so captures no longer attach to it; run `lt session use {link_code}` again to "
    "verify it."
)
_HINTS_SHOWN: set[str] = set()
_LINK_CODE_LENGTH = 26
LINK_CODE_PREFIX = "LT-"
# A bare 26-character base32 token, optionally prefixed ``LT-``, that is not
# part of a longer alphanumeric run: the form a person types as a reference.
# Base32 uses no 0/1/8/9, so ordinary hex ids and timestamps never match.
_LINK_CODE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:LT-)?([A-Za-z2-7]{26})(?![A-Za-z0-9])"
)
# Inside a path only the explicit prefix counts: any 26 base32 letters decode
# to 16 bytes, so an unprefixed run (``supplementaryinformationaq/``) would
# otherwise claim a session that does not exist.
_PATH_LINK_CODE_TOKEN = re.compile(
    rf"(?<![A-Za-z0-9]){re.escape(LINK_CODE_PREFIX)}([A-Za-z2-7]{{26}})(?![A-Za-z0-9])"
)


def encode_session_link_code(session_id: str | uuid.UUID) -> str:
    """Return the link code (base32, no padding) for a session UUID.

    The same codec the server prints on a session (``Session.link_code``).
    """

    resolved = session_id if isinstance(session_id, uuid.UUID) else uuid.UUID(str(session_id))
    return _models.encode_session_link_code(resolved)


def decode_session_link_code(link_code: str) -> str:
    """Return the session UUID string for a link code, or raise ``LTValidationError``."""

    normalized = _models.normalize_link_code(str(link_code or ""))
    if normalized.startswith("LT") and len(normalized) == _LINK_CODE_LENGTH + 2:
        normalized = normalized[2:]
    if len(normalized) != _LINK_CODE_LENGTH:
        raise LTValidationError("Session link codes are 26 base32 characters.")
    try:
        session_id = _models.decode_session_link_code(normalized)
    except ValueError as exc:
        raise LTValidationError(f"Session link code is invalid: {exc}") from exc
    # Base32 ignores the last character's two pad bits, so a code that does
    # not re-encode to itself was never printed by the server.
    if _models.encode_session_link_code(session_id) != normalized:
        raise LTValidationError(
            f"Session link code {normalized} is not one the server prints; check it for a typo."
        )
    return str(session_id)


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
    """Find the first ``LT-``-prefixed session link code in a path or title.

    Returns ``(link_code, session_id)`` or ``None``. Only an explicit
    ``LT-<code>`` token counts, and only when the code is in the canonical
    form the server prints (it re-encodes to itself, so its pad bits are
    zero): a 26-letter folder name never claims a session.
    """

    for match in _PATH_LINK_CODE_TOKEN.finditer(str(text or "")):
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


class SessionLookup(Protocol):
    """The one server call recording an active session needs."""

    def get_session(self, session_id: str) -> Mapping[str, Any]: ...


def set_active_session(
    session_ref: str,
    *,
    client: SessionLookup,
    project_id: str | None = None,
    hours: float = DEFAULT_ACTIVE_SESSION_HOURS,
    start: str | Path | None = None,
    dry_run: bool = False,
) -> JsonObject:
    """Verify a session on the server and record it, with its project, for this checkout.

    Raises ``LTAPIError`` when the session does not exist or the server cannot
    be reached, and ``LTValidationError`` when ``project_id`` names a
    different project than the session's own: nothing is written then.
    """

    session_id = strict_session_id(session_ref)
    if hours <= 0:
        raise LTValidationError("--hours must be greater than zero.")
    session_project_id = _server_session_project(client, session_id)
    if project_id and str(project_id) != session_project_id:
        raise LTValidationError(
            f"Session {session_id} belongs to project {session_project_id}, "
            f"not {project_id}."
        )
    now = datetime.now(timezone.utc)
    payload: JsonObject = {
        "version": 1,
        "session_id": session_id,
        "link_code": encode_session_link_code(session_id),
        "project_id": session_project_id,
        "set_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=hours)).isoformat(),
    }
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


def _server_session_project(client: SessionLookup, session_id: str) -> str:
    try:
        record = client.get_session(session_id)
    except httpx.TransportError as exc:
        raise LTAPIError(
            f"Could not reach Lab Tracker to verify session {session_id} ({exc}); "
            "the active session was not changed."
        ) from exc
    except LTAPIError as exc:
        raise LTAPIError(
            f"Session {session_id} could not be verified: {exc} "
            "The active session was not changed."
        ) from exc
    project_id = str(record.get("project_id") or "").strip()
    if not project_id:
        raise LTAPIError(f"Lab Tracker returned session {session_id} without a project.")
    return project_id


def session_target(active: Mapping[str, Any] | None, project_id: str | None) -> str | None:
    """The active session's id when it may be a declared target in ``project_id``.

    ``LAB_TRACKER_SESSION_ID`` is a per-shell choice and always targets. A
    checkout context targets only captures filed into the project recorded
    with it; one recorded without a project (by an older client) is
    unverified, targets nothing, and prints a one-line hint once. A session
    that does not target stays on the capture as plain metadata.
    """

    if not active or not active.get("session_id"):
        return None
    session_id = str(active["session_id"])
    if active.get("source") == ACTIVE_SESSION_SOURCE_ENV:
        return session_id
    recorded_project = str(active.get("project_id") or "").strip()
    if not recorded_project:
        _hint_once(
            UNVERIFIED_SESSION_HINT.format(
                session_id=session_id,
                link_code=active.get("link_code") or session_id,
            )
        )
        return None
    return session_id if recorded_project == str(project_id or "") else None


def _reset_session_hints_for_tests() -> None:
    _HINTS_SHOWN.clear()


def _hint_once(message: str) -> None:
    if message in _HINTS_SHOWN:
        return
    _HINTS_SHOWN.add(message)
    print(message, file=sys.stderr)


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
                "source": ACTIVE_SESSION_SOURCE_ENV,
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
            "source": ACTIVE_SESSION_SOURCE_CHECKOUT,
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
    "ACTIVE_SESSION_SOURCE_CHECKOUT",
    "ACTIVE_SESSION_SOURCE_ENV",
    "SessionLookup",
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
    "session_target",
    "set_active_session",
    "strict_session_id",
]
