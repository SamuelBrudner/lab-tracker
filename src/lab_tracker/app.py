"""FastAPI application setup."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import FileResponse, JSONResponse, RedirectResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.api_routes import register_routes
from lab_tracker.auth import AuthContext, AuthService, TokenService, extract_bearer_token
from lab_tracker.config import get_settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.db_models import (
    AnalysisModel,
    ClaimModel,
    DatasetModel,
    NoteModel,
    ProjectModel,
    QuestionModel,
    SessionModel,
    VisualizationModel,
)
from lab_tracker.dependencies import get_sqlalchemy_repository, set_active_repository
from lab_tracker.errors import AuthError
from lab_tracker.logging import configure_logging
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.schemas import ErrorEnvelope, ErrorInfo
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


_START_TIME = datetime.now(timezone.utc)
_FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
_PUBLIC_PATHS = frozenset(
    {
        "/",
        "/app",
        "/app/",
        "/health",
        "/metrics",
        "/readiness",
        "/auth/login",
        "/auth/register",
        "/openapi.json",
        "/docs",
        "/redoc",
    }
)


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


def _empty_store_counts() -> dict[str, int]:
    return {
        "projects": 0,
        "questions": 0,
        "datasets": 0,
        "notes": 0,
        "sessions": 0,
        # Acquisition outputs are not yet persisted in SQLAlchemy.
        "acquisition_outputs": 0,
        "analyses": 0,
        "claims": 0,
        "visualizations": 0,
    }


def _count_rows(session: Session, model: type) -> int:
    count = session.scalar(select(func.count()).select_from(model))
    return int(count or 0)


def _store_counts_from_database(session_factory: sessionmaker[Session]) -> dict[str, int]:
    counts = _empty_store_counts()
    try:
        with session_factory() as session:
            counts["projects"] = _count_rows(session, ProjectModel)
            counts["questions"] = _count_rows(session, QuestionModel)
            counts["datasets"] = _count_rows(session, DatasetModel)
            counts["notes"] = _count_rows(session, NoteModel)
            counts["sessions"] = _count_rows(session, SessionModel)
            counts["analyses"] = _count_rows(session, AnalysisModel)
            counts["claims"] = _count_rows(session, ClaimModel)
            counts["visualizations"] = _count_rows(session, VisualizationModel)
    except SQLAlchemyError:
        # Schema setup is validated separately; observability should still respond.
        return _empty_store_counts()
    return counts


def _metrics_snapshot(
    session_factory: sessionmaker[Session],
    *,
    environment: str,
    app_name: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "status": "ok",
        "timestamp": now.isoformat(),
        "uptime_seconds": (now - _START_TIME).total_seconds(),
        "app": {"name": app_name, "environment": environment},
        "store": _store_counts_from_database(session_factory),
    }


def _auth_error_response(message: str) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorInfo(code="auth_error", message=message))
    return JSONResponse(status_code=401, content=payload.model_dump())


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    # Keep docs assets and tests reachable without credentials.
    return (
        path.startswith("/docs/")
        or path.startswith("/redoc/")
        or path.startswith("/_test/")
        or path.startswith("/app/")
    )


def _configure_auth_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.method == "OPTIONS" or _is_public_path(request.url.path):
            return await call_next(request)
        try:
            token = extract_bearer_token(request.headers.get("Authorization"))
            claims = app.state.token_service.verify_access_token(token)
            user = app.state.auth_service.get_user_by_id(claims.user_id)
            if user is None:
                raise AuthError("Invalid token.")
            request.state.auth_context = AuthContext(user_id=user.user_id, role=user.role)
        except AuthError as exc:
            return _auth_error_response(str(exc))
        return await call_next(request)


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


def _configure_frontend_routes(app: FastAPI) -> None:
    index_file = _FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return
    app.mount(
        "/app/static",
        StaticFiles(directory=_FRONTEND_DIR),
        name="frontend-static",
    )

    @app.get("/", include_in_schema=False)
    def root_redirect():
        return RedirectResponse(url="/app")

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{_path:path}", include_in_schema=False)
    def frontend_index(_path: str = ""):
        return FileResponse(index_file)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = get_engine(settings)
    session_factory = get_session_factory(engine=engine)
    auth_service = AuthService(session_factory=session_factory)
    token_service = TokenService(
        settings.auth_secret_key,
        ttl_minutes=settings.auth_token_ttl_minutes,
    )
    app = FastAPI(
        title=settings.app_name,
        dependencies=[Depends(get_sqlalchemy_repository)],
    )
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    app.state.auth_service = auth_service
    app.state.token_service = token_service
    _configure_auth_middleware(app)
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
        return _metrics_snapshot(
            session_factory,
            environment=settings.environment,
            app_name=settings.app_name,
        )

    _configure_frontend_routes(app)
    register_routes(
        app,
        api,
        auth_service=auth_service,
        token_service=token_service,
    )

    return app
