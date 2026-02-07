"""FastAPI application setup."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Engine
from starlette.responses import JSONResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.api_routes import register_routes
from lab_tracker.config import get_settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.dependencies import get_sqlalchemy_repository, set_active_repository
from lab_tracker.logging import configure_logging
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


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


def _configure_database_session_middleware(
    app: FastAPI,
) -> None:
    @app.middleware("http")
    async def db_session_middleware(request: Request, call_next):
        db_session = request.app.state.db_session_factory()
        request.state.db_session = db_session
        repository = SQLAlchemyLabTrackerRepository(db_session)
        request.state.lab_tracker_repository = repository
        set_active_repository(repository)
        try:
            response = await call_next(request)
            if response.status_code >= 400:
                db_session.rollback()
            else:
                db_session.commit()
            return response
        except Exception:
            db_session.rollback()
            raise
        finally:
            set_active_repository(None)
            db_session.close()


def _configure_database_shutdown_hook(app: FastAPI, *, engine: Engine) -> None:
    @app.on_event("shutdown")
    def dispose_engine() -> None:
        engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = get_engine(settings)
    session_factory = get_session_factory(engine=engine)
    app = FastAPI(
        title=settings.app_name,
        dependencies=[Depends(get_sqlalchemy_repository)],
    )
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    _configure_database_session_middleware(app)
    _configure_database_shutdown_hook(app, engine=engine)
    raw_storage = LocalNoteStorage(settings.note_storage_path)
    api = LabTrackerAPI(raw_storage=raw_storage)
    try:
        with session_factory() as bootstrap_session:
            api.hydrate_from_repository(SQLAlchemyLabTrackerRepository(bootstrap_session))
    except SQLAlchemyError:
        # Schema setup is validated separately; startup should still succeed for health checks.
        pass

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
