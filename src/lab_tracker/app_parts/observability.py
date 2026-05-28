"""Health, readiness, and metrics route registration."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import JSONResponse

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

_START_TIME = datetime.now(timezone.utc)


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


def register_observability_routes(
    app: FastAPI,
    *,
    session_factory: sessionmaker[Session],
    note_storage_path: str,
    file_storage_path: str,
    environment: str,
    app_name: str,
) -> None:
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

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
            "checks": checks,
        }
        if status == "ok":
            return payload
        return JSONResponse(status_code=503, content=payload)

    @app.get("/metrics")
    def metrics():
        payload = _metrics_snapshot(
            session_factory,
            environment=environment,
            app_name=app_name,
        )
        payload["auth"] = {"enabled": app.state.auth_enabled}
        return payload
