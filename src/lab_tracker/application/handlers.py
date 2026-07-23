"""Composition root for the request-scoped application boundary."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session as OrmSession

from lab_tracker.api import LabTrackerAPI
from lab_tracker.artifact_resolution import ResolverRegistry
from lab_tracker.config import Settings
from lab_tracker.file_storage import FileStorageBackend
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.repository import LabTrackerRepository

from .catalog_queries import CatalogQueries
from .context_queries import ContextQueries
from .file_commands import DatasetFileCommands, VisualizationFileCommands
from .managed_deletions import ManagedDeletionCommands


@dataclass(frozen=True)
class RequestHandlers:
    """One request's typed commands and queries over one transaction identity."""

    catalogs: CatalogQueries
    context: ContextQueries
    dataset_files: DatasetFileCommands
    visualization_files: VisualizationFileCommands
    deletions: ManagedDeletionCommands

    @classmethod
    def compose(
        cls,
        *,
        api: LabTrackerAPI,
        repository: LabTrackerRepository,
        session: OrmSession,
        file_storage: FileStorageBackend,
        raw_note_storage: LocalNoteStorage,
        settings: Settings,
        resolver_registry: ResolverRegistry | None,
    ) -> RequestHandlers:
        """Bind every handler to the middleware's existing request resources."""

        return cls(
            catalogs=CatalogQueries(api=api, repository=repository),
            context=ContextQueries(
                api=api,
                repository=repository,
                session=session,
                resolver_registry=resolver_registry,
            ),
            dataset_files=DatasetFileCommands(
                api=api,
                repository=repository,
                session=session,
                storage=file_storage,
                settings=settings,
            ),
            visualization_files=VisualizationFileCommands(
                api=api,
                session=session,
                storage=file_storage,
                settings=settings,
            ),
            deletions=ManagedDeletionCommands(
                api=api,
                session=session,
                file_storage=file_storage,
                raw_note_storage=raw_note_storage,
            ),
        )
