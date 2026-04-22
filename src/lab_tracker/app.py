"""FastAPI application setup."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import FileResponse, JSONResponse, RedirectResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, AuthService, TokenService, extract_bearer_token
from lab_tracker.config import get_settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.db_models import (
    AcquisitionOutputModel,
    AnalysisModel,
    ClaimModel,
    DatasetModel,
    NoteModel,
    ProjectModel,
    QuestionModel,
    SessionModel,
    VisualizationModel,
)
from lab_tracker.dependencies import get_sqlalchemy_repository
from lab_tracker.errors import AuthError
from lab_tracker.file_storage import LocalFileStorageBackend
from lab_tracker.logging import configure_logging
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.schemas import ErrorEnvelope, ErrorInfo
from lab_tracker.routes import register_routes
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.services.search_backends import InMemorySubstringSearchBackend


_START_TIME = datetime.now(timezone.utc)
_FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"
_logger = logging.getLogger(__name__)
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


def _storage_dir_check(name: str, path: Path) -> dict[str, str]:
    resolved = path.expanduser()
    if resolved.exists():
        if not resolved.is_dir():
            return {
                "name": name,
                "status": "fail",
                "path": str(resolved),
                "detail": "path exists but is not a directory",
            }
        if os.access(resolved, os.W_OK):
            return {"name": name, "status": "ok", "path": str(resolved)}
        return {
            "name": name,
            "status": "fail",
            "path": str(resolved),
            "detail": "path is not writable",
        }

    parent = _nearest_existing_parent(resolved)
    if parent is None:
        return {
            "name": name,
            "status": "fail",
            "path": str(resolved),
            "detail": "no existing parent directory",
        }
    if os.access(parent, os.W_OK):
        return {
            "name": name,
            "status": "ok",
            "path": str(resolved),
            "detail": "path will be created on first write",
        }
    return {
        "name": name,
        "status": "fail",
        "path": str(resolved),
        "detail": f"parent directory not writable: {parent}",
    }


def _note_storage_check(path: Path) -> dict[str, str]:
    return _storage_dir_check("note_storage", path)


def _file_storage_check(path: Path) -> dict[str, str]:
    return _storage_dir_check("file_storage", path)


def _empty_store_counts() -> dict[str, int]:
    return {
        "projects": 0,
        "questions": 0,
        "datasets": 0,
        "notes": 0,
        "sessions": 0,
        "acquisition_outputs": 0,
        "analyses": 0,
        "claims": 0,
        "visualizations": 0,
    }


def _count_rows(session: Session, model: type) -> int:
    count = session.scalar(select(func.count()).select_from(model))
    return int(count or 0)


def _store_counts_from_database(
    session_factory: sessionmaker[Session],
) -> tuple[dict[str, int], str | None]:
    counts = _empty_store_counts()
    try:
        with session_factory() as session:
            counts["projects"] = _count_rows(session, ProjectModel)
            counts["questions"] = _count_rows(session, QuestionModel)
            counts["datasets"] = _count_rows(session, DatasetModel)
            counts["notes"] = _count_rows(session, NoteModel)
            counts["sessions"] = _count_rows(session, SessionModel)
            counts["acquisition_outputs"] = _count_rows(session, AcquisitionOutputModel)
            counts["analyses"] = _count_rows(session, AnalysisModel)
            counts["claims"] = _count_rows(session, ClaimModel)
            counts["visualizations"] = _count_rows(session, VisualizationModel)
    except SQLAlchemyError as exc:
        return _empty_store_counts(), f"{exc.__class__.__name__}: {exc}"
    return counts, None


def _database_check(session_factory: sessionmaker[Session]) -> dict[str, str]:
    _, database_error = _store_counts_from_database(session_factory)
    if database_error is None:
        return {"name": "database", "status": "ok"}
    return {
        "name": "database",
        "status": "fail",
        "detail": database_error,
    }


def _search_check(api: LabTrackerAPI) -> dict[str, str]:
    snapshot = api.search_health()
    if not snapshot.degraded:
        return {
            "name": "search",
            "status": "ok",
            "backend": snapshot.backend_name,
        }
    return {
        "name": "search",
        "status": "fail",
        "backend": snapshot.backend_name,
        "detail": snapshot.last_failure_message or "search backend degraded",
        "operation": snapshot.last_failure_operation or "unknown",
        "repair": (
            "run `uv run python -m lab_tracker.reindex --reset` for persistent backends; "
            "restart the app to rebuild the in-memory default backend"
        ),
    }


def _metrics_snapshot(
    session_factory: sessionmaker[Session],
    *,
    environment: str,
    app_name: str,
    api: LabTrackerAPI,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    store, database_error = _store_counts_from_database(session_factory)
    search_snapshot = api.search_health()
    payload: dict[str, Any] = {
        "status": "ok" if database_error is None and not search_snapshot.degraded else "fail",
        "timestamp": now.isoformat(),
        "uptime_seconds": (now - _START_TIME).total_seconds(),
        "app": {"name": app_name, "environment": environment},
        "store": store,
        "search": {
            "backend": search_snapshot.backend_name,
            "degraded": search_snapshot.degraded,
            "failure_count": search_snapshot.failure_count,
            "last_failure_at": search_snapshot.last_failure_at,
            "last_failure_message": search_snapshot.last_failure_message,
            "last_failure_operation": search_snapshot.last_failure_operation,
            "repair": (
                "run `uv run python -m lab_tracker.reindex --reset` for persistent backends; "
                "restart the app to rebuild the in-memory default backend"
            ),
        },
    }
    errors: list[dict[str, str]] = []
    if database_error is not None:
        errors.append({"name": "database", "detail": database_error})
    if search_snapshot.degraded:
        errors.append(
            {
                "name": "search",
                "detail": search_snapshot.last_failure_message or "search backend degraded",
            }
        )
    if errors:
        payload["errors"] = errors
    return payload


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
    *,
    api: LabTrackerAPI,
) -> None:
    @app.middleware("http")
    async def db_session_middleware(request: Request, call_next):
        db_session = request.app.state.db_session_factory()
        request.state.db_session = db_session
        repository = SQLAlchemyLabTrackerRepository(db_session)
        request.state.lab_tracker_repository = repository
        request_context = api.build_request_context(repository)
        request.state.lab_tracker_api = api.bind_request_context(request_context)
        committed = False
        try:
            response = await call_next(request)
            if response.status_code >= 400:
                db_session.rollback()
            else:
                db_session.commit()
                committed = True
            return response
        except Exception:
            db_session.rollback()
            raise
        finally:
            try:
                request_context.finish(
                    committed=committed,
                    apply_search_op=lambda operation, args: request.state.lab_tracker_api._apply_search_op_safely(  # noqa: SLF001
                        operation,
                        *args,
                    ),
                    run_deferred_actions=lambda actions, label: request.state.lab_tracker_api._run_deferred_actions(  # noqa: SLF001
                        actions,
                        label=label,
                    ),
                )
            finally:
                db_session.close()


def _configure_frontend_routes(app: FastAPI) -> None:
    index_file = _FRONTEND_DIR / "index.html"
    if not index_file.exists():
        _logger.warning(
            "Frontend files not found at %s; /app routes will not be served.",
            _FRONTEND_DIR,
        )
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
    file_storage_backend = LocalFileStorageBackend(settings.file_storage_path)
    raw_note_storage = LocalNoteStorage(settings.note_storage_path)
    search_backend = InMemorySubstringSearchBackend()
    ocr_backend = None
    lab_tracker_api = LabTrackerAPI(
        raw_storage=raw_note_storage,
        search_backend=search_backend,
        ocr_backend=ocr_backend,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        dependencies=[Depends(get_sqlalchemy_repository)],
        lifespan=lifespan,
    )
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory
    app.state.auth_service = auth_service
    app.state.token_service = token_service
    app.state.file_storage_backend = file_storage_backend
    app.state.raw_note_storage = raw_note_storage
    app.state.search_backend = search_backend
    app.state.ocr_backend = ocr_backend
    app.state.lab_tracker_api = lab_tracker_api
    _configure_auth_middleware(app)
    _configure_database_session_middleware(app, api=app.state.lab_tracker_api)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

    @app.get("/readiness")
    def readiness():
        checks = [
            _database_check(session_factory),
            _search_check(app.state.lab_tracker_api),
            _note_storage_check(Path(settings.note_storage_path)),
            _file_storage_check(Path(settings.file_storage_path)),
        ]
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
            api=app.state.lab_tracker_api,
        )

    _configure_frontend_routes(app)
    register_routes(
        app,
        app.state.lab_tracker_api,
        auth_service=auth_service,
        token_service=token_service,
        bootstrap_admin_token=settings.bootstrap_admin_token,
    )

    return app
