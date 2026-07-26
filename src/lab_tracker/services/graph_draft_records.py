"""Persistence-facing graph-draft records shared by lifecycle coordinators."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, OpaqueTargetNotFoundError
from lab_tracker.models import (
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftMode,
)
from lab_tracker.services.base import BaseService, ServiceContext


class GraphDraftReadAuthorization(Protocol):
    """Narrow authorization role needed for opaque graph-draft reads."""

    def can_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> bool: ...


class GraphDraftRecords(BaseService):
    """Own graph-draft record reads and the shared change-set save invariant."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        authorization: GraphDraftReadAuthorization,
    ) -> None:
        super().__init__(context)
        self.authorization = authorization

    def save_graph_change_set(self, change_set: GraphChangeSet) -> None:
        change_set.operation_count = len(change_set.operations)
        with self.unit_of_work() as repository:
            repository.graph_change_sets.save(change_set)

    def get_graph_draft_batch_run(self, run_id: UUID) -> GraphDraftBatchRun:
        run = self.repository.graph_draft_batch_runs.get(run_id)
        if run is None:
            raise NotFoundError("Graph draft batch run does not exist.")
        return run

    def list_graph_draft_batch_runs(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphDraftBatchRunStatus | None = None,
    ) -> list[GraphDraftBatchRun]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_graph_draft_batch_runs(
                project_id=project_id,
                status=status.value if status is not None else None,
                limit=None,
                offset=0,
            ),
        )

    def get_graph_change_set(self, change_set_id: UUID) -> GraphChangeSet:
        change_set = self.repository.graph_change_sets.get(change_set_id)
        if change_set is None:
            raise NotFoundError("Graph draft does not exist.")
        return change_set

    def get_graph_change_set_for_read(
        self,
        change_set_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        """Authorize from scalar scope before materializing a graph draft."""

        project_id = self.repository.graph_change_sets.project_id_for(change_set_id)
        if project_id is None or not self.authorization.can_read(project_id, actor=actor):
            raise OpaqueTargetNotFoundError("Graph draft does not exist.")
        try:
            return self.get_graph_change_set(change_set_id)
        except NotFoundError as exc:
            raise OpaqueTargetNotFoundError("Graph draft does not exist.") from exc

    def list_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
        draft_mode: GraphDraftMode | None = None,
        batch_key: str | None = None,
    ) -> list[GraphChangeSet]:
        change_sets, _ = self.query_graph_change_sets(
            project_id=project_id,
            status=status,
            source_note_id=source_note_id,
            draft_mode=draft_mode,
            batch_key=batch_key,
            limit=None,
            offset=0,
        )
        return change_sets

    def query_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
        draft_mode: GraphDraftMode | None = None,
        batch_key: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_operations: bool = True,
    ) -> tuple[list[GraphChangeSet], int]:
        return self.repository.query_graph_change_sets(
            project_id=project_id,
            project_ids=project_ids,
            status=status.value if status is not None else None,
            source_note_id=source_note_id,
            draft_mode=draft_mode.value if draft_mode is not None else None,
            batch_key=batch_key,
            limit=limit,
            offset=offset,
            include_operations=include_operations,
        )

    def list_batch_graph_drafts(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
    ) -> list[GraphChangeSet]:
        return self.list_graph_change_sets(
            project_id=project_id,
            status=status,
            draft_mode=GraphDraftMode.GRAPH_BATCH,
        )
