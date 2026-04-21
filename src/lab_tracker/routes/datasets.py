"""Dataset routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.db_models import DatasetFileModel, DatasetModel
from lab_tracker.errors import NotFoundError
from lab_tracker.models import Dataset, DatasetStatus
from lab_tracker.schemas import DatasetCreate, DatasetUpdate, Envelope, ListEnvelope

from .shared import (
    api_from_request,
    actor_from_request,
    dataset_default_status,
    db_session_from_request,
    file_storage_from_request,
    list_response,
    repository_from_request,
    validate_pagination,
)
from .dataset_files import _delete_stored_dataset_file


def build_datasets_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/datasets",
        response_model=Envelope[Dataset],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_dataset(payload: DatasetCreate, request: Request):
        actor = actor_from_request(request)
        dataset = api_from_request(request, api).create_dataset(
            project_id=payload.project_id,
            primary_question_id=payload.primary_question_id,
            secondary_question_ids=payload.secondary_question_ids,
            status=payload.status or dataset_default_status(),
            commit_manifest=payload.commit_manifest,
            commit_hash=payload.commit_hash,
            actor=actor,
        )
        return Envelope(data=dataset)

    @router.get("/datasets", response_model=ListEnvelope[Dataset])
    def list_datasets(
        request: Request,
        project_id: UUID | None = None,
        status: DatasetStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        datasets, total = repository_from_request(request).query_datasets(
            project_id=project_id,
            status=status.value if status is not None else None,
            limit=limit,
            offset=offset,
        )
        return list_response(datasets, limit=limit, offset=offset, total=total)

    @router.get("/datasets/{dataset_id}", response_model=Envelope[Dataset])
    def get_dataset(dataset_id: UUID, request: Request):
        dataset = api_from_request(request, api).get_dataset(dataset_id)
        return Envelope(data=dataset)

    @router.patch("/datasets/{dataset_id}", response_model=Envelope[Dataset])
    def update_dataset(dataset_id: UUID, payload: DatasetUpdate, request: Request):
        actor = actor_from_request(request)
        dataset = api_from_request(request, api).update_dataset(
            dataset_id,
            status=payload.status,
            question_links=payload.question_links,
            commit_manifest=payload.commit_manifest,
            commit_hash=payload.commit_hash,
            actor=actor,
        )
        return Envelope(data=dataset)

    @router.delete("/datasets/{dataset_id}", response_model=Envelope[Dataset])
    def delete_dataset(dataset_id: UUID, request: Request):
        request_api = api_from_request(request, api)
        actor = actor_from_request(request)
        db_session = db_session_from_request(request)
        storage_backend = file_storage_from_request(request)
        storage_ids = [
            UUID(value)
            for value in db_session.scalars(
                select(DatasetFileModel.storage_id).where(
                    DatasetFileModel.dataset_id == str(dataset_id)
                )
            )
        ]
        dataset = request_api.delete_dataset(dataset_id, actor=actor)
        db_session.flush()
        for storage_id in storage_ids:
            request_api.run_after_commit(
                lambda storage_id=storage_id: _delete_stored_dataset_file(
                    storage_backend,
                    storage_id,
                )
            )
        return Envelope(data=dataset)

    return router
