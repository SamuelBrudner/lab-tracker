"""Personal access token management routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.auth import AuthService, PersonalAccessToken, PersonalAccessTokenService
from lab_tracker.errors import AuthError
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    PersonalAccessTokenCreate,
    PersonalAccessTokenIssuedRead,
    PersonalAccessTokenRead,
)

from .shared import actor_from_request, list_response


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

    return router


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
