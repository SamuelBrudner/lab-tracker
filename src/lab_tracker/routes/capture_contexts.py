"""Capture context preset routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import CaptureContext
from lab_tracker.qr import build_qr_svg
from lab_tracker.schemas import (
    CaptureContextCreate,
    CaptureContextRead,
    CaptureContextUpdate,
    Envelope,
    ListEnvelope,
)

from .shared import (
    actor_from_request,
    api_from_request,
    list_response,
    paginate,
    validate_pagination,
)
from .url_helpers import resolve_public_base_url


def build_capture_contexts_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/capture-contexts", response_model=ListEnvelope[CaptureContextRead])
    def list_capture_contexts(
        request: Request,
        project_id: UUID | None = None,
        include_revoked: bool = False,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        contexts = api_from_request(request, api).list_capture_contexts(
            project_id=project_id,
            include_revoked=include_revoked,
            actor=actor_from_request(request),
        )
        page, total = paginate(contexts, limit, offset)
        return list_response(
            [_capture_context_read(request, context) for context in page],
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.get(
        "/capture-contexts/{capture_context_id}",
        response_model=Envelope[CaptureContextRead],
    )
    def get_capture_context(capture_context_id: UUID, request: Request):
        context = api_from_request(request, api).get_capture_context(
            capture_context_id,
            actor=actor_from_request(request),
        )
        return Envelope(data=_capture_context_read(request, context))

    @router.post(
        "/capture-contexts",
        response_model=Envelope[CaptureContextRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_capture_context(payload: CaptureContextCreate, request: Request):
        context = api_from_request(request, api).create_capture_context(
            project_id=payload.project_id,
            label=payload.label,
            site_label=payload.site_label,
            place_label=payload.place_label,
            default_hint=payload.default_hint,
            default_targets=payload.default_targets,
            actor=actor_from_request(request),
        )
        return Envelope(data=_capture_context_read(request, context))

    @router.patch(
        "/capture-contexts/{capture_context_id}",
        response_model=Envelope[CaptureContextRead],
    )
    def update_capture_context(
        capture_context_id: UUID,
        payload: CaptureContextUpdate,
        request: Request,
    ):
        context = api_from_request(request, api).update_capture_context(
            capture_context_id,
            updates=payload.model_dump(exclude_unset=True),
            actor=actor_from_request(request),
        )
        return Envelope(data=_capture_context_read(request, context))

    @router.delete(
        "/capture-contexts/{capture_context_id}",
        response_model=Envelope[CaptureContextRead],
    )
    def revoke_capture_context(capture_context_id: UUID, request: Request):
        context = api_from_request(request, api).revoke_capture_context(
            capture_context_id,
            actor=actor_from_request(request),
        )
        return Envelope(data=_capture_context_read(request, context))

    return router


def _capture_context_read(request: Request, context: CaptureContext) -> CaptureContextRead:
    capture_url = (
        f"{resolve_public_base_url(request)}/app/capture?context={context.capture_context_id}"
    )
    return CaptureContextRead(
        **context.model_dump(),
        capture_url=capture_url,
        capture_qr_svg=build_qr_svg(capture_url),
    )
