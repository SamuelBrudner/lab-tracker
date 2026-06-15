"""Record export routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import RecordExport
from lab_tracker.schemas import Envelope

from .shared import actor_from_request, api_from_request


def _request_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def build_record_exports_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/record-exports/users/{user_id:uuid}",
        response_model=Envelope[RecordExport],
    )
    def export_user_records(user_id: UUID, request: Request):
        actor = actor_from_request(request)
        export = api_from_request(request, api).export_user_records(
            user_id=user_id,
            base_url=_request_base_url(request),
            actor=actor,
        )
        return Envelope(data=export)

    @router.get(
        "/groups/{group_id:uuid}/record-exports/users/{user_id:uuid}",
        response_model=Envelope[RecordExport],
    )
    def export_group_user_records(group_id: UUID, user_id: UUID, request: Request):
        actor = actor_from_request(request)
        export = api_from_request(request, api).export_user_records(
            user_id=user_id,
            group_id=group_id,
            base_url=_request_base_url(request),
            actor=actor,
        )
        return Envelope(data=export)

    return router
