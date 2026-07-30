"""Credential-safe normalization for model-provider failures.

Provider exceptions are unusually likely to contain request URLs, headers, or
upstream response bodies.  Normalize those details once, before they cross an
exception, logging, or persistence boundary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"

_SENSITIVE_QUERY_PARAMETER = (
    r"(?:key|api[_-]?key|access[_-]?token|auth[_-]?token|token)"
)
_QUERY_SECRET_RE = re.compile(
    rf"(?P<separator>[?&]){_SENSITIVE_QUERY_PARAMETER}="
    r"(?P<value>[^&#\s\"'<>]*)",
    re.IGNORECASE,
)
_ENCODED_QUERY_SECRET_RE = re.compile(
    rf"(?P<separator>%3[fF]|%26){_SENSITIVE_QUERY_PARAMETER}(?:=|%3[dD])"
    r"(?P<value>.*?)(?=%26|[\s\"'<>]|$)",
    re.IGNORECASE,
)
_SENSITIVE_HEADER_RE = re.compile(
    r"\b(?P<name>x-goog-api-key|x-api-key|api-key|authorization)"
    r"\s*[:=]\s*(?:Bearer\s+)?(?P<value>[^\s,;}\]]+)",
    re.IGNORECASE,
)
_GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
_OPENAI_ANTHROPIC_API_KEY_RE = re.compile(
    r"\bsk-(?:ant-)?[0-9A-Za-z_-]{12,}\b",
    re.IGNORECASE,
)
_LAB_TRACKER_BEARER_RE = re.compile(
    r"\b(?:linv|lpat|ldev|lpair)_[0-9A-Za-z._~-]+",
    re.IGNORECASE,
)

_PROVIDER_SECRET_FIELDS = (
    "openai_api_key",
    "anthropic_api_key",
    "google_api_key",
)


def configured_provider_secrets(settings: object | None) -> tuple[str, ...]:
    """Return non-empty provider credentials without assuming a Settings type."""

    if settings is None:
        return ()
    return tuple(
        secret
        for field_name in _PROVIDER_SECRET_FIELDS
        if (secret := str(getattr(settings, field_name, "") or "").strip())
    )


def provider_error_message(
    value: object,
    *,
    secrets: Iterable[str] = (),
) -> str:
    """Return useful provider diagnostics with credential material removed."""

    text = str(value)
    normalized_secrets = sorted(
        {secret.strip() for secret in secrets if secret and secret.strip()},
        key=len,
        reverse=True,
    )
    for secret in normalized_secrets:
        text = text.replace(secret, REDACTED)
        encoded_forms = {
            quote(secret, safe=""),
            quote_plus(secret, safe=""),
        }
        for encoded_secret in encoded_forms:
            if encoded_secret and encoded_secret != secret:
                text = text.replace(encoded_secret, REDACTED)

    # Remove the parameter name as well as its value.  That prevents error
    # metadata from retaining a credential-shaped ``?key=...`` URL fragment.
    text = _QUERY_SECRET_RE.sub(
        lambda match: f"{match.group('separator')}{REDACTED}",
        text,
    )
    text = _ENCODED_QUERY_SECRET_RE.sub(
        lambda match: f"{match.group('separator')}{REDACTED}",
        text,
    )
    text = _SENSITIVE_HEADER_RE.sub(
        lambda match: f"{match.group('name')}: {REDACTED}",
        text,
    )
    text = _LAB_TRACKER_BEARER_RE.sub(REDACTED, text)
    text = _GOOGLE_API_KEY_RE.sub(REDACTED, text)
    return _OPENAI_ANTHROPIC_API_KEY_RE.sub(REDACTED, text)
