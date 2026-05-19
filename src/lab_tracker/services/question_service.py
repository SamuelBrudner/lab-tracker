"""Question domain service mixin."""

from __future__ import annotations

from typing import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    EntityRef,
    EntityType,
    Note,
    Question,
    QuestionRefactor,
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


class QuestionRefactorResult:
    def __init__(
        self,
        *,
        source_question: Question,
        replacement_question: Question,
        refactor: QuestionRefactor,
    ) -> None:
        self.source_question = source_question
        self.replacement_question = replacement_question
        self.refactor = refactor


class QuestionServiceMixin:
    def _question_graph(self, project_id: UUID) -> dict[UUID, Question]:
        questions = self._query_from_repository(
            attribute_name="questions",
            loader=lambda repository: repository.query_questions(
                project_id=project_id,
                limit=None,
                offset=0,
            ),
            entity_id_getter=lambda question: question.question_id,
        )
        if questions is not None:
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
        questions = self._query_from_repository(
            attribute_name="questions",
            loader=lambda repository: repository.query_questions(
                project_id=project_id,
                status=status.value if status is not None else None,
                question_type=question_type.value if question_type is not None else None,
                search=search,
                parent_question_id=parent_question_id,
                ancestor_question_id=ancestor_question_id,
                limit=None,
                offset=0,
            ),
            entity_id_getter=lambda question: question.question_id,
        )
        if questions is None:
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

    def list_question_refactors(
        self,
        question_id: UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[QuestionRefactor]:
        refactors = self._query_from_repository(
            attribute_name="question_refactors",
            loader=lambda repository: repository.query_question_refactors(
                question_id=question_id,
                limit=limit,
                offset=offset,
            ),
            entity_id_getter=lambda refactor: refactor.refactor_id,
        )
        if refactors is not None:
            return refactors
        items = [
            refactor
            for refactor in self._store.question_refactors.values()
            if refactor.source_question_id == question_id
            or refactor.replacement_question_id == question_id
        ]
        items.sort(key=lambda refactor: (refactor.created_at, refactor.refactor_id))
        return self._slice_entities(items, limit=limit, offset=offset)

    def refactor_question(
        self,
        question_id: UUID,
        *,
        replacement_text: str,
        replacement_question_type: QuestionType,
        replacement_status: QuestionStatus,
        reason: str,
        replacement_hypothesis: str | None = None,
        replacement_parent_question_ids: Iterable[UUID] | None = None,
        child_question_ids_to_reparent: Iterable[UUID] | None = None,
        note_ids_to_retarget: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> QuestionRefactorResult:
        require_role(actor, WRITE_ROLES)
        source = self.get_question(question_id)
        if source.status not in {QuestionStatus.STAGED, QuestionStatus.ACTIVE}:
            raise ValidationError("Only staged or active questions can be refactored.")
        if source.superseded_by_question_id is not None:
            raise ValidationError("Question has already been superseded.")
        if replacement_status not in {QuestionStatus.STAGED, QuestionStatus.ACTIVE}:
            raise ValidationError("Replacement question status must be staged or active.")
        _ensure_non_empty(replacement_text, "replacement.text")
        _ensure_non_empty(reason, "reason")
        parent_ids = (
            list(source.parent_question_ids)
            if replacement_parent_question_ids is None
            else _unique_ids(replacement_parent_question_ids)
        )
        if source.question_id in parent_ids:
            raise ValidationError("Replacement question cannot use the source as a parent.")
        for parent_id in parent_ids:
            parent = self.get_question(parent_id)
            if parent.project_id != source.project_id:
                raise ValidationError("Parent question must belong to the same project.")

        replacement = Question(
            question_id=uuid4(),
            project_id=source.project_id,
            text=replacement_text.strip(),
            question_type=replacement_question_type,
            hypothesis=replacement_hypothesis.strip() if replacement_hypothesis else None,
            status=replacement_status,
            parent_question_ids=parent_ids,
            supersedes_question_id=source.question_id,
            created_by=_actor_user_id(actor),
        )
        graph = self._question_graph(source.project_id)
        graph[replacement.question_id] = replacement
        _ensure_question_parents_dag(replacement.question_id, parent_ids, graph)

        child_ids = _unique_ids(child_question_ids_to_reparent)
        note_ids = _unique_ids(note_ids_to_retarget)
        children = self._children_to_reparent(source, replacement, child_ids, graph)
        notes = self._notes_to_retarget(source, replacement, note_ids)

        source_snapshot = source.model_dump(mode="json")
        source.status = QuestionStatus.SUPERSEDED
        source.superseded_by_question_id = replacement.question_id
        source.updated_at = utc_now()

        now = utc_now()
        for child in children:
            child.parent_question_ids = [
                replacement.question_id if parent_id == source.question_id else parent_id
                for parent_id in child.parent_question_ids
            ]
            child.updated_at = now
        for note in notes:
            note.targets = _retarget_question_refs(
                note.targets,
                source_id=source.question_id,
                replacement_id=replacement.question_id,
            )
            note.updated_at = now

        relationship_changes = {
            "child_question_ids_reparented": [str(item.question_id) for item in children],
            "note_ids_retargeted": [str(item.note_id) for item in notes],
            "dataset_session_analysis_claim_links_moved": False,
        }
        refactor = QuestionRefactor(
            refactor_id=uuid4(),
            project_id=source.project_id,
            source_question_id=source.question_id,
            replacement_question_id=replacement.question_id,
            reason=reason.strip(),
            source_snapshot=source_snapshot,
            replacement_snapshot=replacement.model_dump(mode="json"),
            relationship_changes=relationship_changes,
            created_by=_actor_user_id(actor),
        )

        self._remember_entity("questions", source.question_id, source)
        self._remember_entity("questions", replacement.question_id, replacement)
        self._remember_entity("question_refactors", refactor.refactor_id, refactor)
        for child in children:
            self._remember_entity("questions", child.question_id, child)
        for note in notes:
            self._remember_entity("notes", note.note_id, note)

        def _persist(repository) -> None:  # noqa: ANN001
            repository.questions.save(source)
            repository.questions.save(replacement)
            for child in children:
                repository.questions.save(child)
            for note in notes:
                repository.notes.save(note)
            repository.question_refactors.save(refactor)

        self._run_repository_write(_persist)
        return QuestionRefactorResult(
            source_question=source,
            replacement_question=replacement,
            refactor=refactor,
        )

    def _children_to_reparent(
        self,
        source: Question,
        replacement: Question,
        child_ids: list[UUID],
        graph: dict[UUID, Question],
    ) -> list[Question]:
        children: list[Question] = []
        for child_id in child_ids:
            child = self.get_question(child_id)
            if child.project_id != source.project_id:
                raise ValidationError("Child question must belong to the same project.")
            if source.question_id not in child.parent_question_ids:
                raise ValidationError("Child question is not directly parented by the source.")
            parent_ids = [
                replacement.question_id if parent_id == source.question_id else parent_id
                for parent_id in child.parent_question_ids
            ]
            _ensure_question_parents_dag(child.question_id, parent_ids, graph)
            graph[child.question_id] = child.model_copy(update={"parent_question_ids": parent_ids})
            children.append(child)
        return children

    def _notes_to_retarget(
        self,
        source: Question,
        replacement: Question,
        note_ids: list[UUID],
    ) -> list[Note]:
        notes: list[Note] = []
        for note_id in note_ids:
            note = self.get_note(note_id)
            if note.project_id != source.project_id:
                raise ValidationError("Note must belong to the same project.")
            if not _targets_question(note.targets, source.question_id):
                raise ValidationError("Note does not target the source question.")
            if _targets_question(note.targets, replacement.question_id):
                continue
            notes.append(note)
        return notes

    def delete_question(self, question_id: UUID, *, actor: AuthContext | None = None) -> Question:
        require_role(actor, WRITE_ROLES)
        question = self.get_question(question_id)
        self._forget_entity("questions", question_id)
        self._run_repository_write(lambda repository: repository.questions.delete(question_id))
        return question


def _targets_question(targets: Iterable[EntityRef], question_id: UUID) -> bool:
    return any(
        target.entity_type == EntityType.QUESTION and target.entity_id == question_id
        for target in targets
    )


def _retarget_question_refs(
    targets: Iterable[EntityRef],
    *,
    source_id: UUID,
    replacement_id: UUID,
) -> list[EntityRef]:
    next_targets: list[EntityRef] = []
    seen: set[tuple[EntityType, UUID]] = set()
    for target in targets:
        next_target = (
            EntityRef(entity_type=EntityType.QUESTION, entity_id=replacement_id)
            if target.entity_type == EntityType.QUESTION and target.entity_id == source_id
            else target
        )
        key = (next_target.entity_type, next_target.entity_id)
        if key in seen:
            continue
        seen.add(key)
        next_targets.append(next_target)
    return next_targets
