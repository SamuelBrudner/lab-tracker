"""Store attributed intent without changing the note or session's existing links."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from lab_tracker.db_models import WorkIntentModel
from lab_tracker.work_intent import WorkIntent, WorkScope


class SQLAlchemyWorkIntentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, scope: WorkScope, work_id: UUID) -> WorkIntent | None:
        return self.for_work(scope, {work_id}).get(work_id)

    def for_work(self, scope: WorkScope, work_ids: set[UUID]) -> dict[UUID, WorkIntent]:
        if not work_ids:
            return {}
        column = WorkIntentModel.note_id if scope == "note" else WorkIntentModel.session_id
        rows = self._session.scalars(
            select(WorkIntentModel)
            .where(column.in_(work_ids))
            .execution_options(populate_existing=True)
        )
        intents = {}
        for row in rows:
            work_id = row.note_id if scope == "note" else row.session_id
            if work_id is not None:
                intents[work_id] = WorkIntent.model_validate(
                    {
                        "scope": scope,
                        "work_id": work_id,
                        "kind": row.kind,
                        "question_id": row.question_id,
                        "question_text": row.question_text,
                        "recorded_by_user_id": row.recorded_by_user_id,
                        "recorded_at": row.recorded_at,
                        "source": row.source,
                        "review_draft_id": row.review_draft_id,
                    }
                )
        return intents

    def save(self, intent: WorkIntent, *, replace_existing: bool) -> bool:
        self._session.flush()
        field = "note_id" if intent.scope == "note" else "session_id"
        values = intent.model_dump(exclude={"scope", "work_id"})
        insert = (
            postgres_insert
            if self._session.get_bind().dialect.name == "postgresql"
            else sqlite_insert
        )
        statement = insert(WorkIntentModel).values({field: intent.work_id, **values})
        if replace_existing:
            statement = statement.on_conflict_do_update(index_elements=[field], set_=values)
        else:
            statement = statement.on_conflict_do_nothing(index_elements=[field])
        result = self._session.execute(statement)
        return getattr(result, "rowcount", 0) == 1
