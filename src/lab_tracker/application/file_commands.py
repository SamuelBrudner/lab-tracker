"""Transactional dataset and visualization file application commands."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.auth import AuthContext
from lab_tracker.config import Settings
from lab_tracker.db_models import AnalysisModel, DatasetFileModel, DatasetModel, VisualizationModel
from lab_tracker.db_types import ensure_uuid
from lab_tracker.errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from lab_tracker.file_storage import StoredFileMetadata
from lab_tracker.models import (
    Analysis,
    Dataset,
    DatasetFile,
    DatasetStatus,
    Visualization,
    utc_now,
)
from lab_tracker.upload_security import validate_upload_content_type

from .types import AssetMutationResult, FileDownload, Page

_logger = logging.getLogger(__name__)

_ABSENT_ASSET_PRECONDITION = "absent"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class FileDeleteStorage(Protocol):
    def delete(self, storage_id: UUID) -> None: ...


class FileStorage(FileDeleteStorage, Protocol):
    """Blob operations used by dataset and visualization file commands."""

    def store_stream(
        self,
        chunks: Iterable[bytes],
        *,
        filename: str,
        content_type: str,
        max_bytes: int | None = None,
    ) -> StoredFileMetadata: ...

    def iter_chunks(
        self,
        storage_id: UUID,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> Iterable[bytes]: ...

    def exists(self, storage_id: UUID) -> bool: ...


class FileCommandAccess(Protocol):
    """Domain and transaction callbacks used by file commands."""

    def get_analysis(self, analysis_id: UUID) -> Analysis: ...

    def get_dataset_for_read(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Dataset: ...

    def get_visualization(self, viz_id: UUID) -> Visualization: ...

    def get_visualization_for_read(
        self,
        viz_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> tuple[Visualization, UUID]: ...

    def require_project_contributor(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...

    def run_after_commit(self, action: Callable[[], None]) -> None: ...

    def run_after_rollback(self, action: Callable[[], None]) -> None: ...


class DatasetFileLocking(Protocol):
    """Transaction-scoped lock intents shared by file writes and cascades."""

    def lock_experiment_updates(self, experiment_ids: Iterable[UUID]) -> None:
        """Serialize Experiment membership before taking Dataset locks."""

    def lock_dataset_file_mutation(
        self,
        project_id: UUID,
        dataset_id: UUID,
    ) -> None: ...

    def lock_dataset_deletion(
        self,
        project_id: UUID,
        dataset_id: UUID,
    ) -> None: ...

    def lock_project_deletion(self, project_id: UUID) -> None: ...


class DatasetFileRepository(DatasetFileLocking, Protocol):
    def query_dataset_files(
        self,
        *,
        dataset_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DatasetFile], int]: ...


def delete_stored_dataset_file(
    storage_backend: FileDeleteStorage,
    storage_id: UUID,
) -> None:
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


def delete_stored_visualization_file(
    storage_backend: FileDeleteStorage,
    storage_id: UUID,
) -> None:
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


def locked_visualization_rows(
    session: OrmSession,
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
        statement = statement.where(
            VisualizationModel.analysis_id == str(analysis_id)
        )
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
    return list(session.scalars(statement))


def locked_visualization_row(
    session: OrmSession,
    viz_id: UUID,
) -> VisualizationModel | None:
    rows = locked_visualization_rows(session, viz_id=viz_id)
    return rows[0] if rows else None


def _enforce_content_length_limit(
    raw_content_length: str | None,
    *,
    max_bytes: int,
) -> None:
    if raw_content_length is None:
        return
    try:
        content_length = int(raw_content_length)
    except ValueError:
        return
    if content_length > max_bytes:
        raise PayloadTooLargeError(
            f"Upload exceeds the configured limit of {max_bytes} bytes."
        )


@dataclass(frozen=True)
class DatasetFileCommands:
    """Dataset blob operations sharing the request transaction and cleanup queues."""

    api: FileCommandAccess
    repository: DatasetFileRepository
    session: OrmSession
    storage: FileStorage
    settings: Settings

    def upload(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext,
        filename: str,
        content_type: str | None,
        chunks: Iterable[bytes],
        raw_content_length: str | None,
    ) -> DatasetFile:
        dataset_row = self.session.get(DatasetModel, str(dataset_id))
        if dataset_row is None:
            raise NotFoundError("Dataset does not exist.")
        project_id = ensure_uuid(dataset_row.project_id)
        self.api.require_project_contributor(
            project_id,
            actor=actor,
        )
        if dataset_row.status != DatasetStatus.STAGED.value:
            raise ValidationError(
                "Files can only be attached while dataset status is staged."
            )
        _enforce_content_length_limit(
            raw_content_length,
            max_bytes=self.settings.max_upload_bytes,
        )

        cleaned_filename = filename.strip()
        if not cleaned_filename:
            raise ValidationError("filename must not be empty.")
        path = cleaned_filename
        self.repository.lock_dataset_file_mutation(project_id, dataset_id)
        dataset_row = self.session.get(DatasetModel, str(dataset_id))
        if dataset_row is None:
            raise NotFoundError("Dataset does not exist.")
        if dataset_row.status != DatasetStatus.STAGED.value:
            raise ValidationError(
                "Files can only be attached while dataset status is staged."
            )
        existing = self.session.scalar(
            select(DatasetFileModel).where(
                DatasetFileModel.dataset_id == str(dataset_id),
                DatasetFileModel.path == path,
            )
        )
        if existing is not None:
            raise ConflictError("Dataset file path already exists.")

        normalized_content_type = validate_upload_content_type(content_type)
        metadata = self.storage.store_stream(
            chunks,
            filename=cleaned_filename,
            content_type=normalized_content_type,
        )
        storage_id = metadata.storage_id
        if metadata.size_bytes <= 0:
            with suppress(Exception):
                self.storage.delete(storage_id)
            raise ValidationError("file must not be empty.")
        try:
            row = DatasetFileModel(
                dataset_id=str(dataset_id),
                storage_id=str(storage_id),
                path=path,
                filename=cleaned_filename,
                content_type=normalized_content_type,
                size_bytes=metadata.size_bytes,
                checksum=metadata.sha256,
            )
            self.session.add(row)
            self.session.flush()
        except IntegrityError as exc:
            with suppress(Exception):
                self.storage.delete(storage_id)
            raise ConflictError("Dataset file could not be registered.") from exc
        except Exception:
            with suppress(Exception):
                self.storage.delete(storage_id)
            raise
        self.api.run_after_rollback(
            partial(
                delete_stored_dataset_file,
                self.storage,
                storage_id,
            )
        )
        return DatasetFile(
            file_id=ensure_uuid(row.file_id),
            path=row.path,
            checksum=row.checksum,
            size_bytes=row.size_bytes,
        )

    def list(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext,
        limit: int,
        offset: int,
    ) -> Page[DatasetFile]:
        self.api.get_dataset_for_read(dataset_id, actor=actor)
        files, total = self.repository.query_dataset_files(
            dataset_id=dataset_id,
            limit=limit,
            offset=offset,
        )
        return Page(items=files, total=total)

    def download(
        self,
        dataset_id: UUID,
        file_id: UUID,
        *,
        actor: AuthContext,
    ) -> FileDownload:
        self.api.get_dataset_for_read(dataset_id, actor=actor)
        row = self.session.get(DatasetFileModel, str(file_id))
        if row is None or str(row.dataset_id) != str(dataset_id):
            raise NotFoundError("Dataset file does not exist.")
        return FileDownload(
            chunks=self.storage.iter_chunks(ensure_uuid(row.storage_id)),
            filename=row.filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
        )

    def delete(
        self,
        dataset_id: UUID,
        file_id: UUID,
        *,
        actor: AuthContext,
    ) -> DatasetFile:
        dataset_row = self.session.get(DatasetModel, str(dataset_id))
        if dataset_row is None:
            raise NotFoundError("Dataset does not exist.")
        project_id = ensure_uuid(dataset_row.project_id)
        self.api.require_project_contributor(
            project_id,
            actor=actor,
        )
        if dataset_row.status != DatasetStatus.STAGED.value:
            raise ValidationError(
                "Files can only be attached while dataset status is staged."
            )
        self.repository.lock_dataset_file_mutation(project_id, dataset_id)
        dataset_row = self.session.get(DatasetModel, str(dataset_id))
        if dataset_row is None:
            raise NotFoundError("Dataset does not exist.")
        if dataset_row.status != DatasetStatus.STAGED.value:
            raise ValidationError(
                "Files can only be attached while dataset status is staged."
            )
        row = self.session.get(DatasetFileModel, str(file_id))
        if row is None or str(row.dataset_id) != str(dataset_id):
            raise NotFoundError("Dataset file does not exist.")
        payload = DatasetFile(
            file_id=file_id,
            path=row.path,
            checksum=row.checksum,
            size_bytes=row.size_bytes,
        )
        storage_id = ensure_uuid(row.storage_id)
        self.session.delete(row)
        self.session.flush()
        self.api.run_after_commit(
            partial(
                delete_stored_dataset_file,
                self.storage,
                storage_id,
            )
        )
        return payload


@dataclass(frozen=True)
class VisualizationFileCommands:
    """Visualization asset operations with locking and compare-and-set semantics."""

    api: FileCommandAccess
    session: OrmSession
    storage: FileStorage
    settings: Settings

    def upload(
        self,
        viz_id: UUID,
        *,
        actor: AuthContext,
        filename: str,
        content_type: str | None,
        chunks: Iterable[bytes],
        checksum_sha256: str | None,
        size_bytes: int | None,
        expected_current_storage_id: str | None,
        raw_content_length: str | None,
    ) -> AssetMutationResult[Visualization]:
        visualization = self.api.get_visualization(viz_id)
        analysis = self.api.get_analysis(visualization.analysis_id)
        self.api.require_project_contributor(analysis.project_id, actor=actor)
        _enforce_content_length_limit(
            raw_content_length,
            max_bytes=self.settings.max_upload_bytes,
        )

        cleaned_filename = filename.strip()
        if not cleaned_filename:
            raise ValidationError("filename must not be empty.")
        normalized_content_type = validate_upload_content_type(content_type)
        declared_asset = _normalize_declared_asset(checksum_sha256, size_bytes)
        asset_precondition = _normalize_asset_precondition(
            expected_current_storage_id
        )

        # Acquire the row lock before any durable storage write. Concurrent
        # retries then reuse the winner or fail their compare-and-set without an
        # orphaned candidate blob.
        row = locked_visualization_row(self.session, viz_id)
        if row is None:
            raise NotFoundError("Visualization does not exist.")

        if declared_asset is not None:
            declared_checksum, declared_size = declared_asset
            if _stored_row_asset_matches(
                row,
                self.storage,
                filename=cleaned_filename,
                content_type=normalized_content_type,
                size_bytes=declared_size,
                checksum=declared_checksum,
            ):
                return AssetMutationResult(
                    entity=self.api.get_visualization(viz_id),
                    outcome="reused",
                )

        _enforce_asset_precondition(row, asset_precondition)
        old_storage_id = (
            ensure_uuid(row.asset_storage_id) if row.asset_storage_id else None
        )
        metadata = self.storage.store_stream(
            chunks,
            filename=cleaned_filename,
            content_type=normalized_content_type,
            max_bytes=self.settings.max_upload_bytes,
        )
        storage_id = metadata.storage_id
        if metadata.size_bytes <= 0:
            with suppress(Exception):
                self.storage.delete(storage_id)
            raise ValidationError("file must not be empty.")
        try:
            _validate_stored_metadata(metadata, declared_asset)
        except Exception:
            delete_stored_visualization_file(self.storage, storage_id)
            raise

        if old_storage_id is not None and _metadata_matches_row(
            metadata,
            row,
            self.storage,
        ):
            delete_stored_visualization_file(self.storage, storage_id)
            return AssetMutationResult(
                entity=self.api.get_visualization(viz_id),
                outcome="reused",
            )

        try:
            row.asset_storage_id = storage_id
            row.asset_filename = cleaned_filename
            row.asset_content_type = normalized_content_type
            row.asset_size_bytes = metadata.size_bytes
            row.asset_checksum = metadata.sha256
            row.updated_at = utc_now()
            self.session.flush()
        except Exception:
            with suppress(Exception):
                self.storage.delete(storage_id)
            raise

        self.api.run_after_rollback(
            partial(
                delete_stored_visualization_file,
                self.storage,
                storage_id,
            )
        )
        if old_storage_id is not None:
            self.api.run_after_commit(
                partial(
                    delete_stored_visualization_file,
                    self.storage,
                    old_storage_id,
                )
            )
        return AssetMutationResult(
            entity=self.api.get_visualization(viz_id),
            outcome="replaced" if old_storage_id is not None else "created",
        )

    def download(
        self,
        viz_id: UUID,
        *,
        actor: AuthContext,
    ) -> FileDownload:
        self.api.get_visualization_for_read(viz_id, actor=actor)
        row = self.session.get(VisualizationModel, str(viz_id))
        if row is None:
            raise NotFoundError("Visualization does not exist.")
        if not row.asset_storage_id:
            raise NotFoundError("Visualization file does not exist.")
        return FileDownload(
            chunks=self.storage.iter_chunks(ensure_uuid(row.asset_storage_id)),
            filename=row.asset_filename or "figure",
            content_type=row.asset_content_type or "application/octet-stream",
            size_bytes=row.asset_size_bytes or 0,
            nosniff=True,
        )


def _normalize_declared_asset(
    checksum_sha256: str | None,
    size_bytes: int | None,
) -> tuple[str, int] | None:
    if checksum_sha256 is None and size_bytes is None:
        return None
    if checksum_sha256 is None or size_bytes is None:
        raise ValidationError(
            "checksum_sha256 and size_bytes must be supplied together."
        )
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
    storage_backend: FileStorage,
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
    storage_id = row.asset_storage_id
    if storage_id is None:
        return False
    return bool(storage_backend.exists(storage_id))


def _metadata_matches_row(
    metadata: StoredFileMetadata,
    row: VisualizationModel,
    storage_backend: FileStorage,
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
