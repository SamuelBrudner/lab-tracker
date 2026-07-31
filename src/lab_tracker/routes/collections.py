"""Acquisition collection capture and bounded read routes."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import Response

from lab_tracker.api import LabTrackerAPI
from lab_tracker.collection_models import (
    AcquisitionCollectionMember,
    AcquisitionCollectionSnapshot,
    AcquisitionCollectionSummary,
)
from lab_tracker.collection_schemas import AcquisitionCollectionSnapshotCreate
from lab_tracker.schemas import Envelope, ListEnvelope

from .shared import (
    actor_from_request,
    api_from_request,
    content_disposition_header,
    list_response,
    validate_pagination,
)


def build_collections_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/sessions/{session_id}/collections/{collection_key}/snapshots",
        response_model=Envelope[AcquisitionCollectionSnapshot],
        status_code=http_status.HTTP_201_CREATED,
    )
    def capture_collection_snapshot(
        session_id: UUID,
        collection_key: str,
        payload: AcquisitionCollectionSnapshotCreate,
        request: Request,
    ):
        result = api_from_request(request, api).capture_collection_snapshot(
            session_id=session_id,
            collection_key=collection_key,
            client_capture_id=payload.client_capture_id,
            observed_at=payload.observed_at,
            complete=payload.complete,
            schema_version=payload.manifest.schema_version,
            members=payload.manifest.members,
            source_provider=payload.source_provider,
            source_uri=payload.source_uri,
            actor=actor_from_request(request),
        )
        return Envelope(
            data=result.snapshot,
            meta={
                "snapshot_reused": result.snapshot_reused,
                "current_pointer_changed": result.current_pointer_changed,
            },
        )

    @router.get(
        "/sessions/{session_id}/collections",
        response_model=ListEnvelope[AcquisitionCollectionSummary],
    )
    def list_session_collections(
        session_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        collections, total = api_from_request(
            request,
            api,
        ).list_acquisition_collections(
            session_id=session_id,
            limit=limit,
            offset=offset,
            actor=actor_from_request(request),
        )
        return list_response(
            collections,
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.get(
        "/collections/{collection_id}/snapshots",
        response_model=ListEnvelope[AcquisitionCollectionSnapshot],
    )
    def list_collection_snapshots(
        collection_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        snapshots, total = api_from_request(
            request,
            api,
        ).list_collection_snapshots(
            collection_id=collection_id,
            limit=limit,
            offset=offset,
            actor=actor_from_request(request),
        )
        return list_response(
            snapshots,
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.get(
        "/collection-snapshots/{snapshot_id}",
        response_model=Envelope[AcquisitionCollectionSnapshot],
    )
    def get_collection_snapshot(snapshot_id: UUID, request: Request):
        snapshot = api_from_request(request, api).get_collection_snapshot(
            snapshot_id,
            actor=actor_from_request(request),
        )
        return Envelope(data=snapshot)

    @router.get(
        "/collection-snapshots/{snapshot_id}/members",
        response_model=ListEnvelope[AcquisitionCollectionMember],
    )
    def list_collection_members(
        snapshot_id: UUID,
        request: Request,
        limit: int = 100,
        offset: int = 0,
        q: str | None = None,
    ):
        validate_pagination(limit, offset)
        members, total = api_from_request(
            request,
            api,
        ).list_collection_members(
            snapshot_id=snapshot_id,
            limit=limit,
            offset=offset,
            query=q,
            actor=actor_from_request(request),
        )
        return list_response(
            members,
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.get("/collection-snapshots/{snapshot_id}/manifest")
    def download_collection_manifest(snapshot_id: UUID, request: Request):
        manifest = api_from_request(request, api).get_collection_manifest(
            snapshot_id,
            actor=actor_from_request(request),
        )
        content = json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        filename = f"collection-snapshot-{snapshot_id}.manifest.json"
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": content_disposition_header(
                    "attachment",
                    filename,
                )
            },
        )

    return router
