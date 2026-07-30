"""Repository interfaces for persistence backends."""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    ClaimEdge,
    Dataset,
    DatasetFile,
    DataStore,
    EntityVersion,
    EvidenceBundleRecord,
    ExplorationNode,
    Goal,
    GoalLink,
    GraphChangeSet,
    GraphDraftBatchRun,
    GraphDraftBatchSettings,
    GroupMembership,
    Note,
    NoteMetadataScalar,
    OwnershipReassignment,
    Project,
    ProjectGroup,
    ProjectMembership,
    ProvenanceLink,
    Question,
    QuestionRefactor,
    RecordExportEvent,
    RecordExportRecords,
    ReviewEmailDelivery,
    Session,
    SupervisionEdge,
    UsageEvent,
    UsageEventRollup,
    Visualization,
)

EntityT = TypeVar("EntityT")


class EvidenceBundleKeyRaceError(Exception):
    """The scoped evidence-bundle idempotency key lost a concurrent insert race."""


class DataStoreNameRaceError(Exception):
    """A data-store name was inserted concurrently within the same exact scope."""


class DataStoreForeignKeyRaceError(Exception):
    """A data-store registration target disappeared before its insert."""


class DataStoreInsertError(Exception):
    """A data-store insert failed for an unclassified persistence reason."""


class EntityRepository(Protocol, Generic[EntityT]):
    """CRUD contract for a single entity type."""

    def get(self, entity_id: UUID) -> EntityT | None:
        """Return one entity by ID, or None when it does not exist."""

    def list(self) -> list[EntityT]:
        """Return every entity of this type."""

    def save(self, entity: EntityT) -> None:
        """Persist an entity create/update operation."""

    def delete(self, entity_id: UUID) -> EntityT | None:
        """Delete one entity by ID and return the removed value."""


class NoteRepository(EntityRepository[Note], Protocol):
    """Note persistence operations that preserve concurrent human edits."""

    def try_claim_auto_transcription(
        self,
        note_id: UUID,
        *,
        claim_id: UUID,
        claimed_at: datetime,
        stale_before: datetime,
    ) -> Note | None:
        """Atomically claim one transcript-free note for a provider call.

        Return the claimed current note, or ``None`` when another live claim
        or an existing transcript makes the automatic call unnecessary.
        """

    def apply_auto_transcription_result(
        self,
        note_id: UUID,
        *,
        claim_id: UUID,
        claimed_updated_at: datetime,
        text: str,
        metadata_updates: dict[str, NoteMetadataScalar],
        updated_at: datetime,
    ) -> Note | None:
        """Apply a provider result only while the caller still owns the claim."""

    def release_auto_transcription_claim(
        self,
        note_id: UUID,
        *,
        claim_id: UUID,
        updated_at: datetime,
    ) -> Note | None:
        """Release a matching claim after provider or local preparation failure."""

    def apply_transcription_result(
        self,
        note_id: UUID,
        *,
        text: str,
        metadata_updates: dict[str, NoteMetadataScalar],
        updated_at: datetime,
        expected_updated_at: datetime | None = None,
        only_if_unchanged: bool = False,
    ) -> Note | None:
        """Merge a transcript into the latest note state.

        When ``only_if_unchanged`` is true, return the current note without
        applying the model result if the note changed after
        ``expected_updated_at`` or already has a transcript.
        """


class EvidenceBundleRepository(Protocol):
    """Append-only persistence needed by the atomic evidence-bundle command."""

    def get(self, entity_id: UUID) -> EvidenceBundleRecord | None:
        """Return one durable idempotency record by ID."""

    def list(self) -> list[EvidenceBundleRecord]:
        """Return durable idempotency records for diagnostics."""

    def insert(self, entity: EvidenceBundleRecord) -> None:
        """Append a durable idempotency record; existing records are immutable."""

    def get_by_key(
        self,
        *,
        project_id: UUID,
        created_by: str,
        idempotency_key: str,
    ) -> EvidenceBundleRecord | None:
        """Return the record for one principal-scoped idempotency key."""


class DataStoreRepository(Protocol):
    """Persistence operations for append-only registered data stores."""

    def get(self, entity_id: UUID) -> DataStore | None:
        """Return one registered store by ID."""

    def list(self) -> list[DataStore]:
        """Return all registered stores."""

    def save(self, entity: DataStore) -> None:
        """Accept only an exact no-op re-save of an existing registration."""

    def insert(self, entity: DataStore) -> None:
        """Append a store, translating persistence failures to safe port errors."""

    def reserve_registration_write(self) -> None:
        """Reserve backend write authority for an admitted registration."""

    def query(
        self,
        *,
        project_id: UUID | None = None,
        group_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[builtins.list[DataStore], int]:
        """Query stores by their direct registration scope."""

    def get_by_name(self, project_id: UUID, name: str) -> DataStore | None:
        """Resolve a project store name, including inherited group stores."""

    def scoped_store_by_name(
        self,
        *,
        project_id: UUID | None = None,
        group_id: UUID | None = None,
        name: str,
    ) -> DataStore | None:
        """Resolve a store name in exactly one direct scope."""

    def list_effective_for_project(
        self,
        project_id: UUID,
    ) -> builtins.list[DataStore]:
        """Return direct and inherited stores visible to a project."""

    def get_default(
        self,
        project_id: UUID | None = None,
        *,
        group_id: UUID | None = None,
    ) -> DataStore | None:
        """Return the first default store in one exact scope."""

    def clear_default(
        self,
        project_id: UUID | None = None,
        *,
        group_id: UUID | None = None,
        except_store_id: UUID | None = None,
    ) -> None:
        """Clear default flags in one exact scope except for an optional store."""


class GraphChangeSetRepository(EntityRepository[GraphChangeSet], Protocol):
    """Persistence operations specific to graph-draft change sets."""

    def project_id_for(self, change_set_id: UUID) -> UUID | None:
        """Resolve a draft's project without materializing its operations."""


class ReviewEmailOutboxRepository(Protocol):
    """Persistence contract for durable review-email delivery."""

    def get(self, entity_id: UUID) -> ReviewEmailDelivery | None:
        """Return one delivery by ID, or None when it does not exist."""

    def list(self) -> list[ReviewEmailDelivery]:
        """Return all deliveries for diagnostics."""

    def save(self, entity: ReviewEmailDelivery) -> None:
        """Persist a delivery create/update operation."""

    def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> ReviewEmailDelivery | None:
        """Return the delivery for one globally unique idempotency key."""

    def claim_next(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        claim_token: UUID,
    ) -> ReviewEmailDelivery | None:
        """Atomically lease the next due or stale delivery."""


class LabTrackerRepository(Protocol):
    """Repository surface expected by the Lab Tracker domain layer."""

    # These are read-only protocol properties even though concrete repositories
    # assign them once in ``__init__``. Read-only members are covariant, so a
    # focused SQLAlchemy repository can satisfy an ``EntityRepository`` role
    # structurally without inheriting this broad protocol nominally.
    @property
    def projects(self) -> EntityRepository[Project]: ...

    @property
    def project_groups(self) -> EntityRepository[ProjectGroup]: ...

    @property
    def project_memberships(self) -> EntityRepository[ProjectMembership]: ...

    @property
    def group_memberships(self) -> EntityRepository[GroupMembership]: ...

    @property
    def supervision_edges(self) -> EntityRepository[SupervisionEdge]: ...

    @property
    def ownership_reassignments(self) -> EntityRepository[OwnershipReassignment]: ...

    @property
    def record_export_events(self) -> EntityRepository[RecordExportEvent]: ...

    @property
    def usage_events(self) -> EntityRepository[UsageEvent]: ...

    @property
    def usage_event_rollups(self) -> EntityRepository[UsageEventRollup]: ...

    @property
    def questions(self) -> EntityRepository[Question]: ...

    @property
    def question_refactors(self) -> EntityRepository[QuestionRefactor]: ...

    @property
    def datasets(self) -> EntityRepository[Dataset]: ...

    @property
    def notes(self) -> NoteRepository: ...

    @property
    def sessions(self) -> EntityRepository[Session]: ...

    @property
    def acquisition_outputs(self) -> EntityRepository[AcquisitionOutput]: ...

    @property
    def analyses(self) -> EntityRepository[Analysis]: ...

    @property
    def claims(self) -> EntityRepository[Claim]: ...

    @property
    def claim_edges(self) -> EntityRepository[ClaimEdge]: ...

    @property
    def exploration_nodes(self) -> EntityRepository[ExplorationNode]: ...

    @property
    def provenance_links(self) -> EntityRepository[ProvenanceLink]: ...

    @property
    def entity_versions(self) -> EntityRepository[EntityVersion]: ...

    @property
    def goals(self) -> EntityRepository[Goal]: ...

    @property
    def data_stores(self) -> DataStoreRepository: ...

    @property
    def evidence_bundles(self) -> EvidenceBundleRepository: ...

    @property
    def visualizations(self) -> EntityRepository[Visualization]: ...

    @property
    def graph_change_sets(self) -> GraphChangeSetRepository: ...

    @property
    def graph_draft_batch_settings(self) -> EntityRepository[GraphDraftBatchSettings]: ...

    @property
    def graph_draft_batch_runs(self) -> EntityRepository[GraphDraftBatchRun]: ...

    @property
    def review_email_outbox(self) -> ReviewEmailOutboxRepository: ...

    def user_exists(self, user_id: UUID) -> bool:
        """Return whether a user exists for FK-backed attribution."""

    def fetch_questions(self, question_ids: list[UUID]) -> list[Question]:
        """Fetch questions in the provided order."""

    def fetch_notes(self, note_ids: list[UUID]) -> list[Note]:
        """Fetch notes in the provided order."""

    def lock_project_question_dag(self, project_id: UUID) -> None:
        """Serialize question-DAG validation and mutation for one project."""

    def lock_project_deletion_guard(self, project_id: UUID) -> None:
        """Keep a project alive while a graph command locks its child rows."""

    def lock_dataset_updates(
        self,
        project_id: UUID,
        dataset_ids: Iterable[UUID],
    ) -> None:
        """Serialize full-snapshot dataset writes with file mutations."""

    def query_projects(
        self,
        *,
        group_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        created_by: str | None = None,
        client_capture_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Project], int]:
        """Query projects with filters and pagination."""

    def query_project_groups(
        self,
        *,
        kind: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ProjectGroup], int]:
        """Query project groups with filters and pagination."""

    def query_project_memberships(
        self,
        *,
        project_id: UUID | None = None,
        user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ProjectMembership], int]:
        """Query project memberships with optional project/user filters."""

    def get_project_membership(
        self,
        *,
        project_id: UUID,
        user_id: UUID,
    ) -> ProjectMembership | None:
        """Return one project membership by project and user."""

    def lock_project_owner_memberships(self, project_id: UUID) -> None:
        """Lock direct owner membership rows for a project during invariant checks."""

    def query_group_memberships(
        self,
        *,
        group_id: UUID | None = None,
        user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GroupMembership], int]:
        """Query group memberships with optional group/user filters."""

    def get_group_membership(
        self,
        *,
        group_id: UUID,
        user_id: UUID,
    ) -> GroupMembership | None:
        """Return one group membership by group and user."""

    def query_supervision_edges(
        self,
        *,
        supervisor_user_id: UUID | None = None,
        supervisee_user_id: UUID | None = None,
        active_only: bool = False,
        as_of: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[SupervisionEdge], int]:
        """Query dated supervision edges."""

    def query_ownership_reassignments(
        self,
        *,
        from_user_id: UUID | None = None,
        to_user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[OwnershipReassignment], int]:
        """Query ownership reassignment audit records."""

    def reassign_ownership(
        self,
        *,
        reassignment_id: UUID,
        from_user_id: UUID,
        to_user_id: UUID,
        reason: str,
        created_by: str | None,
        created_by_user_id: UUID | None,
        created_at: datetime,
    ) -> OwnershipReassignment:
        """Move attribution values and record the reassignment audit row."""

    def query_record_export_events(
        self,
        *,
        user_id: UUID | None = None,
        group_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[RecordExportEvent], int]:
        """Query user record export audit events."""

    def query_usage_events(
        self,
        *,
        project_id: UUID | None = None,
        verb: str | None = None,
        resource_type: str | None = None,
        surface: str | None = None,
        outcome: str | None = None,
        occurred_before: datetime | None = None,
        occurred_on_or_after: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[UsageEvent], int]:
        """Query local usage telemetry events."""

    def usage_event_summary(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, object]]:
        """Return usage event counts by day, verb, and resource type."""

    def rollup_usage_events_before(self, cutoff: datetime) -> int:
        """Summarize and delete raw usage events older than cutoff."""

    def records_attributed_to_user(
        self,
        *,
        user_id: UUID,
        project_ids: set[UUID] | None = None,
    ) -> RecordExportRecords:
        """Return records attributed to a user for export/offboarding recovery."""

    def query_questions(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        question_ids: set[UUID] | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        client_capture_id: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        superseded_by_question_ids: set[UUID] | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
        updated_first: bool = False,
    ) -> tuple[list[Question], int]:
        """Query questions with filters and pagination."""

    def query_question_refactors(
        self,
        *,
        question_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[QuestionRefactor], int]:
        """Query refactor history where the question is source or replacement."""

    def query_datasets(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Dataset], int]:
        """Query datasets with filters and pagination."""

    def query_notes(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        note_ids: set[UUID] | None = None,
        status: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        client_capture_id: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Note], int]:
        """Query notes with filters and pagination."""

    def project_ids_with_search_matches(
        self,
        *,
        search: str,
        project_ids: set[UUID] | None = None,
        limit: int | None = None,
    ) -> set[UUID]:
        """Return distinct project IDs whose questions or notes match search."""

    def query_sessions(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        session_type: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Session], int]:
        """Query sessions with filters and pagination."""

    def query_acquisition_outputs(
        self,
        *,
        session_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[AcquisitionOutput], int]:
        """Query acquisition outputs with filters and pagination."""

    def query_dataset_files(
        self,
        *,
        dataset_id: UUID,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DatasetFile], int]:
        """Query dataset files with pagination."""

    def query_analyses(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Analysis], int]:
        """Query analyses with filters and pagination."""

    def query_claims(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Claim], int]:
        """Query claims with filters and pagination."""

    def query_claim_edges(
        self,
        *,
        project_id: UUID | None = None,
        claim_id: UUID | None = None,
        target_claim_id: UUID | None = None,
        relation: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ClaimEdge], int]:
        """Query typed claim-to-claim edges with filters and pagination."""

    def query_exploration_nodes(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        node_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[ExplorationNode], int]:
        """Query exploration trajectory nodes with filters and pagination."""

    def query_provenance_links(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[ProvenanceLink], int]:
        """Query provenance links with project scope, status, and pagination."""

    def query_entity_versions(
        self,
        *,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        change_set_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[EntityVersion], int]:
        """Query recorded entity snapshots with filters and pagination."""

    def query_goals(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        goal_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        target_entity_keys: set[tuple[str, UUID]] | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Goal], int]:
        """Query goals with filters and optional reverse node lookup."""

    def query_goal_links(
        self,
        *,
        goal_id: UUID | None = None,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        link_status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GoalLink], int]:
        """Query goal links with filters and pagination."""

    def query_visualizations(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Visualization], int]:
        """Query visualizations with filters and pagination."""

    def query_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        source_note_id: UUID | None = None,
        draft_mode: str | None = None,
        batch_key: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_operations: bool = True,
    ) -> tuple[list[GraphChangeSet], int]:
        """Query graph draft change sets with filters and pagination."""

    def claim_graph_change_set_for_commit(
        self,
        change_set_id: UUID,
    ) -> GraphChangeSet | None:
        """Atomically move a ready/submitted graph draft into commit processing."""

    def get_graph_draft_batch_settings_by_project(
        self,
        project_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> GraphDraftBatchSettings | None:
        """Return graph draft batch settings for a project."""

    def list_graph_draft_batch_settings_for_project(
        self,
        project_id: UUID,
    ) -> list[GraphDraftBatchSettings]:
        """Return all graph draft batch settings rows for a project."""

    def list_due_graph_draft_batch_settings(
        self,
        now: datetime,
    ) -> list[GraphDraftBatchSettings]:
        """Return batch settings whose next_run_at is due."""

    def claim_due_graph_draft_batch_settings(
        self,
        settings_id: UUID,
        *,
        observed_next_run_at: datetime,
        next_run_at: datetime,
        updated_at: datetime,
        updated_by: str | None,
    ) -> GraphDraftBatchSettings | None:
        """Advance a due batch settings row only if its observed schedule is unchanged."""

    def get_graph_draft_batch_run_by_key(self, batch_key: str) -> GraphDraftBatchRun | None:
        """Return one batch run by idempotency key."""

    def latest_successful_graph_draft_batch_run(
        self,
        project_id: UUID,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> GraphDraftBatchRun | None:
        """Return the latest successful/skipped batch run for a project."""

    def successful_graph_draft_batch_source_note_ids_at_window_end(
        self,
        project_id: UUID,
        window_end: datetime,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> set[UUID]:
        """Return source note IDs from successful batch runs ending at a window boundary."""

    def query_graph_draft_batch_runs(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GraphDraftBatchRun], int]:
        """Query graph draft batch run history."""

    def claim_next_pending_graph_draft_batch_run(
        self,
        *,
        claimed_at: datetime,
    ) -> GraphDraftBatchRun | None:
        """Atomically claim the oldest pending graph draft batch run."""

    def list_dataset_files(self, dataset_id: UUID) -> list[DatasetFile]:
        """Return all files attached to a dataset."""

    def list_dataset_note_target_ids(self, dataset_id: UUID) -> list[UUID]:
        """Return note IDs that target the dataset."""

    def commit(self) -> None:
        """Commit the current unit of work."""

    def rollback(self) -> None:
        """Rollback the current unit of work."""

    def savepoint(self) -> AbstractContextManager[None]:
        """Isolate a recoverable write inside the current transaction."""
