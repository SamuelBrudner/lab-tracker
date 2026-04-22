"""Dataset review routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import require_role
from lab_tracker.errors import NotFoundError
from lab_tracker.models import DatasetReview, DatasetReviewStatus
from lab_tracker.schemas import (
    DatasetReviewRequest,
    DatasetReviewUpdate,
    Envelope,
    ListEnvelope,
)
from lab_tracker.services.shared import WRITE_ROLES

from .shared import (
    api_from_request,
    actor_from_request,
    list_response,
    repository_from_request,
    validate_pagination,
)


def build_dataset_reviews_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.post(
        "/datasets/{dataset_id}/review",
        response_model=Envelope[DatasetReview],
        status_code=http_status.HTTP_201_CREATED,
    )
    def request_dataset_review(
        dataset_id: UUID,
        request: Request,
        payload: DatasetReviewRequest | None = None,
    ):
        actor = actor_from_request(request)
        review = api_from_request(request, api).request_dataset_review(
            dataset_id,
            comments=payload.comments if payload else None,
            actor=actor,
        )
        return Envelope(data=review)

    @router.get("/datasets/{dataset_id}/review", response_model=Envelope[DatasetReview])
    def get_dataset_review(dataset_id: UUID, request: Request):
        request_api = api_from_request(request, api)
        request_api.get_dataset(dataset_id)
        reviews = request_api.list_dataset_reviews(dataset_id=dataset_id)
        if not reviews:
            raise NotFoundError("Dataset review does not exist.")
        pending = [review for review in reviews if review.status == DatasetReviewStatus.PENDING]
        review = pending[0] if pending else reviews[-1]
        return Envelope(data=review)

    @router.patch("/datasets/{dataset_id}/review", response_model=Envelope[DatasetReview])
    def resolve_dataset_review(
        dataset_id: UUID,
        payload: DatasetReviewUpdate,
        request: Request,
    ):
        request_api = api_from_request(request, api)
        actor = actor_from_request(request)
        request_api.get_dataset(dataset_id)
        pending = request_api.list_dataset_reviews(
            dataset_id=dataset_id,
            status=DatasetReviewStatus.PENDING,
        )
        if not pending:
            raise NotFoundError("Pending dataset review does not exist.")
        status_by_action = {
            "approve": DatasetReviewStatus.APPROVED,
            "request_changes": DatasetReviewStatus.CHANGES_REQUESTED,
            "reject": DatasetReviewStatus.REJECTED,
        }
        review = request_api.resolve_dataset_review(
            pending[0].review_id,
            status=status_by_action[payload.action.value],
            comments=payload.comments,
            actor=actor,
        )
        return Envelope(data=review)

    @router.get("/reviews/pending", response_model=ListEnvelope[DatasetReview])
    def list_pending_reviews(request: Request, limit: int = 50, offset: int = 0):
        actor = actor_from_request(request)
        require_role(actor, WRITE_ROLES)
        validate_pagination(limit, offset)
        reviews, total = repository_from_request(request).query_dataset_reviews(
            status=DatasetReviewStatus.PENDING.value,
            limit=limit,
            offset=offset,
        )
        return list_response(reviews, limit=limit, offset=offset, total=total)

    return router
