"""Per-domain delegation mixins for LabTrackerAPI.

Split out of api.py to keep the facade file cohesive and give each domain
its own edit locality. These are mixins: LabTrackerAPI inherits them, so
``self`` exposes the composed services and the usage-telemetry helpers
(``_with_usage_event``, ``record_usage_event``) defined on the facade.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from lab_tracker.api_parts._base import _first_uuid
from lab_tracker.auth import AuthContext
from lab_tracker.models import (
    Question,
    UsageEventResourceType,
    UsageEventVerb,
)

if TYPE_CHECKING:
    from lab_tracker.services import QuestionService, ServiceContext

UsageResultT = TypeVar("UsageResultT")


class QuestionsApiMixin:
    if TYPE_CHECKING:
        questions: QuestionService
        _service_context: ServiceContext

        def _with_usage_event(
            self,
            action: Callable[[], UsageResultT],
            *,
            verb: UsageEventVerb,
            resource_type: UsageEventResourceType,
            actor: AuthContext | None = None,
            resource_id: UUID | None = None,
            project_id: UUID | None = None,
            resource_id_attr: str | None = None,
            project_id_attr: str | None = "project_id",
        ) -> UsageResultT: ...

    def create_question(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.questions.create_question(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.QUESTION,
            actor=kwargs.get("actor"),
            resource_id_attr="question_id",
        )

    def create_question_result(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.questions.create_question_result(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.QUESTION,
            actor=kwargs.get("actor"),
            resource_id_attr="question_id",
        )

    def get_question(self, question_id: UUID) -> Question:
        return self.questions.get_question(question_id)

    def list_questions(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.list_questions(*args, **kwargs)

    def list_questions_filtered(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.list_questions_filtered(*args, **kwargs)

    def update_question(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.questions.update_question(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.QUESTION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="question_id",
        )

    def list_question_refactors(self, *args: Any, **kwargs: Any) -> Any:
        return self.questions.list_question_refactors(*args, **kwargs)

    def refactor_question(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.questions.refactor_question(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.QUESTION_REFACTOR,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id_attr=None,
        )

    def delete_question(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.questions.delete_question(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.QUESTION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="question_id",
        )

    def search_questions(
        self,
        query: str,
        *,
        project_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Question]:
        repository = self._service_context.active_repository()
        questions, _ = repository.query_questions(
            project_id=project_id,
            search=query,
            limit=limit,
            offset=offset,
        )
        return questions
