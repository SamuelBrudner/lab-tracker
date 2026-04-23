"""Provenance export routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.provenance import (
    build_analysis_provenance_document,
    build_dataset_provenance_document,
)

from .shared import api_from_request, repository_from_request


def _request_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def build_provenance_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/datasets/{dataset_id}/provenance")
    def get_dataset_provenance(dataset_id: UUID, request: Request):
        dataset = api_from_request(request, api).get_dataset(dataset_id)
        payload = build_dataset_provenance_document(_request_base_url(request), dataset)
        return JSONResponse(content=payload, media_type="application/ld+json")

    @router.get("/analyses/{analysis_id}/provenance")
    def get_analysis_provenance(analysis_id: UUID, request: Request):
        request_api = api_from_request(request, api)
        repository = repository_from_request(request)
        analysis = request_api.get_analysis(analysis_id)
        datasets = [request_api.get_dataset(dataset_id) for dataset_id in analysis.dataset_ids]
        claims, _ = repository.query_claims(analysis_id=analysis_id, limit=None, offset=0)
        visualizations, _ = repository.query_visualizations(
            analysis_id=analysis_id,
            limit=None,
            offset=0,
        )
        payload = build_analysis_provenance_document(
            _request_base_url(request),
            analysis,
            datasets=datasets,
            claims=claims,
            visualizations=visualizations,
        )
        return JSONResponse(content=payload, media_type="application/ld+json")

    return router
