"""ASGI entrypoint for lab tracker."""

from __future__ import annotations

from lab_tracker.app import create_app

app = create_app()
