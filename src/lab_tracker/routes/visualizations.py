"""Visualization routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.errors import AuthError
from lab_tracker.models import Visualization
from lab_tracker.schemas import Envelope, ListEnvelope, VisualizationCreate, VisualizationUpdate

from .shared import (
    actor_from_request,
    api_from_request,
    ensure_project_read,
    list_response,
    paginate,
    repository_from_request,
    validate_pagination,
)


def build_visualizations_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/visualizations",
        response_model=Envelope[Visualization],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_visualization(payload: VisualizationCreate, request: Request):
        actor = actor_from_request(request)
        visualization = api_from_request(request, api).create_visualization(
            analysis_id=payload.analysis_id,
            viz_type=payload.viz_type,
            file_path=payload.file_path,
            caption=payload.caption,
            related_claim_ids=payload.related_claim_ids,
            actor=actor,
        )
        return Envelope(data=visualization)

    @router.get("/visualizations", response_model=ListEnvelope[Visualization])
    def list_visualizations(
        request: Request,
        project_id: UUID | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
        visualizations, _ = repository_from_request(request).query_visualizations(
            project_id=project_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            limit=None,
            offset=0,
        )
        visible = _filter_visualizations_for_access(request, visualizations)
        items, total = paginate(visible, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def get_visualization(viz_id: UUID, request: Request):
        visualization = api_from_request(request, api).get_visualization(viz_id)
        analysis = api_from_request(request, api).get_analysis(visualization.analysis_id)
        ensure_project_read(request, analysis.project_id)
        return Envelope(data=visualization)

    @router.patch("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def update_visualization(viz_id: UUID, payload: VisualizationUpdate, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_visualization(viz_id)
        analysis = api_from_request(request, api).get_analysis(existing.analysis_id)
        ensure_project_read(request, analysis.project_id)
        visualization = api_from_request(request, api).update_visualization(
            viz_id,
            viz_type=payload.viz_type,
            file_path=payload.file_path,
            caption=payload.caption,
            related_claim_ids=payload.related_claim_ids,
            actor=actor,
        )
        return Envelope(data=visualization)

    @router.delete("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def delete_visualization(viz_id: UUID, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_visualization(viz_id)
        analysis = api_from_request(request, api).get_analysis(existing.analysis_id)
        ensure_project_read(request, analysis.project_id)
        visualization = api_from_request(request, api).delete_visualization(viz_id, actor=actor)
        return Envelope(data=visualization)

    return router


def _filter_visualizations_for_access(
    request: Request,
    visualizations: list[Visualization],
) -> list[Visualization]:
    request_api = api_from_request(request)
    visible: list[Visualization] = []
    for visualization in visualizations:
        analysis = request_api.get_analysis(visualization.analysis_id)
        try:
            ensure_project_read(request, analysis.project_id)
        except AuthError:
            continue
        visible.append(visualization)
    return visible
