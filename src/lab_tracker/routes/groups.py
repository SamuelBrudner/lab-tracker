"""Project group routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.errors import NotFoundError
from lab_tracker.models import (
    GroupMembership,
    ProjectGroup,
    ProjectGroupKind,
    ProjectMembership,
    UsageEventResourceType,
)
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import (
    Envelope,
    GroupMembershipCreate,
    GroupMembershipUpdate,
    GroupProjectMembershipBulkCreate,
    ListEnvelope,
    ProjectGroupCreate,
    ProjectGroupUpdate,
)

from .shared import (
    actor_from_request,
    api_from_request,
    ensure_group_owner,
    ensure_group_read,
    list_response,
    paginate,
    record_usage_view,
    validate_pagination,
)


def build_groups_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/groups",
        response_model=Envelope[ProjectGroup],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_group(payload: ProjectGroupCreate, request: Request):
        actor = actor_from_request(request)
        group = api_from_request(request, api).create_project_group(
            name=payload.name,
            description=payload.description or "",
            kind=payload.kind or ProjectGroupKind.LAB,
            group_read_all=payload.group_read_all or False,
            actor=actor,
        )
        return Envelope(data=group)

    @router.get("/groups", response_model=ListEnvelope[ProjectGroup])
    def list_groups(
        request: Request,
        kind: ProjectGroupKind | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        actor = actor_from_request(request)
        groups = api_from_request(request, api).list_project_groups(actor=actor)
        if kind is not None:
            groups = [group for group in groups if group.kind == kind]
        items, total = paginate(groups, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get("/groups/{group_id:uuid}", response_model=Envelope[ProjectGroup])
    def get_group(group_id: UUID, request: Request):
        group = api_from_request(request, api).get_project_group(group_id)
        ensure_group_read(request, group.group_id)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.PROJECT_GROUP,
            resource_id=group.group_id,
        )
        return Envelope(data=group)

    @router.patch("/groups/{group_id:uuid}", response_model=Envelope[ProjectGroup])
    def update_group(group_id: UUID, payload: ProjectGroupUpdate, request: Request):
        actor = actor_from_request(request)
        group = api_from_request(request, api).update_project_group(
            group_id,
            actor=actor,
            **provided_fields(payload),
        )
        return Envelope(data=group)

    @router.delete("/groups/{group_id:uuid}", response_model=Envelope[ProjectGroup])
    def delete_group(group_id: UUID, request: Request):
        actor = actor_from_request(request)
        group = api_from_request(request, api).delete_project_group(group_id, actor=actor)
        return Envelope(data=group)

    @router.get(
        "/groups/{group_id:uuid}/members",
        response_model=ListEnvelope[GroupMembership],
    )
    def list_group_members(
        group_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        actor = actor_from_request(request)
        members = api_from_request(request, api).list_group_memberships(
            group_id=group_id,
            actor=actor,
        )
        items, total = paginate(members, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.post(
        "/groups/{group_id:uuid}/members",
        response_model=Envelope[GroupMembership],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_group_member(
        group_id: UUID,
        payload: GroupMembershipCreate,
        request: Request,
    ):
        actor = actor_from_request(request)
        user_id = _resolve_member_user_id(request, payload)
        membership = api_from_request(request, api).upsert_group_membership(
            group_id,
            user_id,
            payload.role,
            actor=actor,
        )
        return Envelope(data=membership)

    @router.patch(
        "/groups/{group_id:uuid}/members/{user_id:uuid}",
        response_model=Envelope[GroupMembership],
    )
    def update_group_member(
        group_id: UUID,
        user_id: UUID,
        payload: GroupMembershipUpdate,
        request: Request,
    ):
        actor = actor_from_request(request)
        membership = api_from_request(request, api).upsert_group_membership(
            group_id,
            user_id,
            payload.role,
            actor=actor,
        )
        return Envelope(data=membership)

    @router.delete(
        "/groups/{group_id:uuid}/members/{user_id:uuid}",
        response_model=Envelope[GroupMembership],
    )
    def delete_group_member(group_id: UUID, user_id: UUID, request: Request):
        actor = actor_from_request(request)
        membership = api_from_request(request, api).delete_group_membership(
            group_id,
            user_id,
            actor=actor,
        )
        return Envelope(data=membership)

    @router.post(
        "/groups/{group_id:uuid}/project-memberships",
        response_model=Envelope[list[ProjectMembership]],
    )
    def bulk_upsert_group_project_memberships(
        group_id: UUID,
        payload: GroupProjectMembershipBulkCreate,
        request: Request,
    ):
        actor = actor_from_request(request)
        ensure_group_owner(request, group_id)
        user_id = _resolve_member_user_id(request, payload)
        memberships = api_from_request(request, api).upsert_group_project_memberships(
            group_id,
            user_id,
            payload.role,
            actor=actor,
        )
        return Envelope(data=memberships)

    @router.delete(
        "/groups/{group_id:uuid}/project-memberships/{user_id:uuid}",
        response_model=Envelope[list[ProjectMembership]],
    )
    def bulk_delete_group_project_memberships(
        group_id: UUID,
        user_id: UUID,
        request: Request,
    ):
        actor = actor_from_request(request)
        memberships = api_from_request(request, api).delete_group_project_memberships(
            group_id,
            user_id,
            actor=actor,
        )
        return Envelope(data=memberships)

    return router


def _resolve_member_user_id(
    request: Request,
    payload: GroupMembershipCreate | GroupProjectMembershipBulkCreate,
) -> UUID:
    if payload.user_id is not None:
        user = request.app.state.auth_service.get_user_by_id(payload.user_id)
        if user is None:
            raise NotFoundError("User does not exist.")
        return payload.user_id
    if payload.username:
        user = request.app.state.auth_service.get_user(payload.username)
        if user is not None:
            return user.user_id
    raise NotFoundError("User does not exist.")
