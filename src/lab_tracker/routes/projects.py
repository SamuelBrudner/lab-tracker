"""Project routes."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.db_models import DatasetFileModel, DatasetModel, NoteModel
from lab_tracker.errors import NotFoundError
from lab_tracker.models import Project, ProjectStatus
from lab_tracker.schemas import Envelope, ListEnvelope, ProjectCreate, ProjectUpdate

from .shared import (
    api_from_request,
    actor_from_request,
    db_session_from_request,
    file_storage_from_request,
    list_response,
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
        projects, total = repository_from_request(request).query_projects(
            status=status.value if status is not None else None,
            limit=limit,
            offset=offset,
        )
        return list_response(projects, limit=limit, offset=offset, total=total)

    @router.get("/projects/{project_id}", response_model=Envelope[Project])
    def get_project(project_id: UUID, request: Request):
        project = api_from_request(request, api).get_project(project_id)
        return Envelope(data=project)

    @router.patch("/projects/{project_id}", response_model=Envelope[Project])
    def update_project(project_id: UUID, payload: ProjectUpdate, request: Request):
        actor = actor_from_request(request)
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
        return Envelope(data=project)

    return router
