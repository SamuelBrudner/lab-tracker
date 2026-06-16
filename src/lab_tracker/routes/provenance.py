"""Provenance export routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.provenance import (
    build_analysis_provenance_document,
    build_claim_provenance_document,
    build_dataset_provenance_document,
)

from .shared import api_from_request, ensure_project_read, repository_from_request


def _request_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def build_provenance_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/datasets/{dataset_id}/provenance")
    def get_dataset_provenance(dataset_id: UUID, request: Request):
        dataset = api_from_request(request, api).get_dataset(dataset_id)
        ensure_project_read(request, dataset.project_id)
        repository = repository_from_request(request)
        supervision_edges, _ = repository.query_supervision_edges(limit=None, offset=0)
        payload = build_dataset_provenance_document(
            _request_base_url(request),
            dataset,
            supervision_edges=supervision_edges,
        )
        return JSONResponse(content=payload, media_type="application/ld+json")

    @router.get("/analyses/{analysis_id}/provenance")
    def get_analysis_provenance(analysis_id: UUID, request: Request):
        request_api = api_from_request(request, api)
        repository = repository_from_request(request)
        analysis = request_api.get_analysis(analysis_id)
        ensure_project_read(request, analysis.project_id)
        datasets = [request_api.get_dataset(dataset_id) for dataset_id in analysis.dataset_ids]
        claims, _ = repository.query_claims(analysis_id=analysis_id, limit=None, offset=0)
        claim_ids = {claim.claim_id for claim in claims}
        claim_edges, _ = repository.query_claim_edges(
            project_id=analysis.project_id,
            limit=None,
            offset=0,
        )
        claim_edges = [edge for edge in claim_edges if edge.claim_id in claim_ids]
        visualizations, _ = repository.query_visualizations(
            analysis_id=analysis_id,
            limit=None,
            offset=0,
        )
        supervision_edges, _ = repository.query_supervision_edges(limit=None, offset=0)
        payload = build_analysis_provenance_document(
            _request_base_url(request),
            analysis,
            datasets=datasets,
            claims=claims,
            visualizations=visualizations,
            claim_edges=claim_edges,
            supervision_edges=supervision_edges,
        )
        return JSONResponse(content=payload, media_type="application/ld+json")

    @router.get("/claims/{claim_id}/provenance")
    def get_claim_provenance(claim_id: UUID, request: Request):
        request_api = api_from_request(request, api)
        repository = repository_from_request(request)
        claim = request_api.get_claim(claim_id)
        ensure_project_read(request, claim.project_id)

        analyses = [
            request_api.get_analysis(analysis_id)
            for analysis_id in claim.supported_by_analysis_ids
        ]
        dataset_ids = set(claim.supported_by_dataset_ids)
        for analysis in analyses:
            dataset_ids.update(analysis.dataset_ids)
        datasets = [request_api.get_dataset(dataset_id) for dataset_id in sorted(dataset_ids)]
        question_ids = set(claim.answers_question_ids)
        for dataset in datasets:
            question_ids.update(link.question_id for link in dataset.question_links)
        questions = [
            request_api.get_question(question_id) for question_id in sorted(question_ids)
        ]
        visualizations, _ = repository.query_visualizations(
            claim_id=claim_id,
            limit=None,
            offset=0,
        )
        claim_edges, _ = repository.query_claim_edges(
            project_id=claim.project_id,
            limit=None,
            offset=0,
        )
        claim_edges = [
            edge
            for edge in claim_edges
            if edge.claim_id == claim_id or edge.target_claim_id == claim_id
        ]
        supervision_edges, _ = repository.query_supervision_edges(limit=None, offset=0)
        payload = build_claim_provenance_document(
            _request_base_url(request),
            claim,
            analyses=analyses,
            datasets=datasets,
            questions=questions,
            visualizations=visualizations,
            claim_edges=claim_edges,
            supervision_edges=supervision_edges,
        )
        return JSONResponse(
            content=jsonable_encoder(payload),
            media_type="application/ld+json",
        )

    return router
