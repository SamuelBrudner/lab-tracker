"""Record export routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import RecordExport
from lab_tracker.schemas import Envelope

from .shared import actor_from_request, api_from_request, provenance_base_url


def build_record_exports_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/record-exports/users/{user_id:uuid}",
        response_model=Envelope[RecordExport],
    )
    def export_user_records(user_id: UUID, request: Request):
        actor = actor_from_request(request)
        export = api_from_request(request, api).export_user_records(
            user_id=user_id,
            base_url=provenance_base_url(request),
            actor=actor,
        )
        return Envelope(data=export)

    @router.post(
        "/groups/{group_id:uuid}/record-exports/users/{user_id:uuid}",
        response_model=Envelope[RecordExport],
    )
    def export_group_user_records(group_id: UUID, user_id: UUID, request: Request):
        actor = actor_from_request(request)
        export = api_from_request(request, api).export_user_records(
            user_id=user_id,
            group_id=group_id,
            base_url=provenance_base_url(request),
            actor=actor,
        )
        return Envelope(data=export)

    @router.get("/goals/{goal_id:uuid}/ara-artifact")
    def export_goal_artifact(goal_id: UUID, request: Request):
        actor = actor_from_request(request)
        payload = api_from_request(request, api).export_goal_artifact(
            goal_id=goal_id,
            base_url=provenance_base_url(request),
            actor=actor,
        )
        return JSONResponse(
            content=jsonable_encoder(payload),
            media_type="application/ld+json",
        )

    @router.get("/goals/{goal_id:uuid}/ara-artifact/{layer_name}")
    def export_goal_artifact_layer(goal_id: UUID, layer_name: str, request: Request):
        actor = actor_from_request(request)
        payload = api_from_request(request, api).export_goal_artifact(
            goal_id=goal_id,
            base_url=provenance_base_url(request),
            layer_name=layer_name,
            actor=actor,
        )
        return JSONResponse(
            content=jsonable_encoder(payload),
            media_type="application/ld+json",
        )

    @router.get("/questions/{question_id:uuid}/ara-artifact")
    def export_question_subtree(question_id: UUID, request: Request):
        actor = actor_from_request(request)
        payload = api_from_request(request, api).export_question_subtree(
            root_id=question_id,
            base_url=provenance_base_url(request),
            actor=actor,
        )
        return JSONResponse(
            content=jsonable_encoder(payload),
            media_type="application/ld+json",
        )

    @router.get("/questions/{question_id:uuid}/ara-artifact/{layer_name}")
    def export_question_subtree_layer(question_id: UUID, layer_name: str, request: Request):
        actor = actor_from_request(request)
        payload = api_from_request(request, api).export_question_subtree(
            root_id=question_id,
            base_url=provenance_base_url(request),
            layer_name=layer_name,
            actor=actor,
        )
        return JSONResponse(
            content=jsonable_encoder(payload),
            media_type="application/ld+json",
        )

    return router
