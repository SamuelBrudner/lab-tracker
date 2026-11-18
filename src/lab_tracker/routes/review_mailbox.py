"""Interactive enrollment in reply-by-email proposal review."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import Field
from starlette.requests import Request

from lab_tracker.auth import PrincipalType
from lab_tracker.errors import PermissionDeniedError, ValidationError
from lab_tracker.rate_limit import rate_limit_client
from lab_tracker.review_email_transport import ReviewEmailDeliveryError
from lab_tracker.review_mailbox import (
    ReviewMailboxStatus,
    confirm_challenge,
    disable_mailbox,
    issue_challenge,
    mailbox_status,
)
from lab_tracker.schemas import Envelope, RequestModel

from .shared import actor_from_request


class ReviewMailboxChallenge(RequestModel):
    login_email: str | None = Field(default=None, max_length=150)


class ReviewMailboxConfirmation(RequestModel):
    code: str = Field(min_length=1, max_length=100)


def _available(request: Request) -> bool:
    settings = request.app.state.settings
    return bool(
        settings.review_email_enabled
        and settings.review_email_transport == "smtp"
        and settings.review_email_reply_address
        and settings.review_email_authserv_id
        and request.app.state.review_email_provider is not None
    )


def _session_user(request: Request) -> UUID:
    actor = actor_from_request(request)
    if not request.app.state.auth_enabled or actor.principal_type is not PrincipalType.USER:
        raise PermissionDeniedError("Sign in with a user session to manage proposal review email.")
    return actor.user_id


def build_review_mailbox_router() -> APIRouter:
    router = APIRouter(prefix="/auth/review-mailbox")

    @router.get("", response_model=Envelope[ReviewMailboxStatus])
    def get_review_mailbox(request: Request) -> Envelope[ReviewMailboxStatus]:
        user_id = _session_user(request)
        with request.app.state.db_session_factory() as session:
            return Envelope(data=mailbox_status(session, user_id, available=_available(request)))

    @router.post("/challenge", response_model=Envelope[ReviewMailboxStatus])
    def send_review_mailbox_challenge(
        payload: ReviewMailboxChallenge, request: Request
    ) -> Envelope[ReviewMailboxStatus]:
        user_id = _session_user(request)
        if not _available(request):
            raise ValidationError("Proposal review by email is unavailable on this host.")
        limiter = request.app.state.auth_rate_limiter
        limiter.record_attempt(f"review-mailbox:{user_id}", client=rate_limit_client(request))
        with request.app.state.db_session_factory() as session:
            address, code = issue_challenge(session, user_id, payload.login_email)
            try:
                request.app.state.review_email_provider.send_verification_code(address, code)
            except ReviewEmailDeliveryError:
                raise ValidationError(
                    "The verification email could not be delivered. Try again in a minute."
                ) from None
            return Envelope(data=mailbox_status(session, user_id, available=True))

    @router.post("/confirm", response_model=Envelope[ReviewMailboxStatus])
    def confirm_review_mailbox(
        payload: ReviewMailboxConfirmation, request: Request
    ) -> Envelope[ReviewMailboxStatus]:
        user_id = _session_user(request)
        if not _available(request):
            raise ValidationError("Proposal review by email is unavailable on this host.")
        with request.app.state.db_session_factory() as session:
            confirm_challenge(session, user_id, payload.code)
            return Envelope(data=mailbox_status(session, user_id, available=True))

    @router.delete("", response_model=Envelope[ReviewMailboxStatus])
    def disable_review_mailbox(request: Request) -> Envelope[ReviewMailboxStatus]:
        user_id = _session_user(request)
        with request.app.state.db_session_factory() as session:
            disable_mailbox(session, user_id)
            return Envelope(data=mailbox_status(session, user_id, available=_available(request)))

    return router
