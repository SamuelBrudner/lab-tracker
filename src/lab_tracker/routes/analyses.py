"""Analysis routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import Analysis, AnalysisStatus
from lab_tracker.schemas import (
    AnalysisCommitRequest,
    AnalysisCommitResult,
    AnalysisCreate,
    AnalysisUpdate,
    Envelope,
    ListEnvelope,
)

from .shared import (
    actor_from_request,
    analysis_default_status,
    api_from_request,
    list_response,
    repository_from_request,
    validate_pagination,
)


def build_analyses_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/analyses",
        response_model=Envelope[Analysis],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_analysis(payload: AnalysisCreate, request: Request):
        actor = actor_from_request(request)
        analysis = api_from_request(request, api).create_analysis(
            project_id=payload.project_id,
            dataset_ids=payload.dataset_ids,
            method_hash=payload.method_hash,
            code_version=payload.code_version,
            environment_hash=payload.environment_hash,
            status=payload.status or analysis_default_status(),
            actor=actor,
        )
        return Envelope(data=analysis)

    @router.get("/analyses", response_model=ListEnvelope[Analysis])
    def list_analyses(
        request: Request,
        project_id: UUID | None = None,
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
        status: AnalysisStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        analyses, total = repository_from_request(request).query_analyses(
            project_id=project_id,
            dataset_id=dataset_id,
            question_id=question_id,
            status=status.value if status is not None else None,
            limit=limit,
            offset=offset,
        )
        return list_response(analyses, limit=limit, offset=offset, total=total)

    @router.get("/analyses/{analysis_id}", response_model=Envelope[Analysis])
    def get_analysis(analysis_id: UUID, request: Request):
        analysis = api_from_request(request, api).get_analysis(analysis_id)
        return Envelope(data=analysis)

    @router.patch("/analyses/{analysis_id}", response_model=Envelope[Analysis])
    def update_analysis(analysis_id: UUID, payload: AnalysisUpdate, request: Request):
        actor = actor_from_request(request)
        analysis = api_from_request(request, api).update_analysis(
            analysis_id,
            status=payload.status,
            environment_hash=payload.environment_hash,
            actor=actor,
        )
        return Envelope(data=analysis)

    @router.post("/analyses/{analysis_id}/commit", response_model=Envelope[AnalysisCommitResult])
    def commit_analysis(analysis_id: UUID, payload: AnalysisCommitRequest, request: Request):
        actor = actor_from_request(request)
        analysis, claims, visualizations = api_from_request(request, api).commit_analysis(
            analysis_id,
            environment_hash=payload.environment_hash,
            claims=payload.claims,
            visualizations=payload.visualizations,
            actor=actor,
        )
        return Envelope(
            data=AnalysisCommitResult(
                analysis=analysis,
                claims=claims,
                visualizations=visualizations,
            )
        )

    @router.delete("/analyses/{analysis_id}", response_model=Envelope[Analysis])
    def delete_analysis(analysis_id: UUID, request: Request):
        actor = actor_from_request(request)
        analysis = api_from_request(request, api).delete_analysis(analysis_id, actor=actor)
        return Envelope(data=analysis)

    return router
