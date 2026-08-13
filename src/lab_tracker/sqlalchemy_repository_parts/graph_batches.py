"""Graph draft batch settings and run repositories."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    GraphChangeSetModel,
    GraphDraftBatchRunModel,
    GraphDraftBatchSettingsModel,
    ReviewEmailOutboxModel,
)
from lab_tracker.db_types import ensure_uuid
from lab_tracker.models import (
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftBatchSettings,
    GraphDraftBatchTrigger,
    ReviewEmailDelivery,
    ReviewEmailDeliveryStatus,
)
from lab_tracker.repository import EntityRepository, ReviewEmailOutboxRepository
from lab_tracker.sqlalchemy_mapper_parts.common import as_utc

from .common import apply_pagination, count_from_statement


def _uuid(value: str | None) -> UUID | None:
    return ensure_uuid(value) if value else None


def _uuid_str(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value or {})


def _as_utc_optional(value: datetime | None) -> datetime | None:
    return as_utc(value) if value is not None else None


def _for_reviewer(
    stmt,
    *,
    review_assignee_user_id: UUID | None,
    review_assignee: str | None,
):
    if review_assignee_user_id is not None:
        return stmt.where(
            GraphDraftBatchRunModel.review_assignee_user_id
            == str(review_assignee_user_id)
        )
    if review_assignee is not None:
        return stmt.where(
            GraphDraftBatchRunModel.review_assignee_user_id.is_(None),
            GraphDraftBatchRunModel.review_assignee == review_assignee,
        )
    return stmt.where(
        GraphDraftBatchRunModel.review_assignee_user_id.is_(None),
        GraphDraftBatchRunModel.review_assignee.is_(None),
    )


def settings_to_model(settings: GraphDraftBatchSettings) -> GraphDraftBatchSettingsModel:
    return GraphDraftBatchSettingsModel(
        settings_id=str(settings.settings_id),
        project_id=str(settings.project_id),
        user_id=_uuid_str(settings.user_id),
        enabled=settings.enabled,
        cadence_minutes=settings.cadence_minutes,
        run_at_local_time=settings.run_at_local_time,
        timezone_name=settings.timezone_name,
        next_run_at=settings.next_run_at,
        email_notifications_enabled=settings.email_notifications_enabled,
        notification_email=settings.notification_email,
        notification_email_confirmed_at=settings.notification_email_confirmed_at,
        created_at=settings.created_at,
        updated_at=settings.updated_at,
        updated_by=settings.updated_by,
    )


def apply_settings_to_model(
    row: GraphDraftBatchSettingsModel,
    settings: GraphDraftBatchSettings,
) -> None:
    row.project_id = str(settings.project_id)
    row.user_id = _uuid_str(settings.user_id)
    row.enabled = settings.enabled
    row.cadence_minutes = settings.cadence_minutes
    row.run_at_local_time = settings.run_at_local_time
    row.timezone_name = settings.timezone_name
    row.next_run_at = settings.next_run_at
    row.email_notifications_enabled = settings.email_notifications_enabled
    row.notification_email = settings.notification_email
    row.notification_email_confirmed_at = settings.notification_email_confirmed_at
    row.created_at = settings.created_at
    row.updated_at = settings.updated_at
    row.updated_by = settings.updated_by


def settings_from_model(row: GraphDraftBatchSettingsModel) -> GraphDraftBatchSettings:
    return GraphDraftBatchSettings(
        settings_id=ensure_uuid(row.settings_id),
        project_id=ensure_uuid(row.project_id),
        user_id=_uuid(row.user_id),
        enabled=row.enabled,
        cadence_minutes=row.cadence_minutes,
        run_at_local_time=row.run_at_local_time,
        timezone_name=row.timezone_name,
        next_run_at=_as_utc_optional(row.next_run_at),
        # SQLAlchemy column defaults are applied on flush; direct model
        # construction in mapper callers can still expose ``None`` here.
        email_notifications_enabled=bool(row.email_notifications_enabled),
        notification_email=row.notification_email,
        notification_email_confirmed_at=_as_utc_optional(row.notification_email_confirmed_at),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        updated_by=row.updated_by,
    )


def run_to_model(run: GraphDraftBatchRun) -> GraphDraftBatchRunModel:
    return GraphDraftBatchRunModel(
        run_id=str(run.run_id),
        project_id=str(run.project_id),
        trigger=run.trigger.value,
        status=run.status.value,
        window_start=run.window_start,
        window_end=run.window_end,
        note_count=run.note_count,
        source_note_ids=[str(note_id) for note_id in run.source_note_ids],
        batch_key=run.batch_key,
        user_hint=run.user_hint,
        change_set_id=str(run.change_set_id) if run.change_set_id is not None else None,
        summary=run.summary,
        error_metadata=dict(run.error_metadata),
        claim_token=_uuid_str(run.claim_token),
        claimed_at=run.claimed_at,
        lease_expires_at=run.lease_expires_at,
        attempt_count=run.attempt_count,
        created_at=run.created_at,
        updated_at=run.updated_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_by=run.created_by,
        created_by_user_id=_uuid_str(run.created_by_user_id),
        review_assignee=run.review_assignee,
        review_assignee_user_id=_uuid_str(run.review_assignee_user_id),
    )


def apply_run_to_model(row: GraphDraftBatchRunModel, run: GraphDraftBatchRun) -> None:
    row.project_id = str(run.project_id)
    row.trigger = run.trigger.value
    row.status = run.status.value
    row.window_start = run.window_start
    row.window_end = run.window_end
    row.note_count = run.note_count
    row.source_note_ids = [str(note_id) for note_id in run.source_note_ids]
    row.batch_key = run.batch_key
    row.user_hint = run.user_hint
    row.change_set_id = str(run.change_set_id) if run.change_set_id is not None else None
    row.summary = run.summary
    row.error_metadata = dict(run.error_metadata)
    row.claim_token = _uuid_str(run.claim_token)
    row.claimed_at = run.claimed_at
    row.lease_expires_at = run.lease_expires_at
    row.attempt_count = run.attempt_count
    row.created_at = run.created_at
    row.updated_at = run.updated_at
    row.started_at = run.started_at
    row.finished_at = run.finished_at
    row.created_by = run.created_by
    row.created_by_user_id = _uuid_str(run.created_by_user_id)
    row.review_assignee = run.review_assignee
    row.review_assignee_user_id = _uuid_str(run.review_assignee_user_id)


def run_from_model(row: GraphDraftBatchRunModel) -> GraphDraftBatchRun:
    return GraphDraftBatchRun(
        run_id=ensure_uuid(row.run_id),
        project_id=ensure_uuid(row.project_id),
        trigger=GraphDraftBatchTrigger(row.trigger),
        status=GraphDraftBatchRunStatus(row.status),
        window_start=as_utc(row.window_start),
        window_end=as_utc(row.window_end),
        note_count=row.note_count,
        source_note_ids=[
            ensure_uuid(str(note_id)) for note_id in (row.source_note_ids or [])
        ],
        batch_key=row.batch_key,
        user_hint=row.user_hint,
        change_set_id=ensure_uuid(row.change_set_id) if row.change_set_id else None,
        summary=row.summary or "",
        error_metadata=_dict(row.error_metadata),
        claim_token=_uuid(row.claim_token),
        claimed_at=_as_utc_optional(row.claimed_at),
        lease_expires_at=_as_utc_optional(row.lease_expires_at),
        attempt_count=int(row.attempt_count or 0),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        started_at=as_utc(row.started_at),
        finished_at=_as_utc_optional(row.finished_at),
        created_by=row.created_by,
        created_by_user_id=_uuid(row.created_by_user_id),
        review_assignee=row.review_assignee,
        review_assignee_user_id=_uuid(row.review_assignee_user_id),
    )


def email_delivery_to_model(delivery: ReviewEmailDelivery) -> ReviewEmailOutboxModel:
    return ReviewEmailOutboxModel(
        delivery_id=str(delivery.delivery_id),
        change_set_id=_uuid_str(delivery.change_set_id),
        recipient_user_id=_uuid_str(delivery.recipient_user_id),
        event_type=delivery.event_type,
        destination_email=delivery.destination_email,
        idempotency_key=delivery.idempotency_key,
        status=delivery.status,
        attempt_count=delivery.attempt_count,
        next_attempt_at=delivery.next_attempt_at,
        claim_token=_uuid_str(delivery.claim_token),
        claimed_at=delivery.claimed_at,
        lease_expires_at=delivery.lease_expires_at,
        provider_message_id=delivery.provider_message_id,
        last_error=delivery.last_error,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
        accepted_at=delivery.accepted_at,
    )


def apply_email_delivery_to_model(
    row: ReviewEmailOutboxModel,
    delivery: ReviewEmailDelivery,
) -> None:
    row.change_set_id = delivery.change_set_id
    row.recipient_user_id = delivery.recipient_user_id
    row.event_type = delivery.event_type
    row.destination_email = delivery.destination_email
    row.idempotency_key = delivery.idempotency_key
    row.status = delivery.status
    row.attempt_count = delivery.attempt_count
    row.next_attempt_at = delivery.next_attempt_at
    row.claim_token = delivery.claim_token
    row.claimed_at = delivery.claimed_at
    row.lease_expires_at = delivery.lease_expires_at
    row.provider_message_id = delivery.provider_message_id
    row.last_error = delivery.last_error
    row.created_at = delivery.created_at
    row.updated_at = delivery.updated_at
    row.accepted_at = delivery.accepted_at


def email_delivery_from_model(row: ReviewEmailOutboxModel) -> ReviewEmailDelivery:
    return ReviewEmailDelivery(
        delivery_id=ensure_uuid(row.delivery_id),
        change_set_id=_uuid(row.change_set_id),
        recipient_user_id=_uuid(row.recipient_user_id),
        event_type=row.event_type,
        destination_email=row.destination_email,
        idempotency_key=row.idempotency_key,
        status=ReviewEmailDeliveryStatus(row.status),
        attempt_count=row.attempt_count,
        next_attempt_at=_as_utc_optional(row.next_attempt_at),
        claim_token=_uuid(row.claim_token),
        claimed_at=_as_utc_optional(row.claimed_at),
        lease_expires_at=_as_utc_optional(row.lease_expires_at),
        provider_message_id=row.provider_message_id,
        last_error=row.last_error,
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
        accepted_at=_as_utc_optional(row.accepted_at),
    )


class SQLAlchemyGraphDraftBatchSettingsRepository(
    EntityRepository[GraphDraftBatchSettings]
):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> GraphDraftBatchSettings | None:
        self._session.flush()
        row = self._session.get(GraphDraftBatchSettingsModel, str(entity_id))
        return settings_from_model(row) if row is not None else None

    def get_by_project(
        self,
        project_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> GraphDraftBatchSettings | None:
        self._session.flush()
        stmt = select(GraphDraftBatchSettingsModel).where(
            GraphDraftBatchSettingsModel.project_id == str(project_id)
        )
        if user_id is None:
            stmt = stmt.where(GraphDraftBatchSettingsModel.user_id.is_(None))
        else:
            stmt = stmt.where(GraphDraftBatchSettingsModel.user_id == str(user_id))
        row = self._session.scalar(stmt)
        return settings_from_model(row) if row is not None else None

    def list_for_project(self, project_id: UUID) -> list[GraphDraftBatchSettings]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(GraphDraftBatchSettingsModel)
                .where(GraphDraftBatchSettingsModel.project_id == str(project_id))
                .order_by(
                    GraphDraftBatchSettingsModel.user_id,
                    GraphDraftBatchSettingsModel.settings_id,
                )
            )
        )
        return [settings_from_model(row) for row in rows]

    def list(self) -> list[GraphDraftBatchSettings]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(GraphDraftBatchSettingsModel).order_by(
                    GraphDraftBatchSettingsModel.project_id
                )
            )
        )
        return [settings_from_model(row) for row in rows]

    def list_due(self, now: datetime) -> list[GraphDraftBatchSettings]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(GraphDraftBatchSettingsModel)
                .where(GraphDraftBatchSettingsModel.enabled.is_(True))
                .where(GraphDraftBatchSettingsModel.next_run_at <= now)
                .order_by(GraphDraftBatchSettingsModel.next_run_at)
            )
        )
        return [settings_from_model(row) for row in rows]

    def claim_due(
        self,
        settings_id: UUID,
        *,
        observed_next_run_at: datetime,
        next_run_at: datetime,
        updated_at: datetime,
        updated_by: str | None,
    ) -> GraphDraftBatchSettings | None:
        self._session.flush()
        result = self._session.execute(
            update(GraphDraftBatchSettingsModel)
            .where(GraphDraftBatchSettingsModel.settings_id == str(settings_id))
            .where(GraphDraftBatchSettingsModel.enabled.is_(True))
            .where(GraphDraftBatchSettingsModel.next_run_at == observed_next_run_at)
            .values(
                next_run_at=next_run_at,
                updated_at=updated_at,
                updated_by=updated_by,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        row = self._session.get(GraphDraftBatchSettingsModel, str(settings_id))
        if row is None:
            return None
        self._session.refresh(row)
        return settings_from_model(row)

    def save(self, entity: GraphDraftBatchSettings) -> None:
        self._session.flush()
        row = self._session.get(GraphDraftBatchSettingsModel, str(entity.settings_id))
        if row is None:
            self._session.add(settings_to_model(entity))
        else:
            apply_settings_to_model(row, entity)

    def delete(self, entity_id: UUID) -> GraphDraftBatchSettings | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(GraphDraftBatchSettingsModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity


class SQLAlchemyReviewEmailOutboxRepository(ReviewEmailOutboxRepository):
    """SQLAlchemy-backed durable queue for review-email deliveries."""

    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> ReviewEmailDelivery | None:
        self._session.flush()
        row = self._session.get(ReviewEmailOutboxModel, str(entity_id))
        return email_delivery_from_model(row) if row is not None else None

    def list(self) -> list[ReviewEmailDelivery]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(ReviewEmailOutboxModel).order_by(
                    ReviewEmailOutboxModel.created_at,
                    ReviewEmailOutboxModel.delivery_id,
                )
            )
        )
        return [email_delivery_from_model(row) for row in rows]

    def save(self, entity: ReviewEmailDelivery) -> None:
        self._session.flush()
        row = self._session.get(ReviewEmailOutboxModel, str(entity.delivery_id))
        if row is None:
            self._session.add(email_delivery_to_model(entity))
        else:
            apply_email_delivery_to_model(row, entity)

    def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> ReviewEmailDelivery | None:
        self._session.flush()
        row = self._session.scalar(
            select(ReviewEmailOutboxModel).where(
                ReviewEmailOutboxModel.idempotency_key == idempotency_key
            )
        )
        return email_delivery_from_model(row) if row is not None else None

    def claim_next(
        self,
        *,
        now: datetime,
        lease_until: datetime,
        claim_token: UUID,
    ) -> ReviewEmailDelivery | None:
        """Lease one due delivery without allowing two workers to own it."""

        if lease_until <= now:
            raise ValueError("lease_until must be later than now.")

        self._session.flush()
        due_unclaimed = and_(
            ReviewEmailOutboxModel.status.in_(
                [
                    ReviewEmailDeliveryStatus.PENDING,
                    ReviewEmailDeliveryStatus.RETRYABLE,
                ]
            ),
            ReviewEmailOutboxModel.next_attempt_at <= now,
        )
        stale_claim = and_(
            ReviewEmailOutboxModel.status == ReviewEmailDeliveryStatus.SENDING,
            ReviewEmailOutboxModel.lease_expires_at.is_not(None),
            ReviewEmailOutboxModel.lease_expires_at <= now,
        )
        eligible = or_(due_unclaimed, stale_claim)
        candidate_stmt = (
            select(ReviewEmailOutboxModel)
            .where(eligible)
            .order_by(
                ReviewEmailOutboxModel.next_attempt_at,
                ReviewEmailOutboxModel.created_at,
                ReviewEmailOutboxModel.delivery_id,
            )
            .limit(1)
        )
        if self._session.get_bind().dialect.name == "postgresql":
            candidate_stmt = candidate_stmt.with_for_update(skip_locked=True)
        row = self._session.scalar(candidate_stmt)
        if row is None:
            return None

        # The conditional update keeps the SQLite path safe when another
        # transaction selected the same row before either writer acquired the
        # database write lock. PostgreSQL additionally holds a SKIP LOCKED row
        # lock from the selection above.
        result = self._session.execute(
            update(ReviewEmailOutboxModel)
            .where(ReviewEmailOutboxModel.delivery_id == row.delivery_id)
            .where(eligible)
            .values(
                status=ReviewEmailDeliveryStatus.SENDING,
                attempt_count=ReviewEmailOutboxModel.attempt_count + 1,
                claim_token=claim_token,
                claimed_at=now,
                lease_expires_at=lease_until,
                provider_message_id=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        self._session.refresh(row)
        return email_delivery_from_model(row)


class SQLAlchemyGraphDraftBatchRunRepository(EntityRepository[GraphDraftBatchRun]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def get(self, entity_id: UUID) -> GraphDraftBatchRun | None:
        self._session.flush()
        row = self._session.get(GraphDraftBatchRunModel, str(entity_id))
        return run_from_model(row) if row is not None else None

    def get_by_batch_key(self, batch_key: str) -> GraphDraftBatchRun | None:
        self._session.flush()
        row = self._session.scalar(
            select(GraphDraftBatchRunModel).where(GraphDraftBatchRunModel.batch_key == batch_key)
        )
        return run_from_model(row) if row is not None else None

    def latest_successful_for_project(
        self,
        project_id: UUID,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> GraphDraftBatchRun | None:
        self._session.flush()
        stmt = (
            select(GraphDraftBatchRunModel)
            .where(GraphDraftBatchRunModel.project_id == str(project_id))
            .where(
                GraphDraftBatchRunModel.status.in_(
                    [
                        GraphDraftBatchRunStatus.READY.value,
                        GraphDraftBatchRunStatus.SKIPPED.value,
                    ]
                )
            )
        )
        # Legacy unassigned runs are their own bucket. Historically this
        # branch omitted the reviewer predicate and therefore allowed one
        # user's cursor to advance another user's review window.
        stmt = _for_reviewer(
            stmt,
            review_assignee_user_id=review_assignee_user_id,
            review_assignee=review_assignee,
        )
        row = self._session.scalar(
            stmt.order_by(GraphDraftBatchRunModel.window_end.desc()).limit(1)
        )
        return run_from_model(row) if row is not None else None

    def latest_for_project(
        self,
        project_id: UUID,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> GraphDraftBatchRun | None:
        self._session.flush()
        stmt = _for_reviewer(
            select(GraphDraftBatchRunModel).where(
                GraphDraftBatchRunModel.project_id == str(project_id)
            ),
            review_assignee_user_id=review_assignee_user_id,
            review_assignee=review_assignee,
        )
        row = self._session.scalar(
            stmt.order_by(
                GraphDraftBatchRunModel.created_at.desc(),
                GraphDraftBatchRunModel.run_id.desc(),
            ).limit(1)
        )
        return run_from_model(row) if row is not None else None

    def active_for_project(
        self,
        project_id: UUID,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> list[GraphDraftBatchRun]:
        self._session.flush()
        stmt = _for_reviewer(
            select(GraphDraftBatchRunModel)
            .where(GraphDraftBatchRunModel.project_id == str(project_id))
            .where(
                GraphDraftBatchRunModel.status.in_(
                    [
                        GraphDraftBatchRunStatus.PENDING.value,
                        GraphDraftBatchRunStatus.RUNNING.value,
                    ]
                )
            ),
            review_assignee_user_id=review_assignee_user_id,
            review_assignee=review_assignee,
        )
        rows = list(
            self._session.scalars(
                stmt.order_by(
                    GraphDraftBatchRunModel.created_at.desc(),
                    GraphDraftBatchRunModel.run_id.desc(),
                )
            )
        )
        return [run_from_model(row) for row in rows]

    def successful_source_note_ids_at_window_end(
        self,
        project_id: UUID,
        window_end: datetime,
        *,
        review_assignee_user_id: UUID | None = None,
        review_assignee: str | None = None,
    ) -> set[UUID]:
        self._session.flush()
        stmt = (
            select(GraphChangeSetModel.source_note_ids)
            .join(
                GraphDraftBatchRunModel,
                GraphDraftBatchRunModel.change_set_id == GraphChangeSetModel.change_set_id,
            )
            .where(GraphDraftBatchRunModel.project_id == str(project_id))
            .where(GraphDraftBatchRunModel.status == GraphDraftBatchRunStatus.READY.value)
            .where(GraphDraftBatchRunModel.window_end == window_end)
        )
        stmt = _for_reviewer(
            stmt,
            review_assignee_user_id=review_assignee_user_id,
            review_assignee=review_assignee,
        )
        rows = self._session.execute(stmt)
        note_ids: set[UUID] = set()
        for (source_note_ids,) in rows:
            note_ids.update(ensure_uuid(str(note_id)) for note_id in (source_note_ids or []))
        return note_ids

    def list(self) -> list[GraphDraftBatchRun]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(GraphDraftBatchRunModel).order_by(
                    GraphDraftBatchRunModel.created_at.desc(),
                    GraphDraftBatchRunModel.run_id,
                )
            )
        )
        return [run_from_model(row) for row in rows]

    def query(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[GraphDraftBatchRun], int]:
        self._session.flush()
        stmt = select(GraphDraftBatchRunModel)
        count_stmt = select(GraphDraftBatchRunModel.run_id)
        if project_id is not None:
            stmt = stmt.where(GraphDraftBatchRunModel.project_id == str(project_id))
            count_stmt = count_stmt.where(GraphDraftBatchRunModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(GraphDraftBatchRunModel.status == status)
            count_stmt = count_stmt.where(GraphDraftBatchRunModel.status == status)
        stmt = stmt.order_by(
            GraphDraftBatchRunModel.created_at.desc(),
            GraphDraftBatchRunModel.run_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return [run_from_model(row) for row in rows], total

    def claim_next_pending(
        self,
        *,
        claimed_at: datetime,
        lease_until: datetime,
        claim_token: UUID,
    ) -> GraphDraftBatchRun | None:
        if lease_until <= claimed_at:
            raise ValueError("lease_until must be later than claimed_at.")
        self._session.flush()
        eligible = self._claimable_at(claimed_at)
        candidate_stmt = (
            select(GraphDraftBatchRunModel)
            .where(eligible)
            .order_by(GraphDraftBatchRunModel.created_at, GraphDraftBatchRunModel.run_id)
            .limit(1)
        )
        if self._session.get_bind().dialect.name == "postgresql":
            candidate_stmt = candidate_stmt.with_for_update(skip_locked=True)
        row = self._session.scalar(candidate_stmt)
        if row is None:
            return None
        return self._claim_row(
            row.run_id,
            eligible=eligible,
            claimed_at=claimed_at,
            lease_until=lease_until,
            claim_token=claim_token,
        )

    def claim(
        self,
        run_id: UUID,
        *,
        claimed_at: datetime,
        lease_until: datetime,
        claim_token: UUID,
    ) -> GraphDraftBatchRun | None:
        if lease_until <= claimed_at:
            raise ValueError("lease_until must be later than claimed_at.")
        self._session.flush()
        return self._claim_row(
            str(run_id),
            eligible=self._claimable_at(claimed_at),
            claimed_at=claimed_at,
            lease_until=lease_until,
            claim_token=claim_token,
        )

    def _claim_row(
        self,
        run_id: UUID | str,
        *,
        eligible: Any,
        claimed_at: datetime,
        lease_until: datetime,
        claim_token: UUID,
    ) -> GraphDraftBatchRun | None:
        result = self._session.execute(
            update(GraphDraftBatchRunModel)
            .where(GraphDraftBatchRunModel.run_id == str(run_id))
            .where(eligible)
            .values(
                status=GraphDraftBatchRunStatus.RUNNING.value,
                claim_token=str(claim_token),
                claimed_at=claimed_at,
                lease_expires_at=lease_until,
                attempt_count=GraphDraftBatchRunModel.attempt_count + 1,
                started_at=claimed_at,
                updated_at=claimed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        self._session.expire_all()
        claimed = self._session.get(GraphDraftBatchRunModel, str(run_id))
        if claimed is None:
            return None
        self._session.refresh(claimed)
        return run_from_model(claimed)

    def renew_claim(
        self,
        run_id: UUID,
        claim_token: UUID,
        *,
        renewed_at: datetime,
        lease_until: datetime,
    ) -> GraphDraftBatchRun | None:
        if lease_until <= renewed_at:
            raise ValueError("lease_until must be later than renewed_at.")
        self._session.flush()
        result = self._session.execute(
            update(GraphDraftBatchRunModel)
            .where(GraphDraftBatchRunModel.run_id == str(run_id))
            .where(
                GraphDraftBatchRunModel.status
                == GraphDraftBatchRunStatus.RUNNING.value
            )
            .where(GraphDraftBatchRunModel.claim_token == str(claim_token))
            .where(GraphDraftBatchRunModel.lease_expires_at > renewed_at)
            .values(lease_expires_at=lease_until, updated_at=renewed_at)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        self._session.expire_all()
        return self.get(run_id)

    def finish_claim(
        self,
        run: GraphDraftBatchRun,
        claim_token: UUID,
        *,
        finished_at: datetime,
    ) -> GraphDraftBatchRun | None:
        if run.status not in {
            GraphDraftBatchRunStatus.READY,
            GraphDraftBatchRunStatus.SKIPPED,
            GraphDraftBatchRunStatus.FAILED,
        }:
            raise ValueError("A claimed batch run may only finish in a terminal status.")
        self._session.flush()
        result = self._session.execute(
            update(GraphDraftBatchRunModel)
            .where(GraphDraftBatchRunModel.run_id == str(run.run_id))
            .where(
                GraphDraftBatchRunModel.status
                == GraphDraftBatchRunStatus.RUNNING.value
            )
            .where(GraphDraftBatchRunModel.claim_token == str(claim_token))
            .where(GraphDraftBatchRunModel.lease_expires_at > finished_at)
            .values(
                status=run.status.value,
                change_set_id=_uuid_str(run.change_set_id),
                summary=run.summary,
                error_metadata=dict(run.error_metadata),
                claim_token=None,
                claimed_at=None,
                lease_expires_at=None,
                finished_at=finished_at,
                updated_at=finished_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        self._session.expire_all()
        return self.get(run.run_id)

    @staticmethod
    def _claimable_at(now: datetime) -> Any:
        return or_(
            GraphDraftBatchRunModel.status == GraphDraftBatchRunStatus.PENDING.value,
            and_(
                GraphDraftBatchRunModel.status
                == GraphDraftBatchRunStatus.RUNNING.value,
                or_(
                    GraphDraftBatchRunModel.claim_token.is_(None),
                    GraphDraftBatchRunModel.lease_expires_at.is_(None),
                    GraphDraftBatchRunModel.lease_expires_at <= now,
                ),
            ),
        )

    def save(self, entity: GraphDraftBatchRun) -> None:
        self._session.flush()
        row = self._session.get(GraphDraftBatchRunModel, str(entity.run_id))
        if row is None:
            self._session.add(run_to_model(entity))
        else:
            apply_run_to_model(row, entity)

    def delete(self, entity_id: UUID) -> GraphDraftBatchRun | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(GraphDraftBatchRunModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity
