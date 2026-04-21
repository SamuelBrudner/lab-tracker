"""Session, analysis, claim, and visualization routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimStatus,
    Dataset,
    DatasetStatus,
    Session,
    SessionStatus,
    SessionType,
    Visualization,
)
from lab_tracker.schemas import (
    AcquisitionOutputCreate,
    AnalysisCommitRequest,
    AnalysisCommitResult,
    AnalysisCreate,
    AnalysisUpdate,
    ClaimCreate,
    ClaimUpdate,
    Envelope,
    ListEnvelope,
    SessionCreate,
    SessionDatasetPromotionRequest,
    SessionPromotionRequest,
    SessionUpdate,
    VisualizationCreate,
    VisualizationUpdate,
)

from .shared import (
    actor_from_request,
    analysis_default_status,
    list_response,
    repository_from_request,
    session_default_status,
    validate_pagination,
)


def build_sessions_analysis_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/sessions",
        response_model=Envelope[Session],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_session(payload: SessionCreate, request: Request):
        actor = actor_from_request(request)
        session = api.create_session(
            project_id=payload.project_id,
            session_type=payload.session_type,
            primary_question_id=payload.primary_question_id,
            status=payload.status or session_default_status(),
            actor=actor,
        )
        return Envelope(data=session)

    @router.get("/sessions", response_model=ListEnvelope[Session])
    def list_sessions(
        request: Request,
        project_id: UUID | None = None,
        status: SessionStatus | None = None,
        session_type: SessionType | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        sessions, total = repository_from_request(request).query_sessions(
            project_id=project_id,
            status=status.value if status is not None else None,
            session_type=session_type.value if session_type is not None else None,
            limit=limit,
            offset=offset,
        )
        return list_response(sessions, limit=limit, offset=offset, total=total)

    @router.get("/sessions/by-link/{link_code}", response_model=Envelope[Session])
    def get_session_by_link_code(link_code: str):
        session = api.get_session_by_link_code(link_code)
        return Envelope(data=session)

    @router.get("/sessions/{session_id}", response_model=Envelope[Session])
    def get_session(session_id: UUID):
        session = api.get_session(session_id)
        return Envelope(data=session)

    @router.patch("/sessions/{session_id}", response_model=Envelope[Session])
    def update_session(session_id: UUID, payload: SessionUpdate, request: Request):
        actor = actor_from_request(request)
        session = api.update_session(
            session_id,
            status=payload.status,
            ended_at=payload.ended_at,
            actor=actor,
        )
        return Envelope(data=session)

    @router.get("/sessions/{session_id}/outputs", response_model=ListEnvelope[AcquisitionOutput])
    def list_session_outputs(
        request: Request,
        session_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        outputs, total = repository_from_request(request).query_acquisition_outputs(
            session_id=session_id,
            limit=limit,
            offset=offset,
        )
        return list_response(outputs, limit=limit, offset=offset, total=total)

    @router.post(
        "/sessions/{session_id}/outputs",
        response_model=Envelope[AcquisitionOutput],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_session_output(
        session_id: UUID,
        payload: AcquisitionOutputCreate,
        request: Request,
    ):
        actor = actor_from_request(request)
        output = api.register_acquisition_output(
            session_id,
            file_path=payload.file_path,
            checksum=payload.checksum,
            size_bytes=payload.size_bytes,
            actor=actor,
        )
        return Envelope(data=output)

    @router.delete("/sessions/{session_id}", response_model=Envelope[Session])
    def delete_session(session_id: UUID, request: Request):
        actor = actor_from_request(request)
        session = api.delete_session(session_id, actor=actor)
        return Envelope(data=session)

    @router.post("/sessions/{session_id}/promote", response_model=Envelope[Session])
    def promote_operational_session(
        session_id: UUID,
        payload: SessionPromotionRequest,
        request: Request,
    ):
        actor = actor_from_request(request)
        session = api.promote_operational_session(
            session_id,
            primary_question_id=payload.primary_question_id,
            actor=actor,
        )
        return Envelope(data=session)

    @router.post(
        "/sessions/{session_id}/promote-to-dataset",
        response_model=Envelope[Dataset],
        status_code=http_status.HTTP_201_CREATED,
    )
    def promote_operational_session_to_dataset(
        session_id: UUID,
        payload: SessionDatasetPromotionRequest,
        request: Request,
    ):
        actor = actor_from_request(request)
        dataset = api.promote_operational_session_to_dataset(
            session_id,
            primary_question_id=payload.primary_question_id,
            secondary_question_ids=payload.secondary_question_ids,
            status=payload.status or DatasetStatus.COMMITTED,
            commit_manifest=payload.commit_manifest,
            actor=actor,
        )
        return Envelope(data=dataset)

    @router.post(
        "/analyses",
        response_model=Envelope[Analysis],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_analysis(payload: AnalysisCreate, request: Request):
        actor = actor_from_request(request)
        analysis = api.create_analysis(
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
    def get_analysis(analysis_id: UUID):
        analysis = api.get_analysis(analysis_id)
        return Envelope(data=analysis)

    @router.patch("/analyses/{analysis_id}", response_model=Envelope[Analysis])
    def update_analysis(analysis_id: UUID, payload: AnalysisUpdate, request: Request):
        actor = actor_from_request(request)
        analysis = api.update_analysis(
            analysis_id,
            status=payload.status,
            environment_hash=payload.environment_hash,
            actor=actor,
        )
        return Envelope(data=analysis)

    @router.post("/analyses/{analysis_id}/commit", response_model=Envelope[AnalysisCommitResult])
    def commit_analysis(analysis_id: UUID, payload: AnalysisCommitRequest, request: Request):
        actor = actor_from_request(request)
        analysis, claims, visualizations = api.commit_analysis(
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
        analysis = api.delete_analysis(analysis_id, actor=actor)
        return Envelope(data=analysis)

    @router.post(
        "/claims",
        response_model=Envelope[Claim],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_claim(payload: ClaimCreate, request: Request):
        actor = actor_from_request(request)
        claim = api.create_claim(
            project_id=payload.project_id,
            statement=payload.statement,
            confidence=payload.confidence,
            status=payload.status or ClaimStatus.PROPOSED,
            supported_by_dataset_ids=payload.supported_by_dataset_ids,
            supported_by_analysis_ids=payload.supported_by_analysis_ids,
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
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        claims, total = repository_from_request(request).query_claims(
            project_id=project_id,
            status=status.value if status is not None else None,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            limit=limit,
            offset=offset,
        )
        return list_response(claims, limit=limit, offset=offset, total=total)

    @router.get("/claims/{claim_id}", response_model=Envelope[Claim])
    def get_claim(claim_id: UUID):
        claim = api.get_claim(claim_id)
        return Envelope(data=claim)

    @router.patch("/claims/{claim_id}", response_model=Envelope[Claim])
    def update_claim(claim_id: UUID, payload: ClaimUpdate, request: Request):
        actor = actor_from_request(request)
        claim = api.update_claim(
            claim_id,
            statement=payload.statement,
            confidence=payload.confidence,
            status=payload.status,
            supported_by_dataset_ids=payload.supported_by_dataset_ids,
            supported_by_analysis_ids=payload.supported_by_analysis_ids,
            actor=actor,
        )
        return Envelope(data=claim)

    @router.delete("/claims/{claim_id}", response_model=Envelope[Claim])
    def delete_claim(claim_id: UUID, request: Request):
        actor = actor_from_request(request)
        claim = api.delete_claim(claim_id, actor=actor)
        return Envelope(data=claim)

    @router.post(
        "/visualizations",
        response_model=Envelope[Visualization],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_visualization(payload: VisualizationCreate, request: Request):
        actor = actor_from_request(request)
        visualization = api.create_visualization(
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
        visualizations, total = repository_from_request(request).query_visualizations(
            project_id=project_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            limit=limit,
            offset=offset,
        )
        return list_response(visualizations, limit=limit, offset=offset, total=total)

    @router.get("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def get_visualization(viz_id: UUID):
        visualization = api.get_visualization(viz_id)
        return Envelope(data=visualization)

    @router.patch("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def update_visualization(viz_id: UUID, payload: VisualizationUpdate, request: Request):
        actor = actor_from_request(request)
        visualization = api.update_visualization(
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
        visualization = api.delete_visualization(viz_id, actor=actor)
        return Envelope(data=visualization)

    return router
