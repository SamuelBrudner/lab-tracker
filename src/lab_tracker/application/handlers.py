"""Composition root for the request-scoped application boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session as OrmSession

from lab_tracker.artifact_resolution import ResolverRegistry
from lab_tracker.config import Settings

from .catalog_queries import CatalogAccess, CatalogQueries, CatalogRepository
from .context_queries import ContextAccess, ContextQueries, ContextRepository
from .file_commands import (
    DatasetFileCommands,
    DatasetFileRepository,
    FileCommandAccess,
    FileStorage,
    VisualizationFileCommands,
)
from .managed_deletions import (
    DeleteStorage,
    ManagedDeletionAccess,
    ManagedDeletionCommands,
)


class RequestHandlerApi(
    CatalogAccess,
    ContextAccess,
    FileCommandAccess,
    ManagedDeletionAccess,
    Protocol,
):
    """Aggregate of the narrow API roles bound to one request."""


class RequestHandlerRepository(
    CatalogRepository,
    ContextRepository,
    DatasetFileRepository,
    Protocol,
):
    """Aggregate of repository roles used by request handlers."""


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
        api: RequestHandlerApi,
        repository: RequestHandlerRepository,
        session: OrmSession,
        file_storage: FileStorage,
        raw_note_storage: DeleteStorage,
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
                locks=repository,
                session=session,
                file_storage=file_storage,
                raw_note_storage=raw_note_storage,
            ),
        )
