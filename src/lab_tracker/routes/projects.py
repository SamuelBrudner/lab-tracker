"""Project routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import Response

from lab_tracker.api import LabTrackerAPI
from lab_tracker.errors import NotFoundError
from lab_tracker.models import (
    Project,
    ProjectMembership,
    ProjectStatus,
    PublicationReadinessReport,
    UsageEventResourceType,
)
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    ProjectCreate,
    ProjectMembershipCreate,
    ProjectMembershipUpdate,
    ProjectUpdate,
)

from .shared import (
    actor_from_request,
    api_from_request,
    ensure_project_owner,
    ensure_project_read,
    handlers_from_request,
    list_response,
    paginate,
    project_default_status,
    record_usage_view,
    validate_pagination,
)


def build_projects_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/projects",
        response_model=Envelope[Project],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_project(payload: ProjectCreate, request: Request, response: Response):
        actor = actor_from_request(request)
        result = api_from_request(request, api).create_project_result(
            name=payload.name,
            description=payload.description or "",
            status=payload.status or project_default_status(),
            group_id=payload.group_id,
            client_capture_id=payload.client_capture_id,
            actor=actor,
        )
        if result.reused:
            response.status_code = http_status.HTTP_200_OK
        return Envelope(data=result.entity)

    @router.get("/projects", response_model=ListEnvelope[Project])
    def list_projects(
        request: Request,
        status: ProjectStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        page = handlers_from_request(request).catalogs.list_projects(
            actor=actor_from_request(request),
            status=status.value if status is not None else None,
            limit=limit,
            offset=offset,
        )
        return list_response(
            page.items,
            limit=limit,
            offset=offset,
            total=page.total,
        )

    @router.get("/projects/{project_id}", response_model=Envelope[Project])
    def get_project(project_id: UUID, request: Request):
        project = api_from_request(request, api).get_project(project_id)
        ensure_project_read(request, project.project_id)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.PROJECT,
            resource_id=project.project_id,
            project_id=project.project_id,
        )
        return Envelope(data=project)

    @router.get(
        "/projects/{project_id}/publication-readiness",
        response_model=Envelope[PublicationReadinessReport],
    )
    def publication_readiness(project_id: UUID, request: Request):
        actor = actor_from_request(request)
        report = api_from_request(request, api).check_publication_readiness(
            project_id,
            actor=actor,
        )
        return Envelope(data=report)

    @router.patch("/projects/{project_id}", response_model=Envelope[Project])
    def update_project(project_id: UUID, payload: ProjectUpdate, request: Request):
        actor = actor_from_request(request)
        ensure_project_owner(request, project_id)
        project = api_from_request(request, api).update_project(
            project_id,
            actor=actor,
            **provided_fields(payload),
        )
        return Envelope(data=project)

    @router.delete("/projects/{project_id}", response_model=Envelope[Project])
    def delete_project(project_id: UUID, request: Request):
        actor = actor_from_request(request)
        project = handlers_from_request(request).deletions.delete_project(
            project_id,
            actor=actor,
        )
        return Envelope(data=project)

    @router.get("/projects/{project_id}/members", response_model=ListEnvelope[ProjectMembership])
    def list_project_members(
        project_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        ensure_project_read(request, project_id)
        members = api_from_request(request, api).list_project_memberships(project_id=project_id)
        items, total = paginate(members, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.post(
        "/projects/{project_id}/members",
        response_model=Envelope[ProjectMembership],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_project_member(
        project_id: UUID,
        payload: ProjectMembershipCreate,
        request: Request,
    ):
        actor = actor_from_request(request)
        request_api = api_from_request(request, api)
        request_api.require_project_owner(project_id, actor=actor)
        user_id = _resolve_member_user_id(request, payload)
        membership = request_api.upsert_project_membership(
            project_id,
            user_id,
            payload.role,
            actor=actor,
        )
        return Envelope(data=membership)

    @router.patch(
        "/projects/{project_id}/members/{user_id:uuid}",
        response_model=Envelope[ProjectMembership],
    )
    def update_project_member(
        project_id: UUID,
        user_id: UUID,
        payload: ProjectMembershipUpdate,
        request: Request,
    ):
        actor = actor_from_request(request)
        request_api = api_from_request(request, api)
        request_api.require_project_owner(project_id, actor=actor)
        request_api.get_project(project_id)
        _ensure_member_user_exists(request, user_id)
        membership = request_api.update_project_membership(
            project_id,
            user_id,
            payload.role,
            actor=actor,
        )
        return Envelope(data=membership)

    @router.delete(
        "/projects/{project_id}/members/{user_id:uuid}",
        response_model=Envelope[ProjectMembership],
    )
    def delete_project_member(project_id: UUID, user_id: UUID, request: Request):
        actor = actor_from_request(request)
        membership = api_from_request(request, api).delete_project_membership(
            project_id,
            user_id,
            actor=actor,
        )
        return Envelope(data=membership)

    return router


def _resolve_member_user_id(request: Request, payload: ProjectMembershipCreate) -> UUID:
    if payload.user_id is not None:
        _ensure_member_user_exists(request, payload.user_id)
        return payload.user_id
    if payload.username:
        user = request.app.state.auth_service.get_user(payload.username)
        if user is not None:
            return user.user_id
    raise NotFoundError("User does not exist.")


def _ensure_member_user_exists(request: Request, user_id: UUID) -> None:
    if request.app.state.auth_service.get_user_by_id(user_id) is None:
        raise NotFoundError("User does not exist.")
