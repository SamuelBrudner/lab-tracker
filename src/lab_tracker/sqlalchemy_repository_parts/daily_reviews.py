"""Daily graph review SQLAlchemy repository."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    DailyGraphReviewChangeSetModel,
    DailyGraphReviewModel,
)
from lab_tracker.models import DailyGraphReview, DailyGraphReviewStatus
from lab_tracker.repository import EntityRepository

from .common import apply_pagination, count_from_statement, replace_child_rows


def _uuid(value: str | None) -> UUID | None:
    return UUID(value) if value else None


def _dict(value: object) -> dict[str, object]:
    return dict(value or {})


def review_to_model(review: DailyGraphReview) -> DailyGraphReviewModel:
    return DailyGraphReviewModel(
        review_id=str(review.review_id),
        project_id=str(review.project_id),
        window_start=review.window_start,
        window_end=review.window_end,
        status=review.status.value,
        summary=review.summary,
        error_metadata=dict(review.error_metadata),
        created_by=review.created_by,
        created_at=review.created_at,
        updated_at=review.updated_at,
        reviewed_at=review.reviewed_at,
        reviewed_by=review.reviewed_by,
    )


def apply_review_to_model(row: DailyGraphReviewModel, review: DailyGraphReview) -> None:
    row.project_id = str(review.project_id)
    row.window_start = review.window_start
    row.window_end = review.window_end
    row.status = review.status.value
    row.summary = review.summary
    row.error_metadata = dict(review.error_metadata)
    row.created_by = review.created_by
    row.created_at = review.created_at
    row.updated_at = review.updated_at
    row.reviewed_at = review.reviewed_at
    row.reviewed_by = review.reviewed_by


def review_from_model(
    row: DailyGraphReviewModel,
    *,
    change_set_ids: list[UUID] | None = None,
) -> DailyGraphReview:
    return DailyGraphReview(
        review_id=UUID(row.review_id),
        project_id=UUID(row.project_id),
        window_start=row.window_start,
        window_end=row.window_end,
        status=DailyGraphReviewStatus(row.status),
        summary=row.summary,
        change_set_ids=list(change_set_ids or []),
        error_metadata=_dict(row.error_metadata),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        reviewed_at=row.reviewed_at,
        reviewed_by=row.reviewed_by,
    )


class SQLAlchemyDailyGraphReviewRepository(EntityRepository[DailyGraphReview]):
    def __init__(self, session: OrmSession) -> None:
        self._session = session

    def _change_set_ids_for(self, review_ids: list[str]) -> dict[str, list[UUID]]:
        if not review_ids:
            return {}
        rows = list(
            self._session.scalars(
                select(DailyGraphReviewChangeSetModel)
                .where(DailyGraphReviewChangeSetModel.review_id.in_(review_ids))
                .order_by(
                    DailyGraphReviewChangeSetModel.review_id,
                    DailyGraphReviewChangeSetModel.sequence,
                )
            )
        )
        change_set_map: dict[str, list[UUID]] = {}
        for row in rows:
            resolved = _uuid(row.change_set_id)
            if resolved is not None:
                change_set_map.setdefault(row.review_id, []).append(resolved)
        return change_set_map

    def _from_rows(self, rows: list[DailyGraphReviewModel]) -> list[DailyGraphReview]:
        change_set_map = self._change_set_ids_for([row.review_id for row in rows])
        return [
            review_from_model(row, change_set_ids=change_set_map.get(row.review_id, []))
            for row in rows
        ]

    def get(self, entity_id: UUID) -> DailyGraphReview | None:
        self._session.flush()
        row = self._session.get(DailyGraphReviewModel, str(entity_id))
        if row is None:
            return None
        return self._from_rows([row])[0]

    def list(self) -> list[DailyGraphReview]:
        self._session.flush()
        rows = list(
            self._session.scalars(
                select(DailyGraphReviewModel).order_by(
                    DailyGraphReviewModel.window_end.desc(),
                    DailyGraphReviewModel.review_id,
                )
            )
        )
        return self._from_rows(rows)

    def save(self, entity: DailyGraphReview) -> None:
        entity_id = str(entity.review_id)
        row = self._session.get(DailyGraphReviewModel, entity_id)
        if row is None:
            self._session.add(review_to_model(entity))
        else:
            apply_review_to_model(row, entity)
        replace_child_rows(
            self._session,
            DailyGraphReviewChangeSetModel,
            DailyGraphReviewChangeSetModel.review_id,
            entity_id,
            [
                DailyGraphReviewChangeSetModel(
                    review_id=entity_id,
                    change_set_id=str(change_set_id),
                    sequence=index,
                )
                for index, change_set_id in enumerate(entity.change_set_ids, start=1)
            ],
        )

    def delete(self, entity_id: UUID) -> DailyGraphReview | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        row = self._session.get(DailyGraphReviewModel, str(entity_id))
        if row is not None:
            self._session.delete(row)
        return entity

    def query(
        self,
        *,
        project_id: UUID | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[DailyGraphReview], int]:
        self._session.flush()
        stmt = select(DailyGraphReviewModel)
        count_stmt = select(DailyGraphReviewModel.review_id)
        if project_id is not None:
            stmt = stmt.where(DailyGraphReviewModel.project_id == str(project_id))
            count_stmt = count_stmt.where(DailyGraphReviewModel.project_id == str(project_id))
        if status is not None:
            stmt = stmt.where(DailyGraphReviewModel.status == status)
            count_stmt = count_stmt.where(DailyGraphReviewModel.status == status)
        stmt = stmt.order_by(
            DailyGraphReviewModel.window_end.desc(),
            DailyGraphReviewModel.review_id,
        )
        total = count_from_statement(self._session, count_stmt)
        rows = list(self._session.scalars(apply_pagination(stmt, limit=limit, offset=offset)))
        return self._from_rows(rows), total
