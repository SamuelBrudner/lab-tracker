"""Daily graph review routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.config import get_settings
from lab_tracker.graph_drafting import OpenAIGraphDraftClient
from lab_tracker.models import DailyGraphReview, DailyGraphReviewStatus
from lab_tracker.schemas import (
    DailyGraphReviewCreate,
    DailyGraphReviewMarkReviewedRequest,
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


def build_daily_reviews_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/daily-graph-reviews",
        response_model=Envelope[DailyGraphReview],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_daily_graph_review(payload: DailyGraphReviewCreate, request: Request):
        actor = actor_from_request(request)
        draft_client = _draft_client_from_request(request)
        try:
            review = api_from_request(request, api).create_daily_graph_review(
                payload.project_id,
                window_start=payload.window_start,
                window_end=payload.window_end,
                draft_client=draft_client,
                actor=actor,
            )
        finally:
            close = getattr(draft_client, "close", None)
            if callable(close):
                close()
        return Envelope(data=review)

    @router.get("/daily-graph-reviews", response_model=ListEnvelope[DailyGraphReview])
    def list_daily_graph_reviews(
        request: Request,
        project_id: UUID | None = None,
        status: DailyGraphReviewStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        reviews = api_from_request(request, api).list_daily_graph_reviews(
            project_id=project_id,
            status=status,
        )
        items, total = paginate(reviews, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get(
        "/daily-graph-reviews/{review_id:uuid}",
        response_model=Envelope[DailyGraphReview],
    )
    def get_daily_graph_review(review_id: UUID, request: Request):
        review = api_from_request(request, api).get_daily_graph_review(review_id)
        return Envelope(data=review)

    @router.post(
        "/daily-graph-reviews/{review_id:uuid}/reviewed",
        response_model=Envelope[DailyGraphReview],
    )
    def mark_daily_graph_review_reviewed(
        review_id: UUID,
        request: Request,
        payload: DailyGraphReviewMarkReviewedRequest | None = None,
    ):
        _ = payload
        actor = actor_from_request(request)
        review = api_from_request(request, api).mark_daily_graph_review_reviewed(
            review_id,
            actor=actor,
        )
        return Envelope(data=review)

    return router


def _draft_client_from_request(request: Request):
    settings = getattr(request.app.state, "settings", None) or get_settings()
    factory = getattr(request.app.state, "graph_draft_client_factory", None)
    if callable(factory):
        return factory(settings)
    return OpenAIGraphDraftClient.from_settings(settings)
