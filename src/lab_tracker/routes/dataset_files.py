"""Dataset file routes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, UploadFile
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import StreamingResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import DatasetFile
from lab_tracker.schemas import Envelope, ListEnvelope

from .shared import (
    actor_from_request,
    content_disposition_header,
    handlers_from_request,
    list_response,
    validate_pagination,
)


def build_dataset_files_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/datasets/{dataset_id}/files",
        response_model=Envelope[DatasetFile],
        status_code=http_status.HTTP_201_CREATED,
    )
    def upload_dataset_file(
        dataset_id: UUID,
        request: Request,
        file: Annotated[UploadFile, File()],
    ):
        dataset_file = handlers_from_request(request).dataset_files.upload(
            dataset_id,
            actor=actor_from_request(request),
            filename=file.filename or "",
            content_type=file.content_type,
            chunks=iter(lambda: file.file.read(1024 * 1024), b""),
            raw_content_length=request.headers.get("content-length"),
        )
        return Envelope(data=dataset_file)

    @router.get("/datasets/{dataset_id}/files", response_model=ListEnvelope[DatasetFile])
    def list_dataset_files(
        dataset_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        page = handlers_from_request(request).dataset_files.list(
            dataset_id,
            actor=actor_from_request(request),
            limit=limit,
            offset=offset,
        )
        return list_response(
            page.items,
            limit=limit,
            offset=offset,
            total=page.total,
        )

    @router.get("/datasets/{dataset_id}/files/{file_id}/download")
    def download_dataset_file(
        dataset_id: UUID,
        file_id: UUID,
        request: Request,
    ):
        download = handlers_from_request(request).dataset_files.download(
            dataset_id,
            file_id,
            actor=actor_from_request(request),
        )
        headers = {
            "Content-Disposition": content_disposition_header(
                "attachment",
                download.filename,
            ),
            "Content-Length": str(download.size_bytes),
        }
        return StreamingResponse(
            download.chunks,
            media_type=download.content_type,
            headers=headers,
        )

    @router.delete("/datasets/{dataset_id}/files/{file_id}", response_model=Envelope[DatasetFile])
    def delete_dataset_file(
        dataset_id: UUID,
        file_id: UUID,
        request: Request,
    ):
        dataset_file = handlers_from_request(request).dataset_files.delete(
            dataset_id,
            file_id,
            actor=actor_from_request(request),
        )
        return Envelope(data=dataset_file)

    return router
