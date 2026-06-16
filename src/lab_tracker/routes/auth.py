"""Authentication routes."""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.auth import (
    LOCAL_AUTH_USERNAME,
    AuthService,
    InvitationTokenService,
    Role,
    TokenService,
)
from lab_tracker.errors import AuthError
from lab_tracker.schemas import (
    AuthBootstrapStatus,
    AuthInvitationCreate,
    AuthInvitationRead,
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthTokenRead,
    AuthUserRead,
    AuthUserUpdate,
    Envelope,
    ListEnvelope,
)

from .shared import (
    actor_from_authorization_header,
    actor_from_request,
    auth_token_read,
    auth_user_read,
    list_response,
    paginate,
    validate_pagination,
)


def build_auth_router(
    *,
    auth_service: AuthService,
    token_service: TokenService,
    invitation_token_service: InvitationTokenService,
    bootstrap_admin_token: str | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/auth/bootstrap-status", response_model=Envelope[AuthBootstrapStatus])
    def auth_bootstrap_status():
        expected = (bootstrap_admin_token or "").strip()
        has_users = auth_service.has_users()
        return Envelope(
            data=AuthBootstrapStatus(
                has_users=has_users,
                bootstrap_admin_configured=bool(expected),
                first_admin_available=not has_users and bool(expected),
            )
        )

    @router.post(
        "/auth/register",
        response_model=Envelope[AuthTokenRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def register_auth(payload: AuthRegisterRequest, request: Request):
        registration_role = payload.role
        if payload.invite_token:
            invitation = invitation_token_service.verify_invitation_token(payload.invite_token)
            invited_email = invitation_token_service.normalize_email(invitation.email)
            provided_username = invitation_token_service.normalize_email(payload.username)
            if provided_username != invited_email:
                raise AuthError("Invitation token does not match this email address.")
            registration_role = invitation.role
        elif payload.role != Role.VIEWER:
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
            role=registration_role,
        )
        token = token_service.issue_access_token(user)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.post(
        "/auth/invitations",
        response_model=Envelope[AuthInvitationRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_auth_invitation(payload: AuthInvitationCreate, request: Request):
        _ensure_admin(request)
        email = invitation_token_service.normalize_email(payload.email)
        invite_token, expires_at = invitation_token_service.issue_invitation_token(
            email=email,
            role=payload.role,
        )
        base_url = _public_base_url(request)
        invite_url = f"{base_url}/app?{urlencode({'invite': invite_token, 'email': email})}"
        mailto_url = _mailto_invitation_url(
            email=email,
            invite_url=invite_url,
            role=payload.role,
            expires_at=expires_at,
        )
        return Envelope(
            data=AuthInvitationRead(
                email=email,
                role=payload.role,
                invite_url=invite_url,
                mailto_url=mailto_url,
                expires_at=expires_at,
            )
        )

    @router.get("/auth/users", response_model=ListEnvelope[AuthUserRead])
    def list_auth_users(request: Request, limit: int = 50, offset: int = 0):
        validate_pagination(limit, offset)
        _ensure_admin(request)
        users = [auth_user_read(user) for user in auth_service.list_users()]
        items, total = paginate(users, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.patch("/auth/users/{user_id:uuid}", response_model=Envelope[AuthUserRead])
    def update_auth_user(user_id: UUID, payload: AuthUserUpdate, request: Request):
        _ensure_admin(request)
        user = auth_service.update_user(
            user_id,
            role=payload.role,
            password=payload.password,
        )
        return Envelope(data=auth_user_read(user))

    @router.post("/auth/login", response_model=Envelope[AuthTokenRead])
    def login_auth(payload: AuthLoginRequest):
        user = auth_service.authenticate(payload.username, payload.password)
        token = token_service.issue_access_token(user)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.post("/auth/refresh", response_model=Envelope[AuthTokenRead])
    def refresh_auth(request: Request):
        if not request.app.state.auth_enabled:
            raise AuthError("Token refresh is unavailable when authentication is disabled.")
        actor = actor_from_request(request)
        user = auth_service.get_user_by_id(actor.user_id)
        if user is None:
            raise AuthError("Authentication required.")
        token = token_service.issue_access_token(user)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.get("/auth/me", response_model=Envelope[AuthUserRead])
    def auth_me(request: Request):
        actor = actor_from_request(request)
        if not request.app.state.auth_enabled:
            user = AuthUserRead(
                user_id=UUID(str(actor.user_id)),
                username=LOCAL_AUTH_USERNAME,
                role=actor.role,
                created_at=datetime.now(timezone.utc),
            )
            return Envelope(data=user, meta={"auth_enabled": False})
        user = auth_service.get_user_by_id(actor.user_id)
        if user is None:
            raise AuthError("Authentication required.")
        return Envelope(data=auth_user_read(user), meta={"auth_enabled": True})

    return router


def _ensure_admin(request: Request) -> None:
    actor = actor_from_request(request)
    if actor.role != Role.ADMIN:
        raise AuthError("Admin privileges required.")


def _public_base_url(request: Request) -> str:
    configured = str(getattr(request.app.state.settings, "public_base_url", "") or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _mailto_invitation_url(
    *,
    email: str,
    invite_url: str,
    role: Role,
    expires_at: datetime,
) -> str:
    subject = "Lab Tracker invitation"
    body = (
        "You have been invited to Lab Tracker.\n\n"
        f"Open this link to create your password and sign in as {role.value}:\n"
        f"{invite_url}\n\n"
        f"This invitation expires at {expires_at.isoformat()}."
    )
    return f"mailto:{quote(email)}?{urlencode({'subject': subject, 'body': body})}"
