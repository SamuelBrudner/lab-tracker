"""Claim routes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    Claim,
    ClaimEdge,
    ClaimStatus,
    EntityType,
    EntityVersion,
    EntityVersionDiff,
    UsageEventResourceType,
)
from lab_tracker.schemas import ClaimCreate, ClaimEdgeCreate, ClaimUpdate, Envelope, ListEnvelope

from .provenance import claim_provenance_payload, jsonld_response
from .shared import (
    CreatedByFilter,
    accessible_project_ids_from_request,
    actor_from_request,
    api_from_request,
    ensure_project_read,
    list_response,
    paginate,
    provenance_base_url,
    record_usage_view,
    repository_from_request,
    validate_pagination,
    wants_jsonld,
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
            terminal_reason=payload.terminal_reason,
            falsification_criteria=payload.falsification_criteria,
            verification_plan=payload.verification_plan,
            refuting_outcome=payload.refuting_outcome,
            supported_by_dataset_ids=payload.supported_by_dataset_ids,
            supported_by_analysis_ids=payload.supported_by_analysis_ids,
            answers_question_ids=payload.answers_question_ids,
            external_citations=payload.external_citations,
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
        created_by: CreatedByFilter = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
            project_ids = None
        else:
            project_ids = accessible_project_ids_from_request(request)
        claims, total = repository_from_request(request).query_claims(
            project_id=project_id,
            project_ids=project_ids,
            status=status.value if status is not None else None,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return list_response(claims, limit=limit, offset=offset, total=total)

    @router.get("/claims/{claim_id}", response_model=Envelope[Claim])
    def get_claim(claim_id: UUID, request: Request):
        claim = api_from_request(request, api).get_claim(claim_id)
        ensure_project_read(request, claim.project_id)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.CLAIM,
            resource_id=claim.claim_id,
            project_id=claim.project_id,
        )
        if wants_jsonld(request):
            return jsonld_response(claim_provenance_payload(request, api, claim_id))
        base_url = provenance_base_url(request)
        return Envelope(
            data=claim,
            meta={"iri": f"{base_url}/claims/{claim.claim_id}"},
        )

    @router.get("/claims/{claim_id}/versions", response_model=ListEnvelope[EntityVersion])
    def list_claim_versions(
        claim_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        claim = api_from_request(request, api).get_claim(claim_id)
        ensure_project_read(request, claim.project_id)
        versions = api_from_request(request, api).list_entity_versions(
            entity_type=EntityType.CLAIM,
            entity_id=claim_id,
        )
        items, total = paginate(versions, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get(
        "/claims/{claim_id}/versions/diff",
        response_model=Envelope[EntityVersionDiff],
    )
    def diff_claim_versions(
        claim_id: UUID,
        request: Request,
        from_version: int,
        to_version: int,
    ):
        claim = api_from_request(request, api).get_claim(claim_id)
        ensure_project_read(request, claim.project_id)
        diff = api_from_request(request, api).diff_entity_versions(
            entity_type=EntityType.CLAIM,
            entity_id=claim_id,
            from_version=from_version,
            to_version=to_version,
        )
        return Envelope(data=diff)

    @router.patch("/claims/{claim_id}", response_model=Envelope[Claim])
    def update_claim(claim_id: UUID, payload: ClaimUpdate, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_claim(claim_id)
        ensure_project_read(request, existing.project_id)
        claim = api_from_request(request, api).update_claim(
            claim_id,
            statement=payload.statement,
            confidence=payload.confidence,
            status=payload.status,
            terminal_reason=payload.terminal_reason,
            falsification_criteria=payload.falsification_criteria,
            verification_plan=payload.verification_plan,
            refuting_outcome=payload.refuting_outcome,
            supported_by_dataset_ids=payload.supported_by_dataset_ids,
            supported_by_analysis_ids=payload.supported_by_analysis_ids,
            answers_question_ids=payload.answers_question_ids,
            external_citations=payload.external_citations,
            actor=actor,
        )
        return Envelope(data=claim)

    @router.post(
        "/claims/{claim_id}/edges",
        response_model=Envelope[ClaimEdge],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_claim_edge(claim_id: UUID, payload: ClaimEdgeCreate, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_claim(claim_id)
        ensure_project_read(request, existing.project_id)
        edge = api_from_request(request, api).create_claim_edge(
            claim_id,
            target_claim_id=payload.target_claim_id,
            relation=payload.relation,
            actor=actor,
        )
        return Envelope(data=edge)

    @router.get("/claims/{claim_id}/edges", response_model=ListEnvelope[ClaimEdge])
    def list_claim_edges(
        claim_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        claim = api_from_request(request, api).get_claim(claim_id)
        ensure_project_read(request, claim.project_id)
        edges = api_from_request(request, api).list_claim_edges(claim_id=claim_id)
        items, total = paginate(edges, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.delete("/claims/{claim_id}", response_model=Envelope[Claim])
    def delete_claim(claim_id: UUID, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_claim(claim_id)
        ensure_project_read(request, existing.project_id)
        claim = api_from_request(request, api).delete_claim(claim_id, actor=actor)
        return Envelope(data=claim)

    return router
