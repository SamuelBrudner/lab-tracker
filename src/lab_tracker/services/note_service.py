"""Note domain service mixin."""

from __future__ import annotations

from typing import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    EntityRef,
    EntityTagSuggestion,
    EntityType,
    ExtractedEntity,
    Note,
    NoteRawAsset,
    NoteStatus,
    Question,
    QuestionSource,
    QuestionStatus,
    QuestionType,
    TagSuggestionStatus,
    utc_now,
)
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _DEFAULT_TAG_MAP,
    _TagMapping,
    _build_entity_tag_suggestion,
    _build_extracted_entity,
    _build_note_provenance,
    _build_note_tag_provenance,
    _get_or_raise,
    _normalize_note_metadata,
    _resolve_tag_mappings,
    _tag_suggestion_key,
)


class NoteServiceMixin:
    def create_note(
        self,
        project_id: UUID,
        raw_content: str | None = None,
        *,
        raw_asset: NoteRawAsset | None = None,
        transcribed_text: str | None = None,
        extracted_entities: Iterable[ExtractedEntity | tuple[str, float, str]] | None = None,
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
        resolved_entities: list[ExtractedEntity] = []
        for item in extracted_entities or []:
            if isinstance(item, ExtractedEntity):
                label, confidence, provenance = item.label, item.confidence, item.provenance
            else:
                label, confidence, provenance = item
            resolved_entities.append(_build_extracted_entity(label, confidence, provenance))
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
        self._search_backend.upsert_notes([note])
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
        extracted_entities: Iterable[ExtractedEntity | tuple[str, float, str]] | None = None,
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
        extracted_entities: Iterable[ExtractedEntity | tuple[str, float, str]] | None = None,
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
            resolved_entities: list[ExtractedEntity] = []
            for item in extracted_entities:
                if isinstance(item, ExtractedEntity):
                    label, confidence, provenance = item.label, item.confidence, item.provenance
                else:
                    label, confidence, provenance = item
                resolved_entities.append(_build_extracted_entity(label, confidence, provenance))
            note.extracted_entities = resolved_entities
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
        self._search_backend.upsert_notes([note])
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
                updated = suggestion.model_copy(
                    update={
                        "status": status,
                        "reviewed_by": resolved_reviewed_by,
                        "reviewed_at": resolved_reviewed_at,
                    }
                )
                note.tag_suggestions[index] = updated
                note.updated_at = utc_now()
                self._run_repository_write(lambda repository: repository.notes.save(note))
                return updated
        raise NotFoundError("Tag suggestion does not exist.")

    def delete_note(self, note_id: UUID, *, actor: AuthContext | None = None) -> Note:
        require_role(actor, WRITE_ROLES)
        note = self.get_note(note_id)
        del self._store.notes[note_id]
        self._search_backend.delete_notes([note_id])
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
        raw_asset_bytes: bytes | None = None
        backend = self._question_extraction_backend
        if (
            backend.requires_raw_asset_bytes(note)
            and note.raw_asset is not None
            and self._raw_storage is not None
        ):
            raw_asset_bytes = self._raw_storage.read(note.raw_asset.storage_id)
        candidates = backend.extract_questions(note, raw_asset_bytes=raw_asset_bytes)
        if not candidates:
            return []
        existing = {
            question.text.casefold() for question in self.list_questions(project_id=note.project_id)
        }
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
