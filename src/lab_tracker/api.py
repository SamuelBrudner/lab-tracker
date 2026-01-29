"""API scaffolding for lab tracker."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, Role, require_role
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
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
    utc_now,
)

WRITE_ROLES = {Role.ADMIN, Role.EDITOR}


class InMemoryStore:
    def __init__(self) -> None:
        self.projects: dict[UUID, Project] = {}
        self.questions: dict[UUID, Question] = {}
        self.datasets: dict[UUID, Dataset] = {}
        self.notes: dict[UUID, Note] = {}
        self.sessions: dict[UUID, Session] = {}
        self.analyses: dict[UUID, Analysis] = {}


class LabTrackerAPI:
    def __init__(self, store: InMemoryStore | None = None) -> None:
        self._store = store or InMemoryStore()

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
        return project

    def delete_project(self, project_id: UUID, *, actor: AuthContext | None = None) -> Project:
        require_role(actor, WRITE_ROLES)
        project = self.get_project(project_id)
        del self._store.projects[project_id]
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
        parent_ids = _unique_ids(parent_question_ids)
        for parent_id in parent_ids:
            parent = self.get_question(parent_id)
            if parent.project_id != project_id:
                raise ValidationError("Parent question must belong to the same project.")
        question = Question(
            question_id=uuid4(),
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
        return question

    def get_question(self, question_id: UUID) -> Question:
        return _get_or_raise(self._store.questions, question_id, "Question")

    def list_questions(self, *, project_id: UUID | None = None) -> list[Question]:
        if project_id is None:
            return list(self._store.questions.values())
        return [q for q in self._store.questions.values() if q.project_id == project_id]

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
            question.status = status
        if parent_question_ids is not None:
            parent_ids = _unique_ids(parent_question_ids)
            for parent_id in parent_ids:
                parent = self.get_question(parent_id)
                if parent.project_id != question.project_id:
                    raise ValidationError("Parent question must belong to the same project.")
            question.parent_question_ids = parent_ids
        question.updated_at = utc_now()
        return question

    def delete_question(self, question_id: UUID, *, actor: AuthContext | None = None) -> Question:
        require_role(actor, WRITE_ROLES)
        question = self.get_question(question_id)
        del self._store.questions[question_id]
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
        return dataset

    def delete_dataset(self, dataset_id: UUID, *, actor: AuthContext | None = None) -> Dataset:
        require_role(actor, WRITE_ROLES)
        dataset = self.get_dataset(dataset_id)
        del self._store.datasets[dataset_id]
        return dataset

    def create_note(
        self,
        project_id: UUID,
        raw_content: str,
        *,
        transcribed_text: str | None = None,
        extracted_entities: Iterable[tuple[str, float, str]] | None = None,
        tag_suggestions: Iterable[EntityTagSuggestion] | None = None,
        targets: Iterable[EntityRef] | None = None,
        status: NoteStatus = NoteStatus.STAGED,
        actor: AuthContext | None = None,
        created_by: str | None = None,
    ) -> Note:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        _ensure_non_empty(raw_content, "raw_content")
        resolved_targets = list(targets or [])
        for target in resolved_targets:
            self._ensure_target_exists(target, project_id)
        resolved_entities = [
            _build_extracted_entity(label, confidence, provenance)
            for label, confidence, provenance in (extracted_entities or [])
        ]
        resolved_tag_suggestions = list(tag_suggestions or [])
        note = Note(
            note_id=uuid4(),
            project_id=project_id,
            raw_content=raw_content.strip(),
            transcribed_text=transcribed_text.strip() if transcribed_text else None,
            extracted_entities=resolved_entities,
            tag_suggestions=resolved_tag_suggestions,
            targets=resolved_targets,
            status=status,
            created_by=created_by,
        )
        self._store.notes[note.note_id] = note
        return note

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
        targets: Iterable[EntityRef] | None = None,
        status: NoteStatus | None = None,
        actor: AuthContext | None = None,
    ) -> Note:
        require_role(actor, WRITE_ROLES)
        note = self.get_note(note_id)
        if transcribed_text is not None:
            note.transcribed_text = transcribed_text.strip() if transcribed_text else None
        if targets is not None:
            resolved_targets = list(targets)
            for target in resolved_targets:
                self._ensure_target_exists(target, note.project_id)
            note.targets = resolved_targets
        if status is not None:
            note.status = status
        note.updated_at = utc_now()
        return note

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
                return updated
        raise NotFoundError("Tag suggestion does not exist.")

    def delete_note(self, note_id: UUID, *, actor: AuthContext | None = None) -> Note:
        require_role(actor, WRITE_ROLES)
        note = self.get_note(note_id)
        del self._store.notes[note_id]
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
        return session

    def get_session(self, session_id: UUID) -> Session:
        return _get_or_raise(self._store.sessions, session_id, "Session")

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
        return session

    def delete_session(self, session_id: UUID, *, actor: AuthContext | None = None) -> Session:
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        del self._store.sessions[session_id]
        return session

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
        manifest_with_session = _manifest_input_with_source(commit_manifest, session.session_id)
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
        return analysis

    def get_analysis(self, analysis_id: UUID) -> Analysis:
        return _get_or_raise(self._store.analyses, analysis_id, "Analysis")

    def list_analyses(self, *, project_id: UUID | None = None) -> list[Analysis]:
        if project_id is None:
            return list(self._store.analyses.values())
        return [a for a in self._store.analyses.values() if a.project_id == project_id]

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
        if status is not None:
            analysis.status = status
        if environment_hash is not None:
            analysis.environment_hash = environment_hash.strip() if environment_hash else None
        analysis.updated_at = utc_now()
        return analysis

    def delete_analysis(self, analysis_id: UUID, *, actor: AuthContext | None = None) -> Analysis:
        require_role(actor, WRITE_ROLES)
        analysis = self.get_analysis(analysis_id)
        del self._store.analyses[analysis_id]
        return analysis

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
        }
        store = entity_map.get(target.entity_type)
        if store is None:
            raise ValidationError("Unsupported target entity type.")
        entity = store.get(target.entity_id)
        if entity is None:
            raise NotFoundError(f"{target.entity_type.value.capitalize()} does not exist.")
        if hasattr(entity, "project_id") and entity.project_id != project_id:
            raise ValidationError("Target must belong to the same project.")


def _ensure_non_empty(value: str, field_name: str) -> None:
    if not value or not str(value).strip():
        raise ValidationError(f"{field_name} must not be empty.")


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


def _ensure_primary_question_active(question: Question) -> None:
    if question.status != QuestionStatus.ACTIVE:
        raise ValidationError("Primary question must be active to commit a dataset.")


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


def _normalize_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    if not metadata:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in metadata.items():
        _ensure_non_empty(key, "metadata key")
        cleaned_key = key.strip()
        cleaned_value = value.strip() if isinstance(value, str) else str(value)
        cleaned[cleaned_key] = cleaned_value
    return cleaned


def _manifest_input_from_commit(manifest: DatasetCommitManifest) -> DatasetCommitManifestInput:
    return DatasetCommitManifestInput(
        files=list(manifest.files),
        metadata=dict(manifest.metadata),
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
    return DatasetCommitManifest(
        files=_normalize_dataset_files(manifest_input.files),
        metadata=_normalize_metadata(manifest_input.metadata),
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
    return {
        "files": files,
        "metadata": metadata,
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
