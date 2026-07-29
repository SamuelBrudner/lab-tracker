"""FastAPI middleware registration."""

from __future__ import annotations

import hashlib
import re
from typing import cast

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from lab_tracker.api import LabTrackerAPI
from lab_tracker.application import RequestHandlers
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
from lab_tracker.store_health import (
    StoreHealth,
    StoreHealthProbeUnavailable,
    StoreProbe,
    StoreProbeTarget,
)

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

_ARTIFACT_RESOLUTION_PATH = "/external-artifacts/resolve"
_ARTIFACT_RESOLUTION_RETRY_AFTER_SECONDS = "1"
_ARTIFACT_RESOLUTION_SATURATED_MESSAGE = "Artifact resolution is temporarily unavailable."
_STORE_HEALTH_PATH = re.compile(r"/data-stores/[^/]+/health\Z")
_STORE_HEALTH_RETRY_AFTER_SECONDS = "1"
_STORE_HEALTH_SATURATED_MESSAGE = "Store health is temporarily unavailable."


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


def _artifact_resolution_saturated_response() -> JSONResponse:
    """Return the same opaque response for global and actor saturation."""

    response = _rate_limited_response(_ARTIFACT_RESOLUTION_SATURATED_MESSAGE)
    response.headers["Retry-After"] = _ARTIFACT_RESOLUTION_RETRY_AFTER_SECONDS
    return response


def _store_health_saturated_response() -> JSONResponse:
    """Return the same opaque response for global and actor saturation."""

    response = _rate_limited_response(_STORE_HEALTH_SATURATED_MESSAGE)
    response.headers["Retry-After"] = _STORE_HEALTH_RETRY_AFTER_SECONDS
    return response


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
        or path.startswith("/r/")
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


async def _apply_artifact_resolution_admission(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    """Apply no-wait resolution capacity after authoritative authentication."""

    if (
        request.method != "POST"
        or _route_relative_path(request) != _ARTIFACT_RESOLUTION_PATH
    ):
        return await call_next(request)
    actor = getattr(request.state, "auth_context", None)
    if not isinstance(actor, AuthContext):
        return _auth_error_response("Authentication required.")
    lease = request.app.state.artifact_resolution_admission.try_acquire(actor.user_id)
    if lease is None:
        return _artifact_resolution_saturated_response()
    try:
        return await call_next(request)
    finally:
        lease.release()


def configure_artifact_resolution_admission_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def artifact_resolution_admission_middleware(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        return await _apply_artifact_resolution_admission(request, call_next)


async def _apply_store_health_admission(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    """Apply no-wait health capacity before the ordinary request DB scope."""

    path = _route_relative_path(request)
    if (
        request.method != "GET"
        or not isinstance(path, str)
        or _STORE_HEALTH_PATH.fullmatch(path) is None
    ):
        return await call_next(request)
    actor = getattr(request.state, "auth_context", None)
    if not isinstance(actor, AuthContext):
        return _auth_error_response("Authentication required.")
    lease = request.app.state.store_health_admission.try_acquire(actor.user_id)
    if lease is None:
        return _store_health_saturated_response()
    try:
        return await call_next(request)
    finally:
        lease.release()


def _route_relative_path(request: Request) -> object:
    """Mirror Starlette's root-path removal before matching an API route."""

    path = request.scope["path"]
    root_path = request.scope.get("root_path", "")
    if (
        not isinstance(path, str)
        or not isinstance(root_path, str)
        or not root_path
        or not path.startswith(root_path)
        or (path != root_path and path[len(root_path)] != "/")
    ):
        return path
    if path == root_path:
        return ""
    return path[len(root_path) :]


def configure_store_health_admission_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def store_health_admission_middleware(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        return await _apply_store_health_admission(request, call_next)


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
        repository = SQLAlchemyLabTrackerRepository(db_session)
        request_scope = api.request_scope(
            repository,
            surface=_usage_surface_from_request(request),
            close=db_session.close,
        )
        request_scope.__enter__()
        try:
            request.state.lab_tracker_api = request_scope.api
            request.state.lab_tracker_handlers = RequestHandlers.compose(
                api=request_scope.api,
                repository=repository,
                session=db_session,
                file_storage=request.app.state.file_storage_backend,
                raw_note_storage=request.app.state.raw_note_storage,
                settings=request.app.state.settings,
                resolver_registry=getattr(
                    request.app.state,
                    "resolver_registry",
                    None,
                ),
                release_read_scope=request_scope.release_read_scope,
                store_health_checker=_store_health_checker_from_app(request),
            )
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


def _unavailable_store_health_checker(
    _target: StoreProbeTarget,
) -> StoreHealth:
    """Fail closed when the composition root omitted mandatory health wiring."""

    raise StoreHealthProbeUnavailable()


def _store_health_checker_from_app(request: Request) -> StoreProbe:
    """Capture the configured checker or an explicit fail-closed sentinel."""

    return cast(
        StoreProbe,
        getattr(
            request.app.state,
            "store_health_checker",
            _unavailable_store_health_checker,
        ),
    )


def _usage_surface_from_request(request: Request) -> str:
    surface = (request.headers.get("X-LabTracker-Surface") or "").strip().lower()
    if surface in {"mcp", "cli"}:
        return surface
    return "http"
