"""Provenance export routes and shared document assembly.

The payload builders here back both the ``/{resource}/{id}/provenance``
routes and JSON-LD content negotiation on the resource URIs themselves
(``GET /datasets/{id}`` with ``Accept: application/ld+json``), so a
document's ``@id`` dereferences to the same document.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_tracker.api import LabTrackerAPI

from .shared import (
    actor_from_request,
    handlers_from_request,
    provenance_base_url,
)


def dataset_provenance_payload(
    request: Request,
    api: LabTrackerAPI,
    dataset_id: UUID,
) -> dict[str, object]:
    return handlers_from_request(request).context.dataset_provenance(
        dataset_id,
        actor=actor_from_request(request),
        base_url=provenance_base_url(request),
    )


def analysis_provenance_payload(
    request: Request,
    api: LabTrackerAPI,
    analysis_id: UUID,
) -> dict[str, object]:
    return handlers_from_request(request).context.analysis_provenance(
        analysis_id,
        actor=actor_from_request(request),
        base_url=provenance_base_url(request),
    )


def claim_provenance_payload(
    request: Request,
    api: LabTrackerAPI,
    claim_id: UUID,
) -> dict[str, object]:
    return handlers_from_request(request).context.claim_provenance(
        claim_id,
        actor=actor_from_request(request),
        base_url=provenance_base_url(request),
    )


def jsonld_response(payload: dict[str, object]) -> JSONResponse:
    return JSONResponse(
        content=jsonable_encoder(payload),
        media_type="application/ld+json",
    )


def build_provenance_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/datasets/{dataset_id}/provenance")
    def get_dataset_provenance(dataset_id: UUID, request: Request):
        return jsonld_response(dataset_provenance_payload(request, api, dataset_id))

    @router.get("/analyses/{analysis_id}/provenance")
    def get_analysis_provenance(analysis_id: UUID, request: Request):
        return jsonld_response(analysis_provenance_payload(request, api, analysis_id))

    @router.get("/claims/{claim_id}/provenance")
    def get_claim_provenance(claim_id: UUID, request: Request):
        return jsonld_response(claim_provenance_payload(request, api, claim_id))

    return router
