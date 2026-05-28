"""Project routes."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.db_models import (
    AnalysisModel,
    DatasetFileModel,
    DatasetModel,
    NoteModel,
    VisualizationModel,
)
from lab_tracker.errors import NotFoundError
from lab_tracker.models import Project, ProjectMembership, ProjectStatus
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
    db_session_from_request,
    ensure_project_owner,
    ensure_project_read,
    file_storage_from_request,
    filter_project_scoped_items,
    list_response,
    paginate,
    project_default_status,
    repository_from_request,
    validate_pagination,
)

_logger = logging.getLogger(__name__)


def _delete_stored_file(storage_backend: object, storage_id: UUID) -> None:
    try:
        storage_backend.delete(storage_id)
    except NotFoundError:
        return
    except Exception as exc:
        _logger.warning(
            "Failed to delete project-scoped storage object %s: %s",
            storage_id,
            exc,
            exc_info=True,
        )


def build_projects_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/projects",
        response_model=Envelope[Project],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_project(payload: ProjectCreate, request: Request):
        actor = actor_from_request(request)
        project = api_from_request(request, api).create_project(
            name=payload.name,
            description=payload.description or "",
            status=payload.status or project_default_status(),
            actor=actor,
        )
        return Envelope(data=project)

    @router.get("/projects", response_model=ListEnvelope[Project])
    def list_projects(
        request: Request,
        status: ProjectStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        projects, _ = repository_from_request(request).query_projects(
            status=status.value if status is not None else None,
            limit=None,
            offset=0,
        )
        visible = filter_project_scoped_items(request, projects)
        items, total = paginate(visible, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get("/projects/{project_id}", response_model=Envelope[Project])
    def get_project(project_id: UUID, request: Request):
        project = api_from_request(request, api).get_project(project_id)
        ensure_project_read(request, project.project_id)
        return Envelope(data=project)

    @router.patch("/projects/{project_id}", response_model=Envelope[Project])
    def update_project(project_id: UUID, payload: ProjectUpdate, request: Request):
        actor = actor_from_request(request)
        ensure_project_owner(request, project_id)
        project = api_from_request(request, api).update_project(
            project_id,
            name=payload.name,
            description=payload.description,
            status=payload.status,
            actor=actor,
        )
        return Envelope(data=project)

    @router.delete("/projects/{project_id}", response_model=Envelope[Project])
    def delete_project(project_id: UUID, request: Request):
        request_api = api_from_request(request, api)
        actor = actor_from_request(request)
        ensure_project_owner(request, project_id)
        db_session = db_session_from_request(request)
        file_storage_backend = file_storage_from_request(request)
        raw_note_storage = request.app.state.raw_note_storage
        dataset_file_storage_ids = [
            UUID(value)
            for value in db_session.scalars(
                select(DatasetFileModel.storage_id)
                .join(DatasetModel, DatasetModel.dataset_id == DatasetFileModel.dataset_id)
                .where(DatasetModel.project_id == str(project_id))
            )
        ]
        raw_note_storage_ids = [
            UUID(value)
            for value in db_session.scalars(
                select(NoteModel.raw_storage_id).where(
                    NoteModel.project_id == str(project_id),
                    NoteModel.raw_storage_id.is_not(None),
                )
            )
        ]
        visualization_storage_ids = [
            UUID(value)
            for value in db_session.scalars(
                select(VisualizationModel.asset_storage_id)
                .join(
                    AnalysisModel,
                    AnalysisModel.analysis_id == VisualizationModel.analysis_id,
                )
                .where(
                    AnalysisModel.project_id == str(project_id),
                    VisualizationModel.asset_storage_id.is_not(None),
                )
            )
        ]
        project = request_api.delete_project(project_id, actor=actor)
        db_session.flush()
        for storage_id in dataset_file_storage_ids:
            request_api.run_after_commit(
                lambda storage_id=storage_id: _delete_stored_file(
                    file_storage_backend,
                    storage_id,
                )
            )
        for storage_id in raw_note_storage_ids:
            request_api.run_after_commit(
                lambda storage_id=storage_id: _delete_stored_file(
                    raw_note_storage,
                    storage_id,
                )
            )
        for storage_id in visualization_storage_ids:
            request_api.run_after_commit(
                lambda storage_id=storage_id: _delete_stored_file(
                    file_storage_backend,
                    storage_id,
                )
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
        user_id = _resolve_member_user_id(request, payload)
        membership = api_from_request(request, api).upsert_project_membership(
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
        membership = api_from_request(request, api).upsert_project_membership(
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
        user = request.app.state.auth_service.get_user_by_id(payload.user_id)
        if user is None:
            raise NotFoundError("User does not exist.")
        return payload.user_id
    if payload.username:
        user = request.app.state.auth_service.get_user(payload.username)
        if user is not None:
            return user.user_id
    raise NotFoundError("User does not exist.")
