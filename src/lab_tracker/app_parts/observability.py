"""Health, readiness, and metrics route registration."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import JSONResponse

from lab_tracker.auth import Role
from lab_tracker.db_models import (
    AcquisitionOutputModel,
    AnalysisModel,
    ClaimModel,
    DatasetModel,
    GraphChangeSetModel,
    NoteModel,
    ProjectModel,
    QuestionModel,
    SessionModel,
    VisualizationModel,
)
from lab_tracker.errors import AuthError, PermissionDeniedError

_START_TIME = datetime.now(timezone.utc)
_logger = logging.getLogger(__name__)


def _nearest_existing_parent(path: Path) -> Path | None:
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if parent.exists():
        return parent
    return None


def _storage_dir_check(name: str, path: Path) -> dict[str, str]:
    """Report storage writability without exposing server filesystem paths.

    The resolved path (and any blocking parent) is only written to the server
    log; the HTTP payload carries the check name, status, and a static detail.
    """
    resolved = path.expanduser()
    if resolved.exists():
        if not resolved.is_dir():
            return _storage_failure(name, resolved, "path exists but is not a directory")
        if os.access(resolved, os.W_OK):
            return {"name": name, "status": "ok"}
        return _storage_failure(name, resolved, "path is not writable")

    parent = _nearest_existing_parent(resolved)
    if parent is None:
        return _storage_failure(name, resolved, "no existing parent directory")
    if os.access(parent, os.W_OK):
        return {
            "name": name,
            "status": "ok",
            "detail": "path will be created on first write",
        }
    return _storage_failure(
        name,
        resolved,
        "parent directory not writable",
        blocking_parent=parent,
    )


def _storage_failure(
    name: str,
    path: Path,
    detail: str,
    *,
    blocking_parent: Path | None = None,
) -> dict[str, str]:
    if blocking_parent is None:
        _logger.warning("Readiness check %s failed for %s: %s.", name, path, detail)
    else:
        _logger.warning(
            "Readiness check %s failed for %s: %s (%s).",
            name,
            path,
            detail,
            blocking_parent,
        )
    return {"name": name, "status": "fail", "detail": detail}


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
        "graph_change_sets": 0,
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
            counts["graph_change_sets"] = _count_rows(session, GraphChangeSetModel)
    except SQLAlchemyError as exc:
        return _empty_store_counts(), _database_failure_detail("metrics", exc)
    return counts, None


def _database_failure_detail(probe: str, exc: SQLAlchemyError) -> str:
    """Log the raw driver error server-side and return a redacted public detail.

    Driver messages can carry SQL text, bound parameters, hostnames, and
    database user names, so only the exception class leaves the process.
    """
    _logger.warning("Database %s probe failed: %s: %s", probe, exc.__class__.__name__, exc)
    return f"database unavailable ({exc.__class__.__name__})"


def _database_connectivity_error(session_factory: sessionmaker[Session]) -> str | None:
    """Cheap readiness probe: one read of the single-row Alembic version table.

    Unlike a bare ``SELECT 1`` this also fails when the connected database has
    no Lab Tracker schema, without scanning any domain table.
    """
    try:
        with session_factory() as session:
            session.execute(text("SELECT version_num FROM alembic_version")).first()
    except SQLAlchemyError as exc:
        return _database_failure_detail("readiness", exc)
    return None


def _database_check(session_factory: sessionmaker[Session]) -> dict[str, str]:
    database_error = _database_connectivity_error(session_factory)
    if database_error is None:
        return {"name": "database", "status": "ok"}
    return {
        "name": "database",
        "status": "fail",
        "detail": database_error,
    }


def _metrics_snapshot(
    session_factory: sessionmaker[Session],
    *,
    environment: str,
    app_name: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    store, database_error = _store_counts_from_database(session_factory)
    payload: dict[str, Any] = {
        "status": "ok" if database_error is None else "fail",
        "timestamp": now.isoformat(),
        "uptime_seconds": (now - _START_TIME).total_seconds(),
        "app": {"name": app_name, "environment": environment},
        "store": store,
    }
    errors: list[dict[str, str]] = []
    if database_error is not None:
        errors.append({"name": "database", "detail": database_error})
    if errors:
        payload["errors"] = errors
    return payload


def _require_admin(request: Request) -> None:
    actor = getattr(request.state, "auth_context", None)
    if actor is None:
        raise AuthError("Authentication required.")
    if actor.role != Role.ADMIN:
        raise PermissionDeniedError("Only admins can read instance metrics.")


def register_observability_routes(
    app: FastAPI,
    *,
    session_factory: sessionmaker[Session],
    note_storage_path: str,
    file_storage_path: str,
    environment: str,
    app_name: str,
    source_revision: str,
    source_version: str | None,
) -> None:
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "app": {
                "name": app_name,
                "environment": environment,
                "source_revision": source_revision,
                # The release clients compare against (lab_tracker.client_release).
                "version": source_version,
            },
        }

    @app.get("/readiness")
    def readiness():
        checks = [
            _database_check(session_factory),
            _note_storage_check(Path(note_storage_path)),
            _file_storage_check(Path(file_storage_path)),
        ]
        status = "ok" if all(check["status"] == "ok" for check in checks) else "fail"
        payload = {
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "auth": {"enabled": bool(app.state.auth_enabled)},
            "checks": checks,
        }
        if status == "ok":
            return payload
        return JSONResponse(status_code=503, content=payload)

    @app.get("/metrics")
    def metrics(request: Request):
        # Readiness stays open to any authenticated principal (hosted MCP
        # startup and ``lt readiness`` probe it with non-admin credentials);
        # metrics carries instance-wide entity counts across every project,
        # so only admins may read it.
        _require_admin(request)
        payload = _metrics_snapshot(
            session_factory,
            environment=environment,
            app_name=app_name,
        )
        payload["auth"] = {"enabled": app.state.auth_enabled}
        return payload
