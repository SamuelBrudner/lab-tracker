"""Route helpers for URLs embedded in setup surfaces."""

from __future__ import annotations

from starlette.requests import Request

from lab_tracker.config import get_settings


def resolve_public_base_url(request: Request) -> str:
    """Pick the base URL scanners and setup commands should use."""

    settings = getattr(request.app.state, "settings", None) or get_settings()
    configured = (settings.public_base_url or "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")
