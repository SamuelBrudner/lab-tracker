"""Canonical Lab Tracker instance URL handling.

An instance has one public/API origin. Browser and API routes are derived from
that origin; role-specific environment variables are compatibility aliases,
not separate configuration that callers should have to keep in sync.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

BASE_URL_ENV = "LAB_TRACKER_BASE_URL"
LEGACY_CANONICAL_BASE_URL_ENV = "LAB_TRACKER_CANONICAL_BASE_URL"
LEGACY_MCP_BASE_URL_ENV = "LAB_TRACKER_MCP_BASE_URL"
LEGACY_PUBLIC_BASE_URL_ENV = "LAB_TRACKER_PUBLIC_BASE_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def normalize_instance_base_url(
    value: str | None,
    *,
    setting_name: str = BASE_URL_ENV,
    allow_empty: bool = False,
) -> str:
    """Return a scheme+authority origin with no path or trailing slash.

    ``/app`` is accepted as a migration convenience because people naturally
    copy the browser URL. It is removed so API clients never request
    ``/app/projects`` and browser links never become ``/app/app``.
    """

    cleaned = str(value or "").strip()
    if not cleaned:
        if allow_empty:
            return ""
        raise ValueError(f"{setting_name} must not be empty.")
    if any(character.isspace() for character in cleaned):
        raise ValueError(f"{setting_name} must not contain whitespace.")

    try:
        parsed = urlsplit(cleaned)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"{setting_name} is not a valid URL.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError(f"{setting_name} must be an absolute http:// or https:// URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{setting_name} must not contain credentials.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{setting_name} contains an invalid port.") from exc
    if parsed.query or parsed.fragment:
        raise ValueError(f"{setting_name} must not contain a query or fragment.")

    path = parsed.path.rstrip("/")
    if path not in {"", "/app"}:
        raise ValueError(
            f"{setting_name} must be an origin with no path; a trailing /app "
            "browser route is accepted and removed."
        )
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", ""))


def resolve_instance_base_url(
    candidates: Iterable[tuple[str, str | None]],
    *,
    default: str = DEFAULT_BASE_URL,
) -> str:
    """Resolve the first configured URL in explicit precedence order."""

    for setting_name, value in candidates:
        if str(value or "").strip():
            return normalize_instance_base_url(value, setting_name=setting_name)
    return normalize_instance_base_url(default, setting_name="default base URL")


def build_instance_url(base_url: str, path: str) -> str:
    """Derive a route URL from an instance origin."""

    origin = normalize_instance_base_url(base_url)
    cleaned_path = str(path or "").strip()
    if not cleaned_path:
        return origin
    return f"{origin}/{cleaned_path.lstrip('/')}"


__all__ = [
    "BASE_URL_ENV",
    "DEFAULT_BASE_URL",
    "LEGACY_CANONICAL_BASE_URL_ENV",
    "LEGACY_MCP_BASE_URL_ENV",
    "LEGACY_PUBLIC_BASE_URL_ENV",
    "build_instance_url",
    "normalize_instance_base_url",
    "resolve_instance_base_url",
]
