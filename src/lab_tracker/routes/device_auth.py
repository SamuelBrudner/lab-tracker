"""Device-token enrollment and management routes (lab-tracker-bbd)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.auth import (
    AuthService,
    DeviceAuthService,
    DeviceToken,
    require_interactive_admin,
)
from lab_tracker.errors import NotFoundError, PermissionDeniedError
from lab_tracker.instance_url import build_instance_url
from lab_tracker.qr_svg import (
    QR_BORDER,
    QR_DARK,
    QR_ERROR,
    QR_LIGHT,
    QR_MODULE_SIZE,
    build_qr_svg,
    resolve_public_base_url,
)
from lab_tracker.schemas import (
    DeviceConsumeRead,
    DeviceConsumeRequest,
    DeviceEnrollmentCreate,
    DeviceEnrollmentRead,
    DeviceTokenRead,
    Envelope,
    ListEnvelope,
)

from .shared import actor_from_request, list_response, paginate, validate_pagination

# Kept as module names: the enrollment tests and any operator scripts pin
# the phone-scanner-friendly rendering through these.
_ENROLLMENT_QR_ERROR = QR_ERROR
_ENROLLMENT_QR_MODULE_SIZE = QR_MODULE_SIZE
_ENROLLMENT_QR_BORDER = QR_BORDER
_ENROLLMENT_QR_DARK = QR_DARK
_ENROLLMENT_QR_LIGHT = QR_LIGHT
_resolve_public_base_url = resolve_public_base_url
_build_enrollment_qr_svg = build_qr_svg


def build_device_auth_router(
    *,
    auth_service: AuthService,
    device_auth_service: DeviceAuthService,
) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/auth/devices/enrollment",
        response_model=Envelope[DeviceEnrollmentRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_enrollment(payload: DeviceEnrollmentCreate, request: Request):
        actor = actor_from_request(request)
        if actor.is_device:
            raise PermissionDeniedError("Pairing must be initiated from a logged-in user session.")
        ttl_minutes = payload.ttl_minutes if payload.ttl_minutes is not None else 5
        offer = device_auth_service.create_enrollment(actor.user_id, ttl_minutes=ttl_minutes)
        base_url = _resolve_public_base_url(request)
        enroll_url = build_instance_url(base_url, "/app/enroll")
        enrollment_url = f"{enroll_url}?offer={offer.offer_token}"
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
            raise PermissionDeniedError("Listing devices requires user credentials.")
        devices = device_auth_service.list_devices(actor.user_id)
        items = [_device_token_read(device) for device in devices]
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
            raise PermissionDeniedError("Revoking devices requires user credentials.")
        device = device_auth_service.revoke_device(actor.user_id, device_token_id)
        return Envelope(data=_device_token_read(device))

    @router.get(
        "/auth/users/{user_id:uuid}/devices",
        response_model=ListEnvelope[DeviceTokenRead],
    )
    def list_user_devices(
        user_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        require_interactive_admin(actor_from_request(request))
        if auth_service.get_user_by_id(user_id) is None:
            raise NotFoundError("User does not exist.")
        devices = [
            _device_token_read(device) for device in device_auth_service.list_devices(user_id)
        ]
        items, total = paginate(devices, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.delete(
        "/auth/users/{user_id:uuid}/devices/{device_token_id:uuid}",
        response_model=Envelope[DeviceTokenRead],
    )
    def revoke_user_device(user_id: UUID, device_token_id: UUID, request: Request):
        require_interactive_admin(actor_from_request(request))
        if auth_service.get_user_by_id(user_id) is None:
            raise NotFoundError("User does not exist.")
        device = device_auth_service.revoke_device(user_id, device_token_id)
        return Envelope(data=_device_token_read(device))

    return router


def _device_token_read(device: DeviceToken) -> DeviceTokenRead:
    return DeviceTokenRead(
        device_token_id=device.device_token_id,
        label=device.label,
        created_at=device.created_at,
        last_used_at=device.last_used_at,
        revoked_at=device.revoked_at,
    )
