"""Read-only on-demand resolution of external artifact pointers.

`POST /external-artifacts/resolve` dereferences a registered store-relative
:class:`~lab_tracker.models.ExternalArtifactReference` embedded on a dataset,
analysis, or claim, returning a bounded, hash-verified view of its content.
Direct locators remain metadata and fail closed without resolver work. The
operation is resolve-by-entity: the caller names the owning entity and the
artifact's index, and access is gated by the same project-read check as reading
that entity, so resolution never widens what a caller can see.

Design: ``docs/external-artifact-resolution-design.md``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.artifact_resolution_limits import (
    MAX_ARTIFACT_BYTE_OFFSET,
    MAX_INLINE_ARTIFACT_BYTES,
    ArtifactContentBounds,
    ArtifactContentBoundsError,
)
from lab_tracker.schemas import Envelope

from .shared import actor_from_request, handlers_from_request


class ResolveExternalArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["analysis", "claim", "dataset"]
    entity_id: UUID
    artifact_index: int = Field(default=0, ge=0)
    content_hash: str | None = None
    max_bytes: (
        Annotated[
            int,
            Field(strict=True, ge=1, le=MAX_INLINE_ARTIFACT_BYTES),
        ]
        | None
    ) = None
    byte_start: (
        Annotated[
            int,
            Field(strict=True, ge=0, le=MAX_ARTIFACT_BYTE_OFFSET),
        ]
        | None
    ) = None
    byte_end: (
        Annotated[
            int,
            Field(strict=True, ge=0, le=MAX_ARTIFACT_BYTE_OFFSET),
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def validate_artifact_content_bounds(self) -> ResolveExternalArtifactRequest:
        try:
            ArtifactContentBounds.for_request(
                self.max_bytes,
                self.byte_start,
                self.byte_end,
            )
        except ArtifactContentBoundsError as exc:
            raise ValueError(str(exc)) from exc
        return self


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
