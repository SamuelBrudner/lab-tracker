"""Question domain service mixin."""

from __future__ import annotations

from typing import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Question,
    QuestionStatus,
    QuestionType,
    utc_now,
)
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _actor_user_id,
    _ensure_non_empty,
    _ensure_question_parents_dag,
    _ensure_question_status_transition,
    _is_question_ancestor,
    question_matches_substring,
    _unique_ids,
)


class QuestionServiceMixin:
    def _question_graph(self, project_id: UUID) -> dict[UUID, Question]:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            questions, _ = repository.query_questions(
                project_id=project_id,
                limit=None,
                offset=0,
            )
            return {question.question_id: question for question in questions}
        return self._store.questions

    def create_question(
        self,
        project_id: UUID,
        text: str,
        question_type: QuestionType,
        *,
        hypothesis: str | None = None,
        status: QuestionStatus = QuestionStatus.STAGED,
        parent_question_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> Question:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        _ensure_non_empty(text, "text")
        question_id = uuid4()
        parent_ids = _unique_ids(parent_question_ids)
        for parent_id in parent_ids:
            parent = self.get_question(parent_id)
            if parent.project_id != project_id:
                raise ValidationError("Parent question must belong to the same project.")
        _ensure_question_parents_dag(
            question_id,
            parent_ids,
            self._question_graph(project_id),
        )
        question = Question(
            question_id=question_id,
            project_id=project_id,
            text=text.strip(),
            question_type=question_type,
            hypothesis=hypothesis.strip() if hypothesis else None,
            status=status,
            parent_question_ids=parent_ids,
            created_by=_actor_user_id(actor),
        )
        self._remember_entity("questions", question.question_id, question)
        self._run_repository_write(lambda repository: repository.questions.save(question))
        return question

    def get_question(self, question_id: UUID) -> Question:
        return self._get_from_repository_or_store(
            attribute_name="questions",
            entity_id=question_id,
            label="Question",
            loader=lambda repository: repository.questions.get(question_id),
        )

    def list_questions(
        self,
        *,
        project_id: UUID | None = None,
        status: QuestionStatus | None = None,
        question_type: QuestionType | None = None,
        search: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
    ) -> list[Question]:
        return self.list_questions_filtered(
            project_id=project_id,
            status=status,
            question_type=question_type,
            search=search,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
        )

    def list_questions_filtered(
        self,
        *,
        project_id: UUID | None = None,
        status: QuestionStatus | None = None,
        question_type: QuestionType | None = None,
        search: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
    ) -> list[Question]:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            questions, _ = repository.query_questions(
                project_id=project_id,
                status=status.value if status is not None else None,
                question_type=question_type.value if question_type is not None else None,
                parent_question_id=parent_question_id,
                ancestor_question_id=ancestor_question_id,
                limit=None,
                offset=0,
            )
            self._cache_entities(
                "questions",
                questions,
                lambda question: question.question_id,
            )
        else:
            if project_id is None:
                questions = list(self._store.questions.values())
            else:
                questions = [
                    question
                    for question in self._store.questions.values()
                    if question.project_id == project_id
                ]
            if status is not None:
                questions = [question for question in questions if question.status == status]
            if question_type is not None:
                questions = [
                    question for question in questions if question.question_type == question_type
                ]
            if parent_question_id is not None:
                questions = [
                    question
                    for question in questions
                    if parent_question_id in question.parent_question_ids
                ]
            if ancestor_question_id is not None:
                questions = [
                    question
                    for question in questions
                    if question.question_id != ancestor_question_id
                    and _is_question_ancestor(
                        question.question_id,
                        ancestor_question_id,
                        self._store.questions,
                    )
                ]
        if search is not None and search.strip():
            questions = [
                question for question in questions if question_matches_substring(question, search)
            ]
        return questions

    def update_question(
        self,
        question_id: UUID,
        *,
        text: str | None = None,
        question_type: QuestionType | None = None,
        hypothesis: str | None = None,
        status: QuestionStatus | None = None,
        parent_question_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> Question:
        require_role(actor, WRITE_ROLES)
        question = self.get_question(question_id)
        if text is not None:
            _ensure_non_empty(text, "text")
            question.text = text.strip()
        if question_type is not None:
            question.question_type = question_type
        if hypothesis is not None:
            question.hypothesis = hypothesis.strip() if hypothesis else None
        if status is not None:
            _ensure_question_status_transition(question.status, status)
            question.status = status
        if parent_question_ids is not None:
            parent_ids = _unique_ids(parent_question_ids)
            for parent_id in parent_ids:
                parent = self.get_question(parent_id)
                if parent.project_id != question.project_id:
                    raise ValidationError("Parent question must belong to the same project.")
            _ensure_question_parents_dag(
                question.question_id,
                parent_ids,
                self._question_graph(question.project_id),
            )
            question.parent_question_ids = parent_ids
        question.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.questions.save(question))
        return question

    def delete_question(self, question_id: UUID, *, actor: AuthContext | None = None) -> Question:
        require_role(actor, WRITE_ROLES)
        question = self.get_question(question_id)
        self._forget_entity("questions", question_id)
        self._run_repository_write(lambda repository: repository.questions.delete(question_id))
        return question
