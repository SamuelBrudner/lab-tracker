"""FastAPI application setup."""

from __future__ import annotations

from fastapi import FastAPI

from lab_tracker.app_parts.frontend import configure_frontend_routes
from lab_tracker.app_parts.middleware import (
    configure_artifact_resolution_admission_middleware,
    configure_auth_middleware,
    configure_database_session_middleware,
    configure_security_headers_middleware,
)
from lab_tracker.app_parts.observability import register_observability_routes
from lab_tracker.app_parts.runtime import (
    build_app_runtime,
    configure_app_state,
    make_lifespan,
)
from lab_tracker.config import get_settings
from lab_tracker.routes import register_routes


def create_app() -> FastAPI:
    settings = get_settings()
    runtime = build_app_runtime(settings)
    app = FastAPI(
        title=settings.app_name,
        lifespan=make_lifespan(runtime),
    )
    configure_app_state(app, runtime)
    configure_database_session_middleware(app, api=app.state.lab_tracker_api)
    configure_artifact_resolution_admission_middleware(app)
    configure_auth_middleware(app)
    configure_security_headers_middleware(app)
    register_observability_routes(
        app,
        session_factory=runtime.session_factory,
        note_storage_path=settings.note_storage_path,
        file_storage_path=settings.file_storage_path,
        environment=settings.environment,
        app_name=settings.app_name,
    )
    configure_frontend_routes(app)
    register_routes(
        app,
        app.state.lab_tracker_api,
        auth_service=runtime.auth_service,
        token_service=runtime.token_service,
        invitation_token_service=runtime.invitation_token_service,
        device_auth_service=runtime.device_auth_service,
        personal_access_token_service=runtime.personal_access_token_service,
        bootstrap_admin_token=settings.bootstrap_admin_token,
    )

    return app
