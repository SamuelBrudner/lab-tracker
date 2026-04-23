"""Authentication routes."""

from __future__ import annotations

import hmac

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.auth import AuthService, Role, TokenService
from lab_tracker.errors import AuthError
from lab_tracker.schemas import (
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthTokenRead,
    AuthUserRead,
    Envelope,
)

from .shared import (
    actor_from_authorization_header,
    actor_from_request,
    auth_token_read,
    auth_user_read,
)


def build_auth_router(
    *,
    auth_service: AuthService,
    token_service: TokenService,
    bootstrap_admin_token: str | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/auth/register",
        response_model=Envelope[AuthTokenRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def register_auth(payload: AuthRegisterRequest, request: Request):
        if payload.role != Role.VIEWER:
            if payload.role == Role.ADMIN and not auth_service.has_users():
                expected = (bootstrap_admin_token or "").strip()
                provided = (payload.bootstrap_token or "").strip()
                if not expected:
                    raise AuthError("Admin bootstrap is not configured for this deployment.")
                if not provided:
                    raise AuthError("Bootstrap token required to create initial admin user.")
                if not hmac.compare_digest(provided, expected):
                    raise AuthError("Invalid bootstrap token.")
            else:
                actor = actor_from_authorization_header(
                    request,
                    auth_service=auth_service,
                    token_service=token_service,
                )
                if actor.role != Role.ADMIN:
                    raise AuthError("Admin privileges required to register non-viewer users.")
        user = auth_service.register_user(
            username=payload.username,
            password=payload.password,
            role=payload.role,
        )
        token = token_service.issue_access_token(user)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.post("/auth/login", response_model=Envelope[AuthTokenRead])
    def login_auth(payload: AuthLoginRequest):
        user = auth_service.authenticate(payload.username, payload.password)
        token = token_service.issue_access_token(user)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.post("/auth/refresh", response_model=Envelope[AuthTokenRead])
    def refresh_auth(request: Request):
        actor = actor_from_request(request)
        user = auth_service.get_user_by_id(actor.user_id)
        if user is None:
            raise AuthError("Authentication required.")
        token = token_service.issue_access_token(user)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.get("/auth/me", response_model=Envelope[AuthUserRead])
    def auth_me(request: Request):
        actor = actor_from_request(request)
        user = auth_service.get_user_by_id(actor.user_id)
        if user is None:
            raise AuthError("Authentication required.")
        return Envelope(data=auth_user_read(user))

    return router
