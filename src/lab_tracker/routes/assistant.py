"""Assistant-facing read-only context routes."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request

from lab_tracker.decision_context import (
    JsonObject,
    RepositoryDecisionContextReader,
    build_decision_context,
)
from lab_tracker.schemas import AssistantDecisionContextRequest

from .shared import repository_from_request


def build_assistant_router() -> APIRouter:
    router = APIRouter()

    @router.post("/assistant/decision-context")
    def get_decision_context(
        payload: AssistantDecisionContextRequest,
        request: Request,
    ) -> JsonObject:
        """Build bounded graph context before research-facing assistant decisions."""
        reader = RepositoryDecisionContextReader(repository_from_request(request))
        return build_decision_context(
            reader,
            task_kind=payload.task_kind,
            query=payload.query,
            project_id=str(payload.project_id) if payload.project_id else None,
            question_id=str(payload.question_id) if payload.question_id else None,
            dataset_id=str(payload.dataset_id) if payload.dataset_id else None,
            analysis_id=str(payload.analysis_id) if payload.analysis_id else None,
            claim_id=str(payload.claim_id) if payload.claim_id else None,
            visualization_id=(
                str(payload.visualization_id) if payload.visualization_id else None
            ),
            limit=payload.limit,
        )

    return router
