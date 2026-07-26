"""Dataset routes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import Dataset, DatasetStatus, UsageEventResourceType
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import DatasetCreate, DatasetUpdate, Envelope, ListEnvelope

from .provenance import dataset_provenance_payload, jsonld_response
from .shared import (
    CreatedByFilter,
    actor_from_request,
    api_from_request,
    dataset_default_status,
    ensure_project_read,
    handlers_from_request,
    list_response,
    provenance_base_url,
    record_usage_view,
    validate_pagination,
    wants_jsonld,
)


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
            terminal_reason=payload.terminal_reason,
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
        created_by: CreatedByFilter = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        page = handlers_from_request(request).catalogs.list_datasets(
            actor=actor_from_request(request),
            project_id=project_id,
            status=status.value if status is not None else None,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return list_response(
            page.items,
            limit=limit,
            offset=offset,
            total=page.total,
        )

    @router.get("/datasets/{dataset_id}", response_model=Envelope[Dataset])
    def get_dataset(dataset_id: UUID, request: Request):
        dataset = api_from_request(request, api).get_dataset_for_read(
            dataset_id,
            actor=actor_from_request(request),
        )
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.DATASET,
            resource_id=dataset.dataset_id,
            project_id=dataset.project_id,
        )
        if wants_jsonld(request):
            return jsonld_response(dataset_provenance_payload(request, api, dataset_id))
        base_url = provenance_base_url(request)
        return Envelope(
            data=dataset,
            meta={"iri": f"{base_url}/datasets/{dataset.dataset_id}"},
        )

    @router.patch("/datasets/{dataset_id}", response_model=Envelope[Dataset])
    def update_dataset(dataset_id: UUID, payload: DatasetUpdate, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_dataset(dataset_id)
        ensure_project_read(request, existing.project_id)
        dataset = api_from_request(request, api).update_dataset(
            dataset_id,
            actor=actor,
            **provided_fields(payload),
        )
        return Envelope(data=dataset)

    @router.delete("/datasets/{dataset_id}", response_model=Envelope[Dataset])
    def delete_dataset(dataset_id: UUID, request: Request):
        actor = actor_from_request(request)
        dataset = handlers_from_request(request).deletions.delete_dataset(
            dataset_id,
            actor=actor,
        )
        return Envelope(data=dataset)

    return router
