"""FastAPI middleware registration."""

from __future__ import annotations

import hashlib

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import (
    DEVICE_TOKEN_PREFIX,
    LOCAL_AUTH_USER_ID,
    LPAT_TOKEN_PREFIX,
    AuthContext,
    PrincipalType,
    Role,
    device_principal_can_access,
    extract_bearer_token,
    service_principal_can_access,
)
from lab_tracker.errors import AuthError, RateLimitError
from lab_tracker.schemas import ErrorEnvelope, ErrorInfo
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

_APP_CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com data:",
        "img-src 'self' data: blob:",
        "media-src 'self' data: blob:",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

_PUBLIC_PATHS = frozenset(
    {
        "/",
        "/app",
        "/app/",
        "/health",
        "/auth/login",
        "/auth/register",
        "/auth/bootstrap-status",
        "/auth/devices/consume",
        "/terms",
        "/openapi.json",
        "/docs",
        "/redoc",
    }
)

_CSP_PATH_PREFIXES = (
    "/app",
    "/datasets/",
    "/notes/",
    "/visualizations/",
)


def _auth_error_response(message: str) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorInfo(code="auth_error", message=message))
    return JSONResponse(status_code=401, content=payload.model_dump())


def _device_forbidden_response(message: str) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorInfo(code="device_forbidden", message=message))
    return JSONResponse(status_code=403, content=payload.model_dump())


def _service_forbidden_response(message: str) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorInfo(code="service_forbidden", message=message))
    return JSONResponse(status_code=403, content=payload.model_dump())


def _rate_limited_response(message: str) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorInfo(code="rate_limited", message=message))
    return JSONResponse(status_code=429, content=payload.model_dump())


def local_auth_context() -> AuthContext:
    return AuthContext(user_id=LOCAL_AUTH_USER_ID, role=Role.ADMIN)


def system_auth_context() -> AuthContext:
    """Non-interactive automation principal for in-process background work.

    Carries admin role so it can enumerate due batches and DRAFT graph
    proposals, but its ``SYSTEM`` principal_type is non-interactive, so the
    accept and commit gates reject it: automation proposes, humans commit.
    """

    return AuthContext(
        user_id=LOCAL_AUTH_USER_ID,
        role=Role.ADMIN,
        principal_type=PrincipalType.SYSTEM,
    )


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    # Keep docs and app assets reachable without credentials.
    return (
        path.startswith("/docs/")
        or path.startswith("/redoc/")
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
                principal = await run_in_threadpool(
                    app.state.device_auth_service.verify_device_token,
                    token,
                )
                if principal is None:
                    raise AuthError("Invalid device token.")
                if not device_principal_can_access(request.method, request.url.path):
                    return _device_forbidden_response(
                        "This action is not permitted for paired devices."
                    )
                user = await run_in_threadpool(
                    app.state.auth_service.get_user_by_id,
                    principal.user_id,
                )
                if user is None:
                    raise AuthError("Invalid device token.")
                request.state.auth_context = AuthContext(
                    user_id=principal.user_id,
                    role=user.role,
                    principal_type=PrincipalType.DEVICE,
                    device_token_id=principal.device_token_id,
                )
            elif token.startswith(LPAT_TOKEN_PREFIX):
                pat_rate_key = _pat_rate_key(request, token)
                app.state.pat_rate_limiter.check(pat_rate_key)
                principal = await run_in_threadpool(
                    app.state.personal_access_token_service.verify_token,
                    token,
                )
                if principal is None:
                    app.state.pat_rate_limiter.record_failure(pat_rate_key)
                    raise AuthError("Invalid personal access token.")
                if not service_principal_can_access(
                    request.method,
                    request.url.path,
                    read_only=principal.read_only,
                    role=principal.role,
                    scope=principal.scope,
                ):
                    app.state.pat_rate_limiter.record_failure(pat_rate_key)
                    return _service_forbidden_response("Not permitted for this token.")
                user = await run_in_threadpool(
                    app.state.auth_service.get_user_by_id,
                    principal.user_id,
                )
                if user is None:
                    app.state.pat_rate_limiter.record_failure(pat_rate_key)
                    raise AuthError("Invalid personal access token.")
                app.state.pat_rate_limiter.reset(pat_rate_key)
                request.state.auth_context = AuthContext(
                    user_id=principal.user_id,
                    role=principal.role,
                    principal_type=PrincipalType.SERVICE,
                )
            else:
                claims = await run_in_threadpool(
                    app.state.token_service.verify_access_token,
                    token,
                )
                user = await run_in_threadpool(
                    app.state.auth_service.get_user_by_id,
                    claims.user_id,
                )
                if user is None:
                    raise AuthError("Invalid token.")
                request.state.auth_context = AuthContext(user_id=user.user_id, role=user.role)
        except RateLimitError as exc:
            return _rate_limited_response(str(exc))
        except AuthError as exc:
            return _auth_error_response(str(exc))
        return await call_next(request)


def configure_security_headers_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        if _should_apply_csp(request.url.path):
            response.headers.setdefault(
                "Content-Security-Policy",
                _APP_CONTENT_SECURITY_POLICY,
            )
        return response


def _should_apply_csp(path: str) -> bool:
    if path == "/" or path.startswith("/openapi"):
        return False
    return any(path.startswith(prefix) for prefix in _CSP_PATH_PREFIXES)


def _pat_rate_key(request: Request, token: str) -> str:
    client_host = request.client.host if request.client is not None else "unknown"
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"lpat:{client_host}:{token_hash[:24]}"


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
        request_scope = api.request_scope(
            repository,
            surface=_usage_surface_from_request(request),
            close=db_session.close,
        )
        request_scope.__enter__()
        try:
            request.state.lab_tracker_api = request_scope.api
            response = await call_next(request)
        except BaseException as exc:
            await run_in_threadpool(
                request_scope.__exit__,
                type(exc),
                exc,
                exc.__traceback__,
            )
            raise
        return await run_in_threadpool(_complete_request_scope_response, request_scope, response)


def _complete_request_scope_response(request_scope, response):
    try:
        return request_scope.complete_response(response)
    finally:
        request_scope.__exit__(None, None, None)


def _usage_surface_from_request(request: Request) -> str:
    surface = (request.headers.get("X-LabTracker-Surface") or "").strip().lower()
    if surface in {"mcp", "cli"}:
        return surface
    return "http"
