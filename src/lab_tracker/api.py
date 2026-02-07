"""API scaffolding for lab tracker."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, Role, require_role
from lab_tracker.dependencies import get_active_repository
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimInput,
    ClaimStatus,
    decode_session_link_code,
    Dataset,
    DatasetCommitManifest,
    DatasetCommitManifestInput,
    DatasetStatus,
    DatasetFile,
    EntityRef,
    EntityTagSuggestion,
    ExtractedEntity,
    EntityType,
    Note,
    NoteRawAsset,
    NoteStatus,
    Project,
    ProjectStatus,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionSource,
    QuestionStatus,
    QuestionType,
    Session,
    SessionStatus,
    SessionType,
    TagSuggestionStatus,
    Visualization,
    VisualizationInput,
    utc_now,
)
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.repository import LabTrackerRepository

WRITE_ROLES = {Role.ADMIN, Role.EDITOR}


class InMemoryStore:
    def __init__(self) -> None:
        self.projects: dict[UUID, Project] = {}
        self.questions: dict[UUID, Question] = {}
        self.datasets: dict[UUID, Dataset] = {}
        self.notes: dict[UUID, Note] = {}
        self.sessions: dict[UUID, Session] = {}
        self.acquisition_outputs: dict[UUID, AcquisitionOutput] = {}
        self.analyses: dict[UUID, Analysis] = {}
        self.claims: dict[UUID, Claim] = {}
        self.visualizations: dict[UUID, Visualization] = {}


class LabTrackerAPI:
    def __init__(
        self,
        store: InMemoryStore | None = None,
        *,
        raw_storage: LocalNoteStorage | None = None,
        repository: LabTrackerRepository | None = None,
        allow_in_memory: bool = False,
    ) -> None:
        self._store = store or InMemoryStore()
        self._raw_storage = raw_storage
        self._repository = repository
        self._allow_in_memory = allow_in_memory or store is not None
        if repository is not None:
            self.hydrate_from_repository(repository)

    @classmethod
    def in_memory(
        cls,
        *,
        raw_storage: LocalNoteStorage | None = None,
        store: InMemoryStore | None = None,
    ) -> "LabTrackerAPI":
        return cls(
            store=store,
            raw_storage=raw_storage,
            allow_in_memory=True,
        )

    def _active_repository(self) -> LabTrackerRepository | None:
        return get_active_repository() or self._repository

    def hydrate_from_repository(
        self,
        repository: LabTrackerRepository | None = None,
    ) -> None:
        resolved_repository = repository or self._active_repository()
        if resolved_repository is None:
            return
        self._store.projects = {
            project.project_id: project
            for project in resolved_repository.projects.list()
        }
        self._store.questions = {
            question.question_id: question
            for question in resolved_repository.questions.list()
        }
        self._store.datasets = {
            dataset.dataset_id: dataset
            for dataset in resolved_repository.datasets.list()
        }
        self._store.notes = {
            note.note_id: note
            for note in resolved_repository.notes.list()
        }
        self._store.sessions = {
            session.session_id: session
            for session in resolved_repository.sessions.list()
        }
        self._store.analyses = {
            analysis.analysis_id: analysis
            for analysis in resolved_repository.analyses.list()
        }
        self._store.claims = {
            claim.claim_id: claim
            for claim in resolved_repository.claims.list()
        }
        self._store.visualizations = {
            visualization.viz_id: visualization
            for visualization in resolved_repository.visualizations.list()
        }
        try:
            acquisition_outputs = resolved_repository.acquisition_outputs.list()
        except NotImplementedError:
            acquisition_outputs = []
        self._store.acquisition_outputs = {
            output.output_id: output
            for output in acquisition_outputs
        }

    def _run_repository_write(
        self,
        operation: Callable[[LabTrackerRepository], None],
    ) -> None:
        resolved_repository = self._active_repository()
        if resolved_repository is None:
            if not self._allow_in_memory:
                raise RuntimeError(
                    "In-memory runtime persistence is deprecated. "
                    "Configure a SQLAlchemy repository context, or use "
                    "LabTrackerAPI.in_memory() for explicit non-persistent mode."
                )
            return
        try:
            operation(resolved_repository)
            resolved_repository.commit()
        except Exception:
            resolved_repository.rollback()
            raise

    def create_project(
        self,
        name: str,
        description: str = "",
        status: ProjectStatus = ProjectStatus.ACTIVE,
        *,
        actor: AuthContext | None = None,
        created_by: str | None = None,
    ) -> Project:
        require_role(actor, WRITE_ROLES)
        _ensure_non_empty(name, "name")
        project = Project(
            project_id=uuid4(),
            name=name.strip(),
            description=description.strip(),
            status=status,
            created_by=created_by,
        )
        self._store.projects[project.project_id] = project
        self._run_repository_write(lambda repository: repository.projects.save(project))
        return project

    def get_project(self, project_id: UUID) -> Project:
        return _get_or_raise(self._store.projects, project_id, "Project")

    def list_projects(self) -> list[Project]:
        return list(self._store.projects.values())

    def update_project(
        self,
        project_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        status: ProjectStatus | None = None,
        actor: AuthContext | None = None,
    ) -> Project:
        require_role(actor, WRITE_ROLES)
        project = self.get_project(project_id)
        if name is not None:
            _ensure_non_empty(name, "name")
            project.name = name.strip()
        if description is not None:
            project.description = description.strip()
        if status is not None:
            project.status = status
        project.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.projects.save(project))
        return project

    def delete_project(self, project_id: UUID, *, actor: AuthContext | None = None) -> Project:
        require_role(actor, WRITE_ROLES)
        project = self.get_project(project_id)
        del self._store.projects[project_id]
        self._run_repository_write(
            lambda repository: repository.projects.delete(project_id)
        )
        return project

    def create_question(
        self,
        project_id: UUID,
        text: str,
        question_type: QuestionType,
        *,
        hypothesis: str | None = None,
        status: QuestionStatus = QuestionStatus.STAGED,
        parent_question_ids: Iterable[UUID] | None = None,
        created_from: QuestionSource = QuestionSource.MANUAL,
        actor: AuthContext | None = None,
        created_by: str | None = None,
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
        _ensure_question_parents_dag(question_id, parent_ids, self._store.questions)
        question = Question(
            question_id=question_id,
            project_id=project_id,
            text=text.strip(),
            question_type=question_type,
            hypothesis=hypothesis.strip() if hypothesis else None,
            status=status,
            parent_question_ids=parent_ids,
            created_from=created_from,
            created_by=created_by,
        )
        self._store.questions[question.question_id] = question
        self._run_repository_write(
            lambda repository: repository.questions.save(question)
        )
        return question

    def get_question(self, question_id: UUID) -> Question:
        return _get_or_raise(self._store.questions, question_id, "Question")

    def list_questions(
        self,
        *,
        project_id: UUID | None = None,
        status: QuestionStatus | None = None,
        question_type: QuestionType | None = None,
        created_from: QuestionSource | None = None,
        search: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
    ) -> list[Question]:
        return self.list_questions_filtered(
            project_id=project_id,
            status=status,
            question_type=question_type,
            created_from=created_from,
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
        created_from: QuestionSource | None = None,
        search: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
    ) -> list[Question]:
        if project_id is None:
            questions = list(self._store.questions.values())
        else:
            questions = [q for q in self._store.questions.values() if q.project_id == project_id]
        if status is not None:
            questions = [question for question in questions if question.status == status]
        if question_type is not None:
            questions = [question for question in questions if question.question_type == question_type]
        if created_from is not None:
            questions = [question for question in questions if question.created_from == created_from]
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
            needle = search.casefold()
            questions = [
                question
                for question in questions
                if needle in question.text.casefold()
                or (question.hypothesis and needle in question.hypothesis.casefold())
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
            _ensure_question_parents_dag(question.question_id, parent_ids, self._store.questions)
            question.parent_question_ids = parent_ids
        question.updated_at = utc_now()
        self._run_repository_write(
            lambda repository: repository.questions.save(question)
        )
        return question

    def delete_question(self, question_id: UUID, *, actor: AuthContext | None = None) -> Question:
        require_role(actor, WRITE_ROLES)
        question = self.get_question(question_id)
        del self._store.questions[question_id]
        self._run_repository_write(
            lambda repository: repository.questions.delete(question_id)
        )
        return question

    def create_dataset(
        self,
        project_id: UUID,
        primary_question_id: UUID,
        *,
        secondary_question_ids: Iterable[UUID] | None = None,
        status: DatasetStatus = DatasetStatus.STAGED,
        commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None = None,
        commit_hash: str | None = None,
        actor: AuthContext | None = None,
        created_by: str | None = None,
    ) -> Dataset:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        if primary_question_id is None:
            raise ValidationError("primary_question_id is required.")
        primary_question = self.get_question(primary_question_id)
        if primary_question.project_id != project_id:
            raise ValidationError("Primary question must belong to the same project.")
        secondary_ids = _unique_ids(secondary_question_ids)
        if primary_question_id in secondary_ids:
            raise ValidationError("Primary question cannot be secondary.")
        for question_id in secondary_ids:
            question = self.get_question(question_id)
            if question.project_id != project_id:
                raise ValidationError("Secondary questions must belong to the same project.")
        question_links = [
            QuestionLink(question_id=primary_question_id, role=QuestionLinkRole.PRIMARY),
            *[
                QuestionLink(question_id=question_id, role=QuestionLinkRole.SECONDARY)
                for question_id in secondary_ids
            ],
        ]
        resolved_manifest = _build_commit_manifest(
            commit_manifest,
            question_links,
        )
        self._ensure_source_session_valid(resolved_manifest.source_session_id, project_id)
        resolved_commit_hash = _compute_commit_hash(resolved_manifest)
        _validate_commit_hash(commit_hash, resolved_commit_hash)
        dataset = Dataset(
            dataset_id=uuid4(),
            project_id=project_id,
            commit_hash=resolved_commit_hash,
            primary_question_id=primary_question_id,
            question_links=question_links,
            commit_manifest=resolved_manifest,
            status=status,
            created_by=created_by,
        )
        if status == DatasetStatus.COMMITTED:
            _ensure_primary_question_active(primary_question)
        self._store.datasets[dataset.dataset_id] = dataset
        self._run_repository_write(
            lambda repository: repository.datasets.save(dataset)
        )
        return dataset

    def get_dataset(self, dataset_id: UUID) -> Dataset:
        return _get_or_raise(self._store.datasets, dataset_id, "Dataset")

    def list_datasets(self, *, project_id: UUID | None = None) -> list[Dataset]:
        if project_id is None:
            return list(self._store.datasets.values())
        return [d for d in self._store.datasets.values() if d.project_id == project_id]

    def update_dataset(
        self,
        dataset_id: UUID,
        *,
        status: DatasetStatus | None = None,
        question_links: Iterable[QuestionLink] | None = None,
        commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None = None,
        commit_hash: str | None = None,
        actor: AuthContext | None = None,
    ) -> Dataset:
        require_role(actor, WRITE_ROLES)
        dataset = self.get_dataset(dataset_id)
        was_committed = dataset.status == DatasetStatus.COMMITTED
        if was_committed:
            if commit_hash is not None or question_links is not None or commit_manifest is not None:
                raise ValidationError("Committed datasets are immutable.")
            if status == DatasetStatus.STAGED:
                raise ValidationError("Committed datasets cannot return to staged.")
        if question_links is not None:
            links = list(question_links)
            primary_links = [link for link in links if link.role == QuestionLinkRole.PRIMARY]
            if len(primary_links) != 1:
                raise ValidationError("Dataset must have exactly one primary question link.")
            seen: set[UUID] = set()
            for link in links:
                if link.question_id in seen:
                    raise ValidationError("Duplicate question link.")
                seen.add(link.question_id)
                question = self.get_question(link.question_id)
                if question.project_id != dataset.project_id:
                    raise ValidationError("Question links must belong to the same project.")
            dataset.question_links = links
            dataset.primary_question_id = primary_links[0].question_id
        if commit_manifest is not None or question_links is not None:
            base_manifest = commit_manifest or _manifest_input_from_commit(dataset.commit_manifest)
            resolved_manifest = _build_commit_manifest(
                base_manifest,
                dataset.question_links,
            )
            self._ensure_source_session_valid(resolved_manifest.source_session_id, dataset.project_id)
            resolved_commit_hash = _compute_commit_hash(resolved_manifest)
            _validate_commit_hash(commit_hash, resolved_commit_hash)
            dataset.commit_manifest = resolved_manifest
            dataset.commit_hash = resolved_commit_hash
        else:
            _validate_commit_hash(commit_hash, _compute_commit_hash(dataset.commit_manifest))
        if status is not None:
            if status == DatasetStatus.COMMITTED and dataset.status != DatasetStatus.COMMITTED:
                primary_question = self.get_question(dataset.primary_question_id)
                _ensure_primary_question_active(primary_question)
            dataset.status = status
        dataset.updated_at = utc_now()
        self._run_repository_write(
            lambda repository: repository.datasets.save(dataset)
        )
        return dataset

    def delete_dataset(self, dataset_id: UUID, *, actor: AuthContext | None = None) -> Dataset:
        require_role(actor, WRITE_ROLES)
        dataset = self.get_dataset(dataset_id)
        del self._store.datasets[dataset_id]
        self._run_repository_write(
            lambda repository: repository.datasets.delete(dataset_id)
        )
        return dataset

    def create_note(
        self,
        project_id: UUID,
        raw_content: str | None = None,
        *,
        raw_asset: NoteRawAsset | None = None,
        transcribed_text: str | None = None,
        extracted_entities: Iterable[tuple[str, float, str]] | None = None,
        tag_suggestions: Iterable[EntityTagSuggestion] | None = None,
        targets: Iterable[EntityRef] | None = None,
        metadata: dict[str, str] | None = None,
        status: NoteStatus = NoteStatus.STAGED,
        actor: AuthContext | None = None,
        created_by: str | None = None,
    ) -> Note:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        raw_text = raw_content.strip() if raw_content else ""
        if not raw_text and raw_asset is None:
            raise ValidationError("raw_content or raw_asset must be provided.")
        resolved_targets = list(targets or [])
        for target in resolved_targets:
            self._ensure_target_exists(target, project_id)
        resolved_metadata = _normalize_note_metadata(metadata)
        resolved_entities = [
            _build_extracted_entity(label, confidence, provenance)
            for label, confidence, provenance in (extracted_entities or [])
        ]
        resolved_tag_suggestions = list(tag_suggestions or [])
        note = Note(
            note_id=uuid4(),
            project_id=project_id,
            raw_content=raw_text,
            raw_asset=raw_asset,
            transcribed_text=transcribed_text.strip() if transcribed_text else None,
            extracted_entities=resolved_entities,
            tag_suggestions=resolved_tag_suggestions,
            targets=resolved_targets,
            metadata=resolved_metadata,
            status=status,
            created_by=created_by,
        )
        self._store.notes[note.note_id] = note
        self._run_repository_write(lambda repository: repository.notes.save(note))
        return note

    def upload_note_raw(
        self,
        project_id: UUID,
        content: bytes,
        *,
        filename: str,
        content_type: str,
        transcribed_text: str | None = None,
        extracted_entities: Iterable[tuple[str, float, str]] | None = None,
        targets: Iterable[EntityRef] | None = None,
        metadata: dict[str, str] | None = None,
        status: NoteStatus = NoteStatus.STAGED,
        actor: AuthContext | None = None,
        created_by: str | None = None,
    ) -> Note:
        require_role(actor, WRITE_ROLES)
        if self._raw_storage is None:
            raise ValidationError("Raw storage backend is not configured.")
        asset = self._raw_storage.store(
            content,
            filename=filename,
            content_type=content_type,
        )
        return self.create_note(
            project_id=project_id,
            raw_content=None,
            raw_asset=asset,
            transcribed_text=transcribed_text,
            extracted_entities=extracted_entities,
            targets=targets,
            metadata=metadata,
            status=status,
            actor=actor,
            created_by=created_by,
        )

    def get_note(self, note_id: UUID) -> Note:
        return _get_or_raise(self._store.notes, note_id, "Note")

    def list_notes(self, *, project_id: UUID | None = None) -> list[Note]:
        if project_id is None:
            return list(self._store.notes.values())
        return [n for n in self._store.notes.values() if n.project_id == project_id]

    def update_note(
        self,
        note_id: UUID,
        *,
        transcribed_text: str | None = None,
        extracted_entities: Iterable[tuple[str, float, str]] | None = None,
        targets: Iterable[EntityRef] | None = None,
        metadata: dict[str, str] | None = None,
        status: NoteStatus | None = None,
        actor: AuthContext | None = None,
    ) -> Note:
        require_role(actor, WRITE_ROLES)
        note = self.get_note(note_id)
        if transcribed_text is not None:
            note.transcribed_text = transcribed_text.strip() if transcribed_text else None
        if extracted_entities is not None:
            note.extracted_entities = [
                _build_extracted_entity(label, confidence, provenance)
                for label, confidence, provenance in extracted_entities
            ]
        if targets is not None:
            resolved_targets = list(targets)
            for target in resolved_targets:
                self._ensure_target_exists(target, note.project_id)
            note.targets = resolved_targets
        if metadata is not None:
            note.metadata = _normalize_note_metadata(metadata)
        if status is not None:
            note.status = status
        note.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.notes.save(note))
        return note

    def download_note_raw(self, note_id: UUID) -> tuple[NoteRawAsset, bytes]:
        note = self.get_note(note_id)
        if note.raw_asset is None:
            raise NotFoundError("Note does not have raw content.")
        if self._raw_storage is None:
            raise ValidationError("Raw storage backend is not configured.")
        content = self._raw_storage.read(note.raw_asset.storage_id)
        return note.raw_asset, content

    def suggest_entity_tags(
        self,
        note_id: UUID,
        *,
        mapping: dict[str, list["_TagMapping"]] | None = None,
        provenance: str | None = None,
        actor: AuthContext | None = None,
    ) -> list[EntityTagSuggestion]:
        require_role(actor, WRITE_ROLES)
        note = self.get_note(note_id)
        if not note.extracted_entities:
            return []
        resolved_mapping = mapping or _DEFAULT_TAG_MAP
        if not resolved_mapping:
            return []
        provenance_tag = provenance or _build_note_tag_provenance(note.note_id)
        existing = {_tag_suggestion_key(suggestion) for suggestion in note.tag_suggestions}
        new_suggestions: list[EntityTagSuggestion] = []
        for entity in note.extracted_entities:
            for term in _resolve_tag_mappings(entity.label, resolved_mapping):
                suggestion = _build_entity_tag_suggestion(
                    entity_label=entity.label,
                    term=term,
                    extracted_confidence=entity.confidence,
                    provenance=provenance_tag,
                )
                key = _tag_suggestion_key(suggestion)
                if key in existing:
                    continue
                note.tag_suggestions.append(suggestion)
                new_suggestions.append(suggestion)
                existing.add(key)
        if new_suggestions:
            note.updated_at = utc_now()
            self._run_repository_write(lambda repository: repository.notes.save(note))
        return new_suggestions

    def list_entity_tag_suggestions(
        self,
        note_id: UUID,
        *,
        status: TagSuggestionStatus | None = None,
    ) -> list[EntityTagSuggestion]:
        note = self.get_note(note_id)
        if status is None:
            return list(note.tag_suggestions)
        return [suggestion for suggestion in note.tag_suggestions if suggestion.status == status]

    def review_entity_tag_suggestion(
        self,
        note_id: UUID,
        suggestion_id: UUID,
        *,
        status: TagSuggestionStatus,
        reviewed_by: str | None = None,
        actor: AuthContext | None = None,
    ) -> EntityTagSuggestion:
        require_role(actor, WRITE_ROLES)
        note = self.get_note(note_id)
        for index, suggestion in enumerate(note.tag_suggestions):
            if suggestion.suggestion_id == suggestion_id:
                if status in {TagSuggestionStatus.ACCEPTED, TagSuggestionStatus.REJECTED}:
                    resolved_reviewed_at = utc_now()
                    resolved_reviewed_by = reviewed_by or (str(actor.user_id) if actor else None)
                else:
                    resolved_reviewed_at = None
                    resolved_reviewed_by = None
                updated = replace(
                    suggestion,
                    status=status,
                    reviewed_by=resolved_reviewed_by,
                    reviewed_at=resolved_reviewed_at,
                )
                note.tag_suggestions[index] = updated
                note.updated_at = utc_now()
                self._run_repository_write(
                    lambda repository: repository.notes.save(note)
                )
                return updated
        raise NotFoundError("Tag suggestion does not exist.")

    def delete_note(self, note_id: UUID, *, actor: AuthContext | None = None) -> Note:
        require_role(actor, WRITE_ROLES)
        note = self.get_note(note_id)
        del self._store.notes[note_id]
        self._run_repository_write(lambda repository: repository.notes.delete(note_id))
        return note

    def extract_questions_from_note(
        self,
        note_id: UUID,
        *,
        question_type: QuestionType = QuestionType.OTHER,
        created_from: QuestionSource = QuestionSource.API,
        provenance: str | None = None,
        actor: AuthContext | None = None,
    ) -> list[Question]:
        """Extract candidate questions from a note and stage them."""
        require_role(actor, WRITE_ROLES)
        note = self.get_note(note_id)
        text = _note_text_for_extraction(note)
        candidates = _extract_question_candidates(text)
        if not candidates:
            return []
        existing = {question.text.casefold() for question in self.list_questions(project_id=note.project_id)}
        staged_questions: list[Question] = []
        provenance_tag = provenance or _build_note_provenance(note.note_id)
        for candidate in candidates:
            key = candidate.casefold()
            if key in existing:
                continue
            question = self.create_question(
                project_id=note.project_id,
                text=candidate,
                question_type=question_type,
                status=QuestionStatus.STAGED,
                created_from=created_from,
                actor=actor,
                created_by=provenance_tag,
            )
            staged_questions.append(question)
            existing.add(key)
        return staged_questions

    def create_session(
        self,
        project_id: UUID,
        session_type: SessionType,
        *,
        primary_question_id: UUID | None = None,
        status: SessionStatus = SessionStatus.ACTIVE,
        actor: AuthContext | None = None,
        created_by: str | None = None,
    ) -> Session:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        if session_type == SessionType.SCIENTIFIC and primary_question_id is None:
            raise ValidationError("Scientific sessions require a primary question.")
        if primary_question_id is not None:
            question = self.get_question(primary_question_id)
            if question.project_id != project_id:
                raise ValidationError("Primary question must belong to the same project.")
        session = Session(
            session_id=uuid4(),
            project_id=project_id,
            session_type=session_type,
            status=status,
            primary_question_id=primary_question_id,
            created_by=created_by,
        )
        self._store.sessions[session.session_id] = session
        self._run_repository_write(
            lambda repository: repository.sessions.save(session)
        )
        return session

    def get_session(self, session_id: UUID) -> Session:
        return _get_or_raise(self._store.sessions, session_id, "Session")

    def get_session_by_link_code(self, link_code: str) -> Session:
        _ensure_non_empty(link_code, "link_code")
        try:
            session_id = decode_session_link_code(link_code)
        except ValueError as exc:
            raise ValidationError("Invalid session link code.") from exc
        return self.get_session(session_id)

    def list_sessions(self, *, project_id: UUID | None = None) -> list[Session]:
        if project_id is None:
            return list(self._store.sessions.values())
        return [s for s in self._store.sessions.values() if s.project_id == project_id]

    def update_session(
        self,
        session_id: UUID,
        *,
        status: SessionStatus | None = None,
        ended_at: datetime | None = None,
        actor: AuthContext | None = None,
    ) -> Session:
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        if status is not None:
            session.status = status
        if ended_at is not None:
            session.ended_at = ended_at
        session.updated_at = utc_now()
        self._run_repository_write(
            lambda repository: repository.sessions.save(session)
        )
        return session

    def delete_session(self, session_id: UUID, *, actor: AuthContext | None = None) -> Session:
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        del self._store.sessions[session_id]
        self._run_repository_write(
            lambda repository: repository.sessions.delete(session_id)
        )
        return session

    def register_acquisition_output(
        self,
        session_id: UUID,
        file_path: str,
        checksum: str,
        *,
        size_bytes: int | None = None,
        actor: AuthContext | None = None,
    ) -> AcquisitionOutput:
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Acquisition outputs require an operational session.")
        _ensure_non_empty(file_path, "file_path")
        _ensure_non_empty(checksum, "checksum")
        if size_bytes is not None and size_bytes < 0:
            raise ValidationError("size_bytes must be 0 or greater.")
        cleaned_path = file_path.strip()
        cleaned_checksum = checksum.strip()
        existing = _find_acquisition_output(self._store.acquisition_outputs, session_id, cleaned_path)
        if existing is not None:
            updated = False
            if existing.checksum != cleaned_checksum:
                existing.checksum = cleaned_checksum
                updated = True
            if size_bytes is not None and existing.size_bytes != size_bytes:
                existing.size_bytes = size_bytes
                updated = True
            if updated:
                existing.updated_at = utc_now()
                try:
                    self._run_repository_write(
                        lambda repository: repository.acquisition_outputs.save(existing)
                    )
                except NotImplementedError:
                    pass
            return existing
        output = AcquisitionOutput(
            output_id=uuid4(),
            session_id=session_id,
            file_path=cleaned_path,
            checksum=cleaned_checksum,
            size_bytes=size_bytes,
        )
        self._store.acquisition_outputs[output.output_id] = output
        try:
            self._run_repository_write(
                lambda repository: repository.acquisition_outputs.save(output)
            )
        except NotImplementedError:
            pass
        return output

    def list_acquisition_outputs(
        self,
        *,
        session_id: UUID | None = None,
    ) -> list[AcquisitionOutput]:
        outputs = list(self._store.acquisition_outputs.values())
        if session_id is None:
            return outputs
        return [output for output in outputs if output.session_id == session_id]

    def delete_acquisition_output(
        self, output_id: UUID, *, actor: AuthContext | None = None
    ) -> AcquisitionOutput:
        require_role(actor, WRITE_ROLES)
        output = _get_or_raise(
            self._store.acquisition_outputs,
            output_id,
            "Acquisition output",
        )
        del self._store.acquisition_outputs[output_id]
        try:
            self._run_repository_write(
                lambda repository: repository.acquisition_outputs.delete(output_id)
            )
        except NotImplementedError:
            pass
        return output

    def promote_operational_session(
        self,
        session_id: UUID,
        primary_question_id: UUID,
        *,
        secondary_question_ids: Iterable[UUID] | None = None,
        status: DatasetStatus = DatasetStatus.COMMITTED,
        commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None = None,
        actor: AuthContext | None = None,
        created_by: str | None = None,
    ) -> Dataset:
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Only operational sessions can be promoted to datasets.")
        outputs = self.list_acquisition_outputs(session_id=session.session_id)
        merged_manifest = _merge_acquisition_outputs(commit_manifest, outputs)
        manifest_with_session = _manifest_input_with_source(merged_manifest, session.session_id)
        return self.create_dataset(
            project_id=session.project_id,
            primary_question_id=primary_question_id,
            secondary_question_ids=secondary_question_ids,
            status=status,
            commit_manifest=manifest_with_session,
            actor=actor,
            created_by=created_by,
        )

    def create_analysis(
        self,
        project_id: UUID,
        dataset_ids: Iterable[UUID],
        method_hash: str,
        code_version: str,
        *,
        environment_hash: str | None = None,
        status: AnalysisStatus = AnalysisStatus.STAGED,
        actor: AuthContext | None = None,
        executed_by: str | None = None,
    ) -> Analysis:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        dataset_id_list = _unique_ids(dataset_ids)
        if not dataset_id_list:
            raise ValidationError("Analysis must reference at least one dataset.")
        for dataset_id in dataset_id_list:
            dataset = self.get_dataset(dataset_id)
            if dataset.project_id != project_id:
                raise ValidationError("Datasets must belong to the same project.")
        _ensure_non_empty(method_hash, "method_hash")
        _ensure_non_empty(code_version, "code_version")
        analysis = Analysis(
            analysis_id=uuid4(),
            project_id=project_id,
            dataset_ids=dataset_id_list,
            method_hash=method_hash.strip(),
            code_version=code_version.strip(),
            environment_hash=environment_hash.strip() if environment_hash else None,
            status=status,
            executed_by=executed_by,
        )
        self._store.analyses[analysis.analysis_id] = analysis
        self._run_repository_write(
            lambda repository: repository.analyses.save(analysis)
        )
        return analysis

    def get_analysis(self, analysis_id: UUID) -> Analysis:
        return _get_or_raise(self._store.analyses, analysis_id, "Analysis")

    def list_analyses(
        self,
        *,
        project_id: UUID | None = None,
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
    ) -> list[Analysis]:
        if project_id is None:
            analyses = list(self._store.analyses.values())
        else:
            analyses = [a for a in self._store.analyses.values() if a.project_id == project_id]
        if dataset_id is not None:
            analyses = [
                analysis
                for analysis in analyses
                if dataset_id in analysis.dataset_ids
            ]
        if question_id is not None:
            analyses = [
                analysis
                for analysis in analyses
                if _analysis_has_question_link(
                    analysis,
                    question_id,
                    self._store.datasets,
                )
            ]
        return analyses

    def update_analysis(
        self,
        analysis_id: UUID,
        *,
        status: AnalysisStatus | None = None,
        environment_hash: str | None = None,
        actor: AuthContext | None = None,
    ) -> Analysis:
        require_role(actor, WRITE_ROLES)
        analysis = self.get_analysis(analysis_id)
        if analysis.status == AnalysisStatus.COMMITTED:
            if environment_hash is not None:
                raise ValidationError("Committed analyses are immutable.")
            if status == AnalysisStatus.STAGED:
                raise ValidationError("Committed analyses cannot return to staged.")
        if status is not None:
            _ensure_analysis_status_transition(analysis.status, status)
            if status == AnalysisStatus.COMMITTED and analysis.status != AnalysisStatus.COMMITTED:
                self._ensure_analysis_datasets_committed(analysis)
            analysis.status = status
        if environment_hash is not None:
            analysis.environment_hash = environment_hash.strip() if environment_hash else None
        analysis.updated_at = utc_now()
        self._run_repository_write(
            lambda repository: repository.analyses.save(analysis)
        )
        return analysis

    def delete_analysis(self, analysis_id: UUID, *, actor: AuthContext | None = None) -> Analysis:
        require_role(actor, WRITE_ROLES)
        analysis = self.get_analysis(analysis_id)
        del self._store.analyses[analysis_id]
        self._run_repository_write(
            lambda repository: repository.analyses.delete(analysis_id)
        )
        return analysis

    def commit_analysis(
        self,
        analysis_id: UUID,
        *,
        environment_hash: str | None = None,
        claims: Iterable[ClaimInput] | None = None,
        visualizations: Iterable[VisualizationInput] | None = None,
        actor: AuthContext | None = None,
    ) -> tuple[Analysis, list[Claim], list[Visualization]]:
        require_role(actor, WRITE_ROLES)
        analysis = self.get_analysis(analysis_id)
        _ensure_analysis_status_transition(analysis.status, AnalysisStatus.COMMITTED)
        if analysis.status == AnalysisStatus.COMMITTED and environment_hash is not None:
            raise ValidationError("Committed analyses are immutable.")
        if analysis.status != AnalysisStatus.COMMITTED:
            self._ensure_analysis_datasets_committed(analysis)
            analysis.status = AnalysisStatus.COMMITTED
        if environment_hash is not None:
            analysis.environment_hash = environment_hash.strip() if environment_hash else None
        analysis.updated_at = utc_now()
        self._run_repository_write(
            lambda repository: repository.analyses.save(analysis)
        )
        created_claims: list[Claim] = []
        for claim_input in claims or []:
            supported_by_analysis_ids = list(claim_input.supported_by_analysis_ids)
            if analysis.analysis_id not in supported_by_analysis_ids:
                supported_by_analysis_ids.append(analysis.analysis_id)
            created_claims.append(
                self.create_claim(
                    project_id=analysis.project_id,
                    statement=claim_input.statement,
                    confidence=claim_input.confidence,
                    status=claim_input.status,
                    supported_by_dataset_ids=claim_input.supported_by_dataset_ids,
                    supported_by_analysis_ids=supported_by_analysis_ids,
                    actor=actor,
                )
            )
        created_visualizations: list[Visualization] = []
        for viz_input in visualizations or []:
            created_visualizations.append(
                self.create_visualization(
                    analysis_id=analysis.analysis_id,
                    viz_type=viz_input.viz_type,
                    file_path=viz_input.file_path,
                    caption=viz_input.caption,
                    related_claim_ids=viz_input.related_claim_ids,
                    actor=actor,
                )
            )
        return analysis, created_claims, created_visualizations

    def create_claim(
        self,
        project_id: UUID,
        statement: str,
        confidence: float,
        *,
        status: ClaimStatus = ClaimStatus.PROPOSED,
        supported_by_dataset_ids: Iterable[UUID] | None = None,
        supported_by_analysis_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> Claim:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        _ensure_non_empty(statement, "statement")
        _ensure_claim_confidence(confidence)
        dataset_ids, analysis_ids = self._resolve_claim_support_links(
            project_id,
            supported_by_dataset_ids,
            supported_by_analysis_ids,
        )
        _ensure_claim_support_links(status, dataset_ids, analysis_ids)
        claim = Claim(
            claim_id=uuid4(),
            project_id=project_id,
            statement=statement.strip(),
            confidence=confidence,
            status=status,
            supported_by_dataset_ids=dataset_ids,
            supported_by_analysis_ids=analysis_ids,
        )
        self._store.claims[claim.claim_id] = claim
        self._run_repository_write(lambda repository: repository.claims.save(claim))
        return claim

    def get_claim(self, claim_id: UUID) -> Claim:
        return _get_or_raise(self._store.claims, claim_id, "Claim")

    def list_claims(
        self,
        *,
        project_id: UUID | None = None,
        status: ClaimStatus | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
    ) -> list[Claim]:
        if project_id is None:
            claims = list(self._store.claims.values())
        else:
            claims = [c for c in self._store.claims.values() if c.project_id == project_id]
        if status is not None:
            claims = [claim for claim in claims if claim.status == status]
        if dataset_id is not None:
            claims = [
                claim
                for claim in claims
                if dataset_id in claim.supported_by_dataset_ids
            ]
        if analysis_id is not None:
            claims = [
                claim
                for claim in claims
                if analysis_id in claim.supported_by_analysis_ids
            ]
        return claims

    def update_claim(
        self,
        claim_id: UUID,
        *,
        statement: str | None = None,
        confidence: float | None = None,
        status: ClaimStatus | None = None,
        supported_by_dataset_ids: Iterable[UUID] | None = None,
        supported_by_analysis_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> Claim:
        require_role(actor, WRITE_ROLES)
        claim = self.get_claim(claim_id)
        next_status = status or claim.status
        _ensure_claim_status_transition(claim.status, next_status)
        if claim.status != ClaimStatus.PROPOSED:
            if (
                statement is not None
                or confidence is not None
                or supported_by_dataset_ids is not None
                or supported_by_analysis_ids is not None
            ):
                raise ValidationError("Only proposed claims can be edited.")
        if statement is not None:
            _ensure_non_empty(statement, "statement")
            claim.statement = statement.strip()
        if confidence is not None:
            _ensure_claim_confidence(confidence)
            claim.confidence = confidence
        if supported_by_dataset_ids is not None or supported_by_analysis_ids is not None:
            dataset_ids, analysis_ids = self._resolve_claim_support_links(
                claim.project_id,
                supported_by_dataset_ids or claim.supported_by_dataset_ids,
                supported_by_analysis_ids or claim.supported_by_analysis_ids,
            )
            claim.supported_by_dataset_ids = dataset_ids
            claim.supported_by_analysis_ids = analysis_ids
        _ensure_claim_support_links(next_status, claim.supported_by_dataset_ids, claim.supported_by_analysis_ids)
        if status is not None:
            claim.status = status
        claim.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.claims.save(claim))
        return claim

    def delete_claim(self, claim_id: UUID, *, actor: AuthContext | None = None) -> Claim:
        require_role(actor, WRITE_ROLES)
        claim = self.get_claim(claim_id)
        del self._store.claims[claim_id]
        self._run_repository_write(lambda repository: repository.claims.delete(claim_id))
        return claim

    def create_visualization(
        self,
        analysis_id: UUID,
        viz_type: str,
        file_path: str,
        *,
        caption: str | None = None,
        related_claim_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> Visualization:
        require_role(actor, WRITE_ROLES)
        analysis = self.get_analysis(analysis_id)
        _ensure_non_empty(viz_type, "viz_type")
        _ensure_non_empty(file_path, "file_path")
        claim_ids = _unique_ids(related_claim_ids)
        for claim_id in claim_ids:
            claim = self.get_claim(claim_id)
            if claim.project_id != analysis.project_id:
                raise ValidationError("Related claims must belong to the same project.")
        visualization = Visualization(
            viz_id=uuid4(),
            analysis_id=analysis_id,
            viz_type=viz_type.strip(),
            file_path=file_path.strip(),
            caption=caption.strip() if caption else None,
            related_claim_ids=claim_ids,
        )
        self._store.visualizations[visualization.viz_id] = visualization
        self._run_repository_write(
            lambda repository: repository.visualizations.save(visualization)
        )
        return visualization

    def get_visualization(self, viz_id: UUID) -> Visualization:
        return _get_or_raise(self._store.visualizations, viz_id, "Visualization")

    def list_visualizations(
        self,
        *,
        project_id: UUID | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
    ) -> list[Visualization]:
        if project_id is None:
            visualizations = list(self._store.visualizations.values())
        else:
            visualizations = [
                viz
                for viz in self._store.visualizations.values()
                if self.get_analysis(viz.analysis_id).project_id == project_id
            ]
        if analysis_id is not None:
            visualizations = [
                viz
                for viz in visualizations
                if viz.analysis_id == analysis_id
            ]
        if claim_id is not None:
            visualizations = [
                viz
                for viz in visualizations
                if claim_id in viz.related_claim_ids
            ]
        return visualizations

    def update_visualization(
        self,
        viz_id: UUID,
        *,
        viz_type: str | None = None,
        file_path: str | None = None,
        caption: str | None = None,
        related_claim_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
    ) -> Visualization:
        require_role(actor, WRITE_ROLES)
        visualization = self.get_visualization(viz_id)
        if viz_type is not None:
            _ensure_non_empty(viz_type, "viz_type")
            visualization.viz_type = viz_type.strip()
        if file_path is not None:
            _ensure_non_empty(file_path, "file_path")
            visualization.file_path = file_path.strip()
        if caption is not None:
            visualization.caption = caption.strip() if caption else None
        if related_claim_ids is not None:
            claim_ids = _unique_ids(related_claim_ids)
            analysis = self.get_analysis(visualization.analysis_id)
            for claim_id in claim_ids:
                claim = self.get_claim(claim_id)
                if claim.project_id != analysis.project_id:
                    raise ValidationError("Related claims must belong to the same project.")
            visualization.related_claim_ids = claim_ids
        visualization.updated_at = utc_now()
        self._run_repository_write(
            lambda repository: repository.visualizations.save(visualization)
        )
        return visualization

    def delete_visualization(
        self,
        viz_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Visualization:
        require_role(actor, WRITE_ROLES)
        visualization = self.get_visualization(viz_id)
        del self._store.visualizations[viz_id]
        self._run_repository_write(
            lambda repository: repository.visualizations.delete(viz_id)
        )
        return visualization

    def _resolve_claim_support_links(
        self,
        project_id: UUID,
        dataset_ids: Iterable[UUID] | None,
        analysis_ids: Iterable[UUID] | None,
    ) -> tuple[list[UUID], list[UUID]]:
        resolved_dataset_ids = _unique_ids(dataset_ids)
        resolved_analysis_ids = _unique_ids(analysis_ids)
        for dataset_id in resolved_dataset_ids:
            dataset = self.get_dataset(dataset_id)
            if dataset.project_id != project_id:
                raise ValidationError("Supporting datasets must belong to the same project.")
        for analysis_id in resolved_analysis_ids:
            analysis = self.get_analysis(analysis_id)
            if analysis.project_id != project_id:
                raise ValidationError("Supporting analyses must belong to the same project.")
        return resolved_dataset_ids, resolved_analysis_ids

    def _ensure_analysis_datasets_committed(self, analysis: Analysis) -> None:
        for dataset_id in analysis.dataset_ids:
            dataset = self.get_dataset(dataset_id)
            if dataset.status != DatasetStatus.COMMITTED:
                raise ValidationError("Analyses can only be committed with committed datasets.")

    def _ensure_source_session_valid(self, source_session_id: UUID | None, project_id: UUID) -> None:
        if source_session_id is None:
            return
        session = self.get_session(source_session_id)
        if session.project_id != project_id:
            raise ValidationError("Source session must belong to the same project.")
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Only operational sessions can be promoted to datasets.")

    def _ensure_target_exists(self, target: EntityRef, project_id: UUID) -> None:
        entity_map = {
            EntityType.PROJECT: self._store.projects,
            EntityType.QUESTION: self._store.questions,
            EntityType.DATASET: self._store.datasets,
            EntityType.NOTE: self._store.notes,
            EntityType.SESSION: self._store.sessions,
            EntityType.ANALYSIS: self._store.analyses,
            EntityType.CLAIM: self._store.claims,
            EntityType.VISUALIZATION: self._store.visualizations,
        }
        store = entity_map.get(target.entity_type)
        if store is None:
            raise ValidationError("Unsupported target entity type.")
        entity = store.get(target.entity_id)
        if entity is None:
            raise NotFoundError(f"{target.entity_type.value.capitalize()} does not exist.")
        if target.entity_type == EntityType.VISUALIZATION:
            analysis = self.get_analysis(entity.analysis_id)
            if analysis.project_id != project_id:
                raise ValidationError("Target must belong to the same project.")
            return
        if hasattr(entity, "project_id") and entity.project_id != project_id:
            raise ValidationError("Target must belong to the same project.")


def _ensure_non_empty(value: str, field_name: str) -> None:
    if not value or not str(value).strip():
        raise ValidationError(f"{field_name} must not be empty.")


def _normalize_note_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    if not metadata:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in metadata.items():
        _ensure_non_empty(key, "metadata key")
        cleaned_key = str(key).strip()
        cleaned_value = value.strip() if isinstance(value, str) else str(value)
        cleaned[cleaned_key] = cleaned_value
    return cleaned


def _get_or_raise(store: dict[UUID, object], entity_id: UUID, label: str):
    try:
        return store[entity_id]
    except KeyError as exc:
        raise NotFoundError(f"{label} does not exist.") from exc


def _unique_ids(values: Iterable[UUID] | None) -> list[UUID]:
    if not values:
        return []
    seen: set[UUID] = set()
    unique: list[UUID] = []
    for value in values:
        if value in seen:
            raise ValidationError("Duplicate id in list.")
        seen.add(value)
        unique.append(value)
    return unique


_QUESTION_STATUS_TRANSITIONS: dict[QuestionStatus, set[QuestionStatus]] = {
    QuestionStatus.STAGED: {QuestionStatus.STAGED, QuestionStatus.ACTIVE, QuestionStatus.ABANDONED},
    QuestionStatus.ACTIVE: {QuestionStatus.ACTIVE, QuestionStatus.ANSWERED, QuestionStatus.ABANDONED},
    QuestionStatus.ANSWERED: {QuestionStatus.ANSWERED},
    QuestionStatus.ABANDONED: {QuestionStatus.ABANDONED},
}

_ANALYSIS_STATUS_TRANSITIONS: dict[AnalysisStatus, set[AnalysisStatus]] = {
    AnalysisStatus.STAGED: {AnalysisStatus.STAGED, AnalysisStatus.COMMITTED, AnalysisStatus.ARCHIVED},
    AnalysisStatus.COMMITTED: {AnalysisStatus.COMMITTED, AnalysisStatus.ARCHIVED},
    AnalysisStatus.ARCHIVED: {AnalysisStatus.ARCHIVED},
}

_CLAIM_STATUS_TRANSITIONS: dict[ClaimStatus, set[ClaimStatus]] = {
    ClaimStatus.PROPOSED: {ClaimStatus.PROPOSED, ClaimStatus.SUPPORTED, ClaimStatus.REJECTED},
    ClaimStatus.SUPPORTED: {ClaimStatus.SUPPORTED},
    ClaimStatus.REJECTED: {ClaimStatus.REJECTED},
}


def _ensure_question_status_transition(
    current_status: QuestionStatus,
    next_status: QuestionStatus,
) -> None:
    allowed = _QUESTION_STATUS_TRANSITIONS.get(current_status, {current_status})
    if next_status not in allowed:
        raise ValidationError(
            "Question status cannot transition from "
            f"{current_status.value} to {next_status.value}."
        )


def _ensure_analysis_status_transition(
    current_status: AnalysisStatus,
    next_status: AnalysisStatus,
) -> None:
    allowed = _ANALYSIS_STATUS_TRANSITIONS.get(current_status, {current_status})
    if next_status not in allowed:
        raise ValidationError(
            "Analysis status cannot transition from "
            f"{current_status.value} to {next_status.value}."
        )


def _ensure_claim_status_transition(
    current_status: ClaimStatus,
    next_status: ClaimStatus,
) -> None:
    allowed = _CLAIM_STATUS_TRANSITIONS.get(current_status, {current_status})
    if next_status not in allowed:
        raise ValidationError(
            "Claim status cannot transition from "
            f"{current_status.value} to {next_status.value}."
        )


def _ensure_question_parents_dag(
    question_id: UUID,
    parent_ids: list[UUID],
    store: dict[UUID, Question],
) -> None:
    if question_id in parent_ids:
        raise ValidationError("Question cannot be its own parent.")
    for parent_id in parent_ids:
        if _is_question_ancestor(parent_id, question_id, store):
            raise ValidationError("Question parent graph must be acyclic.")


def _is_question_ancestor(
    start_id: UUID,
    target_id: UUID,
    store: dict[UUID, Question],
) -> bool:
    stack = [start_id]
    visited: set[UUID] = set()
    while stack:
        current = stack.pop()
        if current == target_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        question = store.get(current)
        if question is None:
            continue
        stack.extend(question.parent_question_ids)
    return False


def _ensure_primary_question_active(question: Question) -> None:
    if question.status != QuestionStatus.ACTIVE:
        raise ValidationError("Primary question must be active to commit a dataset.")


def _ensure_claim_confidence(confidence: float) -> None:
    if confidence < 0 or confidence > 100:
        raise ValidationError("confidence must be between 0 and 100.")


def _ensure_claim_support_links(
    status: ClaimStatus,
    dataset_ids: list[UUID],
    analysis_ids: list[UUID],
) -> None:
    if status == ClaimStatus.SUPPORTED and not (dataset_ids or analysis_ids):
        raise ValidationError("Supported claims require supporting datasets or analyses.")


def _analysis_has_question_link(
    analysis: Analysis,
    question_id: UUID,
    datasets: dict[UUID, Dataset],
) -> bool:
    for dataset_id in analysis.dataset_ids:
        dataset = datasets.get(dataset_id)
        if dataset is None:
            continue
        if any(link.question_id == question_id for link in dataset.question_links):
            return True
    return False


def _unique_strings(values: Iterable[str] | None, field_name: str) -> list[str]:
    if not values:
        return []
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        _ensure_non_empty(value, field_name)
        cleaned = value.strip()
        if cleaned in seen:
            raise ValidationError(f"Duplicate {field_name}.")
        seen.add(cleaned)
        unique.append(cleaned)
    return unique


def _normalize_dataset_files(files: Iterable[DatasetFile]) -> list[DatasetFile]:
    normalized: list[DatasetFile] = []
    seen: set[str] = set()
    for file in files:
        _ensure_non_empty(file.path, "file.path")
        _ensure_non_empty(file.checksum, "file.checksum")
        path = file.path.strip()
        checksum = file.checksum.strip()
        if path in seen:
            raise ValidationError("Duplicate file path in commit manifest.")
        seen.add(path)
        normalized.append(DatasetFile(path=path, checksum=checksum))
    return normalized


def _find_acquisition_output(
    outputs: dict[UUID, AcquisitionOutput],
    session_id: UUID,
    file_path: str,
) -> AcquisitionOutput | None:
    for output in outputs.values():
        if output.session_id == session_id and output.file_path == file_path:
            return output
    return None


def _merge_acquisition_outputs(
    manifest: DatasetCommitManifestInput | DatasetCommitManifest | None,
    outputs: Iterable[AcquisitionOutput],
) -> DatasetCommitManifestInput | DatasetCommitManifest | None:
    outputs_list = list(outputs)
    if not outputs_list:
        return manifest
    if isinstance(manifest, DatasetCommitManifest):
        manifest_input = _manifest_input_from_commit(manifest)
    else:
        manifest_input = manifest or DatasetCommitManifestInput()
    merged_files = list(manifest_input.files)
    seen = {file.path.strip(): file.checksum.strip() for file in manifest_input.files}
    for output in outputs_list:
        path = output.file_path.strip()
        checksum = output.checksum.strip()
        existing = seen.get(path)
        if existing is None:
            merged_files.append(DatasetFile(path=path, checksum=checksum))
            seen[path] = checksum
            continue
        if existing != checksum:
            raise ValidationError("Acquisition output checksum conflict for file path.")
    return DatasetCommitManifestInput(
        files=merged_files,
        metadata=manifest_input.metadata,
        nwb_metadata=manifest_input.nwb_metadata,
        bids_metadata=manifest_input.bids_metadata,
        note_ids=manifest_input.note_ids,
        extraction_provenance=manifest_input.extraction_provenance,
        source_session_id=manifest_input.source_session_id,
    )


_STANDARD_METADATA_KEY_PATTERN = re.compile(r"[^a-z0-9]+")
_NWB_METADATA_ALIASES = {
    "identifier": "identifier",
    "sessiondescription": "session_description",
    "session_description": "session_description",
    "sessionstarttime": "session_start_time",
    "session_start_time": "session_start_time",
}
_BIDS_METADATA_ALIASES = {
    "name": "name",
    "datasetname": "name",
    "bidsversion": "bids_version",
    "bids_version": "bids_version",
    "datasettype": "dataset_type",
    "dataset_type": "dataset_type",
}
_NWB_REQUIRED_METADATA_FIELDS = ("identifier", "session_description", "session_start_time")
_BIDS_REQUIRED_METADATA_FIELDS = ("name", "bids_version")


def _normalize_commit_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    if not metadata:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in metadata.items():
        _ensure_non_empty(key, "metadata key")
        cleaned_key = str(key).strip()
        cleaned_value = value.strip() if isinstance(value, str) else str(value)
        cleaned[cleaned_key] = cleaned_value
    return cleaned


def _canonicalize_standard_metadata_key(key: str) -> str:
    cleaned = str(key).strip().casefold()
    cleaned = _STANDARD_METADATA_KEY_PATTERN.sub("_", cleaned)
    return cleaned.strip("_")


def _normalize_standard_metadata(
    metadata: dict[str, str] | None,
    aliases: dict[str, str],
    standard_name: str,
) -> dict[str, str]:
    if not metadata:
        return {}
    normalized: dict[str, str] = {}
    for key, value in metadata.items():
        _ensure_non_empty(key, f"{standard_name} metadata key")
        canonical = _canonicalize_standard_metadata_key(key)
        canonical = aliases.get(canonical, canonical)
        value_str = value.strip() if isinstance(value, str) else str(value)
        if canonical in normalized and normalized[canonical] != value_str:
            raise ValidationError(
                f"Conflicting {standard_name} metadata values for {canonical}."
            )
        normalized[canonical] = value_str
    return normalized


def _merge_standard_metadata(
    primary: dict[str, str],
    secondary: dict[str, str],
    standard_name: str,
) -> dict[str, str]:
    merged = dict(primary)
    for key, value in secondary.items():
        if key in merged and merged[key] != value:
            raise ValidationError(
                f"Conflicting {standard_name} metadata values for {key}."
            )
        merged.setdefault(key, value)
    return merged


def _split_prefixed_metadata(
    metadata: dict[str, str],
    prefix: str,
) -> tuple[dict[str, str], dict[str, str]]:
    extracted: dict[str, str] = {}
    remaining: dict[str, str] = {}
    prefix_clean = prefix.casefold()
    prefix_len = len(prefix)
    for key, value in metadata.items():
        key_clean = str(key).strip()
        key_lower = key_clean.casefold()
        if (
            key_lower.startswith(prefix_clean)
            and len(key_lower) > prefix_len
            and key_lower[prefix_len] in {".", ":"}
        ):
            stripped = key_clean[prefix_len + 1 :].strip()
            if not stripped:
                raise ValidationError(f"{prefix} metadata key must include a field name.")
            extracted[stripped] = value
            continue
        remaining[key_clean] = value
    return remaining, extracted


def _validate_required_metadata(
    metadata: dict[str, str],
    required_fields: Iterable[str],
    standard_name: str,
) -> None:
    if not metadata:
        return
    missing = [field for field in required_fields if not metadata.get(field)]
    if missing:
        missing_str = ", ".join(missing)
        raise ValidationError(f"{standard_name} metadata requires: {missing_str}.")


def _manifest_input_from_commit(manifest: DatasetCommitManifest) -> DatasetCommitManifestInput:
    return DatasetCommitManifestInput(
        files=list(manifest.files),
        metadata=dict(manifest.metadata),
        nwb_metadata=dict(manifest.nwb_metadata),
        bids_metadata=dict(manifest.bids_metadata),
        note_ids=list(manifest.note_ids),
        extraction_provenance=list(manifest.extraction_provenance),
        source_session_id=manifest.source_session_id,
    )


def _manifest_input_with_source(
    manifest: DatasetCommitManifestInput | DatasetCommitManifest | None,
    source_session_id: UUID,
) -> DatasetCommitManifestInput:
    if isinstance(manifest, DatasetCommitManifest):
        manifest_input = _manifest_input_from_commit(manifest)
    else:
        manifest_input = manifest
    if manifest_input is None:
        return DatasetCommitManifestInput(source_session_id=source_session_id)
    if (
        manifest_input.source_session_id is not None
        and manifest_input.source_session_id != source_session_id
    ):
        raise ValidationError("commit_manifest source_session_id does not match session.")
    return DatasetCommitManifestInput(
        files=manifest_input.files,
        metadata=manifest_input.metadata,
        nwb_metadata=manifest_input.nwb_metadata,
        bids_metadata=manifest_input.bids_metadata,
        note_ids=manifest_input.note_ids,
        extraction_provenance=manifest_input.extraction_provenance,
        source_session_id=source_session_id,
    )


def _build_commit_manifest(
    manifest: DatasetCommitManifestInput | DatasetCommitManifest | None,
    question_links: list[QuestionLink],
) -> DatasetCommitManifest:
    if isinstance(manifest, DatasetCommitManifest):
        manifest_input = _manifest_input_from_commit(manifest)
    else:
        manifest_input = manifest or DatasetCommitManifestInput()
    base_metadata = _normalize_commit_metadata(manifest_input.metadata)
    base_metadata, nwb_prefixed = _split_prefixed_metadata(base_metadata, "nwb")
    base_metadata, bids_prefixed = _split_prefixed_metadata(base_metadata, "bids")
    nwb_metadata = _normalize_standard_metadata(
        manifest_input.nwb_metadata,
        _NWB_METADATA_ALIASES,
        "NWB",
    )
    nwb_prefixed = _normalize_standard_metadata(
        nwb_prefixed,
        _NWB_METADATA_ALIASES,
        "NWB",
    )
    nwb_metadata = _merge_standard_metadata(nwb_metadata, nwb_prefixed, "NWB")
    bids_metadata = _normalize_standard_metadata(
        manifest_input.bids_metadata,
        _BIDS_METADATA_ALIASES,
        "BIDS",
    )
    bids_prefixed = _normalize_standard_metadata(
        bids_prefixed,
        _BIDS_METADATA_ALIASES,
        "BIDS",
    )
    bids_metadata = _merge_standard_metadata(bids_metadata, bids_prefixed, "BIDS")
    _validate_required_metadata(nwb_metadata, _NWB_REQUIRED_METADATA_FIELDS, "NWB")
    _validate_required_metadata(bids_metadata, _BIDS_REQUIRED_METADATA_FIELDS, "BIDS")
    return DatasetCommitManifest(
        files=_normalize_dataset_files(manifest_input.files),
        metadata=base_metadata,
        nwb_metadata=nwb_metadata,
        bids_metadata=bids_metadata,
        note_ids=_unique_ids(manifest_input.note_ids),
        extraction_provenance=_unique_strings(
            manifest_input.extraction_provenance,
            "extraction_provenance",
        ),
        question_links=list(question_links),
        source_session_id=manifest_input.source_session_id,
    )


def _validate_commit_hash(provided: str | None, expected: str) -> None:
    if provided is None:
        return
    _ensure_non_empty(provided, "commit_hash")
    if provided.strip() != expected:
        raise ValidationError("commit_hash must match content-addressed manifest hash.")


_ROLE_ORDER = {QuestionLinkRole.PRIMARY.value: 0, QuestionLinkRole.SECONDARY.value: 1}


def _manifest_payload(manifest: DatasetCommitManifest) -> dict[str, object]:
    files = sorted(
        ({"path": file.path, "checksum": file.checksum} for file in manifest.files),
        key=lambda item: (item["path"], item["checksum"]),
    )
    links = sorted(
        (
            {
                "question_id": str(link.question_id),
                "role": link.role.value,
                "outcome_status": link.outcome_status.value,
            }
            for link in manifest.question_links
        ),
        key=lambda item: (_ROLE_ORDER.get(item["role"], 99), item["question_id"]),
    )
    note_ids = sorted(str(note_id) for note_id in manifest.note_ids)
    extraction_provenance = sorted(manifest.extraction_provenance)
    metadata = {key: manifest.metadata[key] for key in sorted(manifest.metadata)}
    nwb_metadata = {key: manifest.nwb_metadata[key] for key in sorted(manifest.nwb_metadata)}
    bids_metadata = {key: manifest.bids_metadata[key] for key in sorted(manifest.bids_metadata)}
    return {
        "files": files,
        "metadata": metadata,
        "nwb_metadata": nwb_metadata,
        "bids_metadata": bids_metadata,
        "question_links": links,
        "note_ids": note_ids,
        "extraction_provenance": extraction_provenance,
        "source_session_id": str(manifest.source_session_id) if manifest.source_session_id else None,
    }


def _compute_commit_hash(manifest: DatasetCommitManifest) -> str:
    payload = _manifest_payload(manifest)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_extracted_entity(label: str, confidence: float, provenance: str) -> ExtractedEntity:
    _ensure_non_empty(label, "label")
    if not 0.0 <= confidence <= 1.0:
        raise ValidationError("confidence must be between 0 and 1.")
    _ensure_non_empty(provenance, "provenance")
    return ExtractedEntity(
        label=label.strip(),
        confidence=confidence,
        provenance=provenance.strip(),
    )


@dataclass(frozen=True)
class _TagMapping:
    vocabulary: str
    term_label: str
    match_confidence: float = 1.0
    term_id: str | None = None


_DEFAULT_TAG_MAP: dict[str, list[_TagMapping]] = {
    "neuron": [
        _TagMapping(vocabulary="NIFSTD", term_label="Neuron", match_confidence=0.95),
        _TagMapping(vocabulary="NCIT", term_label="Neuron", match_confidence=0.9),
    ],
    "astrocyte": [_TagMapping(vocabulary="NIFSTD", term_label="Astrocyte", match_confidence=0.93)],
    "hippocampus": [_TagMapping(vocabulary="UBERON", term_label="Hippocampus", match_confidence=0.92)],
    "patch clamp": [_TagMapping(vocabulary="OBI", term_label="Patch clamp", match_confidence=0.88)],
}


def _build_entity_tag_suggestion(
    *,
    entity_label: str,
    term: _TagMapping,
    extracted_confidence: float,
    provenance: str,
) -> EntityTagSuggestion:
    _ensure_non_empty(entity_label, "entity_label")
    _ensure_non_empty(term.vocabulary, "vocabulary")
    _ensure_non_empty(term.term_label, "term_label")
    _ensure_non_empty(provenance, "provenance")
    if not 0.0 <= extracted_confidence <= 1.0:
        raise ValidationError("extracted_confidence must be between 0 and 1.")
    if not 0.0 <= term.match_confidence <= 1.0:
        raise ValidationError("match_confidence must be between 0 and 1.")
    term_id = term.term_id or f"{term.vocabulary}:{_slugify_label(term.term_label)}"
    confidence = min(1.0, extracted_confidence * term.match_confidence)
    return EntityTagSuggestion(
        suggestion_id=uuid4(),
        entity_label=entity_label.strip(),
        vocabulary=term.vocabulary.strip(),
        term_id=term_id,
        term_label=term.term_label.strip(),
        confidence=confidence,
        provenance=provenance.strip(),
    )


def _build_note_tag_provenance(note_id: UUID) -> str:
    return f"note:{note_id}|tag-mapper:v1"


def _normalize_entity_label(label: str) -> str:
    cleaned = label.strip().casefold()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[_\-]+", " ", cleaned)
    return cleaned.strip()


def _slugify_label(label: str) -> str:
    cleaned = _normalize_entity_label(label)
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    return slug or "term"


def _resolve_tag_mappings(
    label: str, mapping: dict[str, list[_TagMapping]]
) -> list[_TagMapping]:
    normalized = _normalize_entity_label(label)
    keys_to_try = [normalized]
    if normalized.endswith("s") and len(normalized) > 1:
        keys_to_try.append(normalized[:-1])
    resolved: list[_TagMapping] = []
    for key in keys_to_try:
        resolved.extend(mapping.get(key, []))
    if not resolved:
        for key, terms in mapping.items():
            if key and key in normalized:
                resolved.extend(terms)
    return _dedupe_tag_mappings(resolved)


def _dedupe_tag_mappings(terms: Iterable[_TagMapping]) -> list[_TagMapping]:
    seen: set[tuple[str, str, str | None]] = set()
    unique: list[_TagMapping] = []
    for term in terms:
        key = (term.vocabulary.casefold(), term.term_label.casefold(), term.term_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(term)
    return unique


def _tag_suggestion_key(suggestion: EntityTagSuggestion) -> tuple[str, str, str, str]:
    return (
        suggestion.entity_label.casefold(),
        suggestion.vocabulary.casefold(),
        suggestion.term_id.casefold(),
        suggestion.term_label.casefold(),
    )


_QUESTION_PREFIX_RE = re.compile(r"^\s*(?:q|question)\s*[:\-]\s*(.+)$", re.IGNORECASE)
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")
_QUESTION_SENTENCE_RE = re.compile(r"[^?\n]*\?")


def _note_text_for_extraction(note: Note) -> str:
    if note.transcribed_text:
        return note.transcribed_text
    if note.raw_asset is not None:
        return ""
    return note.raw_content


def _extract_question_candidates(text: str) -> list[str]:
    if not text:
        return []
    candidates: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = _BULLET_PREFIX_RE.sub("", stripped).strip()
        prefix_match = _QUESTION_PREFIX_RE.match(stripped)
        if prefix_match:
            candidate = _clean_question_candidate(prefix_match.group(1))
            if candidate:
                candidates.append(candidate)
            continue
        if "?" in stripped:
            for match in _QUESTION_SENTENCE_RE.findall(stripped):
                candidate = _clean_question_candidate(match)
                if candidate:
                    candidates.append(candidate)
    return _dedupe_casefold(candidates)


def _clean_question_candidate(candidate: str) -> str | None:
    cleaned = candidate.strip()
    if not cleaned:
        return None
    if cleaned.endswith(".") and "?" not in cleaned:
        cleaned = cleaned[:-1].strip()
    alpha_count = sum(1 for char in cleaned if char.isalpha())
    if alpha_count < 3:
        return None
    return cleaned


def _dedupe_casefold(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def _build_note_provenance(note_id: UUID) -> str:
    return f"note:{note_id}|question-extractor:v1"
