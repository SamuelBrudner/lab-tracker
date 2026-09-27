"""Graph draft SQLAlchemy repository."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from hashlib import blake2b
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.sql.elements import ColumnElement

from lab_tracker.db_models import (
    GraphChangeOperationModel,
    GraphChangeSetModel,
    GraphDraftBatchRunModel,
    NoteModel,
    UserModel,
)
from lab_tracker.db_types import ensure_uuid
from lab_tracker.draft_quality import DraftQualityRow
from lab_tracker.errors import ConflictError, ValidationError
from lab_tracker.member_onboarding import ALIGNMENT_MODE_KEY
from lab_tracker.models import (
    DEFERRED_AT_KEY,
    EDITED_AT_KEY,
    AcceptanceMode,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    GraphDraftPurpose,
    GraphDraftSemanticType,
    deferred_operation_count,
    utc_now,
)
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc

from .common import apply_pagination, count_from_statement, replace_child_rows, uuid_values

_GENERATION_KEY_LOCK_DOMAIN = b"lab-tracker:graph-draft-generation:v1\0"


def _generation_key_lock_id(batch_key: str) -> int:
    """Return a stable signed-bigint advisory lock for one generation key."""

    digest = blake2b(
        _GENERATION_KEY_LOCK_DOMAIN + batch_key.encode("utf-8"),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _uuid(value: str | None) -> UUID | None:
    return ensure_uuid(value) if value else None


def _uuid_str(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def review_assignee_matches(
    model: type[GraphChangeSetModel] | type[GraphDraftBatchRunModel],
    user_id: UUID,
) -> ColumnElement[bool]:
    """SQL form of "assigned to this user" for batch reviews and their runs.

    ``review_assignee_user_id`` wins when set; legacy rows carry only the
    string ``review_assignee``. Unassigned rows are project oversight work and
    never match.
    """

    return or_(
        model.review_assignee_user_id == str(user_id),
        and_(
            model.review_assignee_user_id.is_(None),
            model.review_assignee == str(user_id),
        ),
    )


def _dict(value: Any) -> dict[str, Any]:
    return dict(value or {})


def _list(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    return [dict(item) for item in value]


def _as_utc_optional(value: Any) -> Any:
    return as_utc(value) if value is not None else None


def _draft_quality_row_from_tuple(row: Row[Any]) -> DraftQualityRow:
    """Map one LEFT JOIN result tuple onto the ledger's typed row.

    Operation columns are ``None`` for a change set without operations; the
    edited-before-accept flag is evaluated in Python on the fetched JSON so
    the projection needs no dialect-specific JSON SQL.
    """

    (
        change_set_id,
        provider,
        model,
        prompt_version,
        change_set_status,
        created_at,
        reviewed_at,
        clarification_requests,
        semantic_type,
        operation_status,
        acceptance_mode,
        accepted_at,
        error_metadata,
    ) = row
    return DraftQualityRow(
        change_set_id=ensure_uuid(change_set_id),
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        change_set_status=GraphChangeSetStatus(change_set_status),
        change_set_created_at=as_utc(created_at),
        reviewed_at=_as_utc_optional(reviewed_at),
        clarification_request_count=len(list(clarification_requests or [])),
        semantic_type=GraphDraftSemanticType(semantic_type) if semantic_type else None,
        operation_status=(
            GraphChangeOperationStatus(operation_status) if operation_status else None
        ),
        acceptance_mode=AcceptanceMode(acceptance_mode) if acceptance_mode else None,
        accepted_at=_as_utc_optional(accepted_at),
        edited_before_accept=EDITED_AT_KEY in _dict(error_metadata),
    )


def operation_to_model(operation: GraphChangeOperation) -> GraphChangeOperationModel:
    if operation.acceptance_mode == AcceptanceMode.AUTO_ACCEPTED:
        raise ValidationError(
            "auto_accepted is a reserved acceptance mode and must not be persisted."
        )
    return GraphChangeOperationModel(
        operation_id=str(operation.operation_id),
        change_set_id=str(operation.change_set_id),
        sequence=operation.sequence,
        op=operation.op.value,
        entity_type=operation.entity_type.value,
        semantic_type=(
            operation.semantic_type.value if operation.semantic_type is not None else None
        ),
        target_entity_id=_uuid_str(operation.target_entity_id),
        client_ref=operation.client_ref,
        payload=dict(operation.payload),
        rationale=operation.rationale,
        confidence=operation.confidence,
        source_refs=[dict(item) for item in operation.source_refs],
        status=operation.status.value,
        review_note=operation.review_note,
        acceptance_mode=(
            operation.acceptance_mode.value if operation.acceptance_mode is not None else None
        ),
        accepted_by=operation.accepted_by,
        accepted_by_user_id=_uuid_str(operation.accepted_by_user_id),
        accepted_at=operation.accepted_at,
        result_entity_id=_uuid_str(operation.result_entity_id),
        error_metadata=dict(operation.error_metadata),
        created_at=operation.created_at,
        updated_at=operation.updated_at,
    )


def operation_from_model(row: GraphChangeOperationModel) -> GraphChangeOperation:
    return GraphChangeOperation(
        operation_id=ensure_uuid(row.operation_id),
        change_set_id=ensure_uuid(row.change_set_id),
        sequence=row.sequence,
        op=GraphChangeOp(row.op),
        entity_type=EntityType(row.entity_type),
        semantic_type=(
            GraphDraftSemanticType(row.semantic_type) if row.semantic_type else None
        ),
        target_entity_id=_uuid(row.target_entity_id),
        client_ref=row.client_ref,
        payload=_dict(row.payload),
        rationale=row.rationale or "",
        confidence=row.confidence,
        source_refs=_list(row.source_refs),
        status=GraphChangeOperationStatus(row.status),
        review_note=row.review_note,
        acceptance_mode=(
            AcceptanceMode(row.acceptance_mode) if row.acceptance_mode else None
        ),
        accepted_by=row.accepted_by,
        accepted_by_user_id=_uuid(row.accepted_by_user_id),
        accepted_at=as_utc(row.accepted_at) if row.accepted_at else None,
        result_entity_id=_uuid(row.result_entity_id),
        error_metadata=_dict(row.error_metadata),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


def change_set_to_model(change_set: GraphChangeSet) -> GraphChangeSetModel:
    return GraphChangeSetModel(
        change_set_id=str(change_set.change_set_id),
        project_id=str(change_set.project_id),
        source_note_id=str(change_set.source_note_id),
        source_note_ids=[str(note_id) for note_id in change_set.source_note_ids],
        source_checksum=change_set.source_checksum,
        source_content_type=change_set.source_content_type,
        source_filename=change_set.source_filename,
        batch_key=change_set.batch_key,
        batch_window_start=change_set.batch_window_start,
        batch_window_end=change_set.batch_window_end,
        provider=change_set.provider,
        model=change_set.model,
        prompt_version=change_set.prompt_version,
        draft_mode=change_set.draft_mode.value,
        purpose=change_set.purpose.value,
        context_packet=dict(change_set.context_packet),
        summary=change_set.summary,
        uncertain_fields=list(change_set.uncertain_fields),
        clarification_requests=list(change_set.clarification_requests),
        status=change_set.status.value,
        generation_claim_token=_uuid_str(change_set.generation_claim_token),
        generation_claimed_at=change_set.generation_claimed_at,
        generation_lease_expires_at=change_set.generation_lease_expires_at,
        generation_attempt_count=change_set.generation_attempt_count,
        commit_message=change_set.commit_message,
        error_metadata=dict(change_set.error_metadata),
        created_by=change_set.created_by,
        created_by_user_id=_uuid_str(change_set.created_by_user_id),
        review_assignee=change_set.review_assignee,
        review_assignee_user_id=_uuid_str(change_set.review_assignee_user_id),
        created_at=change_set.created_at,
        updated_at=change_set.updated_at,
        submitted_at=change_set.submitted_at,
        submitted_by=change_set.submitted_by,
        reviewed_at=change_set.reviewed_at,
        reviewed_by=change_set.reviewed_by,
        review_note=change_set.review_note,
        committed_at=change_set.committed_at,
        committed_by=change_set.committed_by,
    )


def apply_change_set_to_model(row: GraphChangeSetModel, change_set: GraphChangeSet) -> None:
    row.project_id = str(change_set.project_id)
    row.source_note_id = str(change_set.source_note_id)
    row.source_note_ids = [str(note_id) for note_id in change_set.source_note_ids]
    row.source_checksum = change_set.source_checksum
    row.source_content_type = change_set.source_content_type
    row.source_filename = change_set.source_filename
    row.batch_key = change_set.batch_key
    row.batch_window_start = change_set.batch_window_start
    row.batch_window_end = change_set.batch_window_end
    row.provider = change_set.provider
    row.model = change_set.model
    row.prompt_version = change_set.prompt_version
    row.draft_mode = change_set.draft_mode.value
    row.purpose = change_set.purpose.value
    row.context_packet = dict(change_set.context_packet)
    row.summary = change_set.summary
    row.uncertain_fields = list(change_set.uncertain_fields)
    row.clarification_requests = list(change_set.clarification_requests)
    row.status = change_set.status.value
    row.generation_claim_token = _uuid_str(change_set.generation_claim_token)
    row.generation_claimed_at = change_set.generation_claimed_at
    row.generation_lease_expires_at = change_set.generation_lease_expires_at
    row.generation_attempt_count = change_set.generation_attempt_count
    row.commit_message = change_set.commit_message
    row.error_metadata = dict(change_set.error_metadata)
    row.created_by = change_set.created_by
    row.created_by_user_id = _uuid_str(change_set.created_by_user_id)
    row.review_assignee = change_set.review_assignee
    row.review_assignee_user_id = _uuid_str(change_set.review_assignee_user_id)
    row.created_at = change_set.created_at
    row.updated_at = change_set.updated_at
    row.submitted_at = change_set.submitted_at
    row.submitted_by = change_set.submitted_by
    row.reviewed_at = change_set.reviewed_at
    row.reviewed_by = change_set.reviewed_by
    row.review_note = change_set.review_note
    row.committed_at = change_set.committed_at
    row.committed_by = change_set.committed_by


@dataclass(frozen=True)
class OperationTally:
    """List-view counts for one change set, derived without hydrating operations."""

    operation_count: int
    deferred_count: int


EMPTY_OPERATION_TALLY = OperationTally(operation_count=0, deferred_count=0)


def operation_tally(operations: Iterable[GraphChangeOperation]) -> OperationTally:
    loaded = list(operations)
    return OperationTally(
        operation_count=len(loaded),
        deferred_count=deferred_operation_count(loaded),
    )


def change_set_from_model(
    row: GraphChangeSetModel,
    *,
    operations: Iterable[GraphChangeOperation] = (),
    operation_count: int | None = None,
    deferred_count: int | None = None,
    usernames: dict[str, str] | None = None,
) -> GraphChangeSet:
    resolved_usernames = usernames or {}
    operation_list = list(operations)
    loaded_tally = operation_tally(operation_list)
    return GraphChangeSet(
        change_set_id=ensure_uuid(row.change_set_id),
        project_id=ensure_uuid(row.project_id),
        source_note_id=ensure_uuid(row.source_note_id),
        source_note_ids=[
            ensure_uuid(str(note_id)) for note_id in (row.source_note_ids or [row.source_note_id])
        ],
        source_checksum=row.source_checksum,
        source_content_type=row.source_content_type,
        source_filename=row.source_filename,
        batch_key=row.batch_key,
        batch_window_start=row.batch_window_start,
        batch_window_end=row.batch_window_end,
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        draft_mode=GraphDraftMode(row.draft_mode or GraphDraftMode.GRAPH_CONTEXT.value),
        purpose=GraphDraftPurpose(row.purpose or GraphDraftPurpose.GENERAL.value),
        context_packet=_dict(row.context_packet),
        summary=row.summary or "",
        uncertain_fields=list(row.uncertain_fields or []),
        clarification_requests=list(row.clarification_requests or []),
        status=GraphChangeSetStatus(row.status),
        generation_claim_token=_uuid(row.generation_claim_token),
        generation_claimed_at=_as_utc_optional(row.generation_claimed_at),
        generation_lease_expires_at=_as_utc_optional(
            row.generation_lease_expires_at
        ),
        generation_attempt_count=int(row.generation_attempt_count or 0),
        commit_message=row.commit_message,
        error_metadata=_dict(row.error_metadata),
        operation_count=(
            operation_count if operation_count is not None else loaded_tally.operation_count
        ),
        deferred_count=(
            deferred_count if deferred_count is not None else loaded_tally.deferred_count
        ),
        operations=operation_list,
        created_by=row.created_by,
        created_by_user_id=_uuid(row.created_by_user_id),
        created_by_username=resolved_usernames.get(row.created_by or ""),
        review_assignee=row.review_assignee,
        review_assignee_user_id=_uuid(row.review_assignee_user_id),
        review_assignee_username=resolved_usernames.get(row.review_assignee or ""),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        submitted_at=_as_utc_optional(row.submitted_at),
        submitted_by=row.submitted_by,
        submitted_by_username=resolved_usernames.get(row.submitted_by or ""),
        reviewed_at=_as_utc_optional(row.reviewed_at),
        reviewed_by=row.reviewed_by,
        reviewed_by_username=resolved_usernames.get(row.reviewed_by or ""),
        review_note=row.review_note,
        committed_at=_as_utc_optional(row.committed_at),
        committed_by=row.committed_by,
        committed_by_username=resolved_usernames.get(row.committed_by or ""),
    )


class SQLAlchemyGraphChangeSetRepository(EntityRepository[GraphChangeSet]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _operations_for(self, change_set_ids: list[str]) -> dict[str, list[GraphChangeOperation]]:
        if not change_set_ids:
            return {}
        rows = list(
            self._session.scalars(
                select(GraphChangeOperationModel)
                .where(GraphChangeOperationModel.change_set_id.in_(change_set_ids))
                .order_by(
                    GraphChangeOperationModel.change_set_id,
                    GraphChangeOperationModel.sequence,
                )
            )
        )
        operation_map: dict[str, list[GraphChangeOperation]] = {}
        for row in rows:
            operation_map.setdefault(str(row.change_set_id), []).append(operation_from_model(row))
        return operation_map

    def _operation_tallies_for(self, change_set_ids: list[str]) -> dict[str, OperationTally]:
        """Per-page operation and deferral counts; reads stamps, never hydrates operations.

        The deferral stamp lives in the JSON ``error_metadata`` column, so the
        tally is aggregated in Python to stay dialect-neutral. It is O(page):
        one narrow row per operation of the listed change sets.
        """

        if not change_set_ids:
            return {}
        rows = self._session.execute(
            select(
                GraphChangeOperationModel.change_set_id,
                GraphChangeOperationModel.error_metadata,
            ).where(GraphChangeOperationModel.change_set_id.in_(change_set_ids))
        )
        operation_counts: dict[str, int] = {}
        deferred_counts: dict[str, int] = {}
        for change_set_id, error_metadata in rows:
            key = str(change_set_id)
            operation_counts[key] = operation_counts.get(key, 0) + 1
            if DEFERRED_AT_KEY in _dict(error_metadata):
                deferred_counts[key] = deferred_counts.get(key, 0) + 1
        return {
            key: OperationTally(operation_count=count, deferred_count=deferred_counts.get(key, 0))
            for key, count in operation_counts.items()
        }

    def draft_quality_rows(
        self,
        *,
        project_id: UUID,
        since: datetime | None,
    ) -> list[DraftQualityRow]:
        """One narrow row per (change set, operation); payloads are never selected."""

        self._session.flush()
        stmt = (
            select(
                GraphChangeSetModel.change_set_id,
                GraphChangeSetModel.provider,
                GraphChangeSetModel.model,
                GraphChangeSetModel.prompt_version,
                GraphChangeSetModel.status,
                GraphChangeSetModel.created_at,
                GraphChangeSetModel.reviewed_at,
                GraphChangeSetModel.clarification_requests,
                GraphChangeOperationModel.semantic_type,
                GraphChangeOperationModel.status,
                GraphChangeOperationModel.acceptance_mode,
                GraphChangeOperationModel.accepted_at,
                GraphChangeOperationModel.error_metadata,
            )
            .select_from(GraphChangeSetModel)
            .outerjoin(
                GraphChangeOperationModel,
                GraphChangeOperationModel.change_set_id == GraphChangeSetModel.change_set_id,
            )
            .where(GraphChangeSetModel.project_id == str(project_id))
        )
        if since is not None:
            stmt = stmt.where(GraphChangeSetModel.created_at >= as_utc(since))
        stmt = stmt.order_by(
            GraphChangeSetModel.created_at,
            GraphChangeSetModel.change_set_id,
            GraphChangeOperationModel.sequence,
        )
        return [_draft_quality_row_from_tuple(row) for row in self._session.execute(stmt)]

    def _from_rows(
        self,
        rows: list[GraphChangeSetModel],
        *,
        include_operations: bool = True,
    ) -> list[GraphChangeSet]:
        change_set_ids = [row.change_set_id for row in rows]
        operation_map = self._operations_for(change_set_ids) if include_operations else {}
        tallies = (
            {key: operation_tally(operations) for key, operations in operation_map.items()}
            if include_operations
            else self._operation_tallies_for(change_set_ids)
        )
        # Attribution columns are free-text strings (e.g. "operator-1"); only
        # UUID-shaped values can name a user, and binding anything else to the
        # GUID column would raise. Key the result by the raw stored string,
        # which is what change_set_from_model looks up.
        attribution_user_ids: dict[str, UUID] = {}
        for row in rows:
            for attribution in (
                row.created_by,
                row.review_assignee,
                row.submitted_by,
                row.reviewed_by,
                row.committed_by,
            ):
                if not attribution or attribution in attribution_user_ids:
                    continue
                try:
                    attribution_user_ids[attribution] = UUID(attribution)
                except ValueError:
                    continue
        usernames: dict[str, str] = {}
        if attribution_user_ids:
            user_rows = list(
                self._session.scalars(
                    select(UserModel).where(
                        UserModel.user_id.in_(sorted(set(attribution_user_ids.values()), key=str))
                    )
                )
            )
            usernames_by_id = {user_row.user_id: user_row.username for user_row in user_rows}
            usernames = {
                attribution: usernames_by_id[user_id]
                for attribution, user_id in attribution_user_ids.items()
                if user_id in usernames_by_id
            }
        change_sets: list[GraphChangeSet] = []
        for row in rows:
            tally = tallies.get(str(row.change_set_id), EMPTY_OPERATION_TALLY)
            change_sets.append(
                change_set_from_model(
                    row,
                    operations=operation_map.get(str(row.change_set_id), []),
                    operation_count=tally.operation_count,
                    deferred_count=tally.deferred_count,
                    usernames=usernames,
                )
            )
        return change_sets

    def get(self, entity_id: UUID) -> GraphChangeSet | None:
        self._session.flush()
        row = self._session.get(GraphChangeSetModel, str(entity_id))
        if row is None:
            return None
        return self._from_rows([row])[0]

    def get_for_update(self, entity_id: UUID) -> GraphChangeSet | None:
        self._session.flush()
        row = self._session.scalar(
            select(GraphChangeSetModel)
            .where(GraphChangeSetModel.change_set_id == str(entity_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        return self._from_rows([row])[0]

    def project_id_for(self, change_set_id: UUID) -> UUID | None:
        """Resolve read scope without hydrating operations or attribution."""

        self._session.flush()
        project_id = self._session.scalar(
            select(GraphChangeSetModel.project_id).where(
                GraphChangeSetModel.change_set_id == str(change_set_id)
            )
        )
        return _uuid(project_id)

    def claim_for_generation(
        self,
        candidate: GraphChangeSet,
        *,
        claimed_at: datetime,
        lease_until: datetime,
        claim_token: UUID,
    ) -> tuple[GraphChangeSet, bool]:
        """Create or atomically reclaim one provider-generation attempt."""

        if lease_until <= claimed_at:
            raise ValueError("lease_until must be later than claimed_at.")
        self._session.flush()
        if (
            candidate.batch_key is not None
            and self._session.get_bind().dialect.name == "postgresql"
        ):
            # Serialize only the tiny first-claim transaction for this stable
            # generation key. The lock is transaction scoped and is released
            # before provider I/O, so two first requests cannot both observe
            # an absent row and race on the unique constraint.
            self._session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _generation_key_lock_id(candidate.batch_key)},
            )
        if candidate.purpose == GraphDraftPurpose.MEMBER_CHECKPOINT_ALIGNMENT:
            # Manual alignment takes this same checkpoint row lock before it
            # checks for a live AI draft.  Keeping the order checkpoint ->
            # graph-change-set makes the two modes mutually exclusive even
            # when their requests start concurrently.
            checkpoint = self._session.scalar(
                select(NoteModel)
                .where(NoteModel.note_id == str(candidate.source_note_id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if checkpoint is None:
                raise ValidationError(
                    "Member onboarding source checkpoint is unavailable."
                )
            if dict(checkpoint.note_metadata or {}).get(ALIGNMENT_MODE_KEY) == "manual":
                raise ConflictError(
                    "The checkpoint alignment has already been finalized manually."
                )
        row = (
            self._session.scalar(
                select(GraphChangeSetModel).where(
                    GraphChangeSetModel.batch_key == candidate.batch_key
                )
            )
            if candidate.batch_key is not None
            else None
        )
        if row is None:
            candidate.status = GraphChangeSetStatus.DRAFTING
            candidate.generation_claim_token = claim_token
            candidate.generation_claimed_at = claimed_at
            candidate.generation_lease_expires_at = lease_until
            candidate.generation_attempt_count = 1
            candidate.updated_at = claimed_at
            self._session.add(change_set_to_model(candidate))
            self._session.flush()
            persisted = self.get(candidate.change_set_id)
            if persisted is None:  # pragma: no cover - flush made the row visible
                raise RuntimeError("Claimed graph draft could not be reloaded.")
            return persisted, True

        self._ensure_same_generation_identity(row, candidate)
        eligible = or_(
            GraphChangeSetModel.status == GraphChangeSetStatus.FAILED.value,
            and_(
                GraphChangeSetModel.status == GraphChangeSetStatus.DRAFTING.value,
                or_(
                    GraphChangeSetModel.generation_lease_expires_at.is_(None),
                    GraphChangeSetModel.generation_lease_expires_at <= claimed_at,
                ),
            ),
        )
        result = self._session.execute(
            update(GraphChangeSetModel)
            .where(GraphChangeSetModel.change_set_id == row.change_set_id)
            .where(eligible)
            .values(
                source_note_id=str(candidate.source_note_id),
                source_note_ids=[str(note_id) for note_id in candidate.source_note_ids],
                source_checksum=candidate.source_checksum,
                source_content_type=candidate.source_content_type,
                source_filename=candidate.source_filename,
                batch_window_start=candidate.batch_window_start,
                batch_window_end=candidate.batch_window_end,
                provider=candidate.provider,
                model=candidate.model,
                prompt_version=candidate.prompt_version,
                draft_mode=candidate.draft_mode.value,
                purpose=candidate.purpose.value,
                context_packet=dict(candidate.context_packet),
                summary="",
                uncertain_fields=[],
                clarification_requests=[],
                status=GraphChangeSetStatus.DRAFTING.value,
                generation_claim_token=str(claim_token),
                generation_claimed_at=claimed_at,
                generation_lease_expires_at=lease_until,
                generation_attempt_count=(
                    GraphChangeSetModel.generation_attempt_count + 1
                ),
                commit_message=None,
                error_metadata={},
                review_assignee=candidate.review_assignee,
                review_assignee_user_id=_uuid_str(
                    candidate.review_assignee_user_id
                ),
                updated_at=claimed_at,
                submitted_at=None,
                submitted_by=None,
                reviewed_at=None,
                reviewed_by=None,
                review_note=None,
                committed_at=None,
                committed_by=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            current = self.get(ensure_uuid(row.change_set_id))
            if current is None:  # pragma: no cover - rows are not deleted here
                raise RuntimeError("Graph draft disappeared while claiming generation.")
            return current, False
        self._session.execute(
            delete(GraphChangeOperationModel).where(
                GraphChangeOperationModel.change_set_id == row.change_set_id
            )
        )
        self._session.flush()
        self._session.expire_all()
        claimed = self.get(ensure_uuid(row.change_set_id))
        if claimed is None:  # pragma: no cover - conditional update retained row
            raise RuntimeError("Claimed graph draft could not be reloaded.")
        return claimed, True

    def renew_generation_claim(
        self,
        change_set_id: UUID,
        claim_token: UUID,
        *,
        renewed_at: datetime,
        lease_until: datetime,
    ) -> GraphChangeSet | None:
        if lease_until <= renewed_at:
            raise ValueError("lease_until must be later than renewed_at.")
        self._session.flush()
        result = self._session.execute(
            update(GraphChangeSetModel)
            .where(GraphChangeSetModel.change_set_id == str(change_set_id))
            .where(GraphChangeSetModel.status == GraphChangeSetStatus.DRAFTING.value)
            .where(GraphChangeSetModel.generation_claim_token == str(claim_token))
            .where(GraphChangeSetModel.generation_lease_expires_at > renewed_at)
            .values(
                generation_lease_expires_at=lease_until,
                updated_at=renewed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        self._session.expire_all()
        return self.get(change_set_id)

    def complete_generation_claim(
        self,
        change_set: GraphChangeSet,
        claim_token: UUID,
        *,
        completed_at: datetime,
    ) -> GraphChangeSet | None:
        if change_set.status != GraphChangeSetStatus.READY:
            raise ValueError("Completed graph generation must have READY status.")
        return self._finish_generation_claim(
            change_set,
            claim_token,
            finished_at=completed_at,
        )

    def fail_generation_claim(
        self,
        change_set: GraphChangeSet,
        claim_token: UUID,
        *,
        failed_at: datetime,
    ) -> GraphChangeSet | None:
        if change_set.status != GraphChangeSetStatus.FAILED:
            raise ValueError("Failed graph generation must have FAILED status.")
        return self._finish_generation_claim(
            change_set,
            claim_token,
            finished_at=failed_at,
        )

    def _finish_generation_claim(
        self,
        change_set: GraphChangeSet,
        claim_token: UUID,
        *,
        finished_at: datetime,
    ) -> GraphChangeSet | None:
        self._session.flush()
        result = self._session.execute(
            update(GraphChangeSetModel)
            .where(
                GraphChangeSetModel.change_set_id == str(change_set.change_set_id)
            )
            .where(GraphChangeSetModel.status == GraphChangeSetStatus.DRAFTING.value)
            .where(GraphChangeSetModel.generation_claim_token == str(claim_token))
            .where(GraphChangeSetModel.generation_lease_expires_at > finished_at)
            .values(
                context_packet=dict(change_set.context_packet),
                summary=change_set.summary,
                uncertain_fields=list(change_set.uncertain_fields),
                clarification_requests=list(change_set.clarification_requests),
                status=change_set.status.value,
                generation_claim_token=None,
                generation_claimed_at=None,
                generation_lease_expires_at=None,
                error_metadata=dict(change_set.error_metadata),
                updated_at=finished_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        entity_id = str(change_set.change_set_id)
        replace_child_rows(
            self._session,
            GraphChangeOperationModel,
            GraphChangeOperationModel.change_set_id,
            entity_id,
            [operation_to_model(operation) for operation in change_set.operations],
        )
        self._session.flush()
        self._session.expire_all()
        return self.get(change_set.change_set_id)

    @staticmethod
    def _ensure_same_generation_identity(
        row: GraphChangeSetModel,
        candidate: GraphChangeSet,
    ) -> None:
        persisted_note_ids = [
            str(note_id) for note_id in (row.source_note_ids or [row.source_note_id])
        ]
        candidate_note_ids = [str(note_id) for note_id in candidate.source_note_ids]
        if (
            str(row.project_id) != str(candidate.project_id)
            or str(row.source_note_id) != str(candidate.source_note_id)
            or persisted_note_ids != candidate_note_ids
            or row.draft_mode != candidate.draft_mode.value
            or row.purpose != candidate.purpose.value
        ):
            raise ValidationError(
                "Graph draft generation key was already used for different source fields."
            )

    def list(self) -> list[GraphChangeSet]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(GraphChangeSetModel).order_by(
                    GraphChangeSetModel.created_at.desc(),
                    GraphChangeSetModel.change_set_id,
                )
            )
        )
        return self._from_rows(rows)

    def save(self, entity: GraphChangeSet) -> None:
        entity_id = str(entity.change_set_id)
        self._session.flush()
        row = self._session.get(GraphChangeSetModel, entity_id)
        if row is None:
            self._session.add(change_set_to_model(entity))
        else:
            apply_change_set_to_model(row, entity)
        self._session.flush()
        replace_child_rows(
            self._session,
            GraphChangeOperationModel,
            GraphChangeOperationModel.change_set_id,
            entity_id,
            [operation_to_model(operation) for operation in entity.operations],
        )

    def claim_for_commit(self, entity_id: UUID) -> GraphChangeSet | None:
        self._session.flush()
        result = self._session.execute(
            update(GraphChangeSetModel)
            .where(GraphChangeSetModel.change_set_id == str(entity_id))
            .where(
                GraphChangeSetModel.status.in_(
                    [
                        GraphChangeSetStatus.READY.value,
                        GraphChangeSetStatus.SUBMITTED.value,
                    ]
                )
            )
            .values(status=GraphChangeSetStatus.COMMITTING.value, updated_at=utc_now())
        )
        if result.rowcount != 1:
            return None
        self._session.flush()
        return self.get(entity_id)

    def delete(self, entity_id: UUID) -> GraphChangeSet | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(GraphChangeSetModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        source_note_id: UUID | None = None,
        draft_mode: str | None = None,
        purpose: str | None = None,
        batch_key: str | None = None,
        statuses: set[str] | None = None,
        assigned_to_user_id: UUID | None = None,
        unassigned_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
        include_operations: bool = True,
    ) -> tuple[list[GraphChangeSet], int]:
        self._session.flush()
        if project_ids is not None and not project_ids:
            return [], 0
        if statuses is not None and not statuses:
            return [], 0
        if status is not None and statuses is not None:
            raise ValueError("Pass either status or statuses, not both.")
        if assigned_to_user_id is not None and unassigned_only:
            raise ValueError("assigned_to_user_id and unassigned_only are exclusive.")
        stmt = select(GraphChangeSetModel)
        count_stmt = select(GraphChangeSetModel.change_set_id)
        review_filters = []
        if statuses is not None:
            review_filters.append(GraphChangeSetModel.status.in_(sorted(statuses)))
        if assigned_to_user_id is not None:
            review_filters.append(
                review_assignee_matches(GraphChangeSetModel, assigned_to_user_id)
            )
        if unassigned_only:
            review_filters.append(GraphChangeSetModel.review_assignee_user_id.is_(None))
            review_filters.append(GraphChangeSetModel.review_assignee.is_(None))
        if review_filters:
            stmt = stmt.where(*review_filters)
            count_stmt = count_stmt.where(*review_filters)
        if project_id is not None:
            stmt = stmt.where(GraphChangeSetModel.project_id == str(project_id))
            count_stmt = count_stmt.where(GraphChangeSetModel.project_id == str(project_id))
        if project_ids is not None:
            project_values = uuid_values(project_ids)
            stmt = stmt.where(GraphChangeSetModel.project_id.in_(project_values))
            count_stmt = count_stmt.where(GraphChangeSetModel.project_id.in_(project_values))
        if status is not None:
            stmt = stmt.where(GraphChangeSetModel.status == status)
            count_stmt = count_stmt.where(GraphChangeSetModel.status == status)
        if source_note_id is not None:
            stmt = stmt.where(GraphChangeSetModel.source_note_id == str(source_note_id))
            count_stmt = count_stmt.where(GraphChangeSetModel.source_note_id == str(source_note_id))
        if draft_mode is not None:
            stmt = stmt.where(GraphChangeSetModel.draft_mode == draft_mode)
            count_stmt = count_stmt.where(GraphChangeSetModel.draft_mode == draft_mode)
        if purpose is not None:
            stmt = stmt.where(GraphChangeSetModel.purpose == purpose)
            count_stmt = count_stmt.where(GraphChangeSetModel.purpose == purpose)
        if batch_key is not None:
            stmt = stmt.where(GraphChangeSetModel.batch_key == batch_key)
            count_stmt = count_stmt.where(GraphChangeSetModel.batch_key == batch_key)
        stmt = stmt.order_by(
            GraphChangeSetModel.created_at.desc(),
            GraphChangeSetModel.change_set_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self._from_rows(rows, include_operations=include_operations), total
