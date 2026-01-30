"""FastAPI application setup."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from lab_tracker.api import LabTrackerAPI
from lab_tracker.api_routes import register_routes
from lab_tracker.config import get_settings
from lab_tracker.fastapi_compat import FastAPI
from lab_tracker.logging import configure_logging
from lab_tracker.note_storage import LocalNoteStorage

try:  # pragma: no cover - exercised when Starlette/FastAPI are available.
    from starlette.responses import JSONResponse
except ModuleNotFoundError:  # pragma: no cover - lightweight fallback.

    class JSONResponse:  # type: ignore[override]
        def __init__(self, *, status_code: int, content: dict[str, Any]) -> None:
            self.status_code = status_code
            self._content = content

        def json(self) -> dict[str, Any]:
            return self._content


_START_TIME = datetime.now(timezone.utc)


def _nearest_existing_parent(path: Path) -> Path | None:
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if parent.exists():
        return parent
    return None


def _note_storage_check(path: Path) -> dict[str, str]:
    resolved = path.expanduser()
    if resolved.exists():
        if not resolved.is_dir():
            return {
                "name": "note_storage",
                "status": "fail",
                "path": str(resolved),
                "detail": "path exists but is not a directory",
            }
        if os.access(resolved, os.W_OK):
            return {"name": "note_storage", "status": "ok", "path": str(resolved)}
        return {
            "name": "note_storage",
            "status": "fail",
            "path": str(resolved),
            "detail": "path is not writable",
        }

    parent = _nearest_existing_parent(resolved)
    if parent is None:
        return {
            "name": "note_storage",
            "status": "fail",
            "path": str(resolved),
            "detail": "no existing parent directory",
        }
    if os.access(parent, os.W_OK):
        return {
            "name": "note_storage",
            "status": "ok",
            "path": str(resolved),
            "detail": "path will be created on first write",
        }
    return {
        "name": "note_storage",
        "status": "fail",
        "path": str(resolved),
        "detail": f"parent directory not writable: {parent}",
    }


def _metrics_snapshot(api: LabTrackerAPI, *, environment: str, app_name: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    store = api._store
    return {
        "status": "ok",
        "timestamp": now.isoformat(),
        "uptime_seconds": (now - _START_TIME).total_seconds(),
        "app": {"name": app_name, "environment": environment},
        "store": {
            "projects": len(store.projects),
            "questions": len(store.questions),
            "datasets": len(store.datasets),
            "notes": len(store.notes),
            "sessions": len(store.sessions),
            "acquisition_outputs": len(store.acquisition_outputs),
            "analyses": len(store.analyses),
            "claims": len(store.claims),
            "visualizations": len(store.visualizations),
        },
    }


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title=settings.app_name)
    raw_storage = LocalNoteStorage(settings.note_storage_path)
    api = LabTrackerAPI(raw_storage=raw_storage)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

    @app.get("/readiness")
    def readiness():
        checks = [_note_storage_check(Path(settings.note_storage_path))]
        status = "ok" if all(check["status"] == "ok" for check in checks) else "fail"
        payload = {
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": checks,
        }
        if status == "ok":
            return payload
        return JSONResponse(status_code=503, content=payload)

    @app.get("/metrics")
    def metrics():
        return _metrics_snapshot(api, environment=settings.environment, app_name=settings.app_name)

    register_routes(app, api)

    return app


app = create_app()
