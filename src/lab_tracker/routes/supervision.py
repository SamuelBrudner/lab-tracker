"""Supervision edge routes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import SupervisionEdge
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    SupervisionEdgeCreate,
    SupervisionEdgeUpdate,
)

from .shared import actor_from_request, api_from_request, list_response, validate_pagination


def build_supervision_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/supervision-edges", response_model=ListEnvelope[SupervisionEdge])
    def list_supervision_edges(
        request: Request,
        supervisor_user_id: UUID | None = None,
        supervisee_user_id: UUID | None = None,
        active_only: bool = False,
        as_of: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        actor = actor_from_request(request)
        edges, total = api_from_request(request, api).list_supervision_edges(
            supervisor_user_id=supervisor_user_id,
            supervisee_user_id=supervisee_user_id,
            active_only=active_only,
            as_of=as_of,
            limit=limit,
            offset=offset,
            actor=actor,
        )
        return list_response(edges, limit=limit, offset=offset, total=total)

    @router.post(
        "/supervision-edges",
        response_model=Envelope[SupervisionEdge],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_supervision_edge(payload: SupervisionEdgeCreate, request: Request):
        actor = actor_from_request(request)
        edge = api_from_request(request, api).create_supervision_edge(
            supervisor_user_id=payload.supervisor_user_id,
            supervisee_user_id=payload.supervisee_user_id,
            started_at=payload.started_at,
            ended_at=payload.ended_at,
            actor=actor,
        )
        return Envelope(data=edge)

    @router.get(
        "/supervision-edges/{edge_id:uuid}",
        response_model=Envelope[SupervisionEdge],
    )
    def get_supervision_edge(edge_id: UUID, request: Request):
        actor = actor_from_request(request)
        edge = api_from_request(request, api).get_supervision_edge(edge_id, actor=actor)
        return Envelope(data=edge)

    @router.patch(
        "/supervision-edges/{edge_id:uuid}",
        response_model=Envelope[SupervisionEdge],
    )
    def update_supervision_edge(
        edge_id: UUID,
        payload: SupervisionEdgeUpdate,
        request: Request,
    ):
        actor = actor_from_request(request)
        updates = {
            field_name: getattr(payload, field_name)
            for field_name in payload.model_fields_set
        }
        edge = api_from_request(request, api).update_supervision_edge(
            edge_id,
            actor=actor,
            **updates,
        )
        return Envelope(data=edge)

    @router.delete(
        "/supervision-edges/{edge_id:uuid}",
        response_model=Envelope[SupervisionEdge],
    )
    def delete_supervision_edge(edge_id: UUID, request: Request):
        actor = actor_from_request(request)
        edge = api_from_request(request, api).delete_supervision_edge(edge_id, actor=actor)
        return Envelope(data=edge)

    return router
