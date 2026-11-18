"""Persist explicit review operators independently of capture sources."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from lab_tracker.capture_routing import AssignmentScope, ReviewOperatorAssignment
from lab_tracker.db_models import ReviewOperatorModel


class SQLAlchemyReviewOperatorRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def for_work(
        self, scope: AssignmentScope, work_ids: set[UUID]
    ) -> dict[UUID, ReviewOperatorAssignment]:
        if not work_ids:
            return {}
        column = ReviewOperatorModel.note_id if scope == "note" else ReviewOperatorModel.session_id
        rows = self._session.scalars(
            select(ReviewOperatorModel)
            .where(column.in_(work_ids))
            .execution_options(populate_existing=True)
        )
        assignments = {}
        for row in rows:
            work_id = row.note_id if scope == "note" else row.session_id
            if work_id is not None:
                assignments[work_id] = ReviewOperatorAssignment(
                    scope=scope,
                    work_id=work_id,
                    user_id=row.user_id,
                    assigned_by_user_id=row.assigned_by_user_id,
                    assigned_at=row.assigned_at,
                )
        return assignments

    def save(self, assignment: ReviewOperatorAssignment) -> None:
        field = "note_id" if assignment.scope == "note" else "session_id"
        values = assignment.model_dump(exclude={"scope", "work_id"})
        insert = (
            postgres_insert
            if self._session.get_bind().dialect.name == "postgresql"
            else sqlite_insert
        )
        statement = insert(ReviewOperatorModel).values({field: assignment.work_id, **values})
        self._session.execute(statement.on_conflict_do_update(index_elements=[field], set_=values))
