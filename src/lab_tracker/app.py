"""FastAPI application setup."""

from __future__ import annotations

from datetime import datetime, timezone

from lab_tracker.config import get_settings
from lab_tracker.fastapi_compat import FastAPI
from lab_tracker.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title=settings.app_name)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

    return app


app = create_app()
