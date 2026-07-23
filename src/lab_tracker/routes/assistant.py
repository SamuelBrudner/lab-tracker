"""Assistant-facing read-only context routes."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.decision_context import JsonObject
from lab_tracker.schemas import AssistantDecisionContextRequest

from .shared import (
    actor_from_request,
    handlers_from_request,
)


def build_assistant_router() -> APIRouter:
    router = APIRouter()

    @router.post("/assistant/decision-context")
    def get_decision_context(
        payload: AssistantDecisionContextRequest,
        request: Request,
    ) -> JsonObject:
        """Build bounded graph context before research-facing assistant decisions."""
        return handlers_from_request(request).context.decision_context(
            payload,
            actor=actor_from_request(request),
        )

    return router
