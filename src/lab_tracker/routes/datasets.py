"""Dataset, review, and file routes."""

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
from lab_tracker.models import (
    Dataset,
    DatasetFile,
    DatasetReview,
    DatasetReviewStatus,
    DatasetStatus,
)
from lab_tracker.schemas import (
    DatasetCreate,
    DatasetReviewRequest,
    DatasetReviewUpdate,
    DatasetUpdate,
    Envelope,
    ListEnvelope,
)
from lab_tracker.services.shared import WRITE_ROLES

from .shared import (
    actor_from_request,
    dataset_default_status,
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


def build_datasets_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/datasets",
        response_model=Envelope[Dataset],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_dataset(payload: DatasetCreate, request: Request):
        actor = actor_from_request(request)
        dataset = api.create_dataset(
            project_id=payload.project_id,
            primary_question_id=payload.primary_question_id,
            secondary_question_ids=payload.secondary_question_ids,
            status=payload.status or dataset_default_status(),
            commit_manifest=payload.commit_manifest,
            commit_hash=payload.commit_hash,
            actor=actor,
        )
        return Envelope(data=dataset)

    @router.get("/datasets", response_model=ListEnvelope[Dataset])
    def list_datasets(
        request: Request,
        project_id: UUID | None = None,
        status: DatasetStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        datasets, total = repository_from_request(request).query_datasets(
            project_id=project_id,
            status=status.value if status is not None else None,
            limit=limit,
            offset=offset,
        )
        return list_response(datasets, limit=limit, offset=offset, total=total)

    @router.get("/datasets/{dataset_id}", response_model=Envelope[Dataset])
    def get_dataset(dataset_id: UUID):
        dataset = api.get_dataset(dataset_id)
        return Envelope(data=dataset)

    @router.patch("/datasets/{dataset_id}", response_model=Envelope[Dataset])
    def update_dataset(dataset_id: UUID, payload: DatasetUpdate, request: Request):
        actor = actor_from_request(request)
        dataset = api.update_dataset(
            dataset_id,
            status=payload.status,
            question_links=payload.question_links,
            commit_manifest=payload.commit_manifest,
            commit_hash=payload.commit_hash,
            actor=actor,
        )
        return Envelope(data=dataset)

    @router.delete("/datasets/{dataset_id}", response_model=Envelope[Dataset])
    def delete_dataset(dataset_id: UUID, request: Request):
        actor = actor_from_request(request)
        db_session = db_session_from_request(request)
        storage_backend = file_storage_from_request(request)
        storage_ids = [
            UUID(value)
            for value in db_session.scalars(
                select(DatasetFileModel.storage_id).where(
                    DatasetFileModel.dataset_id == str(dataset_id)
                )
            )
        ]
        dataset = api.delete_dataset(dataset_id, actor=actor)
        db_session.flush()
        for storage_id in storage_ids:
            api.run_after_commit(
                lambda storage_id=storage_id: _delete_stored_dataset_file(
                    storage_backend,
                    storage_id,
                )
            )
        return Envelope(data=dataset)

    @router.post(
        "/datasets/{dataset_id}/review",
        response_model=Envelope[DatasetReview],
        status_code=http_status.HTTP_201_CREATED,
    )
    def request_dataset_review(
        dataset_id: UUID,
        request: Request,
        payload: DatasetReviewRequest | None = None,
    ):
        actor = actor_from_request(request)
        review = api.request_dataset_review(
            dataset_id,
            comments=payload.comments if payload else None,
            actor=actor,
        )
        return Envelope(data=review)

    @router.get("/datasets/{dataset_id}/review", response_model=Envelope[DatasetReview])
    def get_dataset_review(dataset_id: UUID):
        api.get_dataset(dataset_id)
        reviews = api.list_dataset_reviews(dataset_id=dataset_id)
        if not reviews:
            raise NotFoundError("Dataset review does not exist.")
        pending = [review for review in reviews if review.status == DatasetReviewStatus.PENDING]
        review = pending[0] if pending else reviews[-1]
        return Envelope(data=review)

    @router.patch("/datasets/{dataset_id}/review", response_model=Envelope[DatasetReview])
    def resolve_dataset_review(
        dataset_id: UUID,
        payload: DatasetReviewUpdate,
        request: Request,
    ):
        actor = actor_from_request(request)
        api.get_dataset(dataset_id)
        pending = api.list_dataset_reviews(
            dataset_id=dataset_id,
            status=DatasetReviewStatus.PENDING,
        )
        if not pending:
            raise NotFoundError("Pending dataset review does not exist.")
        status_by_action = {
            "approve": DatasetReviewStatus.APPROVED,
            "request_changes": DatasetReviewStatus.CHANGES_REQUESTED,
            "reject": DatasetReviewStatus.REJECTED,
        }
        review = api.resolve_dataset_review(
            pending[0].review_id,
            status=status_by_action[payload.action.value],
            comments=payload.comments,
            actor=actor,
        )
        return Envelope(data=review)

    @router.get("/reviews/pending", response_model=ListEnvelope[DatasetReview])
    def list_pending_reviews(request: Request, limit: int = 50, offset: int = 0):
        actor = actor_from_request(request)
        require_role(actor, WRITE_ROLES)
        validate_pagination(limit, offset)
        reviews, total = repository_from_request(request).query_dataset_reviews(
            status=DatasetReviewStatus.PENDING.value,
            limit=limit,
            offset=offset,
        )
        return list_response(reviews, limit=limit, offset=offset, total=total)

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
        api.run_after_rollback(
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
        api.run_after_commit(
            lambda storage_id=storage_id: _delete_stored_dataset_file(
                storage_backend,
                storage_id,
            )
        )
        return Envelope(data=payload)

    return router
