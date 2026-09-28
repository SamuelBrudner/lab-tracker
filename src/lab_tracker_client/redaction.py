"""Credential scrubbing for text a capture copies into an event.

Pipeline and scheduler logs, pipeline commands (``dvc.lock`` ``cmd``) and
error messages routinely echo tokens: an ``Authorization`` header, a
``--token abc`` argument, ``password=...`` in a connection string, or
``https://user:secret@host`` in a URL. :func:`redact_capture_text` removes
those before the text reaches an outbox event, a rendered note, or note
metadata. It errs on the side of over-redaction: a log excerpt is context for a
reviewer, never a record that must be byte-exact.
"""

from __future__ import annotations

import os
import re

from lab_tracker.provider_error_redaction import REDACTED, provider_error_message

# Environment variables whose values are credentials the client itself holds.
SECRET_ENV_VARS = (
    "LAB_TRACKER_ACCESS_TOKEN",
    "LAB_TRACKER_TOKEN",
    "LAB_TRACKER_PASSWORD",
    "LAB_TRACKER_MCP_PASSWORD",
    "LAB_TRACKER_MCP_TOKEN",
    "LAB_TRACKER_MCP_API_KEY",
    "LAB_TRACKER_API_KEY",
)
_SENSITIVE_WORD = (
    r"(?:password|passwd|passphrase|token|secret|api[_-]?key|access[_-]?key|credentials?)"
)
# ``password=...`` / ``API_KEY: ...`` / ``"token": "..."`` — a key ending in a
# sensitive word, then ``=`` or ``:``, then the value.
_ASSIGNMENT = re.compile(
    rf"(?P<key>[\"']?[\w.-]*{_SENSITIVE_WORD}[\"']?)(?P<sep>\s*[=:]\s*)"
    r"(?P<value>\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)",
    re.IGNORECASE,
)
# ``--token abc`` / ``-password abc`` — a command-line flag naming a secret.
_FLAG = re.compile(
    rf"(?P<flag>(?<![\w-])--?[\w-]*{_SENSITIVE_WORD}[\w-]*)(?P<sep>\s+)(?P<value>[^\s-][^\s]*)",
    re.IGNORECASE,
)
# ``scheme://user:password@host`` — credentials embedded in a URL.
_URL_USERINFO = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")
_TOKEN_SHAPES = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9_-]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"
)
_BEARER = re.compile(r"\bBearer\s+(?!\[REDACTED\])[A-Za-z0-9._~+/-]+=*", re.IGNORECASE)


def redact_capture_text(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Return ``text`` with credential material replaced by ``[REDACTED]``.

    Removes the literal values of the client's own credential environment
    variables and any extra ``secrets``, bearer/API-key headers and query
    parameters, Lab Tracker/OpenAI/Google/GitHub/GitLab/Slack/AWS key shapes,
    ``key=value`` assignments and ``--flag value`` arguments whose name ends in
    a sensitive word, and ``user:password@`` in URLs.
    """

    if not text:
        return text
    known = tuple(
        value
        for value in (*secrets, *(os.getenv(name) or "" for name in SECRET_ENV_VARS))
        if len(value.strip()) >= 4
    )
    cleaned = provider_error_message(text, secrets=known)
    cleaned = _URL_USERINFO.sub(lambda match: f"{match.group('scheme')}{REDACTED}@", cleaned)
    cleaned = _TOKEN_SHAPES.sub(REDACTED, cleaned)
    cleaned = _BEARER.sub(f"Bearer {REDACTED}", cleaned)
    cleaned = _ASSIGNMENT.sub(
        lambda match: f"{match.group('key')}{match.group('sep')}{REDACTED}", cleaned
    )
    return _FLAG.sub(lambda match: f"{match.group('flag')}{match.group('sep')}{REDACTED}", cleaned)


__all__ = ["SECRET_ENV_VARS", "redact_capture_text"]
