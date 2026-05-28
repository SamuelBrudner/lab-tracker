"""Visualization routes."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, UploadFile
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import StreamingResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import require_role
from lab_tracker.db_models import VisualizationModel
from lab_tracker.errors import AuthError, NotFoundError, ValidationError
from lab_tracker.models import Visualization, utc_now
from lab_tracker.schemas import Envelope, ListEnvelope, VisualizationCreate, VisualizationUpdate
from lab_tracker.services.shared import WRITE_ROLES

from .shared import (
    actor_from_request,
    api_from_request,
    db_session_from_request,
    ensure_project_read,
    file_storage_from_request,
    list_response,
    paginate,
    repository_from_request,
    safe_attachment_filename,
    validate_pagination,
)

_logger = logging.getLogger(__name__)


def _delete_stored_visualization_file(storage_backend: object, storage_id: UUID) -> None:
    try:
        storage_backend.delete(storage_id)
    except NotFoundError:
        return
    except Exception as exc:
        _logger.warning(
            "Failed to delete visualization file storage object %s: %s",
            storage_id,
            exc,
            exc_info=True,
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
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
        visualizations, _ = repository_from_request(request).query_visualizations(
            project_id=project_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            limit=None,
            offset=0,
        )
        visible = _filter_visualizations_for_access(request, visualizations)
        items, total = paginate(visible, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.get("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def get_visualization(viz_id: UUID, request: Request):
        visualization = api_from_request(request, api).get_visualization(viz_id)
        analysis = api_from_request(request, api).get_analysis(visualization.analysis_id)
        ensure_project_read(request, analysis.project_id)
        return Envelope(data=visualization)

    @router.post(
        "/visualizations/{viz_id}/file",
        response_model=Envelope[Visualization],
        status_code=http_status.HTTP_201_CREATED,
    )
    async def upload_visualization_file(
        viz_id: UUID,
        request: Request,
        file: Annotated[UploadFile, File()],
    ):
        request_api = api_from_request(request, api)
        actor = actor_from_request(request)
        require_role(actor, WRITE_ROLES)

        db_session = db_session_from_request(request)
        storage_backend = file_storage_from_request(request)

        row = db_session.get(VisualizationModel, str(viz_id))
        if row is None:
            raise NotFoundError("Visualization does not exist.")
        visualization = request_api.get_visualization(viz_id)
        analysis = request_api.get_analysis(visualization.analysis_id)
        ensure_project_read(request, analysis.project_id)

        filename = (file.filename or "").strip()
        if not filename:
            raise ValidationError("filename must not be empty.")
        content_type = (file.content_type or "application/octet-stream").strip()
        if not content_type:
            content_type = "application/octet-stream"

        old_storage_id = UUID(row.asset_storage_id) if row.asset_storage_id else None
        metadata = storage_backend.store_stream(
            iter(lambda: file.file.read(1024 * 1024), b""),
            filename=filename,
            content_type=content_type,
        )
        storage_id = metadata.storage_id
        if metadata.size_bytes <= 0:
            with suppress(Exception):
                storage_backend.delete(storage_id)
            raise ValidationError("file must not be empty.")

        try:
            row.asset_storage_id = str(storage_id)
            row.asset_filename = filename
            row.asset_content_type = content_type
            row.asset_size_bytes = metadata.size_bytes
            row.asset_checksum = metadata.sha256
            row.updated_at = utc_now()
            db_session.flush()
        except Exception:
            with suppress(Exception):
                storage_backend.delete(storage_id)
            raise

        request_api.run_after_rollback(
            lambda storage_id=storage_id: _delete_stored_visualization_file(
                storage_backend,
                storage_id,
            )
        )
        if old_storage_id is not None:
            request_api.run_after_commit(
                lambda storage_id=old_storage_id: _delete_stored_visualization_file(
                    storage_backend,
                    storage_id,
                )
            )

        return Envelope(data=request_api.get_visualization(viz_id))

    @router.get("/visualizations/{viz_id}/file/download")
    def download_visualization_file(viz_id: UUID, request: Request):
        request_api = api_from_request(request, api)
        actor_from_request(request)
        visualization = request_api.get_visualization(viz_id)
        analysis = request_api.get_analysis(visualization.analysis_id)
        ensure_project_read(request, analysis.project_id)

        db_session = db_session_from_request(request)
        storage_backend = file_storage_from_request(request)

        row = db_session.get(VisualizationModel, str(viz_id))
        if row is None:
            raise NotFoundError("Visualization does not exist.")
        if not row.asset_storage_id:
            raise NotFoundError("Visualization file does not exist.")

        headers = {
            "Content-Disposition": (
                f'inline; filename="{safe_attachment_filename(row.asset_filename or "figure")}"'
            ),
            "Content-Length": str(row.asset_size_bytes or 0),
        }
        return StreamingResponse(
            storage_backend.iter_chunks(UUID(row.asset_storage_id)),
            media_type=row.asset_content_type or "application/octet-stream",
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
            viz_type=payload.viz_type,
            file_path=payload.file_path,
            caption=payload.caption,
            related_claim_ids=payload.related_claim_ids,
            actor=actor,
        )
        return Envelope(data=visualization)

    @router.delete("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def delete_visualization(viz_id: UUID, request: Request):
        request_api = api_from_request(request, api)
        actor = actor_from_request(request)
        existing = request_api.get_visualization(viz_id)
        analysis = request_api.get_analysis(existing.analysis_id)
        ensure_project_read(request, analysis.project_id)
        db_session = db_session_from_request(request)
        storage_backend = file_storage_from_request(request)
        row = db_session.get(VisualizationModel, str(viz_id))
        storage_id = (
            UUID(row.asset_storage_id) if row is not None and row.asset_storage_id else None
        )
        visualization = request_api.delete_visualization(viz_id, actor=actor)
        db_session.flush()
        if storage_id is not None:
            request_api.run_after_commit(
                lambda storage_id=storage_id: _delete_stored_visualization_file(
                    storage_backend,
                    storage_id,
                )
            )
        return Envelope(data=visualization)

    return router


def _filter_visualizations_for_access(
    request: Request,
    visualizations: list[Visualization],
) -> list[Visualization]:
    request_api = api_from_request(request)
    visible: list[Visualization] = []
    for visualization in visualizations:
        analysis = request_api.get_analysis(visualization.analysis_id)
        try:
            ensure_project_read(request, analysis.project_id)
        except AuthError:
            continue
        visible.append(visualization)
    return visible
