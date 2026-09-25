"""Claim routes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    ClaimEdge,
    ClaimStatus,
    EntityType,
    EntityVersion,
    EntityVersionDiff,
    UsageEventResourceType,
)
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import (
    ClaimCreate,
    ClaimEdgeCreate,
    ClaimRead,
    ClaimUpdate,
    Envelope,
    ListEnvelope,
)

from .provenance import claim_provenance_payload, jsonld_response
from .shared import (
    CreatedByFilter,
    actor_from_request,
    api_from_request,
    created_by_filter_value,
    ensure_project_read,
    handlers_from_request,
    list_response,
    paginate,
    provenance_base_url,
    record_usage_view,
    validate_pagination,
    wants_jsonld,
)


def build_claims_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/claims",
        response_model=Envelope[ClaimRead],
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
        return Envelope(data=_interpreted(request, api, claim))

    @router.get("/claims", response_model=ListEnvelope[ClaimRead])
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
        page = handlers_from_request(request).catalogs.list_claims(
            actor=actor_from_request(request),
            project_id=project_id,
            status=status.value if status is not None else None,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            created_by=created_by_filter_value(created_by),
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

    @router.get("/claims/{claim_id}", response_model=Envelope[ClaimRead])
    def get_claim(claim_id: UUID, request: Request):
        claim = api_from_request(request, api).get_claim_for_read(
            claim_id,
            actor=actor_from_request(request),
        )
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
            data=_interpreted(request, api, claim),
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
        api_from_request(request, api).get_claim_for_read(
            claim_id,
            actor=actor_from_request(request),
        )
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
        api_from_request(request, api).get_claim_for_read(
            claim_id,
            actor=actor_from_request(request),
        )
        diff = api_from_request(request, api).diff_entity_versions(
            entity_type=EntityType.CLAIM,
            entity_id=claim_id,
            from_version=from_version,
            to_version=to_version,
        )
        return Envelope(data=diff)

    @router.patch("/claims/{claim_id}", response_model=Envelope[ClaimRead])
    def update_claim(claim_id: UUID, payload: ClaimUpdate, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_claim(claim_id)
        ensure_project_read(request, existing.project_id)
        claim = api_from_request(request, api).update_claim(
            claim_id,
            actor=actor,
            **provided_fields(payload),
        )
        return Envelope(data=_interpreted(request, api, claim))

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
        api_from_request(request, api).get_claim_for_read(
            claim_id,
            actor=actor_from_request(request),
        )
        edges = api_from_request(request, api).list_claim_edges(claim_id=claim_id)
        items, total = paginate(edges, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.delete(
        "/claims/{claim_id}/edges/{edge_id}",
        response_model=Envelope[ClaimEdge],
    )
    def delete_claim_edge(claim_id: UUID, edge_id: UUID, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_claim(claim_id)
        ensure_project_read(request, existing.project_id)
        edge = api_from_request(request, api).delete_claim_edge(claim_id, edge_id, actor=actor)
        return Envelope(data=edge)

    @router.delete("/claims/{claim_id}", response_model=Envelope[ClaimRead])
    def delete_claim(claim_id: UUID, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_claim(claim_id)
        ensure_project_read(request, existing.project_id)
        claim = api_from_request(request, api).delete_claim(claim_id, actor=actor)
        return Envelope(data=_interpreted(request, api, claim))

    return router


def _interpreted(request: Request, api: LabTrackerAPI, claim) -> ClaimRead:
    """One claim with its derived effective-status fields attached."""

    [interpreted] = api_from_request(request, api).interpret_claims([claim])
    return interpreted
