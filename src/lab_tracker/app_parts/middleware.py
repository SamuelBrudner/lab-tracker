"""FastAPI middleware registration."""

from __future__ import annotations

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import (
    DEVICE_TOKEN_PREFIX,
    LOCAL_AUTH_USER_ID,
    AuthContext,
    PrincipalType,
    Role,
    device_principal_can_access,
    extract_bearer_token,
)
from lab_tracker.errors import AuthError
from lab_tracker.schemas import ErrorEnvelope, ErrorInfo
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

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
        "/auth/bootstrap-status",
        "/auth/devices/consume",
        "/openapi.json",
        "/docs",
        "/redoc",
    }
)


def _auth_error_response(message: str) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorInfo(code="auth_error", message=message))
    return JSONResponse(status_code=401, content=payload.model_dump())


def _device_forbidden_response(message: str) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorInfo(code="device_forbidden", message=message))
    return JSONResponse(status_code=403, content=payload.model_dump())


def local_auth_context() -> AuthContext:
    return AuthContext(user_id=LOCAL_AUTH_USER_ID, role=Role.ADMIN)


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


def configure_auth_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if not request.app.state.auth_enabled:
            request.state.auth_context = local_auth_context()
            return await call_next(request)
        if request.method == "OPTIONS" or _is_public_path(request.url.path):
            return await call_next(request)
        try:
            token = extract_bearer_token(request.headers.get("Authorization"))
            if token.startswith(DEVICE_TOKEN_PREFIX):
                principal = app.state.device_auth_service.verify_device_token(token)
                if principal is None:
                    raise AuthError("Invalid device token.")
                if not device_principal_can_access(request.method, request.url.path):
                    return _device_forbidden_response(
                        "This action is not permitted for paired devices."
                    )
                user = app.state.auth_service.get_user_by_id(principal.user_id)
                if user is None:
                    raise AuthError("Invalid device token.")
                request.state.auth_context = AuthContext(
                    user_id=principal.user_id,
                    role=user.role,
                    principal_type=PrincipalType.DEVICE,
                    device_token_id=principal.device_token_id,
                )
            else:
                claims = app.state.token_service.verify_access_token(token)
                user = app.state.auth_service.get_user_by_id(claims.user_id)
                if user is None:
                    raise AuthError("Invalid token.")
                request.state.auth_context = AuthContext(user_id=user.user_id, role=user.role)
        except AuthError as exc:
            return _auth_error_response(str(exc))
        return await call_next(request)


def configure_database_session_middleware(
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
        with api.request_scope(repository, close=db_session.close) as request_scope:
            request.state.lab_tracker_api = request_scope.api
            response = await call_next(request)
            return request_scope.complete_response(response)
