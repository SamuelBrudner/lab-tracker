"""Server capture-channel routes: Slack inbound, poller dispatch, capture addresses."""

from __future__ import annotations

import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import Role
from lab_tracker.capture_channels.app_runtime import capture_runtime_from_app
from lab_tracker.capture_channels.common import emails_for_user
from lab_tracker.capture_channels.dispatch import PollRunReport, run_due_pollers
from lab_tracker.capture_channels.email_capture import EmailCaptureConfig
from lab_tracker.capture_channels.slack import (
    MAX_SLACK_BODY_BYTES,
    SlackCapture,
    SlackConfig,
    SlackReply,
    SlackRequestRejected,
    capture_slack_request,
    parse_interactivity,
    parse_slash_command,
    verify_slack_request,
)
from lab_tracker.errors import AuthError, NotFoundError, PayloadTooLargeError, PermissionDeniedError
from lab_tracker.schemas import Envelope

from .shared import actor_from_request, api_from_request, ensure_project_contributor

SLACK_COMMANDS_PATH = "/integrations/slack/commands"
SLACK_INTERACTIVITY_PATH = "/integrations/slack/interactivity"
# Authenticated by Slack's request signature instead of a bearer token.
SLACK_PUBLIC_PATHS = frozenset({SLACK_COMMANDS_PATH, SLACK_INTERACTIVITY_PATH})


class CaptureAddressRead(BaseModel):
    """The caller's private email capture address for one project."""

    project_id: UUID
    address: str
    accepted_senders: list[str]


async def _slack_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_SLACK_BODY_BYTES:
        raise PayloadTooLargeError("Slack request body is too large.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_SLACK_BODY_BYTES:
            raise PayloadTooLargeError("Slack request body is too large.")
    return bytes(body)


SlackBody = Annotated[bytes, Depends(_slack_body)]


def build_integrations_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(SLACK_COMMANDS_PATH, include_in_schema=True)
    def slack_command(request: Request, body: SlackBody) -> Response:
        """Slack slash command (``/lt some text``): stage the text as a note."""

        config, timestamp = _verified_slack_request(request, body)
        try:
            capture = parse_slash_command(body)
        except SlackRequestRejected as exc:
            raise AuthError("Slack request could not be verified.") from exc
        if capture is None:  # Slack's periodic SSL check
            return Response(status_code=200)
        return _slack_reply(_capture(request, api, capture, config, timestamp))

    @router.post(SLACK_INTERACTIVITY_PATH, include_in_schema=True)
    def slack_interactivity(request: Request, body: SlackBody) -> Response:
        """Slack message shortcut ("Save to Lab Tracker"): stage the message."""

        config, timestamp = _verified_slack_request(request, body)
        try:
            parsed = parse_interactivity(body)
        except SlackRequestRejected as exc:
            raise AuthError("Slack request could not be verified.") from exc
        if parsed is None:
            return Response(status_code=200)
        if isinstance(parsed, SlackReply):
            return _slack_reply(parsed)
        return _slack_reply(_capture(request, api, parsed, config, timestamp))

    @router.post("/integrations/run-due", response_model=Envelope[PollRunReport])
    def run_due_integrations(request: Request):
        """Run every configured, due capture poller once (admins and scheduler tokens)."""

        actor = actor_from_request(request)
        if actor.role is not Role.ADMIN:
            raise PermissionDeniedError("Only admins can run the capture pollers.")
        report = run_due_pollers(capture_runtime_from_app(request.app), trigger="http")
        return Envelope(data=report)

    @router.get(
        "/projects/{project_id}/capture-address",
        response_model=Envelope[CaptureAddressRead],
    )
    def get_capture_address(project_id: UUID, request: Request):
        """Your private email capture address for this project."""

        actor = actor_from_request(request)
        if not actor.is_interactive:
            raise PermissionDeniedError(
                "Capture addresses are shown only to a signed-in person, not to tokens."
            )
        ensure_project_contributor(request, project_id)
        config = EmailCaptureConfig.from_settings(request.app.state.settings)
        if config is None:
            raise NotFoundError("Email capture is not configured on this server.")
        user = request.app.state.auth_service.get_user_by_id(actor.user_id)
        senders = emails_for_user(user, config.email_directory) if user is not None else ()
        if user is None or not senders:
            raise NotFoundError(
                "No sender address is registered for you; ask your Lab Tracker admin to add "
                "yours to LAB_TRACKER_CAPTURE_USER_EMAILS."
            )
        return Envelope(
            data=CaptureAddressRead(
                project_id=project_id,
                address=config.address_for(user.user_id, project_id),
                accepted_senders=list(senders),
            )
        )

    return router


def _verified_slack_request(request: Request, body: bytes) -> tuple[SlackConfig, int]:
    config = SlackConfig.from_settings(request.app.state.settings)
    if config is None:
        raise NotFoundError("Slack capture is not configured on this server.")
    timestamp = request.headers.get("x-slack-request-timestamp")
    try:
        verify_slack_request(
            secret=config.signing_secret,
            timestamp=timestamp,
            signature=request.headers.get("x-slack-signature"),
            body=body,
            now=time.time(),
        )
    except SlackRequestRejected as exc:
        raise AuthError("Slack request could not be verified.") from exc
    return config, int(timestamp or 0)


def _capture(
    request: Request,
    api: LabTrackerAPI,
    capture: SlackCapture,
    config: SlackConfig,
    timestamp: int,
) -> SlackReply:
    return capture_slack_request(
        capture,
        config=config,
        users=request.app.state.auth_service,
        api=api_from_request(request, api),
        request_timestamp=timestamp,
    )


def _slack_reply(reply: SlackReply) -> JSONResponse:
    return JSONResponse(reply.body())
