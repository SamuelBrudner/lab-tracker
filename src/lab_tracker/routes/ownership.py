"""Ownership reassignment routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import OwnershipReassignment
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    OwnershipReassignmentCreate,
)

from .shared import actor_from_request, api_from_request, list_response, validate_pagination


def build_ownership_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/ownership-reassignments",
        response_model=Envelope[OwnershipReassignment],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_ownership_reassignment(
        payload: OwnershipReassignmentCreate,
        request: Request,
    ):
        actor = actor_from_request(request)
        reassignment = api_from_request(request, api).reassign_ownership(
            from_user_id=payload.from_user_id,
            to_user_id=payload.to_user_id,
            reason=payload.reason,
            actor=actor,
        )
        return Envelope(data=reassignment)

    @router.get(
        "/ownership-reassignments",
        response_model=ListEnvelope[OwnershipReassignment],
    )
    def list_ownership_reassignments(
        request: Request,
        from_user_id: UUID | None = None,
        to_user_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        actor = actor_from_request(request)
        reassignments, total = api_from_request(request, api).list_ownership_reassignments(
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            limit=limit,
            offset=offset,
            actor=actor,
        )
        return list_response(reassignments, limit=limit, offset=offset, total=total)

    @router.get(
        "/ownership-reassignments/{reassignment_id:uuid}",
        response_model=Envelope[OwnershipReassignment],
    )
    def get_ownership_reassignment(reassignment_id: UUID, request: Request):
        actor = actor_from_request(request)
        reassignment = api_from_request(request, api).get_ownership_reassignment(
            reassignment_id,
            actor=actor,
        )
        return Envelope(data=reassignment)

    return router
