"""Provenance link routes (human-gated content-hash lineage proposals)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import ProvenanceLink, ProvenanceLinkStatus, UsageEventResourceType
from lab_tracker.schemas import Envelope, ListEnvelope, ProvenanceLinkStatusUpdate

from .shared import (
    accessible_project_ids_from_request,
    actor_from_request,
    api_from_request,
    ensure_project_contributor,
    ensure_project_read,
    list_response,
    record_usage_view,
    repository_from_request,
    validate_pagination,
)


def build_provenance_links_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/provenance-links", response_model=ListEnvelope[ProvenanceLink])
    def list_provenance_links(
        request: Request,
        project_id: UUID | None = None,
        status: ProvenanceLinkStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
            project_ids = None
        else:
            project_ids = accessible_project_ids_from_request(request)
        links, total = repository_from_request(request).query_provenance_links(
            project_id=project_id,
            project_ids=project_ids,
            status=status.value if status is not None else None,
            limit=limit,
            offset=offset,
        )
        return list_response(links, limit=limit, offset=offset, total=total)

    @router.get("/provenance-links/{link_id}", response_model=Envelope[ProvenanceLink])
    def get_provenance_link(link_id: UUID, request: Request):
        link = api_from_request(request, api).get_provenance_link(link_id)
        ensure_project_read(request, link.project_id)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.PROVENANCE_LINK,
            resource_id=link.link_id,
            project_id=link.project_id,
        )
        return Envelope(data=link)

    @router.patch("/provenance-links/{link_id}", response_model=Envelope[ProvenanceLink])
    def update_provenance_link(
        link_id: UUID,
        payload: ProvenanceLinkStatusUpdate,
        request: Request,
    ):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_provenance_link(link_id)
        ensure_project_contributor(request, existing.project_id)
        link = api_from_request(request, api).update_provenance_link_status(
            link_id,
            payload.status,
            actor=actor,
        )
        return Envelope(data=link)

    return router
