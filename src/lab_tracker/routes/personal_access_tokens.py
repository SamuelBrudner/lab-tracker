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
    Role,
    User,
    effective_personal_access_token_role,
    require_interactive_admin,
)
from lab_tracker.errors import AuthError, NotFoundError, PermissionDeniedError
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
            raise PermissionDeniedError("Personal access tokens require user credentials.")
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
        return Envelope(
            data=_issued_token_read(issued.token, owner_role=user.role, secret=issued.secret)
        )

    @router.get(
        "/auth/tokens",
        response_model=ListEnvelope[PersonalAccessTokenRead],
    )
    def list_personal_access_tokens(request: Request):
        actor = actor_from_request(request)
        if actor.is_device or actor.is_service:
            raise PermissionDeniedError("Personal access tokens require user credentials.")
        owner = _token_owner(auth_service, actor)
        items = [
            _token_read(token, owner_role=owner.role)
            for token in personal_access_token_service.list_tokens(owner.user_id)
        ]
        return list_response(items, limit=max(len(items), 1), offset=0, total=len(items))

    @router.delete(
        "/auth/tokens/{token_id:uuid}",
        response_model=Envelope[PersonalAccessTokenRead],
    )
    def revoke_personal_access_token(token_id: UUID, request: Request):
        actor = actor_from_request(request)
        if actor.is_device or actor.is_service:
            raise PermissionDeniedError("Personal access tokens require user credentials.")
        owner = _token_owner(auth_service, actor)
        token = personal_access_token_service.revoke_token(owner.user_id, token_id)
        return Envelope(data=_token_read(token, owner_role=owner.role))

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
        require_interactive_admin(actor_from_request(request))
        owner = _target_user(auth_service, user_id)
        tokens = [
            _token_read(token, owner_role=owner.role)
            for token in personal_access_token_service.list_tokens(user_id)
        ]
        items, total = paginate(tokens, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.delete(
        "/auth/users/{user_id:uuid}/tokens/{token_id:uuid}",
        response_model=Envelope[PersonalAccessTokenRead],
    )
    def revoke_user_personal_access_token(user_id: UUID, token_id: UUID, request: Request):
        require_interactive_admin(actor_from_request(request))
        owner = _target_user(auth_service, user_id)
        token = personal_access_token_service.revoke_token(user_id, token_id)
        return Envelope(data=_token_read(token, owner_role=owner.role))

    return router


def _token_owner(auth_service: AuthService, actor: AuthContext) -> User:
    user = auth_service.get_user_by_id(actor.user_id)
    if user is None:
        raise AuthError("Authentication required.")
    return user


def _target_user(auth_service: AuthService, user_id: UUID) -> User:
    user = auth_service.get_user_by_id(user_id)
    if user is None:
        raise NotFoundError("User does not exist.")
    return user


def _token_read(token: PersonalAccessToken, *, owner_role: Role) -> PersonalAccessTokenRead:
    # Report the role the middleware would grant now, not just the stored one.
    return PersonalAccessTokenRead(
        token_id=token.token_id,
        label=token.label,
        role=token.role,
        effective_role=effective_personal_access_token_role(token.role, owner_role=owner_role),
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
    owner_role: Role,
    secret: str,
) -> PersonalAccessTokenIssuedRead:
    return PersonalAccessTokenIssuedRead(
        **_token_read(token, owner_role=owner_role).model_dump(),
        secret=secret,
    )
