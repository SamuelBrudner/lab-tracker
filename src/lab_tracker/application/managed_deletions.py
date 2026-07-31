"""Cascade-aware delete commands and their deferred storage cleanup."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.auth import AuthContext
from lab_tracker.db_models import (
    AnalysisModel,
    DatasetFileModel,
    DatasetModel,
    ExperimentDatasetModel,
    NoteModel,
    ProjectModel,
)
from lab_tracker.db_types import ensure_uuid
from lab_tracker.errors import NotFoundError
from lab_tracker.models import Analysis, Dataset, Project, Visualization

from . import file_commands
from .file_commands import (
    DatasetFileLocking,
    delete_stored_dataset_file,
    delete_stored_visualization_file,
)

_logger = logging.getLogger(__name__)


class DeleteStorage(Protocol):
    def delete(self, storage_id: UUID) -> None: ...


class ManagedDeletionAccess(Protocol):
    """Domain commands and transaction callbacks required by managed deletes."""

    def require_project_owner(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...

    def require_project_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...

    def get_dataset(self, dataset_id: UUID) -> Dataset: ...

    def get_analysis(self, analysis_id: UUID) -> Analysis: ...

    def get_visualization(self, viz_id: UUID) -> Visualization: ...

    def delete_project(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Project: ...

    def delete_dataset(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext | None,
        experiment_dataset_locks_held: bool = False,
    ) -> Dataset: ...

    def delete_analysis(
        self,
        analysis_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Analysis: ...

    def delete_visualization(
        self,
        viz_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Visualization: ...

    def run_after_commit(self, action: Callable[[], None]) -> None: ...


def _delete_project_scoped_file(
    storage_backend: DeleteStorage,
    storage_id: UUID,
) -> None:
    try:
        storage_backend.delete(storage_id)
    except NotFoundError:
        return
    except Exception as exc:
        _logger.warning(
            "Failed to delete project-scoped storage object %s: %s",
            storage_id,
            exc,
            exc_info=True,
        )


@dataclass(frozen=True)
class ManagedDeletionCommands:
    """Deletes whose database cascade must coordinate with blob cleanup."""

    api: ManagedDeletionAccess
    locks: DatasetFileLocking
    session: OrmSession
    file_storage: DeleteStorage
    raw_note_storage: DeleteStorage

    def delete_project(
        self,
        project_id: UUID,
        *,
        actor: AuthContext,
    ) -> Project:
        self.api.require_project_owner(project_id, actor=actor)
        self.locks.lock_project_deletion(project_id)
        locked_project = self.session.scalar(
            select(ProjectModel)
            .where(ProjectModel.project_id == str(project_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_project is None:
            raise NotFoundError("Project does not exist.")
        self.session.scalars(
            select(AnalysisModel)
            .where(AnalysisModel.project_id == str(project_id))
            .order_by(AnalysisModel.analysis_id)
            .with_for_update(of=AnalysisModel)
            .execution_options(populate_existing=True)
        ).all()
        visualization_rows = file_commands.locked_visualization_rows(
            self.session,
            project_id=project_id,
        )
        dataset_file_storage_ids = [
            ensure_uuid(value)
            for value in self.session.scalars(
                select(DatasetFileModel.storage_id)
                .join(
                    DatasetModel,
                    DatasetModel.dataset_id == DatasetFileModel.dataset_id,
                )
                .where(DatasetModel.project_id == str(project_id))
            )
        ]
        raw_note_storage_ids = [
            ensure_uuid(value)
            for value in self.session.scalars(
                select(NoteModel.raw_storage_id).where(
                    NoteModel.project_id == str(project_id),
                    NoteModel.raw_storage_id.is_not(None),
                )
            )
            if value is not None
        ]
        visualization_storage_ids = [
            ensure_uuid(row.asset_storage_id)
            for row in visualization_rows
            if row.asset_storage_id is not None
        ]
        project = self.api.delete_project(project_id, actor=actor)
        self.session.flush()
        for storage_id in dataset_file_storage_ids:
            self.api.run_after_commit(
                partial(
                    _delete_project_scoped_file,
                    self.file_storage,
                    storage_id,
                )
            )
        for storage_id in raw_note_storage_ids:
            self.api.run_after_commit(
                partial(
                    _delete_project_scoped_file,
                    self.raw_note_storage,
                    storage_id,
                )
            )
        for storage_id in visualization_storage_ids:
            self.api.run_after_commit(
                partial(
                    _delete_project_scoped_file,
                    self.file_storage,
                    storage_id,
                )
            )
        return project

    def delete_dataset(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext,
    ) -> Dataset:
        existing = self.api.get_dataset(dataset_id)
        self.api.require_project_read(existing.project_id, actor=actor)
        experiment_ids = [
            ensure_uuid(value)
            for value in self.session.scalars(
                select(ExperimentDatasetModel.experiment_id).where(
                    ExperimentDatasetModel.dataset_id == str(dataset_id)
                )
            )
        ]
        self.locks.lock_experiment_updates(experiment_ids)
        self.locks.lock_dataset_deletion(existing.project_id, dataset_id)
        existing = self.api.get_dataset(dataset_id)
        self.api.require_project_read(existing.project_id, actor=actor)
        storage_ids = [
            ensure_uuid(value)
            for value in self.session.scalars(
                select(DatasetFileModel.storage_id).where(
                    DatasetFileModel.dataset_id == str(dataset_id)
                )
            )
        ]
        dataset = self.api.delete_dataset(
            dataset_id,
            actor=actor,
            experiment_dataset_locks_held=True,
        )
        self.session.flush()
        for storage_id in storage_ids:
            self.api.run_after_commit(
                partial(
                    delete_stored_dataset_file,
                    self.file_storage,
                    storage_id,
                )
            )
        return dataset

    def delete_analysis(
        self,
        analysis_id: UUID,
        *,
        actor: AuthContext,
    ) -> Analysis:
        existing = self.api.get_analysis(analysis_id)
        self.api.require_project_read(existing.project_id, actor=actor)
        locked_analysis = self.session.scalar(
            select(AnalysisModel)
            .where(AnalysisModel.analysis_id == str(analysis_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_analysis is None:
            raise NotFoundError("Analysis does not exist.")
        visualization_rows = file_commands.locked_visualization_rows(
            self.session,
            analysis_id=analysis_id,
        )
        storage_ids = [
            ensure_uuid(row.asset_storage_id)
            for row in visualization_rows
            if row.asset_storage_id is not None
        ]
        analysis = self.api.delete_analysis(analysis_id, actor=actor)
        self.session.flush()
        for storage_id in storage_ids:
            self.api.run_after_commit(
                partial(
                    delete_stored_visualization_file,
                    self.file_storage,
                    storage_id,
                )
            )
        return analysis

    def delete_visualization(
        self,
        viz_id: UUID,
        *,
        actor: AuthContext,
    ) -> Visualization:
        existing = self.api.get_visualization(viz_id)
        analysis = self.api.get_analysis(existing.analysis_id)
        self.api.require_project_read(analysis.project_id, actor=actor)
        row = file_commands.locked_visualization_row(self.session, viz_id)
        if row is None:
            raise NotFoundError("Visualization does not exist.")
        storage_id = (
            ensure_uuid(row.asset_storage_id) if row.asset_storage_id else None
        )
        visualization = self.api.delete_visualization(viz_id, actor=actor)
        self.session.flush()
        if storage_id is not None:
            self.api.run_after_commit(
                partial(
                    delete_stored_visualization_file,
                    self.file_storage,
                    storage_id,
                )
            )
        return visualization
