"""Composition root for the request-scoped application boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session as OrmSession

from lab_tracker.artifact_resolution import ResolverRegistry
from lab_tracker.config import Settings
from lab_tracker.semantic_retrieval import EmbeddingClient
from lab_tracker.store_authority_use import StoreAuthoritySnapshotProvider
from lab_tracker.store_health import StoreProbe

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
from .store_health_queries import StoreHealthAccess, StoreHealthQueries


class RequestHandlerApi(
    CatalogAccess,
    ContextAccess,
    FileCommandAccess,
    ManagedDeletionAccess,
    StoreHealthAccess,
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
    store_health: StoreHealthQueries
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
        store_health_checker: StoreProbe,
        release_read_scope: Callable[[], None],
        store_authority_snapshot_provider: StoreAuthoritySnapshotProvider,
        semantic_client: EmbeddingClient | None = None,
    ) -> RequestHandlers:
        """Bind every handler to the middleware's existing request resources."""

        return cls(
            catalogs=CatalogQueries(api=api, repository=repository),
            context=ContextQueries(
                api=api,
                repository=repository,
                session=session,
                release_read_scope=release_read_scope,
                resolver_registry=resolver_registry,
                store_authority_snapshot_provider=store_authority_snapshot_provider,
                settings=settings,
                semantic_client=semantic_client,
            ),
            store_health=StoreHealthQueries(
                api=api,
                checker=store_health_checker,
                release_read_scope=release_read_scope,
                store_authority_snapshot_provider=store_authority_snapshot_provider,
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
