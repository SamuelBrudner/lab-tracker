"""Read and declare the purpose of a session or capture."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.schemas import Envelope
from lab_tracker.work_intent import WorkIntent, WorkIntentChoice, WorkScope

from .shared import actor_from_request, api_from_request


class WorkIntentRead(BaseModel):
    intent: WorkIntent | None = None


def build_work_intent_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/work-intent/{scope}/{work_id:uuid}", response_model=Envelope[WorkIntentRead])
    def get_work_intent(
        scope: WorkScope, work_id: UUID, request: Request
    ) -> Envelope[WorkIntentRead]:
        intent = api_from_request(request, api).work_intents.get(
            scope, work_id, actor=actor_from_request(request)
        )
        return Envelope(data=WorkIntentRead(intent=intent))

    @router.put("/work-intent/{scope}/{work_id:uuid}", response_model=Envelope[WorkIntent])
    def record_work_intent(
        scope: WorkScope, work_id: UUID, payload: WorkIntentChoice, request: Request
    ) -> Envelope[WorkIntent]:
        return Envelope(
            data=api_from_request(request, api).work_intents.record(
                scope, work_id, payload, actor=actor_from_request(request)
            )
        )

    return router
