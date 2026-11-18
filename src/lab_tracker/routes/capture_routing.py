"""The unassigned capture queue and explicit review operator settings."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.capture_routing import (
    AssignmentScope,
    ReviewOperatorAssignment,
    ReviewOperatorChoice,
    UnassignedMachineCapture,
)
from lab_tracker.schemas import Envelope

from .shared import actor_from_request, api_from_request


class ReviewOperatorRead(BaseModel):
    assignment: ReviewOperatorAssignment | None = None


def build_capture_routing_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/projects/{project_id:uuid}/unassigned-machine-captures",
        response_model=Envelope[list[UnassignedMachineCapture]],
    )
    def unassigned(project_id: UUID, request: Request) -> Envelope[list[UnassignedMachineCapture]]:
        return Envelope(
            data=api_from_request(request, api).capture_routing.unassigned(
                project_id, actor=actor_from_request(request)
            )
        )

    @router.get(
        "/review-operator/{scope}/{work_id:uuid}", response_model=Envelope[ReviewOperatorRead]
    )
    def get_operator(
        scope: AssignmentScope, work_id: UUID, request: Request
    ) -> Envelope[ReviewOperatorRead]:
        assignment = api_from_request(request, api).capture_routing.get(
            scope, work_id, actor=actor_from_request(request)
        )
        return Envelope(data=ReviewOperatorRead(assignment=assignment))

    @router.put(
        "/review-operator/{scope}/{work_id:uuid}", response_model=Envelope[ReviewOperatorAssignment]
    )
    def assign_operator(
        scope: AssignmentScope, work_id: UUID, payload: ReviewOperatorChoice, request: Request
    ) -> Envelope[ReviewOperatorAssignment]:
        return Envelope(
            data=api_from_request(request, api).capture_routing.assign(
                scope, work_id, payload.user_id, actor=actor_from_request(request)
            )
        )

    return router
