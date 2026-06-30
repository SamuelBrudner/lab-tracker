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

import base64
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.artifact_resolution import (
    DEFAULT_MAX_BYTES,
    ResolvedArtifact,
    ResolverRegistry,
    registry_from_env,
    store_relative_reference,
    unresolved,
)
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import ExternalArtifactReference
from lab_tracker.schemas import Envelope

from .shared import api_from_request, ensure_project_read, repository_from_request


class ResolveExternalArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["analysis", "claim", "dataset"]
    entity_id: UUID
    artifact_index: int = Field(default=0, ge=0)
    content_hash: str | None = None
    max_bytes: int | None = Field(default=None, ge=1)
    byte_start: int | None = Field(default=None, ge=0)
    byte_end: int | None = Field(default=None, ge=0)


def _resolver_registry_from_request(request: Request) -> ResolverRegistry:
    registry = getattr(request.app.state, "resolver_registry", None)
    if registry is not None:
        return registry
    return registry_from_env()


def _entity_artifacts(
    api: LabTrackerAPI, payload: ResolveExternalArtifactRequest
) -> tuple[list[ExternalArtifactReference], UUID]:
    if payload.entity_type == "analysis":
        analysis = api.get_analysis(payload.entity_id)
        return list(analysis.external_artifacts), analysis.project_id
    if payload.entity_type == "claim":
        claim = api.get_claim(payload.entity_id)
        return list(claim.external_citations), claim.project_id
    dataset = api.get_dataset(payload.entity_id)
    manifest = dataset.commit_manifest
    artifacts = list(manifest.external_artifacts) if manifest is not None else []
    return artifacts, dataset.project_id


def _locate_reference(
    api: LabTrackerAPI, payload: ResolveExternalArtifactRequest
) -> tuple[ExternalArtifactReference, UUID]:
    artifacts, project_id = _entity_artifacts(api, payload)
    if payload.artifact_index >= len(artifacts):
        raise NotFoundError(
            f"No external artifact at index {payload.artifact_index} on "
            f"{payload.entity_type} {payload.entity_id}."
        )
    ref = artifacts[payload.artifact_index]
    if payload.content_hash is not None and payload.content_hash != ref.content_hash:
        raise ValidationError(
            "content_hash does not match the artifact at the given index."
        )
    return ref, project_id


def _materialize_reference(
    request: Request, ref: ExternalArtifactReference, project_id: UUID
) -> ExternalArtifactReference | ResolvedArtifact:
    """Translate a store:// locator into a concrete reference via its DataStore.

    Returns the reference unchanged for non-store URIs, a concrete reference for a
    resolvable store, or an UNRESOLVED result when the store is missing or its
    kind is not resolvable yet.
    """

    # Prefer the structured field form; fall back to parsing a store:// URI.
    if ref.store_name is not None and ref.locator is not None:
        return _resolve_store(request, ref, project_id, ref.store_name, ref.locator)
    parsed = urlsplit(ref.uri)
    if parsed.scheme.lower() != "store":
        return ref
    name = parsed.netloc
    if not name:
        return unresolved(ref, detail="Store locator is missing a store name.")
    return _resolve_store(request, ref, project_id, name, parsed.path.lstrip("/"))


def _resolve_store(
    request: Request,
    ref: ExternalArtifactReference,
    project_id: UUID,
    name: str,
    path: str,
) -> ExternalArtifactReference | ResolvedArtifact:
    store = repository_from_request(request).data_stores.get_by_name(project_id, name)
    if store is None:
        return unresolved(ref, detail=f"No data store named '{name}' in this project.")
    concrete = store_relative_reference(store, path=path, content_hash=ref.content_hash)
    if concrete is None:
        return unresolved(
            ref, detail=f"Store kind '{store.kind.value}' is not resolvable yet."
        )
    return concrete


def _resolution_payload(
    result: ResolvedArtifact, payload: ResolveExternalArtifactRequest
) -> dict[str, Any]:
    body = result.to_json_dict()
    body["entity_type"] = payload.entity_type
    body["entity_id"] = str(payload.entity_id)
    body["artifact_index"] = payload.artifact_index
    body["content_base64"] = (
        base64.b64encode(result.content).decode("ascii")
        if result.content is not None
        else None
    )
    return body


def build_external_artifacts_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post("/external-artifacts/resolve")
    def resolve_external_artifact(payload: ResolveExternalArtifactRequest, request: Request):
        bound_api = api_from_request(request, api)
        ref, project_id = _locate_reference(bound_api, payload)
        ensure_project_read(request, project_id)

        byte_range: tuple[int, int] | None = None
        if payload.byte_start is not None or payload.byte_end is not None:
            if payload.byte_start is None or payload.byte_end is None:
                raise ValidationError(
                    "byte_start and byte_end must be provided together."
                )
            byte_range = (payload.byte_start, payload.byte_end)

        materialized = _materialize_reference(request, ref, project_id)
        if isinstance(materialized, ResolvedArtifact):
            result = materialized
        else:
            registry = _resolver_registry_from_request(request)
            result = registry.resolve(
                materialized,
                max_bytes=payload.max_bytes or DEFAULT_MAX_BYTES,
                byte_range=byte_range,
            )
        return Envelope(data=_resolution_payload(result, payload))

    return router
