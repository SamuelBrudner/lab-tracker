"""Visualization routes."""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Response, UploadFile
from sqlalchemy import select
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import StreamingResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.db_models import AnalysisModel, VisualizationModel
from lab_tracker.db_types import ensure_uuid
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.file_storage import StoredFileMetadata
from lab_tracker.models import UsageEventResourceType, Visualization, utc_now
from lab_tracker.schemas import Envelope, ListEnvelope, VisualizationCreate, VisualizationUpdate
from lab_tracker.upload_security import (
    enforce_request_content_length_limit,
    validate_upload_content_type,
)

from .shared import (
    CreatedByFilter,
    accessible_project_ids_from_request,
    actor_from_request,
    api_from_request,
    content_disposition_header,
    db_session_from_request,
    ensure_project_contributor,
    ensure_project_read,
    file_storage_from_request,
    list_response,
    record_usage_view,
    repository_from_request,
    validate_pagination,
)

_logger = logging.getLogger(__name__)

_ABSENT_ASSET_PRECONDITION = "absent"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


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


def _locked_visualization_rows(
    db_session,
    *,
    viz_id: UUID | None = None,
    analysis_id: UUID | None = None,
    project_id: UUID | None = None,
) -> list[VisualizationModel]:
    """Reload and lock visualization rows in stable order until transaction end."""

    statement = select(VisualizationModel)
    if viz_id is not None:
        statement = statement.where(VisualizationModel.viz_id == str(viz_id))
    if analysis_id is not None:
        statement = statement.where(VisualizationModel.analysis_id == str(analysis_id))
    if project_id is not None:
        statement = statement.join(
            AnalysisModel,
            AnalysisModel.analysis_id == VisualizationModel.analysis_id,
        ).where(AnalysisModel.project_id == str(project_id))
    statement = (
        statement.order_by(VisualizationModel.viz_id)
        .with_for_update(of=VisualizationModel)
        .execution_options(populate_existing=True)
    )
    return list(db_session.scalars(statement))


def _locked_visualization_row(db_session, viz_id: UUID) -> VisualizationModel | None:
    """Reload and lock one visualization until the request transaction completes."""

    rows = _locked_visualization_rows(db_session, viz_id=viz_id)
    return rows[0] if rows else None


def _normalize_declared_asset(
    checksum_sha256: str | None,
    size_bytes: int | None,
) -> tuple[str, int] | None:
    if checksum_sha256 is None and size_bytes is None:
        return None
    if checksum_sha256 is None or size_bytes is None:
        raise ValidationError("checksum_sha256 and size_bytes must be supplied together.")
    checksum = checksum_sha256.strip().lower()
    if not _SHA256_RE.fullmatch(checksum):
        raise ValidationError("checksum_sha256 must be a SHA-256 hex digest.")
    if isinstance(size_bytes, bool) or size_bytes <= 0:
        raise ValidationError("size_bytes must be greater than zero.")
    return checksum, size_bytes


def _normalize_asset_precondition(value: str | None) -> UUID | str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if cleaned.casefold() == _ABSENT_ASSET_PRECONDITION:
        return _ABSENT_ASSET_PRECONDITION
    try:
        return UUID(cleaned)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "expected_current_storage_id must be 'absent' or a storage UUID."
        ) from exc


def _row_asset_matches(
    row: VisualizationModel,
    *,
    filename: str,
    content_type: str,
    size_bytes: int,
    checksum: str,
) -> bool:
    return (
        row.asset_storage_id is not None
        and row.asset_filename == filename
        and (row.asset_content_type or "").casefold() == content_type.casefold()
        and row.asset_size_bytes == size_bytes
        and (row.asset_checksum or "").casefold() == checksum.casefold()
    )


def _stored_row_asset_matches(
    row: VisualizationModel,
    storage_backend: object,
    *,
    filename: str,
    content_type: str,
    size_bytes: int,
    checksum: str,
) -> bool:
    if not _row_asset_matches(
        row,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        checksum=checksum,
    ):
        return False
    return bool(storage_backend.exists(ensure_uuid(row.asset_storage_id)))


def _metadata_matches_row(
    metadata: StoredFileMetadata,
    row: VisualizationModel,
    storage_backend: object,
) -> bool:
    return _stored_row_asset_matches(
        row,
        storage_backend,
        filename=metadata.filename,
        content_type=metadata.content_type,
        size_bytes=metadata.size_bytes,
        checksum=metadata.sha256,
    )


def _enforce_asset_precondition(
    row: VisualizationModel,
    expected_current_storage_id: UUID | str | None,
) -> None:
    if expected_current_storage_id is None:
        return
    current_storage_id = (
        ensure_uuid(row.asset_storage_id) if row.asset_storage_id is not None else None
    )
    if expected_current_storage_id == _ABSENT_ASSET_PRECONDITION:
        matches = current_storage_id is None
    else:
        matches = current_storage_id == expected_current_storage_id
    if not matches:
        raise ConflictError(
            "Visualization asset changed before the conditional upload; reload and retry."
        )


def _validate_stored_metadata(
    metadata: StoredFileMetadata,
    declared_asset: tuple[str, int] | None,
) -> None:
    if declared_asset is None:
        return
    declared_checksum, declared_size = declared_asset
    mismatches: list[str] = []
    if metadata.size_bytes != declared_size:
        mismatches.append("size_bytes")
    if metadata.sha256.casefold() != declared_checksum.casefold():
        mismatches.append("checksum_sha256")
    if mismatches:
        raise ValidationError(
            "Uploaded file does not match its declared integrity metadata "
            f"({', '.join(mismatches)})."
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
        if project_id is not None:
            ensure_project_read(request, project_id)
            project_ids = None
        else:
            project_ids = accessible_project_ids_from_request(request)
        visualizations, total = repository_from_request(request).query_visualizations(
            project_id=project_id,
            project_ids=project_ids,
            analysis_id=analysis_id,
            claim_id=claim_id,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return list_response(visualizations, limit=limit, offset=offset, total=total)

    @router.get("/visualizations/{viz_id}", response_model=Envelope[Visualization])
    def get_visualization(viz_id: UUID, request: Request):
        visualization = api_from_request(request, api).get_visualization(viz_id)
        analysis = api_from_request(request, api).get_analysis(visualization.analysis_id)
        ensure_project_read(request, analysis.project_id)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.VISUALIZATION,
            resource_id=visualization.viz_id,
            project_id=analysis.project_id,
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
        request_api = api_from_request(request, api)

        db_session = db_session_from_request(request)
        storage_backend = file_storage_from_request(request)

        visualization = request_api.get_visualization(viz_id)
        analysis = request_api.get_analysis(visualization.analysis_id)
        ensure_project_contributor(request, analysis.project_id)
        enforce_request_content_length_limit(
            request,
            max_bytes=request.app.state.settings.max_upload_bytes,
        )

        filename = (file.filename or "").strip()
        if not filename:
            raise ValidationError("filename must not be empty.")
        content_type = validate_upload_content_type(file.content_type)
        declared_asset = _normalize_declared_asset(checksum_sha256, size_bytes)
        asset_precondition = _normalize_asset_precondition(expected_current_storage_id)

        # The row lock is deliberately acquired before any durable storage write.
        # Concurrent retries then either observe and reuse the winner or fail their
        # compare-and-set precondition without creating an orphaned candidate blob.
        row = _locked_visualization_row(db_session, viz_id)
        if row is None:
            raise NotFoundError("Visualization does not exist.")

        if declared_asset is not None:
            declared_checksum, declared_size = declared_asset
            if _stored_row_asset_matches(
                row,
                storage_backend,
                filename=filename,
                content_type=content_type,
                size_bytes=declared_size,
                checksum=declared_checksum,
            ):
                response.status_code = http_status.HTTP_200_OK
                return Envelope(
                    data=request_api.get_visualization(viz_id),
                    meta={"asset_outcome": "reused"},
                )

        _enforce_asset_precondition(row, asset_precondition)

        old_storage_id = ensure_uuid(row.asset_storage_id) if row.asset_storage_id else None
        metadata = storage_backend.store_stream(
            iter(lambda: file.file.read(1024 * 1024), b""),
            filename=filename,
            content_type=content_type,
            max_bytes=request.app.state.settings.max_upload_bytes,
        )
        storage_id = metadata.storage_id
        if metadata.size_bytes <= 0:
            with suppress(Exception):
                storage_backend.delete(storage_id)
            raise ValidationError("file must not be empty.")

        try:
            _validate_stored_metadata(metadata, declared_asset)
        except Exception:
            _delete_stored_visualization_file(storage_backend, storage_id)
            raise

        if old_storage_id is not None and _metadata_matches_row(
            metadata,
            row,
            storage_backend,
        ):
            _delete_stored_visualization_file(storage_backend, storage_id)
            response.status_code = http_status.HTTP_200_OK
            return Envelope(
                data=request_api.get_visualization(viz_id),
                meta={"asset_outcome": "reused"},
            )

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

        return Envelope(
            data=request_api.get_visualization(viz_id),
            meta={"asset_outcome": "replaced" if old_storage_id is not None else "created"},
        )

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
            "Content-Disposition": content_disposition_header(
                "attachment", row.asset_filename or "figure"
            ),
            "Content-Length": str(row.asset_size_bytes or 0),
            "X-Content-Type-Options": "nosniff",
        }
        return StreamingResponse(
            storage_backend.iter_chunks(ensure_uuid(row.asset_storage_id)),
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
        row = _locked_visualization_row(db_session, viz_id)
        if row is None:
            raise NotFoundError("Visualization does not exist.")
        storage_id = ensure_uuid(row.asset_storage_id) if row.asset_storage_id else None
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
