"""FastAPI application setup."""

from __future__ import annotations

from datetime import datetime, timezone

from lab_tracker.api import LabTrackerAPI
from lab_tracker.api_routes import register_routes
from lab_tracker.config import get_settings
from lab_tracker.fastapi_compat import FastAPI
from lab_tracker.logging import configure_logging
from lab_tracker.note_storage import LocalNoteStorage


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title=settings.app_name)
    raw_storage = LocalNoteStorage(settings.note_storage_path)
    api = LabTrackerAPI(raw_storage=raw_storage)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

    register_routes(app, api)

    return app


app = create_app()
