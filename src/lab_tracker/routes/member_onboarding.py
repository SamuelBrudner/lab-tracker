"""Ongoing-project member-onboarding routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    MemberOnboardingAiAlignmentRequest,
    MemberOnboardingCheckpointRequest,
    MemberOnboardingManualAlignmentRequest,
    MemberOnboardingOwnerQueueItem,
    MemberOnboardingRead,
)

from .graph_draft_clients import draft_client_from_request
from .shared import (
    actor_from_request,
    api_from_request,
    list_response,
    paginate,
    validate_pagination,
)


def build_member_onboarding_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/projects/{project_id:uuid}/member-onboarding",
        response_model=Envelope[MemberOnboardingRead],
    )
    def get_member_onboarding(project_id: UUID, request: Request):
        data = api_from_request(request, api).get_member_onboarding(
            project_id,
            actor=actor_from_request(request),
        )
        return Envelope(data=data)

    @router.put(
        "/projects/{project_id:uuid}/member-onboarding/checkpoint",
        response_model=Envelope[MemberOnboardingRead],
        responses={
            http_status.HTTP_201_CREATED: {
                "model": Envelope[MemberOnboardingRead],
                "description": "A new immutable member checkpoint was created.",
            }
        },
    )
    def put_checkpoint(
        project_id: UUID,
        payload: MemberOnboardingCheckpointRequest,
        request: Request,
        response: Response,
    ):
        result = api_from_request(request, api).put_member_onboarding_checkpoint(
            project_id,
            payload,
            actor=actor_from_request(request),
        )
        response.status_code = (
            http_status.HTTP_201_CREATED if result.created else http_status.HTTP_200_OK
        )
        return Envelope(data=result.onboarding)

    @router.put(
        "/projects/{project_id:uuid}/member-onboarding/manual-alignment",
        response_model=Envelope[MemberOnboardingRead],
    )
    def put_manual_alignment(
        project_id: UUID,
        payload: MemberOnboardingManualAlignmentRequest,
        request: Request,
    ):
        data = api_from_request(request, api).put_member_onboarding_manual_alignment(
            project_id,
            payload,
            actor=actor_from_request(request),
        )
        return Envelope(data=data)

    @router.post(
        "/projects/{project_id:uuid}/member-onboarding/ai-alignment",
        response_model=Envelope[MemberOnboardingRead],
        responses={
            http_status.HTTP_202_ACCEPTED: {
                "model": Envelope[MemberOnboardingRead],
                "description": "Another fenced generation attempt is still active.",
                "headers": {
                    "Retry-After": {
                        "description": "Seconds to wait before polling onboarding state.",
                        "schema": {"type": "integer", "minimum": 1},
                    }
                },
            }
        },
    )
    def start_ai_alignment(
        project_id: UUID,
        payload: MemberOnboardingAiAlignmentRequest,
        request: Request,
        response: Response,
    ):
        draft_client = draft_client_from_request(request)
        try:
            result = api_from_request(request, api).start_member_onboarding_ai_alignment(
                project_id,
                external_provider_acknowledged=payload.external_provider_acknowledged,
                draft_client=draft_client,
                actor=actor_from_request(request),
            )
        finally:
            close = getattr(draft_client, "close", None)
            if callable(close):
                close()
        if result.in_progress:
            response.status_code = http_status.HTTP_202_ACCEPTED
            response.headers["Retry-After"] = str(result.retry_after_seconds or 1)
        return Envelope(data=result.onboarding)

    @router.get(
        "/projects/{project_id:uuid}/member-onboarding/owner-queue",
        response_model=ListEnvelope[MemberOnboardingOwnerQueueItem],
    )
    def owner_queue(
        project_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        items = api_from_request(request, api).list_member_onboarding_owner_queue(
            project_id,
            actor=actor_from_request(request),
        )
        page, total = paginate(items, limit, offset)
        return list_response(page, limit=limit, offset=offset, total=total)

    return router
