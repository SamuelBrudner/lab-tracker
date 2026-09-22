"""Ownership reassignment SQLAlchemy repository."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Table, or_, select, update
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm.attributes import InstrumentedAttribute

from lab_tracker.db import Base

# Importing the model module registers every mapped table on Base.registry,
# which the reassignment targets below are derived from.
from lab_tracker.db_models import (
    OwnershipReassignmentModel,
    RecordExportEventModel,
)
from lab_tracker.models import OwnershipReassignment, RecordExportEvent
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc, uuid_from_db, uuid_to_db

from .common import apply_pagination, count_from_statement

# Authorship pairs (free-text principal id, user FK) that ownership
# reassignment moves from the departing user to the successor.
ATTRIBUTION_COLUMN_PAIRS: tuple[tuple[str, str], ...] = (
    ("created_by", "created_by_user_id"),
    ("executed_by", "executed_by_user_id"),
)

_AUDIT_OF_ACTION = "Audit fact of who performed an action; rewriting it would falsify history."
_CREDENTIAL = "Credential bound to the user's own identity; revoked, never reassigned."
_MEMBERSHIP_SUBJECT = "Access-grant subject; handled by membership offboarding, not reassignment."
_REVIEW_ROUTING = "Review routing assignment; reassigned through review assignment flows."

# Every other column that names a user (a users FK or an attribution id) and
# that reassignment deliberately leaves unchanged, with the reason. A new
# user-reference column must be added either to an attribution pair above or
# here; tests/test_ownership_reassignments.py enforces the classification.
USER_COLUMNS_NOT_REASSIGNED: Mapping[tuple[str, str], str] = {
    ("acquisition_collection_captures", "capture_actor_user_id"): _AUDIT_OF_ACTION,
    ("acquisition_collection_snapshots", "capture_actor_user_id"): _AUDIT_OF_ACTION,
    ("device_enrollments", "user_id"): _CREDENTIAL,
    ("device_tokens", "user_id"): _CREDENTIAL,
    ("personal_access_tokens", "user_id"): _CREDENTIAL,
    ("evidence_bundles", "created_by"): (
        "Idempotency scope of the requesting principal (part of a unique key); "
        "rewriting it would replay or collide with another principal's requests."
    ),
    ("graph_change_operations", "accepted_by"): _AUDIT_OF_ACTION,
    ("graph_change_operations", "accepted_by_user_id"): _AUDIT_OF_ACTION,
    ("graph_change_sets", "committed_by"): _AUDIT_OF_ACTION,
    ("graph_change_sets", "submitted_by"): _AUDIT_OF_ACTION,
    ("graph_change_sets", "reviewed_by"): _AUDIT_OF_ACTION,
    ("graph_change_sets", "review_assignee"): _REVIEW_ROUTING,
    ("graph_change_sets", "review_assignee_user_id"): _REVIEW_ROUTING,
    ("graph_draft_batch_runs", "review_assignee"): _REVIEW_ROUTING,
    ("graph_draft_batch_runs", "review_assignee_user_id"): _REVIEW_ROUTING,
    ("graph_draft_batch_settings", "user_id"): "Per-user scheduling preference.",
    ("graph_draft_batch_settings", "updated_by"): _AUDIT_OF_ACTION,
    ("group_memberships", "user_id"): _MEMBERSHIP_SUBJECT,
    ("project_memberships", "user_id"): _MEMBERSHIP_SUBJECT,
    ("invitations", "consumed_by_user_id"): _AUDIT_OF_ACTION,
    ("notes", "archived_by"): _AUDIT_OF_ACTION,
    ("notes", "archived_by_user_id"): _AUDIT_OF_ACTION,
    ("provenance_links", "accepted_by"): _AUDIT_OF_ACTION,
    ("provenance_links", "accepted_by_user_id"): _AUDIT_OF_ACTION,
    ("ownership_reassignments", "from_user_id"): "Reassignment audit subject.",
    ("ownership_reassignments", "to_user_id"): "Reassignment audit subject.",
    ("ownership_reassignments", "created_by"): _AUDIT_OF_ACTION,
    ("ownership_reassignments", "created_by_user_id"): _AUDIT_OF_ACTION,
    ("record_export_events", "user_id"): "Export audit subject.",
    ("record_export_events", "created_by"): _AUDIT_OF_ACTION,
    ("record_export_events", "created_by_user_id"): _AUDIT_OF_ACTION,
    ("review_email_outbox", "recipient_user_id"): "Delivery log of a sent or queued email.",
    ("supervision_edges", "supervisor_user_id"): "Dated supervision relationship.",
    ("supervision_edges", "supervisee_user_id"): "Dated supervision relationship.",
    ("usage_events", "actor_user_id"): "Usage telemetry of who acted.",
}


@dataclass(frozen=True, slots=True)
class AttributionReassignmentTarget:
    """One mapped table whose authorship pair ownership reassignment rewrites."""

    label: str
    model: type[Base]
    text_column: InstrumentedAttribute[Any]
    user_id_column: InstrumentedAttribute[Any]


def _attribution_reassignment_targets() -> tuple[AttributionReassignmentTarget, ...]:
    """Derive reassignment targets from every mapped table with an attribution pair."""

    targets: list[AttributionReassignmentTarget] = []
    mapped_tables: list[tuple[Table, Any]] = []
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if not isinstance(table, Table):
            raise RuntimeError(f"{mapper.class_.__name__} is not mapped to a table.")
        mapped_tables.append((table, mapper))
    for table, mapper in sorted(mapped_tables, key=lambda item: item[0].name):
        pairs: list[tuple[str, str]] = []
        for text_name, user_id_name in ATTRIBUTION_COLUMN_PAIRS:
            if text_name not in table.c or user_id_name not in table.c:
                continue
            excluded = {
                (table.name, text_name) in USER_COLUMNS_NOT_REASSIGNED,
                (table.name, user_id_name) in USER_COLUMNS_NOT_REASSIGNED,
            }
            if excluded == {True, False}:
                raise RuntimeError(
                    f"{table.name}: attribution pair {text_name}/{user_id_name} is only "
                    "partly excluded from ownership reassignment."
                )
            if excluded == {False}:
                pairs.append((text_name, user_id_name))
        if len(pairs) > 1:
            raise RuntimeError(
                f"{table.name} has several attribution pairs; ownership reassignment "
                "counts records per table and cannot label them."
            )
        if not pairs:
            continue
        text_name, user_id_name = pairs[0]
        targets.append(
            AttributionReassignmentTarget(
                label=table.name,
                model=mapper.class_,
                text_column=mapper.get_property_by_column(table.c[text_name]).class_attribute,
                user_id_column=mapper.get_property_by_column(
                    table.c[user_id_name]
                ).class_attribute,
            )
        )
    return tuple(targets)


ATTRIBUTION_REASSIGNMENT_TARGETS: tuple[AttributionReassignmentTarget, ...] = (
    _attribution_reassignment_targets()
)


def ownership_reassignment_from_model(
    row: OwnershipReassignmentModel,
) -> OwnershipReassignment:
    return OwnershipReassignment(
        reassignment_id=uuid_from_db(row.reassignment_id),
        from_user_id=uuid_from_db(row.from_user_id),
        to_user_id=uuid_from_db(row.to_user_id),
        reason=row.reason or "",
        record_counts={key: int(value) for key, value in (row.record_counts or {}).items()},
        created_by=row.created_by,
        created_by_user_id=(
            uuid_from_db(row.created_by_user_id)
            if row.created_by_user_id is not None
            else None
        ),
        created_at=as_utc(row.created_at),
    )


def record_export_event_from_model(row: RecordExportEventModel) -> RecordExportEvent:
    return RecordExportEvent(
        export_id=uuid_from_db(row.export_id),
        user_id=uuid_from_db(row.user_id),
        group_id=uuid_from_db(row.group_id) if row.group_id is not None else None,
        project_ids=[uuid_from_db(project_id) for project_id in (row.project_ids or [])],
        record_counts={key: int(value) for key, value in (row.record_counts or {}).items()},
        created_by=row.created_by,
        created_by_user_id=(
            uuid_from_db(row.created_by_user_id)
            if row.created_by_user_id is not None
            else None
        ),
        created_at=as_utc(row.created_at),
    )


class SQLAlchemyRecordExportEventRepository(EntityRepository[RecordExportEvent]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> RecordExportEvent | None:
        self._session.flush()
        row = self._session.get(RecordExportEventModel, uuid_to_db(entity_id))
        if row is None:
            return None
        return record_export_event_from_model(row)

    def list(self) -> list[RecordExportEvent]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(RecordExportEventModel).order_by(
                    RecordExportEventModel.created_at.desc(),
                    RecordExportEventModel.export_id,
                )
            )
        )
        return [record_export_event_from_model(row) for row in rows]

    def save(self, entity: RecordExportEvent) -> None:
        entity_id = uuid_to_db(entity.export_id)
        row = self._session.get(RecordExportEventModel, entity_id)
        if row is None:
            self._session.add(
                RecordExportEventModel(
                    export_id=entity_id,
                    user_id=uuid_to_db(entity.user_id),
                    group_id=uuid_to_db(entity.group_id)
                    if entity.group_id is not None
                    else None,
                    project_ids=[uuid_to_db(project_id) for project_id in entity.project_ids],
                    record_counts=dict(entity.record_counts),
                    created_by=entity.created_by,
                    created_by_user_id=(
                        uuid_to_db(entity.created_by_user_id)
                        if entity.created_by_user_id is not None
                        else None
                    ),
                    created_at=entity.created_at,
                )
            )
            return
        row.project_ids = [uuid_to_db(project_id) for project_id in entity.project_ids]
        row.record_counts = dict(entity.record_counts)
        row.created_by = entity.created_by
        row.created_by_user_id = (
            uuid_to_db(entity.created_by_user_id)
            if entity.created_by_user_id is not None
            else None
        )

    def delete(self, entity_id: UUID) -> RecordExportEvent | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(RecordExportEventModel, uuid_to_db(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        user_id: UUID | None = None,
        group_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[RecordExportEvent], int]:
        self._session.flush()
        stmt = select(RecordExportEventModel)
        count_stmt = select(RecordExportEventModel.export_id)
        if user_id is not None:
            user = uuid_to_db(user_id)
            stmt = stmt.where(RecordExportEventModel.user_id == user)
            count_stmt = count_stmt.where(RecordExportEventModel.user_id == user)
        if group_id is not None:
            group = uuid_to_db(group_id)
            stmt = stmt.where(RecordExportEventModel.group_id == group)
            count_stmt = count_stmt.where(RecordExportEventModel.group_id == group)
        stmt = stmt.order_by(
            RecordExportEventModel.created_at.desc(),
            RecordExportEventModel.export_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [record_export_event_from_model(row) for row in rows], total


class SQLAlchemyOwnershipReassignmentRepository(EntityRepository[OwnershipReassignment]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> OwnershipReassignment | None:
        self._session.flush()
        row = self._session.get(OwnershipReassignmentModel, str(entity_id))
        if row is None:
            return None
        return ownership_reassignment_from_model(row)

    def list(self) -> list[OwnershipReassignment]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(OwnershipReassignmentModel).order_by(
                    OwnershipReassignmentModel.created_at.desc(),
                    OwnershipReassignmentModel.reassignment_id,
                )
            )
        )
        return [ownership_reassignment_from_model(row) for row in rows]

    def save(self, entity: OwnershipReassignment) -> None:
        entity_id = uuid_to_db(entity.reassignment_id)
        row = self._session.get(OwnershipReassignmentModel, entity_id)
        if row is None:
            self._session.add(
                OwnershipReassignmentModel(
                    reassignment_id=entity_id,
                    from_user_id=uuid_to_db(entity.from_user_id),
                    to_user_id=uuid_to_db(entity.to_user_id),
                    reason=entity.reason,
                    record_counts=dict(entity.record_counts),
                    created_by=entity.created_by,
                    created_by_user_id=(
                        uuid_to_db(entity.created_by_user_id)
                        if entity.created_by_user_id is not None
                        else None
                    ),
                    created_at=entity.created_at,
                )
            )
            return
        row.reason = entity.reason
        row.record_counts = dict(entity.record_counts)
        row.created_by = entity.created_by
        row.created_by_user_id = (
            uuid_to_db(entity.created_by_user_id)
            if entity.created_by_user_id is not None
            else None
        )

    def delete(self, entity_id: UUID) -> OwnershipReassignment | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(OwnershipReassignmentModel, uuid_to_db(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        from_user_id: UUID | None = None,
        to_user_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[OwnershipReassignment], int]:
        self._session.flush()
        stmt = select(OwnershipReassignmentModel)
        count_stmt = select(OwnershipReassignmentModel.reassignment_id)
        if from_user_id is not None:
            from_user = uuid_to_db(from_user_id)
            stmt = stmt.where(OwnershipReassignmentModel.from_user_id == from_user)
            count_stmt = count_stmt.where(OwnershipReassignmentModel.from_user_id == from_user)
        if to_user_id is not None:
            to_user = uuid_to_db(to_user_id)
            stmt = stmt.where(OwnershipReassignmentModel.to_user_id == to_user)
            count_stmt = count_stmt.where(OwnershipReassignmentModel.to_user_id == to_user)
        stmt = stmt.order_by(
            OwnershipReassignmentModel.created_at.desc(),
            OwnershipReassignmentModel.reassignment_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [ownership_reassignment_from_model(row) for row in rows], total

    def reassign(
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
        from_user = uuid_to_db(from_user_id)
        to_user = uuid_to_db(to_user_id)
        counts: dict[str, int] = {}
        for target in ATTRIBUTION_REASSIGNMENT_TARGETS:
            result = self._session.execute(
                update(target.model)
                .where(
                    or_(
                        target.text_column == from_user,
                        target.user_id_column == from_user,
                    )
                )
                .values(
                    {
                        target.text_column.key: to_user,
                        target.user_id_column.key: to_user,
                    }
                )
            )
            counts[target.label] = int(result.rowcount or 0)

        row = OwnershipReassignmentModel(
            reassignment_id=uuid_to_db(reassignment_id),
            from_user_id=from_user,
            to_user_id=to_user,
            reason=reason,
            record_counts=counts,
            created_by=created_by,
            created_by_user_id=(
                uuid_to_db(created_by_user_id)
                if created_by_user_id is not None
                else None
            ),
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return ownership_reassignment_from_model(row)
