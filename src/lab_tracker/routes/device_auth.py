"""Device-token enrollment and management routes (lab-tracker-bbd)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.auth import DeviceAuthService
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
        enrollment_url = f"/app/enroll?offer={offer.offer_token}"
        return Envelope(
            data=DeviceEnrollmentRead(
                enrollment_id=offer.enrollment_id,
                offer_token=offer.offer_token,
                expires_at=offer.expires_at,
                enrollment_url=enrollment_url,
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
            meta={"limit": len(items), "offset": 0, "total": len(items)},
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
