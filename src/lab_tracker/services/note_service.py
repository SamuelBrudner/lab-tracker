"""Note domain service mixin."""

from __future__ import annotations

import logging
from typing import BinaryIO, Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    EntityRef,
    EntityType,
    Note,
    NoteRawAsset,
    NoteStatus,
    utc_now,
)
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _actor_user_id,
    _normalize_note_metadata,
)

_logger = logging.getLogger(__name__)


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
        note = Note(
            note_id=uuid4(),
            project_id=project_id,
            raw_content=raw_text,
            raw_asset=raw_asset,
            transcribed_text=transcribed_text.strip() if transcribed_text else None,
            targets=resolved_targets,
            metadata=resolved_metadata,
            status=status,
            created_by=_actor_user_id(actor),
        )
        self._remember_entity("notes", note.note_id, note)
        self._run_repository_write(lambda repository: repository.notes.save(note))
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
        owns_raw_asset: bool = False,
        transcribed_text: str | None = None,
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
            note = self.create_note(
                project_id=project_id,
                raw_content=None,
                raw_asset=asset,
                transcribed_text=resolved_transcribed_text,
                targets=targets,
                metadata=metadata,
                status=status,
                actor=actor,
            )
        except Exception:
            if asset is not None and (created_asset or owns_raw_asset):
                self._delete_raw_asset(asset)
            raise
        if asset is not None and (created_asset or owns_raw_asset):
            self.run_after_rollback(lambda asset=asset: self._delete_raw_asset(asset))
        return note

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
        targets: Iterable[EntityRef] | None = None,
        metadata: dict[str, str] | None = None,
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

    def delete_note(self, note_id: UUID, *, actor: AuthContext | None = None) -> Note:
        require_role(actor, WRITE_ROLES)
        note = self.get_note(note_id)
        self._forget_entity("notes", note_id)
        self._run_repository_write(lambda repository: repository.notes.delete(note_id))
        if note.raw_asset is not None:
            self.run_after_commit(
                lambda raw_asset=note.raw_asset: self._delete_raw_asset(raw_asset)
            )
        return note

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
