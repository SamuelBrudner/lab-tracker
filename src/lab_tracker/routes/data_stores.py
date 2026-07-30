"""Data store registry routes: register and list data-store locations."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import DataStore
from lab_tracker.schemas import DataStoreCreate, Envelope, ListEnvelope

from .shared import (
    actor_from_request,
    api_from_request,
    handlers_from_request,
    list_response,
    paginate,
    validate_pagination,
)


def build_data_stores_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/data-stores",
        response_model=Envelope[DataStore],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_data_store(payload: DataStoreCreate, request: Request):
        store = api_from_request(request, api).create_data_store(
            project_id=payload.project_id,
            group_id=payload.group_id,
            name=payload.name,
            kind=payload.kind,
            root=payload.root,
            capabilities=payload.capabilities,
            endpoint=payload.endpoint,
            credential_ref=payload.credential_ref,
            authority_grant_id=payload.authority_grant_id,
            is_default=payload.is_default,
            actor=actor_from_request(request),
        )
        return Envelope(data=store)

    @router.get("/data-stores", response_model=ListEnvelope[DataStore])
    def list_data_stores(
        request: Request,
        project_id: UUID | None = None,
        group_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        stores = api_from_request(request, api).list_data_stores(
            project_id=project_id,
            group_id=group_id,
            actor=actor_from_request(request),
        )
        page, total = paginate(stores, limit, offset)
        return list_response(page, limit=limit, offset=offset, total=total)

    @router.get("/data-stores/{store_id}", response_model=Envelope[DataStore])
    def get_data_store(store_id: UUID, request: Request):
        store = api_from_request(request, api).get_data_store_for_read(
            store_id,
            actor=actor_from_request(request),
        )
        return Envelope(data=store)

    @router.get("/data-stores/{store_id}/health")
    def data_store_health(store_id: UUID, request: Request):
        result = handlers_from_request(request).store_health.check(
            store_id,
            actor=actor_from_request(request),
        )
        return Envelope(
            data={
                "store_id": str(result.store_id),
                "kind": result.kind.value,
                **result.health.to_json_dict(),
            }
        )

    return router
