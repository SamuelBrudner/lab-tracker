"""HTTP route registration for Lab Tracker."""

from __future__ import annotations

from fastapi import FastAPI

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import (
    AuthService,
    DeviceAuthService,
    InvitationTokenService,
    PersonalAccessTokenService,
    TokenService,
)

from .analyses import build_analyses_router
from .assistant import build_assistant_router
from .auth import build_auth_router
from .claims import build_claims_router
from .data_stores import build_data_stores_router
from .dataset_files import build_dataset_files_router
from .datasets import build_datasets_router
from .device_auth import build_device_auth_router
from .errors import register_error_handlers
from .exploration import build_exploration_router
from .external_artifacts import build_external_artifacts_router
from .goals import build_goals_router
from .graph_batches import build_graph_batches_router
from .graph_drafts import build_graph_drafts_router
from .groups import build_groups_router
from .notes import build_notes_router
from .ownership import build_ownership_router
from .personal_access_tokens import build_personal_access_tokens_router
from .portfolio import build_portfolio_router
from .project_graph import build_project_graph_router
from .projects import build_projects_router
from .provenance import build_provenance_router
from .provenance_links import build_provenance_links_router
from .questions import build_questions_router
from .record_exports import build_record_exports_router
from .review_delivery import build_review_delivery_router
from .schema import build_schema_router
from .search import build_search_router
from .sessions import build_sessions_router
from .supervision import build_supervision_router
from .usage_events import build_usage_events_router
from .visualizations import build_visualizations_router


def register_routes(
    app: FastAPI,
    api: LabTrackerAPI,
    *,
    auth_service: AuthService,
    token_service: TokenService,
    invitation_token_service: InvitationTokenService,
    device_auth_service: DeviceAuthService,
    personal_access_token_service: PersonalAccessTokenService,
    bootstrap_admin_token: str | None = None,
) -> None:
    register_error_handlers(app)
    app.include_router(
        build_auth_router(
            auth_service=auth_service,
            token_service=token_service,
            invitation_token_service=invitation_token_service,
            bootstrap_admin_token=bootstrap_admin_token,
        )
    )
    app.include_router(
        build_device_auth_router(device_auth_service=device_auth_service)
    )
    app.include_router(
        build_personal_access_tokens_router(
            auth_service=auth_service,
            personal_access_token_service=personal_access_token_service,
        )
    )
    app.include_router(build_projects_router(api))
    app.include_router(build_groups_router(api))
    app.include_router(build_supervision_router(api))
    app.include_router(build_ownership_router(api))
    app.include_router(build_record_exports_router(api))
    app.include_router(build_portfolio_router(api))
    app.include_router(build_project_graph_router(api))
    app.include_router(build_questions_router(api))
    app.include_router(build_datasets_router(api))
    app.include_router(build_dataset_files_router(api))
    app.include_router(build_notes_router(api))
    app.include_router(build_graph_drafts_router(api))
    app.include_router(build_graph_batches_router(api))
    app.include_router(build_review_delivery_router(api))
    app.include_router(build_provenance_router(api))
    app.include_router(build_search_router(api))
    app.include_router(build_usage_events_router(api))
    app.include_router(build_schema_router())
    app.include_router(build_assistant_router())
    app.include_router(build_sessions_router(api))
    app.include_router(build_analyses_router(api))
    app.include_router(build_claims_router(api))
    app.include_router(build_exploration_router(api))
    app.include_router(build_provenance_links_router(api))
    app.include_router(build_external_artifacts_router(api))
    app.include_router(build_data_stores_router(api))
    app.include_router(build_goals_router(api))
    app.include_router(build_visualizations_router(api))


__all__ = [
    "register_routes",
]
