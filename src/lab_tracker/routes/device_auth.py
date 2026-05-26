"""Device-token enrollment and management routes (lab-tracker-bbd)."""

from __future__ import annotations

from uuid import UUID

import segno
from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.auth import DeviceAuthService
from lab_tracker.config import get_settings
from lab_tracker.errors import AuthError
from lab_tracker.schemas import (
    DeviceConsumeRead,
    DeviceConsumeRequest,
    DeviceEnrollmentCreate,
    DeviceEnrollmentRead,
    DeviceTokenRead,
    Envelope,
    ListEnvelope,
)

from .shared import actor_from_request

_ENROLLMENT_QR_ERROR = "l"
_ENROLLMENT_QR_MODULE_SIZE = 8
_ENROLLMENT_QR_BORDER = 6
_ENROLLMENT_QR_DARK = "#000000"
_ENROLLMENT_QR_LIGHT = "#ffffff"


def _resolve_public_base_url(request: Request) -> str:
    """Pick the base URL the paired phone will hit.

    Setting beats inference. Falls back to the request's own host so a
    desktop browser opened at the laptop's LAN IP automatically generates
    a phone-reachable URL; only 127.0.0.1/localhost desktops need the
    explicit LAB_TRACKER_PUBLIC_BASE_URL override.
    """
    settings = getattr(request.app.state, "settings", None) or get_settings()
    configured = (settings.public_base_url or "").strip().rstrip("/")
    if configured:
        return configured
    base = str(request.base_url).rstrip("/")
    return base


def _build_enrollment_qr_svg(url: str) -> str:
    qr = segno.make(url, error=_ENROLLMENT_QR_ERROR)
    module_size = _ENROLLMENT_QR_MODULE_SIZE
    border = _ENROLLMENT_QR_BORDER
    matrix = tuple(tuple(row) for row in qr.matrix)
    matrix_size = len(matrix)
    svg_size = (matrix_size + (border * 2)) * module_size
    dark_rects: list[str] = []
    for y, row in enumerate(matrix):
        run_start: int | None = None
        for x, module in enumerate((*row, 0)):
            if module and run_start is None:
                run_start = x
            if not module and run_start is not None:
                rect_x = (run_start + border) * module_size
                rect_y = (y + border) * module_size
                rect_width = (x - run_start) * module_size
                dark_rects.append(
                    f'<rect x="{rect_x}" y="{rect_y}" '
                    f'width="{rect_width}" height="{module_size}" />'
                )
                run_start = None
    dark_markup = "".join(dark_rects)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_size}" '
        f'height="{svg_size}" viewBox="0 0 {svg_size} {svg_size}" '
        'shape-rendering="crispEdges">'
        f'<rect width="{svg_size}" height="{svg_size}" fill="{_ENROLLMENT_QR_LIGHT}" />'
        f'<g fill="{_ENROLLMENT_QR_DARK}">{dark_markup}</g>'
        "</svg>"
    )


def build_device_auth_router(*, device_auth_service: DeviceAuthService) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/auth/devices/enrollment",
        response_model=Envelope[DeviceEnrollmentRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_enrollment(payload: DeviceEnrollmentCreate, request: Request):
        actor = actor_from_request(request)
        if actor.is_device:
            raise AuthError("Pairing must be initiated from a logged-in user session.")
        ttl_minutes = payload.ttl_minutes if payload.ttl_minutes is not None else 5
        offer = device_auth_service.create_enrollment(actor.user_id, ttl_minutes=ttl_minutes)
        base_url = _resolve_public_base_url(request)
        enrollment_url = f"{base_url}/app/enroll?offer={offer.offer_token}"
        qr_svg = _build_enrollment_qr_svg(enrollment_url)
        return Envelope(
            data=DeviceEnrollmentRead(
                enrollment_id=offer.enrollment_id,
                offer_token=offer.offer_token,
                expires_at=offer.expires_at,
                enrollment_url=enrollment_url,
                enrollment_qr_svg=qr_svg,
            )
        )

    @router.post(
        "/auth/devices/consume",
        response_model=Envelope[DeviceConsumeRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def consume_enrollment(payload: DeviceConsumeRequest):
        issued = device_auth_service.consume_enrollment(
            payload.offer_token,
            label=payload.label,
        )
        return Envelope(
            data=DeviceConsumeRead(
                device_token_id=issued.device_token.device_token_id,
                secret=issued.secret,
                label=issued.device_token.label,
                created_at=issued.device_token.created_at,
            )
        )

    @router.get(
        "/auth/devices",
        response_model=ListEnvelope[DeviceTokenRead],
    )
    def list_devices(request: Request):
        actor = actor_from_request(request)
        if actor.is_device:
            raise AuthError("Listing devices requires user credentials.")
        devices = device_auth_service.list_devices(actor.user_id)
        items = [
            DeviceTokenRead(
                device_token_id=device.device_token_id,
                label=device.label,
                created_at=device.created_at,
                last_used_at=device.last_used_at,
                revoked_at=device.revoked_at,
            )
            for device in devices
        ]
        return ListEnvelope(
            data=items,
            meta={"limit": max(len(items), 1), "offset": 0, "total": len(items)},
        )

    @router.delete(
        "/auth/devices/{device_token_id}",
        response_model=Envelope[DeviceTokenRead],
    )
    def revoke_device(device_token_id: UUID, request: Request):
        actor = actor_from_request(request)
        if actor.is_device:
            raise AuthError("Revoking devices requires user credentials.")
        device = device_auth_service.revoke_device(actor.user_id, device_token_id)
        return Envelope(
            data=DeviceTokenRead(
                device_token_id=device.device_token_id,
                label=device.label,
                created_at=device.created_at,
                last_used_at=device.last_used_at,
                revoked_at=device.revoked_at,
            )
        )

    return router
