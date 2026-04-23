"""Dataset file routes."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, File, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import StreamingResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import require_role
from lab_tracker.db_models import DatasetFileModel, DatasetModel
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.models import DatasetFile, DatasetStatus
from lab_tracker.schemas import Envelope, ListEnvelope
from lab_tracker.services.shared import WRITE_ROLES

from .shared import (
    actor_from_request,
    api_from_request,
    db_session_from_request,
    file_storage_from_request,
    list_response,
    repository_from_request,
    safe_attachment_filename,
    validate_pagination,
)

_logger = logging.getLogger(__name__)


def _delete_stored_dataset_file(storage_backend: object, storage_id: UUID) -> None:
    try:
        storage_backend.delete(storage_id)
    except NotFoundError:
        return
    except Exception as exc:
        _logger.warning(
            "Failed to delete dataset file storage object %s: %s",
            storage_id,
            exc,
            exc_info=True,
        )


def build_dataset_files_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/datasets/{dataset_id}/files",
        response_model=Envelope[DatasetFile],
        status_code=http_status.HTTP_201_CREATED,
    )
    async def upload_dataset_file(
        dataset_id: UUID,
        request: Request,
        file: UploadFile = File(...),
    ):
        request_api = api_from_request(request, api)
        actor = actor_from_request(request)
        require_role(actor, WRITE_ROLES)

        db_session = db_session_from_request(request)
        storage_backend = file_storage_from_request(request)

        dataset_row = db_session.get(DatasetModel, str(dataset_id))
        if dataset_row is None:
            raise NotFoundError("Dataset does not exist.")
        if dataset_row.status != DatasetStatus.STAGED.value:
            raise ValidationError("Files can only be attached while dataset status is staged.")

        filename = (file.filename or "").strip()
        if not filename:
            raise ValidationError("filename must not be empty.")
        path = filename
        existing = db_session.scalar(
            select(DatasetFileModel).where(
                DatasetFileModel.dataset_id == str(dataset_id),
                DatasetFileModel.path == path,
            )
        )
        if existing is not None:
            raise ConflictError("Dataset file path already exists.")

        content_type = (file.content_type or "application/octet-stream").strip()
        if not content_type:
            content_type = "application/octet-stream"
        metadata = storage_backend.store_stream(
            iter(lambda: file.file.read(1024 * 1024), b""),
            filename=filename,
            content_type=content_type,
        )
        storage_id = metadata.storage_id
        if metadata.size_bytes <= 0:
            try:
                storage_backend.delete(storage_id)
            except Exception:
                pass
            raise ValidationError("file must not be empty.")
        try:
            row = DatasetFileModel(
                dataset_id=str(dataset_id),
                storage_id=str(storage_id),
                path=path,
                filename=filename,
                content_type=content_type,
                size_bytes=metadata.size_bytes,
                checksum=metadata.sha256,
            )
            db_session.add(row)
            db_session.flush()
        except IntegrityError as exc:
            try:
                storage_backend.delete(storage_id)
            except Exception:
                pass
            raise ConflictError("Dataset file could not be registered.") from exc
        except Exception:
            try:
                storage_backend.delete(storage_id)
            except Exception:
                pass
            raise
        request_api.run_after_rollback(
            lambda storage_id=storage_id: _delete_stored_dataset_file(
                storage_backend,
                storage_id,
            )
        )

        return Envelope(
            data=DatasetFile(
                file_id=UUID(row.file_id),
                path=row.path,
                checksum=row.checksum,
                size_bytes=row.size_bytes,
            )
        )

    @router.get("/datasets/{dataset_id}/files", response_model=ListEnvelope[DatasetFile])
    def list_dataset_files(
        dataset_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        db_session = db_session_from_request(request)

        dataset_row = db_session.get(DatasetModel, str(dataset_id))
        if dataset_row is None:
            raise NotFoundError("Dataset does not exist.")

        files, total = repository_from_request(request).query_dataset_files(
            dataset_id=dataset_id,
            limit=limit,
            offset=offset,
        )
        return list_response(files, limit=limit, offset=offset, total=total)

    @router.get("/datasets/{dataset_id}/files/{file_id}/download")
    def download_dataset_file(
        dataset_id: UUID,
        file_id: UUID,
        request: Request,
    ):
        actor_from_request(request)

        db_session = db_session_from_request(request)
        storage_backend = file_storage_from_request(request)

        dataset_row = db_session.get(DatasetModel, str(dataset_id))
        if dataset_row is None:
            raise NotFoundError("Dataset does not exist.")

        row = db_session.get(DatasetFileModel, str(file_id))
        if row is None or row.dataset_id != str(dataset_id):
            raise NotFoundError("Dataset file does not exist.")

        headers = {
            "Content-Disposition": (
                f'attachment; filename="{safe_attachment_filename(row.filename)}"'
            ),
            "Content-Length": str(row.size_bytes),
        }
        return StreamingResponse(
            storage_backend.iter_chunks(UUID(row.storage_id)),
            media_type=row.content_type,
            headers=headers,
        )

    @router.delete("/datasets/{dataset_id}/files/{file_id}", response_model=Envelope[DatasetFile])
    def delete_dataset_file(
        dataset_id: UUID,
        file_id: UUID,
        request: Request,
    ):
        request_api = api_from_request(request, api)
        actor = actor_from_request(request)
        require_role(actor, WRITE_ROLES)

        db_session = db_session_from_request(request)
        storage_backend = file_storage_from_request(request)

        dataset_row = db_session.get(DatasetModel, str(dataset_id))
        if dataset_row is None:
            raise NotFoundError("Dataset does not exist.")
        if dataset_row.status != DatasetStatus.STAGED.value:
            raise ValidationError("Files can only be attached while dataset status is staged.")

        row = db_session.get(DatasetFileModel, str(file_id))
        if row is None or row.dataset_id != str(dataset_id):
            raise NotFoundError("Dataset file does not exist.")

        payload = DatasetFile(
            file_id=file_id,
            path=row.path,
            checksum=row.checksum,
            size_bytes=row.size_bytes,
        )
        storage_id = UUID(row.storage_id)
        db_session.delete(row)
        db_session.flush()
        request_api.run_after_commit(
            lambda storage_id=storage_id: _delete_stored_dataset_file(
                storage_backend,
                storage_id,
            )
        )
        return Envelope(data=payload)

    return router
