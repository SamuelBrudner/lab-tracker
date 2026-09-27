"""Assistant-facing read-only context routes."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.decision_context import JsonObject
from lab_tracker.models import UsageEventResourceType, UsageEventVerb
from lab_tracker.schemas import AssistantDecisionContextRequest

from .shared import (
    actor_from_request,
    api_from_request,
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
        actor = actor_from_request(request)
        context = handlers_from_request(request).context.decision_context(
            payload,
            actor=actor,
        )
        # Content-free by construction: only the project, the actor and the
        # surface are recorded — never the query, task kind, or anchor ids.
        api_from_request(request).record_usage_event(
            verb=UsageEventVerb.VIEW,
            resource_type=UsageEventResourceType.DECISION_CONTEXT,
            project_id=payload.project_id,
            actor=actor,
        )
        return context

    return router
