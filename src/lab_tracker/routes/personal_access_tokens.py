"""Personal access token management routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.auth import (
    AuthContext,
    AuthService,
    PersonalAccessToken,
    PersonalAccessTokenService,
    PrincipalType,
    Role,
)
from lab_tracker.errors import AuthError, NotFoundError
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    PersonalAccessTokenCreate,
    PersonalAccessTokenIssuedRead,
    PersonalAccessTokenRead,
)

from .shared import actor_from_request, list_response, paginate, validate_pagination


def build_personal_access_tokens_router(
    *,
    auth_service: AuthService,
    personal_access_token_service: PersonalAccessTokenService,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/auth/tokens",
        response_model=Envelope[PersonalAccessTokenIssuedRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_personal_access_token(payload: PersonalAccessTokenCreate, request: Request):
        actor = actor_from_request(request)
        if actor.is_device or actor.is_service:
            raise AuthError("Personal access tokens require user credentials.")
        user = auth_service.get_user_by_id(actor.user_id)
        if user is None:
            raise AuthError("Authentication required.")
        issued = personal_access_token_service.issue_token(
            user,
            label=payload.label,
            role=payload.role,
            read_only=payload.read_only,
            scope=payload.scope,
            expires_at=payload.expires_at,
        )
        return Envelope(data=_issued_token_read(issued.token, secret=issued.secret))

    @router.get(
        "/auth/tokens",
        response_model=ListEnvelope[PersonalAccessTokenRead],
    )
    def list_personal_access_tokens(request: Request):
        actor = actor_from_request(request)
        if actor.is_device or actor.is_service:
            raise AuthError("Personal access tokens require user credentials.")
        items = [
            _token_read(token)
            for token in personal_access_token_service.list_tokens(actor.user_id)
        ]
        return list_response(items, limit=max(len(items), 1), offset=0, total=len(items))

    @router.delete(
        "/auth/tokens/{token_id:uuid}",
        response_model=Envelope[PersonalAccessTokenRead],
    )
    def revoke_personal_access_token(token_id: UUID, request: Request):
        actor = actor_from_request(request)
        if actor.is_device or actor.is_service:
            raise AuthError("Personal access tokens require user credentials.")
        token = personal_access_token_service.revoke_token(actor.user_id, token_id)
        return Envelope(data=_token_read(token))

    @router.get(
        "/auth/users/{user_id:uuid}/tokens",
        response_model=ListEnvelope[PersonalAccessTokenRead],
    )
    def list_user_personal_access_tokens(
        user_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        _ensure_interactive_admin(actor_from_request(request))
        if auth_service.get_user_by_id(user_id) is None:
            raise NotFoundError("User does not exist.")
        tokens = [
            _token_read(token) for token in personal_access_token_service.list_tokens(user_id)
        ]
        items, total = paginate(tokens, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.delete(
        "/auth/users/{user_id:uuid}/tokens/{token_id:uuid}",
        response_model=Envelope[PersonalAccessTokenRead],
    )
    def revoke_user_personal_access_token(user_id: UUID, token_id: UUID, request: Request):
        _ensure_interactive_admin(actor_from_request(request))
        token = personal_access_token_service.revoke_token(user_id, token_id)
        return Envelope(data=_token_read(token))

    return router


def _ensure_interactive_admin(actor: AuthContext) -> None:
    """Admin credential management needs a person at an admin session.

    Paired devices and lpat_ service tokens are already fenced off /auth/* by
    the middleware; this re-check keeps the routes fail-closed on their own.
    """

    if actor.principal_type is not PrincipalType.USER:
        raise AuthError("Personal access tokens require user credentials.")
    if actor.role is not Role.ADMIN:
        raise AuthError("Admin privileges required.")


def _token_read(token: PersonalAccessToken) -> PersonalAccessTokenRead:
    return PersonalAccessTokenRead(
        token_id=token.token_id,
        label=token.label,
        role=token.role,
        read_only=token.read_only,
        scope=token.scope,
        expires_at=token.expires_at,
        created_at=token.created_at,
        last_used_at=token.last_used_at,
        revoked_at=token.revoked_at,
    )


def _issued_token_read(
    token: PersonalAccessToken,
    *,
    secret: str,
) -> PersonalAccessTokenIssuedRead:
    return PersonalAccessTokenIssuedRead(
        **_token_read(token).model_dump(),
        secret=secret,
    )
