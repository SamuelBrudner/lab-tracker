"""Visualization routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Response, UploadFile
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import StreamingResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import UsageEventResourceType, Visualization
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import Envelope, ListEnvelope, VisualizationCreate, VisualizationUpdate

from .shared import (
    CreatedByFilter,
    actor_from_request,
    api_from_request,
    content_disposition_header,
    ensure_project_read,
    handlers_from_request,
    list_response,
    record_usage_view,
    validate_pagination,
)


def build_visualizations_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/visualizations",
        response_model=Envelope[Visualization],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_visualization(payload: VisualizationCreate, request: Request):
        actor = actor_from_request(request)
        visualization = api_from_request(request, api).create_visualization(
            analysis_id=payload.analysis_id,
            viz_type=payload.viz_type,
            file_path=payload.file_path,
            caption=payload.caption,
            related_claim_ids=payload.related_claim_ids,
            actor=actor,
        )
        return Envelope(data=visualization)

    @router.get("/visualizations", response_model=ListEnvelope[Visualization])
    def list_visualizations(
        request: Request,
        project_id: UUID | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        created_by: CreatedByFilter = None,
        since: datetime | None = None,
        until: datetime | None = None,
        recent_first: bool = False,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        page = handlers_from_request(request).catalogs.list_visualizations(
            actor=actor_from_request(request),
            project_id=project_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return list_response(
            page.items,
            limit=limit,
            offset=offset,
            total=page.total,
        )

    @router.get("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def get_visualization(viz_id: UUID, request: Request):
        visualization, project_id = api_from_request(
            request,
            api,
        ).get_visualization_for_read(
            viz_id,
            actor=actor_from_request(request),
        )
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.VISUALIZATION,
            resource_id=visualization.viz_id,
            project_id=project_id,
        )
        return Envelope(data=visualization)

    @router.post(
        "/visualizations/{viz_id}/file",
        response_model=Envelope[Visualization],
        status_code=http_status.HTTP_201_CREATED,
    )
    def upload_visualization_file(
        viz_id: UUID,
        request: Request,
        response: Response,
        file: Annotated[UploadFile, File()],
        checksum_sha256: Annotated[str | None, Form(max_length=64)] = None,
        size_bytes: Annotated[int | None, Form(gt=0)] = None,
        expected_current_storage_id: Annotated[
            str | None,
            Form(max_length=64),
        ] = None,
    ):
        result = handlers_from_request(request).visualization_files.upload(
            viz_id,
            actor=actor_from_request(request),
            filename=file.filename or "",
            content_type=file.content_type,
            chunks=iter(lambda: file.file.read(1024 * 1024), b""),
            checksum_sha256=checksum_sha256,
            size_bytes=size_bytes,
            expected_current_storage_id=expected_current_storage_id,
            raw_content_length=request.headers.get("content-length"),
        )
        if result.reused:
            response.status_code = http_status.HTTP_200_OK
        return Envelope(
            data=result.entity,
            meta={"asset_outcome": result.outcome},
        )

    @router.get("/visualizations/{viz_id}/file/download")
    def download_visualization_file(viz_id: UUID, request: Request):
        download = handlers_from_request(request).visualization_files.download(
            viz_id,
            actor=actor_from_request(request),
        )
        headers = {
            "Content-Disposition": content_disposition_header(
                "attachment",
                download.filename,
            ),
            "Content-Length": str(download.size_bytes),
        }
        if download.nosniff:
            headers["X-Content-Type-Options"] = "nosniff"
        return StreamingResponse(
            download.chunks,
            media_type=download.content_type,
            headers=headers,
        )

    @router.patch("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def update_visualization(viz_id: UUID, payload: VisualizationUpdate, request: Request):
        actor = actor_from_request(request)
        existing = api_from_request(request, api).get_visualization(viz_id)
        analysis = api_from_request(request, api).get_analysis(existing.analysis_id)
        ensure_project_read(request, analysis.project_id)
        visualization = api_from_request(request, api).update_visualization(
            viz_id,
            actor=actor,
            **provided_fields(payload),
        )
        return Envelope(data=visualization)

    @router.delete("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def delete_visualization(viz_id: UUID, request: Request):
        actor = actor_from_request(request)
        visualization = handlers_from_request(
            request
        ).deletions.delete_visualization(viz_id, actor=actor)
        return Envelope(data=visualization)

    return router
