"""Daily-review delivery routes: a contentless read model plus signed deep links.

These two endpoints are the product seam for delivering the daily review as a
*cue* (an email or push knock) rather than a digest:

* ``GET /batches/editions-ready`` -- an admin/scheduler read model that reports
  which reviewers have a ready edition, as a **contentless** payload (a count and
  a signed link, never any science). The external run-due routine polls this
  after triggering drafts and sends one cue per fresh edition through the user's
  own mailbox; Lab Tracker holds no mail credentials and owns no transport.
* ``GET /r/{token}`` -- the public landing route the cue's deep link points at.
  It verifies the signed, short-TTL token and 302-redirects into the in-app
  accept/reject queue, where authentication and the human review gate still
  apply. An expired or tampered link bounces to "open your Read" -- never a
  silent commit.

See ``docs/daily-review-egress-defaults.md`` for the egress rationale.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import RedirectResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.config import get_settings
from lab_tracker.models import ReadyEdition
from lab_tracker.review_links import sign_review_link, verify_review_link
from lab_tracker.schemas import ListEnvelope

from .shared import actor_from_request, api_from_request, list_response


def _settings_from_request(request: Request):
    return getattr(request.app.state, "settings", None) or get_settings()


def _public_base_url(request: Request, settings) -> str:
    configured = (getattr(settings, "public_base_url", "") or "").strip()
    base = configured or str(request.base_url)
    return base.rstrip("/")


def build_review_delivery_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/batches/editions-ready", response_model=ListEnvelope[ReadyEdition])
    def list_editions_ready(
        request: Request,
        since: datetime | None = None,
        user_id: UUID | None = None,
        include_project_name: bool = False,
    ):
        actor = actor_from_request(request)
        settings = _settings_from_request(request)
        api_obj = api_from_request(request, api)
        editions = api_obj.list_ready_editions(
            since=since,
            review_assignee_user_id=user_id,
            actor=actor,
        )
        base = _public_base_url(request, settings)
        secret = settings.auth_secret_key
        ttl_hours = getattr(settings, "review_link_ttl_hours", 72)
        auth_service = getattr(request.app.state, "auth_service", None)
        for edition in editions:
            token = sign_review_link(secret, edition.change_set_id, ttl_hours=ttl_hours)
            edition.deep_link = f"{base}/r/{token}"
            if (
                auth_service is not None
                and edition.review_assignee_user_id
                and not edition.review_assignee_username
            ):
                try:
                    user = auth_service.get_user_by_id(edition.review_assignee_user_id)
                except Exception:
                    user = None
                if user is not None:
                    edition.review_assignee_username = user.username
            if include_project_name:
                try:
                    edition.project_name = api_obj.get_project(edition.project_id).name
                except Exception:
                    edition.project_name = None
        return list_response(
            editions,
            limit=max(1, len(editions) or 1),
            offset=0,
            total=len(editions),
        )

    @router.get("/r/{token}")
    def consume_review_link(token: str, request: Request):
        settings = _settings_from_request(request)
        change_set_id = verify_review_link(settings.auth_secret_key, token)
        if change_set_id is None:
            # Expired, replayed, or tampered: bounce to "open your Read", never
            # a silent commit and never a leak of which edition existed.
            return RedirectResponse(url="/app/", status_code=302)
        return RedirectResponse(url=f"/app/batches/{change_set_id}", status_code=302)

    return router
