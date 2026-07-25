"""Read-only on-demand resolution of external artifact pointers.

`POST /external-artifacts/resolve` dereferences an
:class:`~lab_tracker.models.ExternalArtifactReference` embedded on a dataset,
analysis, or claim, returning a bounded, hash-verified view of its content. It is
resolve-by-entity: the caller names the owning entity and the artifact's index,
and access is gated by the same project-read check as reading that entity, so
resolution never widens what a caller can see.

Design: ``docs/external-artifact-resolution-design.md``.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.schemas import Envelope

from .shared import actor_from_request, handlers_from_request


class ResolveExternalArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["analysis", "claim", "dataset"]
    entity_id: UUID
    artifact_index: int = Field(default=0, ge=0)
    content_hash: str | None = None
    max_bytes: int | None = Field(default=None, ge=1)
    byte_start: int | None = Field(default=None, ge=0)
    byte_end: int | None = Field(default=None, ge=0)


def build_external_artifacts_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post("/external-artifacts/resolve")
    def resolve_external_artifact(
        payload: ResolveExternalArtifactRequest, request: Request
    ) -> Envelope[dict[str, Any]]:
        context = handlers_from_request(request).context
        prepared = context.prepare_external_artifact_resolution(
            actor=actor_from_request(request),
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            artifact_index=payload.artifact_index,
            content_hash=payload.content_hash,
            max_bytes=payload.max_bytes,
            byte_start=payload.byte_start,
            byte_end=payload.byte_end,
        )
        result = context.resolve_prepared_external_artifact(prepared)
        return Envelope(data=result)

    return router
