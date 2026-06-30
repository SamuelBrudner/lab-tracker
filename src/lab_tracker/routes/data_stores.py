"""Data store registry routes: register and list data-store locations."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.artifact_resolution import check_store_health
from lab_tracker.models import DataStore
from lab_tracker.schemas import DataStoreCreate, Envelope, ListEnvelope

from .shared import (
    actor_from_request,
    api_from_request,
    ensure_project_read,
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
            name=payload.name,
            kind=payload.kind,
            root=payload.root,
            capabilities=payload.capabilities,
            endpoint=payload.endpoint,
            credential_ref=payload.credential_ref,
            is_default=payload.is_default,
            actor=actor_from_request(request),
        )
        return Envelope(data=store)

    @router.get("/data-stores", response_model=ListEnvelope[DataStore])
    def list_data_stores(
        request: Request,
        project_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        stores = api_from_request(request, api).list_data_stores(
            project_id=project_id,
            actor=actor_from_request(request),
        )
        page, total = paginate(stores, limit, offset)
        return list_response(page, limit=limit, offset=offset, total=total)

    @router.get("/data-stores/{store_id}", response_model=Envelope[DataStore])
    def get_data_store(store_id: UUID, request: Request):
        store = api_from_request(request, api).get_data_store(store_id)
        ensure_project_read(request, store.project_id)
        return Envelope(data=store)

    @router.get("/data-stores/{store_id}/health")
    def data_store_health(store_id: UUID, request: Request):
        store = api_from_request(request, api).get_data_store(store_id)
        ensure_project_read(request, store.project_id)
        checker = getattr(request.app.state, "store_health_checker", None)
        health = checker(store) if checker is not None else check_store_health(store)
        return Envelope(
            data={
                "store_id": str(store.store_id),
                "kind": store.kind.value,
                **health.to_json_dict(),
            }
        )

    return router
