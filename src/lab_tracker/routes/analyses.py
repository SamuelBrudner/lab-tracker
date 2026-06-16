"""Analysis routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.db_models import VisualizationModel
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
    CreatedByFilter,
    actor_from_request,
    analysis_default_status,
    api_from_request,
    db_session_from_request,
    ensure_project_read,
    file_storage_from_request,
    filter_project_scoped_items,
    list_response,
    paginate,
    repository_from_request,
    validate_pagination,
)
from .visualizations import _delete_stored_visualization_file


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
        created_by: CreatedByFilter = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
        analyses, _ = repository_from_request(request).query_analyses(
            project_id=project_id,
            dataset_id=dataset_id,
            question_id=question_id,
            status=status.value if status is not None else None,
            created_by=created_by,
            limit=None,
            offset=0,
        )
        visible = filter_project_scoped_items(request, analyses)
        items, total = paginate(visible, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get("/analyses/{analysis_id}", response_model=Envelope[Analysis])
    def get_analysis(analysis_id: UUID, request: Request):
        analysis = api_from_request(request, api).get_analysis(analysis_id)
        ensure_project_read(request, analysis.project_id)
        return Envelope(data=analysis)

    @router.patch("/analyses/{analysis_id}", response_model=Envelope[Analysis])
    def update_analysis(analysis_id: UUID, payload: AnalysisUpdate, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_analysis(analysis_id)
        ensure_project_read(request, existing.project_id)
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
        existing = api_from_request(request, api).get_analysis(analysis_id)
        ensure_project_read(request, existing.project_id)
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
        request_api = api_from_request(request, api)
        actor = actor_from_request(request)
        existing = request_api.get_analysis(analysis_id)
        ensure_project_read(request, existing.project_id)
        db_session = db_session_from_request(request)
        storage_backend = file_storage_from_request(request)
        storage_ids = [
            UUID(value)
            for value in db_session.scalars(
                select(VisualizationModel.asset_storage_id).where(
                    VisualizationModel.analysis_id == str(analysis_id),
                    VisualizationModel.asset_storage_id.is_not(None),
                )
            )
        ]
        analysis = request_api.delete_analysis(analysis_id, actor=actor)
        db_session.flush()
        for storage_id in storage_ids:
            request_api.run_after_commit(
                lambda storage_id=storage_id: _delete_stored_visualization_file(
                    storage_backend,
                    storage_id,
                )
            )
        return Envelope(data=analysis)

    return router
