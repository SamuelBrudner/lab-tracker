"""Question domain service."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.models import (
    EntityOrigin,
    EntityRef,
    EntityType,
    Note,
    Question,
    QuestionRefactor,
    QuestionStatus,
    QuestionType,
    utc_now,
)
from lab_tracker.patching import NOT_PROVIDED, PatchValue, is_provided
from lab_tracker.services.base import BaseService, IdempotentCreateResult, ServiceContext
from lab_tracker.services.goal_link_cleanup import remove_goal_links_to_entity
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.shared import (
    _ensure_question_parents_dag,
    _ensure_question_status_transition,
    actor_user_fk,
    actor_user_id,
    ensure_non_empty,
    normalize_client_capture_id,
    question_matches_substring,
    terminal_reason_for_patch,
    terminal_reason_for_status,
    unique_ids,
)

if TYPE_CHECKING:
    from lab_tracker.services.entity_version_service import EntityVersionService
    from lab_tracker.services.note_service import NoteService


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


class QuestionService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        notes_provider: Callable[[], NoteService],
        versions: EntityVersionService,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self._notes_provider = notes_provider
        self.versions = versions
        self.authorization = authorization

    @property
    def notes(self) -> NoteService:
        return self._notes_provider()

    def _question_graph(self, project_id: UUID) -> dict[UUID, Question]:
        questions = self.query_from_repository(
            loader=lambda repository: repository.query_questions(
                project_id=project_id,
                limit=None,
                offset=0,
            ),
        )
        return {question.question_id: question for question in questions}

    def create_question(
        self,
        project_id: UUID,
        text: str,
        question_type: QuestionType,
        *,
        hypothesis: str | None = None,
        status: QuestionStatus = QuestionStatus.STAGED,
        client_capture_id: str | None = None,
        terminal_reason: str | None = None,
        parent_question_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Question:
        return self.create_question_result(
            project_id,
            text,
            question_type,
            hypothesis=hypothesis,
            status=status,
            client_capture_id=client_capture_id,
            terminal_reason=terminal_reason,
            parent_question_ids=parent_question_ids,
            actor=actor,
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
        ).entity

    def create_question_result(
        self,
        project_id: UUID,
        text: str,
        question_type: QuestionType,
        *,
        hypothesis: str | None = None,
        status: QuestionStatus = QuestionStatus.STAGED,
        client_capture_id: str | None = None,
        terminal_reason: str | None = None,
        parent_question_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> IdempotentCreateResult[Question]:
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        ensure_non_empty(text, "text")
        resolved_text = text.strip()
        resolved_hypothesis = hypothesis.strip() if hypothesis else None
        resolved_client_capture_id = normalize_client_capture_id(client_capture_id)
        question_id = uuid4()
        parent_ids = unique_ids(parent_question_ids)
        for parent_id in parent_ids:
            parent = self.get_question(parent_id)
            if parent.project_id != project_id:
                raise ValidationError("Parent question must belong to the same project.")
        _ensure_question_parents_dag(
            question_id,
            parent_ids,
            self._question_graph(project_id),
        )
        resolved_terminal_reason = terminal_reason_for_status(
            None,
            status,
            QuestionStatus.ABANDONED,
            terminal_reason,
            entity_name="Question",
        )
        if resolved_client_capture_id is not None:
            existing = self._find_client_capture_question(
                project_id,
                resolved_client_capture_id,
            )
            if existing is not None:
                self._ensure_matching_capture_question(
                    existing,
                    text=resolved_text,
                    question_type=question_type,
                    hypothesis=resolved_hypothesis,
                    status=status,
                    terminal_reason=resolved_terminal_reason,
                    parent_question_ids=parent_ids,
                    origin=origin,
                    change_set_id=change_set_id,
                    origin_provider=origin_provider,
                    origin_model=origin_model,
                    origin_prompt_version=origin_prompt_version,
                    client_capture_id=resolved_client_capture_id,
                )
                return IdempotentCreateResult("reused", existing)
        question = Question(
            question_id=question_id,
            project_id=project_id,
            text=resolved_text,
            question_type=question_type,
            hypothesis=resolved_hypothesis,
            status=status,
            client_capture_id=resolved_client_capture_id,
            terminal_reason=resolved_terminal_reason,
            parent_question_ids=parent_ids,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
        )
        try:
            unit_of_work = (
                self.recoverable_unit_of_work
                if resolved_client_capture_id is not None
                else self.unit_of_work
            )
            with unit_of_work() as repository:
                repository.questions.save(question)
                self.versions.record_entity_version(
                    repository,
                    entity_type=EntityType.QUESTION,
                    entity_id=question.question_id,
                    entity=question,
                    actor=actor,
                )
        except IntegrityError as exc:
            if resolved_client_capture_id is None:
                raise
            existing = self._find_client_capture_question(
                project_id,
                resolved_client_capture_id,
            )
            if existing is None:
                raise
            self._ensure_matching_capture_question(
                existing,
                text=resolved_text,
                question_type=question_type,
                hypothesis=resolved_hypothesis,
                status=status,
                terminal_reason=resolved_terminal_reason,
                parent_question_ids=parent_ids,
                origin=origin,
                change_set_id=change_set_id,
                origin_provider=origin_provider,
                origin_model=origin_model,
                origin_prompt_version=origin_prompt_version,
                client_capture_id=resolved_client_capture_id,
                cause=exc,
            )
            return IdempotentCreateResult("reused", existing)
        return IdempotentCreateResult("created", question)

    def _find_client_capture_question(
        self,
        project_id: UUID,
        client_capture_id: str,
    ) -> Question | None:
        questions = self.query_from_repository(
            loader=lambda repository: repository.query_questions(
                project_id=project_id,
                client_capture_id=client_capture_id,
                limit=1,
                offset=0,
            ),
        )
        return questions[0] if questions else None

    @staticmethod
    def _ensure_matching_capture_question(
        existing: Question,
        *,
        text: str,
        question_type: QuestionType,
        hypothesis: str | None,
        status: QuestionStatus,
        terminal_reason: str | None,
        parent_question_ids: Iterable[UUID],
        origin: EntityOrigin,
        change_set_id: UUID | None,
        origin_provider: str | None,
        origin_model: str | None,
        origin_prompt_version: str | None,
        client_capture_id: str,
        cause: Exception | None = None,
    ) -> None:
        supplied = {
            "text": text,
            "question_type": question_type,
            "hypothesis": hypothesis,
            "status": status,
            "terminal_reason": terminal_reason,
            "parent_question_ids": sorted(str(value) for value in parent_question_ids),
            "origin": origin,
            "change_set_id": change_set_id,
            "origin_provider": origin_provider,
            "origin_model": origin_model,
            "origin_prompt_version": origin_prompt_version,
        }
        stored = {
            "text": existing.text,
            "question_type": existing.question_type,
            "hypothesis": existing.hypothesis,
            "status": existing.status,
            "terminal_reason": existing.terminal_reason,
            "parent_question_ids": sorted(str(value) for value in existing.parent_question_ids),
            "origin": existing.origin,
            "change_set_id": existing.change_set_id,
            "origin_provider": existing.origin_provider,
            "origin_model": existing.origin_model,
            "origin_prompt_version": existing.origin_prompt_version,
        }
        conflicts = [field for field, value in supplied.items() if stored[field] != value]
        if conflicts:
            error = ConflictError(
                "Question client_capture_id "
                f"{client_capture_id!r} was already used with different "
                f"field(s): {', '.join(conflicts)}."
            )
            if cause is not None:
                raise error from cause
            raise error

    def get_question(self, question_id: UUID) -> Question:
        return self.get_from_repository(
            entity_id=question_id,
            label="Question",
            loader=lambda repository: repository.questions.get(question_id),
        )

    def get_question_for_read(
        self,
        question_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Question:
        question = self.get_question(question_id)
        if not self.authorization.can_read(question.project_id, actor=actor):
            raise NotFoundError("Question does not exist.")
        return question

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
        questions = self.query_from_repository(
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
        )
        if search is not None and search.strip():
            questions = [
                question for question in questions if question_matches_substring(question, search)
            ]
        return questions

    def update_question(
        self,
        question_id: UUID,
        *,
        text: PatchValue[str | None] = NOT_PROVIDED,
        question_type: PatchValue[QuestionType | None] = NOT_PROVIDED,
        hypothesis: PatchValue[str | None] = NOT_PROVIDED,
        status: PatchValue[QuestionStatus | None] = NOT_PROVIDED,
        terminal_reason: PatchValue[str | None] = NOT_PROVIDED,
        parent_question_ids: PatchValue[Iterable[UUID] | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Question:
        with self.application_transaction():
            located_question = self.get_question(question_id)
            project_id = located_question.project_id
            self.authorization.require_contributor(project_id, actor=actor)
            self.repository.lock_project_question_dag(project_id)

            # Question persistence writes a complete snapshot, including
            # parent and supersession fields, even when a command only changes
            # text or status. Every update must therefore serialize with DAG
            # mutations. Never use locator state after the wait: the repository
            # has refreshed its identity map so this read observes the winning
            # transaction before validation or mutation.
            question = self.get_question(question_id)
            return self._update_question_in_transaction(
                question,
                text=text,
                question_type=question_type,
                hypothesis=hypothesis,
                status=status,
                terminal_reason=terminal_reason,
                parent_question_ids=parent_question_ids,
                actor=actor,
                origin=origin,
                change_set_id=change_set_id,
                origin_provider=origin_provider,
                origin_model=origin_model,
                origin_prompt_version=origin_prompt_version,
            )

    def _update_question_in_transaction(
        self,
        question: Question,
        *,
        text: PatchValue[str | None],
        question_type: PatchValue[QuestionType | None],
        hypothesis: PatchValue[str | None],
        status: PatchValue[QuestionStatus | None],
        terminal_reason: PatchValue[str | None],
        parent_question_ids: PatchValue[Iterable[UUID] | None],
        actor: AuthContext | None,
        origin: EntityOrigin | None,
        change_set_id: UUID | None,
        origin_provider: str | None,
        origin_model: str | None,
        origin_prompt_version: str | None,
    ) -> Question:
        before = question.model_copy(deep=True)
        if is_provided(text):
            if text is None:
                raise ValidationError("text must not be null.")
            ensure_non_empty(text, "text")
            question.text = text.strip()
        if is_provided(question_type):
            if question_type is None:
                raise ValidationError("question_type must not be null.")
            question.question_type = question_type
        if is_provided(hypothesis):
            question.hypothesis = hypothesis.strip() if hypothesis else None
        current_status = question.status
        if is_provided(status):
            if status is None:
                raise ValidationError("status must not be null.")
            next_status = status
            _ensure_question_status_transition(current_status, status)
        else:
            next_status = current_status
        resolved_terminal_reason = terminal_reason_for_patch(
            current_status,
            next_status,
            QuestionStatus.ABANDONED,
            terminal_reason,
            entity_name="Question",
        )
        if is_provided(status):
            question.status = status
        if is_provided(resolved_terminal_reason):
            question.terminal_reason = resolved_terminal_reason
        if is_provided(parent_question_ids):
            if parent_question_ids is None:
                raise ValidationError("parent_question_ids must not be null.")
            parent_ids = unique_ids(parent_question_ids)
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
        if origin is not None:
            question.origin = origin
        if change_set_id is not None:
            question.change_set_id = change_set_id
        if origin_provider is not None:
            question.origin_provider = origin_provider
        if origin_model is not None:
            question.origin_model = origin_model
        if origin_prompt_version is not None:
            question.origin_prompt_version = origin_prompt_version
        if question == before:
            return question
        question.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.questions.save(question)
            self.versions.record_entity_version(
                repository,
                entity_type=EntityType.QUESTION,
                entity_id=question.question_id,
                entity=question,
                actor=actor,
            )
        return question

    def list_question_refactors(
        self,
        question_id: UUID,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[QuestionRefactor]:
        refactors = self.query_from_repository(
            loader=lambda repository: repository.query_question_refactors(
                question_id=question_id,
                limit=limit,
                offset=offset,
            ),
        )
        return refactors

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
        with self.application_transaction():
            located_source = self.get_question(question_id)
            project_id = located_source.project_id
            self.authorization.require_contributor(project_id, actor=actor)
            self.repository.lock_project_question_dag(project_id)

            # Re-read after waiting for the project lock: another command may
            # have superseded the source or changed the parent graph.
            source = self.get_question(question_id)
            return self._refactor_question_under_dag_lock(
                source,
                replacement_text=replacement_text,
                replacement_question_type=replacement_question_type,
                replacement_status=replacement_status,
                reason=reason,
                replacement_hypothesis=replacement_hypothesis,
                replacement_parent_question_ids=replacement_parent_question_ids,
                child_question_ids_to_reparent=child_question_ids_to_reparent,
                note_ids_to_retarget=note_ids_to_retarget,
                actor=actor,
            )

    def _refactor_question_under_dag_lock(
        self,
        source: Question,
        *,
        replacement_text: str,
        replacement_question_type: QuestionType,
        replacement_status: QuestionStatus,
        reason: str,
        replacement_hypothesis: str | None,
        replacement_parent_question_ids: Iterable[UUID] | None,
        child_question_ids_to_reparent: Iterable[UUID] | None,
        note_ids_to_retarget: Iterable[UUID] | None,
        actor: AuthContext | None,
    ) -> QuestionRefactorResult:
        if source.status not in {QuestionStatus.STAGED, QuestionStatus.ACTIVE}:
            raise ValidationError("Only staged or active questions can be refactored.")
        if source.superseded_by_question_id is not None:
            raise ValidationError("Question has already been superseded.")
        if replacement_status not in {QuestionStatus.STAGED, QuestionStatus.ACTIVE}:
            raise ValidationError("Replacement question status must be staged or active.")
        ensure_non_empty(replacement_text, "replacement.text")
        ensure_non_empty(reason, "reason")
        parent_ids = (
            list(source.parent_question_ids)
            if replacement_parent_question_ids is None
            else unique_ids(replacement_parent_question_ids)
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
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        graph = self._question_graph(source.project_id)
        graph[replacement.question_id] = replacement
        _ensure_question_parents_dag(replacement.question_id, parent_ids, graph)

        child_ids = unique_ids(child_question_ids_to_reparent)
        note_ids = unique_ids(note_ids_to_retarget)
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
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )

        def _persist(repository) -> None:  # noqa: ANN001
            repository.questions.save(replacement)
            repository.questions.save(source)
            for child in children:
                repository.questions.save(child)
            for note in notes:
                repository.notes.save(note)
            repository.question_refactors.save(refactor)

        with self.unit_of_work() as repository:
            _persist(repository)
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
            note = self.notes.get_note(note_id)
            if note.project_id != source.project_id:
                raise ValidationError("Note must belong to the same project.")
            if not _targets_question(note.targets, source.question_id):
                raise ValidationError("Note does not target the source question.")
            if _targets_question(note.targets, replacement.question_id):
                continue
            notes.append(note)
        return notes

    def delete_question(self, question_id: UUID, *, actor: AuthContext | None = None) -> Question:
        question = self.get_question(question_id)
        self.authorization.require_contributor(question.project_id, actor=actor)
        self._ensure_question_not_referenced(question)
        with self.unit_of_work() as repository:
            remove_goal_links_to_entity(
                repository,
                entity_type=EntityType.QUESTION,
                entity_id=question_id,
            )
            repository.questions.delete(question_id)
        return question

    def _ensure_question_not_referenced(self, question: Question) -> None:
        datasets = self.query_from_repository(
            loader=lambda repository: repository.query_datasets(
                project_id=question.project_id,
                limit=None,
                offset=0,
            ),
        )
        if any(dataset.primary_question_id == question.question_id for dataset in datasets):
            raise ValidationError(
                "Question cannot be deleted while datasets use it as their primary question."
            )
        if any(
            link.question_id == question.question_id
            for dataset in datasets
            for link in dataset.question_links
        ):
            raise ValidationError(
                "Question cannot be deleted while datasets link to it."
            )
        sessions = self.query_from_repository(
            loader=lambda repository: repository.query_sessions(
                project_id=question.project_id,
                limit=None,
                offset=0,
            ),
        )
        if any(session.primary_question_id == question.question_id for session in sessions):
            raise ValidationError(
                "Question cannot be deleted while sessions use it as their primary question."
            )
        claims = self.query_from_repository(
            loader=lambda repository: repository.query_claims(
                project_id=question.project_id,
                limit=None,
                offset=0,
            ),
        )
        if any(question.question_id in claim.answers_question_ids for claim in claims):
            raise ValidationError(
                "Question cannot be deleted while claims answer it."
            )


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
