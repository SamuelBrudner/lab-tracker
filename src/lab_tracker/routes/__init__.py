"""HTTP route registration for Lab Tracker."""

from __future__ import annotations

from fastapi import FastAPI

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthService, TokenService

from .auth import build_auth_router
from .datasets import build_datasets_router
from .errors import register_error_handlers
from .notes_search import build_notes_search_router
from .projects_questions import build_projects_questions_router
from .sessions_analysis import build_sessions_analysis_router


def register_routes(
    app: FastAPI,
    api: LabTrackerAPI,
    *,
    auth_service: AuthService,
    token_service: TokenService,
    bootstrap_admin_token: str | None = None,
) -> None:
    register_error_handlers(app)
    app.include_router(
        build_auth_router(
            auth_service=auth_service,
            token_service=token_service,
            bootstrap_admin_token=bootstrap_admin_token,
        )
    )
    app.include_router(build_projects_questions_router(api))
    app.include_router(build_datasets_router(api))
    app.include_router(build_notes_search_router(api))
    app.include_router(build_sessions_analysis_router(api))


__all__ = [
    "register_routes",
]
