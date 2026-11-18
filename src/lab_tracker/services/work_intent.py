"""Human-only declarations and confirmations of bench-work intent."""

from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.models import Note, QuestionStatus, Session, utc_now
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.shared import actor_user_fk
from lab_tracker.work_intent import WorkIntent, WorkIntentChoice, WorkScope


class WorkIntentService(BaseService):
    def __init__(
        self, context: ServiceContext, *, authorization: ProjectAuthorizationPolicy
    ) -> None:
        super().__init__(context)
        self.authorization = authorization

    def _work(self, scope: WorkScope, work_id: UUID) -> Note | Session:
        work = (
            self.repository.notes.get(work_id)
            if scope == "note"
            else self.repository.sessions.get(work_id)
        )
        if work is None:
            raise NotFoundError("This capture or session does not exist.")
        return work

    def get(
        self, scope: WorkScope, work_id: UUID, *, actor: AuthContext | None
    ) -> WorkIntent | None:
        work = self._work(scope, work_id)
        if not self.authorization.can_read(work.project_id, actor=actor):
            raise NotFoundError("This capture or session does not exist.")
        return self.repository.work_intents.get(scope, work_id)

    def record(
        self,
        scope: WorkScope,
        work_id: UUID,
        choice: WorkIntentChoice,
        *,
        actor: AuthContext | None,
        review_draft_id: UUID | None = None,
        expected_question_text: str | None = None,
    ) -> WorkIntent:
        self.authorization.require_interactive(actor, action="Recording bench-work intent")
        with self.application_transaction():
            work = self._work(scope, work_id)
            self.authorization.require_contributor(work.project_id, actor=actor)
            question_text = None
            if choice.question_id:
                question = self.repository.questions.get(choice.question_id)
                if (
                    question is None
                    or question.project_id != work.project_id
                    or question.status not in {QuestionStatus.ACTIVE, QuestionStatus.STAGED}
                ):
                    raise ValidationError("Choose an active or staged question in this project.")
                question_text = question.text
                if expected_question_text is not None and question_text != expected_question_text:
                    raise ConflictError("That question has changed. Open the full review.")
            intent = WorkIntent(
                scope=scope,
                work_id=work_id,
                kind=choice.kind,
                question_id=choice.question_id,
                question_text=question_text,
                recorded_by_user_id=actor_user_fk(actor, self.repository),
                recorded_at=utc_now(),
                source="confirmed_in_review" if review_draft_id else "declared",
                review_draft_id=review_draft_id,
            )
            if not self.repository.work_intents.save(
                intent, replace_existing=review_draft_id is None
            ):
                raise ConflictError("A scientist has already recorded the purpose of this work.")
            return intent
