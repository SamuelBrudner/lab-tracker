"""Session and acquisition output routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    AcquisitionOutput,
    Dataset,
    DatasetStatus,
    Session,
    SessionStatus,
    SessionType,
)
from lab_tracker.schemas import (
    AcquisitionOutputCreate,
    Envelope,
    ListEnvelope,
    SessionCreate,
    SessionDatasetPromotionRequest,
    SessionPromotionRequest,
    SessionUpdate,
)

from .shared import (
    actor_from_request,
    api_from_request,
    list_response,
    repository_from_request,
    session_default_status,
    validate_pagination,
)


def build_sessions_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/sessions",
        response_model=Envelope[Session],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_session(payload: SessionCreate, request: Request):
        actor = actor_from_request(request)
        session = api_from_request(request, api).create_session(
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
    def get_session_by_link_code(link_code: str, request: Request):
        session = api_from_request(request, api).get_session_by_link_code(link_code)
        return Envelope(data=session)

    @router.get("/sessions/{session_id}", response_model=Envelope[Session])
    def get_session(session_id: UUID, request: Request):
        session = api_from_request(request, api).get_session(session_id)
        return Envelope(data=session)

    @router.patch("/sessions/{session_id}", response_model=Envelope[Session])
    def update_session(session_id: UUID, payload: SessionUpdate, request: Request):
        actor = actor_from_request(request)
        session = api_from_request(request, api).update_session(
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
        output = api_from_request(request, api).register_acquisition_output(
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
        session = api_from_request(request, api).delete_session(session_id, actor=actor)
        return Envelope(data=session)

    @router.post("/sessions/{session_id}/promote", response_model=Envelope[Session])
    def promote_operational_session(
        session_id: UUID,
        payload: SessionPromotionRequest,
        request: Request,
    ):
        actor = actor_from_request(request)
        session = api_from_request(request, api).promote_operational_session(
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
        dataset = api_from_request(request, api).promote_operational_session_to_dataset(
            session_id,
            primary_question_id=payload.primary_question_id,
            secondary_question_ids=payload.secondary_question_ids,
            status=payload.status or DatasetStatus.COMMITTED,
            commit_manifest=payload.commit_manifest,
            actor=actor,
        )
        return Envelope(data=dataset)

    return router
