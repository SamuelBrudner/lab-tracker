"""Note domain service mixin."""

from __future__ import annotations

import logging
from typing import BinaryIO, Iterable
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
    QuestionExtractionCandidate,
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
    _actor_user_id,
    _build_entity_tag_suggestion,
    _build_extracted_entity,
    _build_note_provenance,
    _build_note_tag_provenance,
    _normalize_note_metadata,
    _resolve_tag_mappings,
    _tag_suggestion_key,
)
from lab_tracker.services.ocr_backends import _sniff_content_type

_logger = logging.getLogger(__name__)
_OCR_UNAVAILABLE_WARNED = False


def _should_attempt_ocr(content: bytes, content_type: str) -> bool:
    # Prefer sniffing the bytes so we only attempt OCR for supported image formats.
    # This avoids trying (and warning) on non-image uploads that are mislabeled.
    return _sniff_content_type(content) is not None


_DESCRIPTIVE_PREFIXES = ("what ", "which ", "when ", "where ", "who ")
_DESCRIPTIVE_HOW_PREFIXES = ("how many", "how much", "how long", "how often")
_HYPOTHESIS_PREFIXES = (
    "does ",
    "do ",
    "can ",
    "could ",
    "will ",
    "would ",
    "is ",
    "are ",
    "should ",
)
_METHOD_HINTS = (
    "protocol",
    "pipeline",
    "workflow",
    "method",
    "assay",
    "setup",
    "configure",
    "implement",
    "optimiz",
    "calibrat",
    "benchmark",
    "validate",
)


def _suggest_question_type(text: str) -> QuestionType:
    """Heuristic suggestion for question_type for extracted candidates.

    This is intentionally lightweight: the UI treats this as a suggestion that the human reviewer
    can override.
    """

    normalized = " ".join((text or "").strip().casefold().split())
    if not normalized:
        return QuestionType.OTHER

    if normalized.startswith(_DESCRIPTIVE_HOW_PREFIXES):
        return QuestionType.DESCRIPTIVE

    if normalized.startswith("how to ") or any(hint in normalized for hint in _METHOD_HINTS):
        return QuestionType.METHOD_DEV

    if normalized.startswith(_DESCRIPTIVE_PREFIXES):
        return QuestionType.DESCRIPTIVE

    if normalized.startswith(_HYPOTHESIS_PREFIXES):
        return QuestionType.HYPOTHESIS_DRIVEN

    if any(
        token in normalized
        for token in (
            "effect",
            "impact",
            "increase",
            "decrease",
            "difference",
            "compare",
            "correlat",
            "predict",
            "cause",
            "lead to",
            "modulat",
        )
    ):
        return QuestionType.HYPOTHESIS_DRIVEN

    return QuestionType.OTHER


class NoteServiceMixin:
    def _delete_raw_asset(self, raw_asset: NoteRawAsset | None) -> None:
        if raw_asset is None or self._raw_storage is None:
            return
        delete = getattr(self._raw_storage, "delete", None)
        if not callable(delete):
            _logger.warning(
                "Raw storage backend does not support deletion for %s.",
                raw_asset.storage_id,
            )
            return
        try:
            delete(raw_asset.storage_id)
        except NotFoundError:
            return
        except Exception as exc:
            _logger.warning(
                "Failed to delete raw note asset %s: %s",
                raw_asset.storage_id,
                exc,
                exc_info=True,
            )

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
            created_by=_actor_user_id(actor),
        )
        self._store.notes[note.note_id] = note
        self._run_repository_write(lambda repository: repository.notes.save(note))
        self._queue_search_op("upsert_notes", [note])
        return note

    def store_note_raw_asset(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        content_type: str,
    ) -> NoteRawAsset:
        if self._raw_storage is None:
            raise ValidationError("Raw storage backend is not configured.")
        store_stream = getattr(self._raw_storage, "store_stream", None)
        if callable(store_stream):
            return store_stream(
                stream,
                filename=filename,
                content_type=content_type,
            )
        return self._raw_storage.store(
            stream.read(),
            filename=filename,
            content_type=content_type,
        )

    def upload_note_raw(
        self,
        project_id: UUID,
        content: bytes | None = None,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        raw_asset: NoteRawAsset | None = None,
        transcribed_text: str | None = None,
        extracted_entities: Iterable[ExtractedEntity | tuple[str, float, str]] | None = None,
        targets: Iterable[EntityRef] | None = None,
        metadata: dict[str, str] | None = None,
        status: NoteStatus = NoteStatus.STAGED,
        actor: AuthContext | None = None,
    ) -> Note:
        require_role(actor, WRITE_ROLES)
        if self._raw_storage is None:
            raise ValidationError("Raw storage backend is not configured.")
        asset = raw_asset
        resolved_content = content
        created_asset = False
        if asset is None:
            if resolved_content is None:
                raise ValidationError("content must not be empty.")
            asset = self._raw_storage.store(
                resolved_content,
                filename=(filename or "").strip(),
                content_type=(content_type or "").strip(),
            )
            created_asset = True
        try:
            if (
                resolved_content is None
                and transcribed_text is None
                and asset.content_type.strip().lower().startswith("image/")
            ):
                resolved_content = self._raw_storage.read(asset.storage_id)

            resolved_transcribed_text = transcribed_text.strip() if transcribed_text else None
            if (
                resolved_transcribed_text is None
                and resolved_content is not None
                and _should_attempt_ocr(resolved_content, asset.content_type)
            ):
                ocr_backend = getattr(self, "_ocr_backend", None)
                if ocr_backend is None:
                    global _OCR_UNAVAILABLE_WARNED
                    if not _OCR_UNAVAILABLE_WARNED:
                        _logger.warning(
                            "OCR backend is unavailable; uploaded notes will not be transcribed. "
                            "Install with `pip install -e '.[ocr]'` to enable OCR."
                        )
                        _OCR_UNAVAILABLE_WARNED = True
                else:
                    try:
                        result = ocr_backend.extract_text(resolved_content, asset.content_type)
                        resolved_transcribed_text = result.text.strip() if result.text else None
                    except Exception as exc:
                        _logger.warning(
                            "OCR failed for uploaded note %s (content_type=%s): %s",
                            asset.filename,
                            asset.content_type,
                            exc,
                            exc_info=True,
                        )
            return self.create_note(
                project_id=project_id,
                raw_content=None,
                raw_asset=asset,
                transcribed_text=resolved_transcribed_text,
                extracted_entities=extracted_entities,
                targets=targets,
                metadata=metadata,
                status=status,
                actor=actor,
            )
        except Exception:
            if asset is not None and (created_asset or raw_asset is not None):
                self._delete_raw_asset(asset)
            raise

    def get_note(self, note_id: UUID) -> Note:
        return self._get_from_repository_or_store(
            attribute_name="notes",
            entity_id=note_id,
            label="Note",
            loader=lambda repository: repository.notes.get(note_id),
        )

    def list_notes(
        self,
        *,
        project_id: UUID | None = None,
        status: NoteStatus | None = None,
        target_entity_type: EntityType | None = None,
        target_entity_id: UUID | None = None,
    ) -> list[Note]:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            notes, _ = repository.query_notes(
                project_id=project_id,
                status=status.value if status is not None else None,
                target_entity_type=(
                    target_entity_type.value if target_entity_type is not None else None
                ),
                target_entity_id=target_entity_id,
                limit=None,
                offset=0,
            )
            return self._cache_entities(
                "notes",
                notes,
                lambda note: note.note_id,
            )
        if project_id is None:
            notes = list(self._store.notes.values())
        else:
            notes = [n for n in self._store.notes.values() if n.project_id == project_id]
        if status is not None:
            notes = [note for note in notes if note.status == status]
        if target_entity_type is not None and target_entity_id is not None:
            notes = [
                note
                for note in notes
                if any(
                    target.entity_type == target_entity_type
                    and target.entity_id == target_entity_id
                    for target in note.targets
                )
            ]
        return notes

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
        self._run_repository_write(lambda repository: repository.notes.save(note))
        self._queue_search_op("upsert_notes", [note])
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
        self._store.notes.pop(note_id, None)
        self._run_repository_write(lambda repository: repository.notes.delete(note_id))
        self._queue_search_op("delete_notes", [note_id])
        self._delete_raw_asset(note.raw_asset)
        return note

    def extract_questions_from_note(
        self,
        note_id: UUID,
        *,
        question_type: QuestionType = QuestionType.OTHER,
        created_from: QuestionSource = QuestionSource.MEETING_CAPTURE,
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
        provenance_tag = provenance or _build_note_provenance(
            note.note_id,
            backend_name=backend.backend_name,
        )
        for candidate in candidates:
            normalized_text = candidate.text.strip()
            key = normalized_text.casefold()
            if key in existing:
                continue
            question = self.create_question(
                project_id=note.project_id,
                text=normalized_text,
                question_type=question_type,
                status=QuestionStatus.STAGED,
                created_from=created_from,
                source_provenance=provenance_tag,
                actor=actor,
            )
            staged_questions.append(question)
            existing.add(key)
        return staged_questions

    def extract_question_candidates_from_note(
        self,
        note_id: UUID,
        *,
        default_question_type: QuestionType | None = None,
        provenance: str | None = None,
        actor: AuthContext | None = None,
    ) -> list[QuestionExtractionCandidate]:
        """Extract candidate questions from a note for human review.

        Unlike :meth:`extract_questions_from_note`, this does not create/stage questions. The
        frontend can present the candidates for editing and then stage accepted items explicitly.
        """

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
        provenance_tag = provenance or _build_note_provenance(
            note.note_id,
            backend_name=backend.backend_name,
        )
        extracted: list[QuestionExtractionCandidate] = []
        for candidate in candidates:
            normalized_text = candidate.text.strip()
            if not normalized_text:
                continue
            key = normalized_text.casefold()
            if key in existing:
                continue
            suggested_type = default_question_type or _suggest_question_type(normalized_text)
            extracted.append(
                QuestionExtractionCandidate(
                    text=normalized_text,
                    confidence=candidate.confidence,
                    suggested_question_type=suggested_type,
                    provenance=provenance_tag,
                )
            )
            existing.add(key)
        return extracted

    def _ensure_target_exists(self, target: EntityRef, project_id: UUID) -> None:
        entity_getters = {
            EntityType.PROJECT: self.get_project,
            EntityType.QUESTION: self.get_question,
            EntityType.DATASET: self.get_dataset,
            EntityType.NOTE: self.get_note,
            EntityType.SESSION: self.get_session,
            EntityType.ANALYSIS: self.get_analysis,
            EntityType.CLAIM: self.get_claim,
            EntityType.VISUALIZATION: self.get_visualization,
        }
        getter = entity_getters.get(target.entity_type)
        if getter is None:
            raise ValidationError("Unsupported target entity type.")
        entity = getter(target.entity_id)
        if target.entity_type == EntityType.VISUALIZATION:
            analysis = self.get_analysis(entity.analysis_id)
            if analysis.project_id != project_id:
                raise ValidationError("Target must belong to the same project.")
            return
        if hasattr(entity, "project_id") and entity.project_id != project_id:
            raise ValidationError("Target must belong to the same project.")
