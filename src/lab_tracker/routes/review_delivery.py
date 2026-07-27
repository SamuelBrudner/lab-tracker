"""Signed review links and administrative review-email diagnostics."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import RedirectResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import Role
from lab_tracker.errors import AuthError
from lab_tracker.models import ReviewEmailDelivery
from lab_tracker.review_links import InvalidReviewLinkToken, verify_review_link
from lab_tracker.schemas import Envelope, ListEnvelope, ReviewEmailTestRequest

from .shared import (
    actor_from_request,
    api_from_request,
    list_response,
    paginate,
    validate_pagination,
)


def build_review_delivery_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/r/{token}", include_in_schema=False)
    def open_review_link(token: str, request: Request):
        try:
            claims = verify_review_link(
                request.app.state.settings.auth_secret_key,
                token,
            )
            delivery = api_from_request(request, api).review_emails.get(
                claims.delivery_id
            )
            verify_review_link(
                request.app.state.settings.auth_secret_key,
                token,
                expected_recipient_user_id=delivery.recipient_user_id,
                expected_delivery_id=delivery.delivery_id,
            )
            if delivery.change_set_id != claims.change_set_id:
                raise InvalidReviewLinkToken("Review link token is invalid or expired.")
        except Exception:
            # Do not leak whether a delivery or review exists.
            return RedirectResponse(url="/app/", status_code=302)
        return RedirectResponse(
            url=f"/app/batches/{claims.change_set_id}",
            status_code=302,
        )

    @router.post(
        "/review-email/test",
        response_model=Envelope[ReviewEmailDelivery],
        status_code=201,
    )
    def enqueue_test_email(payload: ReviewEmailTestRequest, request: Request):
        _require_admin(request)
        delivery = api_from_request(request, api).review_emails.enqueue_test(
            payload.destination_email,
            recipient_user_id=payload.recipient_user_id,
        )
        return Envelope(data=delivery)

    @router.get(
        "/review-email/deliveries",
        response_model=ListEnvelope[ReviewEmailDelivery],
    )
    def list_email_deliveries(
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        _require_admin(request)
        deliveries = api_from_request(request, api).review_emails.list()
        items, total = paginate(deliveries, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    return router


def _require_admin(request: Request) -> None:
    actor = actor_from_request(request)
    if actor.role != Role.ADMIN:
        raise AuthError("Only admins can manage review email delivery.")
