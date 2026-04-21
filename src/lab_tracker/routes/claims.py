"""Claim routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import Claim, ClaimStatus
from lab_tracker.schemas import ClaimCreate, ClaimUpdate, Envelope, ListEnvelope

from .shared import (
    actor_from_request,
    api_from_request,
    list_response,
    repository_from_request,
    validate_pagination,
)


def build_claims_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/claims",
        response_model=Envelope[Claim],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_claim(payload: ClaimCreate, request: Request):
        actor = actor_from_request(request)
        claim = api_from_request(request, api).create_claim(
            project_id=payload.project_id,
            statement=payload.statement,
            confidence=payload.confidence,
            status=payload.status or ClaimStatus.PROPOSED,
            supported_by_dataset_ids=payload.supported_by_dataset_ids,
            supported_by_analysis_ids=payload.supported_by_analysis_ids,
            actor=actor,
        )
        return Envelope(data=claim)

    @router.get("/claims", response_model=ListEnvelope[Claim])
    def list_claims(
        request: Request,
        project_id: UUID | None = None,
        status: ClaimStatus | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        claims, total = repository_from_request(request).query_claims(
            project_id=project_id,
            status=status.value if status is not None else None,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            limit=limit,
            offset=offset,
        )
        return list_response(claims, limit=limit, offset=offset, total=total)

    @router.get("/claims/{claim_id}", response_model=Envelope[Claim])
    def get_claim(claim_id: UUID, request: Request):
        claim = api_from_request(request, api).get_claim(claim_id)
        return Envelope(data=claim)

    @router.patch("/claims/{claim_id}", response_model=Envelope[Claim])
    def update_claim(claim_id: UUID, payload: ClaimUpdate, request: Request):
        actor = actor_from_request(request)
        claim = api_from_request(request, api).update_claim(
            claim_id,
            statement=payload.statement,
            confidence=payload.confidence,
            status=payload.status,
            supported_by_dataset_ids=payload.supported_by_dataset_ids,
            supported_by_analysis_ids=payload.supported_by_analysis_ids,
            actor=actor,
        )
        return Envelope(data=claim)

    @router.delete("/claims/{claim_id}", response_model=Envelope[Claim])
    def delete_claim(claim_id: UUID, request: Request):
        actor = actor_from_request(request)
        claim = api_from_request(request, api).delete_claim(claim_id, actor=actor)
        return Envelope(data=claim)

    return router
