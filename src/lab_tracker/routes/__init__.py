"""HTTP route registration for Lab Tracker."""

from __future__ import annotations

from fastapi import FastAPI

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthService, TokenService

from .analyses import build_analyses_router
from .auth import build_auth_router
from .claims import build_claims_router
from .dataset_files import build_dataset_files_router
from .datasets import build_datasets_router
from .errors import register_error_handlers
from .graph_drafts import build_graph_drafts_router
from .notes import build_notes_router
from .projects import build_projects_router
from .provenance import build_provenance_router
from .questions import build_questions_router
from .search import build_search_router
from .sessions import build_sessions_router
from .visualizations import build_visualizations_router


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
    app.include_router(build_projects_router(api))
    app.include_router(build_questions_router(api))
    app.include_router(build_datasets_router(api))
    app.include_router(build_dataset_files_router(api))
    app.include_router(build_notes_router(api))
    app.include_router(build_graph_drafts_router(api))
    app.include_router(build_provenance_router(api))
    app.include_router(build_search_router(api))
    app.include_router(build_sessions_router(api))
    app.include_router(build_analyses_router(api))
    app.include_router(build_claims_router(api))
    app.include_router(build_visualizations_router(api))


__all__ = [
    "register_routes",
]
