"""Usage telemetry SQLAlchemy repositories."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, delete, func, or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import UsageEventModel, UsageEventRollupModel
from lab_tracker.models import (
    UsageEvent,
    UsageEventOutcome,
    UsageEventResourceType,
    UsageEventRollup,
    UsageEventSurface,
    UsageEventVerb,
)
from lab_tracker.repository import EntityRepository
from lab_tracker.sqlalchemy_mapper_parts.common import uuid_from_db, uuid_to_db

from .common import apply_pagination, count_from_statement

UsageEventPageCursor = tuple[datetime, UUID]
"""``(occurred_at, event_id)`` of the last usage event on a keyset page."""


def usage_event_to_model(event: UsageEvent) -> UsageEventModel:
    return UsageEventModel(
        event_id=uuid_to_db(event.event_id),
        occurred_at=event.occurred_at,
        verb=event.verb.value,
        resource_type=event.resource_type.value,
        resource_id=_uuid_to_db_optional(event.resource_id),
        actor_user_id=_uuid_to_db_optional(event.actor_user_id),
        actor_role=event.actor_role,
        principal_type=event.principal_type,
        surface=event.surface.value if event.surface is not None else None,
        project_id=_uuid_to_db_optional(event.project_id),
        outcome=event.outcome.value,
        duration_ms=event.duration_ms,
        result_count=event.result_count,
    )


def usage_event_from_model(row: UsageEventModel) -> UsageEvent:
    return UsageEvent(
        event_id=uuid_from_db(row.event_id),
        occurred_at=row.occurred_at,
        verb=UsageEventVerb(row.verb),
        resource_type=UsageEventResourceType(row.resource_type),
        resource_id=_uuid_from_db_optional(row.resource_id),
        actor_user_id=_uuid_from_db_optional(row.actor_user_id),
        actor_role=row.actor_role,
        principal_type=row.principal_type,
        surface=UsageEventSurface(row.surface) if row.surface is not None else None,
        project_id=_uuid_from_db_optional(row.project_id),
        outcome=UsageEventOutcome(row.outcome),
        duration_ms=row.duration_ms,
        result_count=row.result_count,
    )


def usage_event_rollup_to_model(rollup: UsageEventRollup) -> UsageEventRollupModel:
    return UsageEventRollupModel(
        rollup_id=uuid_to_db(rollup.rollup_id),
        day=rollup.day,
        verb=rollup.verb.value,
        resource_type=rollup.resource_type.value,
        project_id=_uuid_to_db_optional(rollup.project_id),
        actor_role=rollup.actor_role,
        principal_type=rollup.principal_type,
        surface=rollup.surface.value if rollup.surface is not None else None,
        outcome=rollup.outcome.value,
        event_count=rollup.event_count,
        total_duration_ms=rollup.total_duration_ms,
        total_result_count=rollup.total_result_count,
        created_at=rollup.created_at,
    )


def usage_event_rollup_from_model(row: UsageEventRollupModel) -> UsageEventRollup:
    return UsageEventRollup(
        rollup_id=uuid_from_db(row.rollup_id),
        day=row.day,
        verb=UsageEventVerb(row.verb),
        resource_type=UsageEventResourceType(row.resource_type),
        project_id=_uuid_from_db_optional(row.project_id),
        actor_role=row.actor_role,
        principal_type=row.principal_type,
        surface=UsageEventSurface(row.surface) if row.surface is not None else None,
        outcome=UsageEventOutcome(row.outcome),
        event_count=row.event_count,
        total_duration_ms=row.total_duration_ms,
        total_result_count=row.total_result_count,
        created_at=row.created_at,
    )


class SQLAlchemyUsageEventRepository(EntityRepository[UsageEvent]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> UsageEvent | None:
        self._session.flush()
        row = self._session.get(UsageEventModel, str(entity_id))
        return usage_event_from_model(row) if row is not None else None

    def list(self) -> list[UsageEvent]:
        self._session.flush()
        rows = self._session.scalars(
            select(UsageEventModel).order_by(
                UsageEventModel.occurred_at,
                UsageEventModel.event_id,
            )
        )
        return [usage_event_from_model(row) for row in rows]

    def save(self, entity: UsageEvent) -> None:
        row = self._session.get(UsageEventModel, str(entity.event_id))
        if row is not None:
            return
        self._session.add(usage_event_to_model(entity))

    def delete(self, entity_id: UUID) -> UsageEvent | None:
        row = self._session.get(UsageEventModel, str(entity_id))
        if row is None:
            return None
        entity = usage_event_from_model(row)
        self._session.delete(row)
        return entity

    def query(
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
        self._session.flush()
        clauses = _usage_event_filter_clauses(
            project_id=project_id,
            verb=verb,
            resource_type=resource_type,
            surface=surface,
            outcome=outcome,
            occurred_before=occurred_before,
            occurred_on_or_after=occurred_on_or_after,
        )
        stmt = select(UsageEventModel).where(*clauses)
        count_stmt = select(UsageEventModel.event_id).where(*clauses)
        stmt = stmt.order_by(UsageEventModel.occurred_at.desc(), UsageEventModel.event_id.desc())
        total = count_from_statement(self._session, count_stmt)
        rows = self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset))
        return [usage_event_from_model(row) for row in rows], total

    def query_page(
        self,
        *,
        project_id: UUID | None = None,
        verb: str | None = None,
        resource_type: str | None = None,
        surface: str | None = None,
        outcome: str | None = None,
        after: UsageEventPageCursor | None = None,
        limit: int,
    ) -> list[UsageEvent]:
        """Return one keyset page in ``query`` order (newest first), without a count.

        ``after`` is the ``(occurred_at, event_id)`` of the last event of the
        previous page. Keyset paging keeps pages stable while new events are
        written, unlike offsets, which shift as rows are inserted ahead of them.
        """

        if limit < 1:
            raise ValueError("Usage event page limit must be positive.")
        self._session.flush()
        clauses = _usage_event_filter_clauses(
            project_id=project_id,
            verb=verb,
            resource_type=resource_type,
            surface=surface,
            outcome=outcome,
            occurred_before=None,
            occurred_on_or_after=None,
        )
        if after is not None:
            after_occurred_at, after_event_id = after
            clauses.append(
                or_(
                    UsageEventModel.occurred_at < after_occurred_at,
                    and_(
                        UsageEventModel.occurred_at == after_occurred_at,
                        UsageEventModel.event_id < after_event_id,
                    ),
                )
            )
        stmt = (
            select(UsageEventModel)
            .where(*clauses)
            .order_by(UsageEventModel.occurred_at.desc(), UsageEventModel.event_id.desc())
            .limit(limit)
        )
        return [usage_event_from_model(row) for row in self._session.scalars(stmt)]


def _usage_event_filter_clauses(
    *,
    project_id: UUID | None,
    verb: str | None,
    resource_type: str | None,
    surface: str | None,
    outcome: str | None,
    occurred_before: datetime | None,
    occurred_on_or_after: datetime | None,
) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = []
    for column, value in (
        (UsageEventModel.project_id, str(project_id) if project_id is not None else None),
        (UsageEventModel.verb, verb),
        (UsageEventModel.resource_type, resource_type),
        (UsageEventModel.surface, surface),
        (UsageEventModel.outcome, outcome),
    ):
        if value is not None:
            clauses.append(column == value)
    if occurred_before is not None:
        clauses.append(UsageEventModel.occurred_at < occurred_before)
    if occurred_on_or_after is not None:
        clauses.append(UsageEventModel.occurred_at >= occurred_on_or_after)
    return clauses


class SQLAlchemyUsageEventRollupRepository(EntityRepository[UsageEventRollup]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> UsageEventRollup | None:
        self._session.flush()
        row = self._session.get(UsageEventRollupModel, str(entity_id))
        return usage_event_rollup_from_model(row) if row is not None else None

    def list(self) -> list[UsageEventRollup]:
        self._session.flush()
        rows = self._session.scalars(
            select(UsageEventRollupModel).order_by(
                UsageEventRollupModel.day,
                UsageEventRollupModel.rollup_id,
            )
        )
        return [usage_event_rollup_from_model(row) for row in rows]

    def save(self, entity: UsageEventRollup) -> None:
        row = self._session.get(UsageEventRollupModel, str(entity.rollup_id))
        if row is None:
            self._session.add(usage_event_rollup_to_model(entity))
            return
        row.event_count = entity.event_count
        row.total_duration_ms = entity.total_duration_ms
        row.total_result_count = entity.total_result_count

    def delete(self, entity_id: UUID) -> UsageEventRollup | None:
        row = self._session.get(UsageEventRollupModel, str(entity_id))
        if row is None:
            return None
        entity = usage_event_rollup_from_model(row)
        self._session.delete(row)
        return entity


def summarize_usage_events(
    session: OrmSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, object]]:
    stmt = select(
        func.date(UsageEventModel.occurred_at).label("day"),
        UsageEventModel.verb,
        UsageEventModel.resource_type,
        UsageEventModel.outcome,
        func.count().label("event_count"),
    )
    if start is not None:
        stmt = stmt.where(UsageEventModel.occurred_at >= start)
    if end is not None:
        stmt = stmt.where(UsageEventModel.occurred_at < end)
    stmt = stmt.group_by(
        "day",
        UsageEventModel.verb,
        UsageEventModel.resource_type,
        UsageEventModel.outcome,
    ).order_by("day", UsageEventModel.verb, UsageEventModel.resource_type)
    return [dict(row._mapping) for row in session.execute(stmt)]


_ROLLUP_READ_BATCH_SIZE = 1000
"""Rows fetched per round trip while streaming pre-cutoff usage events."""

RollupBucketKey = tuple[
    date, str, str, UUID | None, str | None, str | None, str | None, str
]
"""``(day, verb, resource_type, project_id, actor_role, principal_type, surface, outcome)``."""


def rollup_usage_events_before(session: OrmSession, cutoff: datetime) -> int:
    # Stream only the bucket columns in batches instead of hydrating every
    # pre-cutoff event as an ORM row; the bucket map stays small.
    events = session.execute(
        select(
            UsageEventModel.occurred_at,
            UsageEventModel.verb,
            UsageEventModel.resource_type,
            UsageEventModel.project_id,
            UsageEventModel.actor_role,
            UsageEventModel.principal_type,
            UsageEventModel.surface,
            UsageEventModel.outcome,
            UsageEventModel.duration_ms,
            UsageEventModel.result_count,
        )
        .where(UsageEventModel.occurred_at < cutoff)
        .execution_options(yield_per=_ROLLUP_READ_BATCH_SIZE)
    )
    buckets: dict[RollupBucketKey, dict[str, int]] = defaultdict(
        lambda: {"event_count": 0, "duration": 0, "result_count": 0}
    )
    for row in events:
        key = (
            row.occurred_at.date(),
            row.verb,
            row.resource_type,
            row.project_id,
            row.actor_role,
            row.principal_type,
            row.surface,
            row.outcome,
        )
        buckets[key]["event_count"] += 1
        buckets[key]["duration"] += int(row.duration_ms or 0)
        buckets[key]["result_count"] += int(row.result_count or 0)
    existing_rollups = _existing_rollups_by_bucket(session, {key[0] for key in buckets})
    for key, values in buckets.items():
        (
            day,
            verb,
            resource_type,
            project_id,
            actor_role,
            principal_type,
            surface,
            outcome,
        ) = key
        existing = existing_rollups.get(key)
        if existing is not None:
            existing.event_count += values["event_count"]
            existing.total_duration_ms += values["duration"]
            existing.total_result_count += values["result_count"]
        else:
            rollup = UsageEventRollup(
                rollup_id=uuid4(),
                day=day,
                verb=UsageEventVerb(verb),
                resource_type=UsageEventResourceType(resource_type),
                project_id=_uuid_from_db_optional(project_id),
                actor_role=actor_role,
                principal_type=principal_type,
                surface=UsageEventSurface(surface) if surface is not None else None,
                outcome=UsageEventOutcome(outcome),
                event_count=values["event_count"],
                total_duration_ms=values["duration"],
                total_result_count=values["result_count"],
            )
            session.add(usage_event_rollup_to_model(rollup))
    deleted = session.execute(
        delete(UsageEventModel)
        .where(UsageEventModel.occurred_at < cutoff)
        .execution_options(synchronize_session=False)
    ).rowcount
    return int(deleted or 0)


def _existing_rollups_by_bucket(
    session: OrmSession,
    days: set[date],
) -> dict[RollupBucketKey, UsageEventRollupModel]:
    """Load, in one query, the rollups the new buckets may merge into."""

    if not days:
        return {}
    rows = session.scalars(
        select(UsageEventRollupModel)
        .where(UsageEventRollupModel.day.between(min(days), max(days)))
        .order_by(UsageEventRollupModel.day, UsageEventRollupModel.rollup_id)
    )
    existing: dict[RollupBucketKey, UsageEventRollupModel] = {}
    for row in rows:
        # NULL dimensions never collide under the unique constraint, so a
        # bucket can have duplicates; merge into the first, as before.
        existing.setdefault(
            (
                row.day,
                row.verb,
                row.resource_type,
                row.project_id,
                row.actor_role,
                row.principal_type,
                row.surface,
                row.outcome,
            ),
            row,
        )
    return existing


def _uuid_to_db_optional(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _uuid_from_db_optional(raw: str | None) -> UUID | None:
    return uuid_from_db(raw) if raw is not None else None
