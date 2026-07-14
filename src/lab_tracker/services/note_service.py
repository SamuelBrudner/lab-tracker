"""Note domain service."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, BinaryIO
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.graph_drafting import PROVIDER, GraphDraftingError
from lab_tracker.models import (
    EntityOrigin,
    EntityRef,
    EntityType,
    Note,
    NoteArchiveReason,
    NoteMetadataScalar,
    NoteRawAsset,
    NoteStatus,
    utc_now,
)
from lab_tracker.services.analysis_service import AnalysisService
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.claim_service import ClaimService
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.goal_link_cleanup import remove_goal_links_to_entity
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.session_service import SessionService
from lab_tracker.services.shared import (
    actor_user_fk,
    actor_user_id,
    normalize_note_metadata,
)
from lab_tracker.services.visualization_service import VisualizationService

if TYPE_CHECKING:
    from lab_tracker.services.capture_context_service import CaptureContextService
    from lab_tracker.services.goal_service import GoalService

_logger = logging.getLogger(__name__)


class NoteService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        capture_contexts: CaptureContextService | None = None,
        questions: QuestionService,
        datasets: DatasetService,
        sessions: SessionService,
        analyses: AnalysisService,
        claims: ClaimService,
        visualizations: VisualizationService,
        goals_provider: Callable[[], GoalService] | None = None,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.capture_contexts = capture_contexts
        self.questions = questions
        self.datasets = datasets
        self.sessions = sessions
        self.analyses = analyses
        self.claims = claims
        self.visualizations = visualizations
        self._goals_provider = goals_provider
        self.authorization = authorization

    @property
    def goals(self) -> GoalService:
        if self._goals_provider is None:
            raise ValidationError("Goal service is not configured.")
        return self._goals_provider()

    def _delete_raw_asset(self, raw_asset: NoteRawAsset | None) -> None:
        if raw_asset is None or self.raw_storage is None:
            return
        delete = getattr(self.raw_storage, "delete", None)
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
        metadata: dict[str, NoteMetadataScalar] | None = None,
        client_capture_id: str | None = None,
        capture_context_id: UUID | None = None,
        status: NoteStatus = NoteStatus.STAGED,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Note:
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        resolved_client_capture_id = _normalize_client_capture_id(client_capture_id)
        if resolved_client_capture_id is not None:
            existing = self._find_client_capture_note(
                project_id,
                resolved_client_capture_id,
            )
            if existing is not None:
                return existing
        raw_text = raw_content.strip() if raw_content else ""
        if not raw_text and raw_asset is None:
            raise ValidationError("raw_content or raw_asset must be provided.")
        resolved_targets = list(targets or [])
        resolved_metadata = normalize_note_metadata(metadata)
        if capture_context_id is not None:
            resolved_targets, resolved_metadata = self._apply_capture_context(
                project_id=project_id,
                targets=resolved_targets,
                metadata=resolved_metadata,
                capture_context_id=capture_context_id,
                actor=actor,
            )
        for target in resolved_targets:
            self.validate_target(target, project_id)
        note = Note(
            note_id=uuid4(),
            project_id=project_id,
            raw_content=raw_text,
            raw_asset=raw_asset,
            transcribed_text=transcribed_text.strip() if transcribed_text else None,
            targets=resolved_targets,
            metadata=resolved_metadata,
            client_capture_id=resolved_client_capture_id,
            status=status,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
        )
        with self.unit_of_work() as repository:
            repository.notes.save(note)
        return note

    def store_note_raw_asset(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        content_type: str,
    ) -> NoteRawAsset:
        if self.raw_storage is None:
            raise ValidationError("Raw storage backend is not configured.")
        store_stream = getattr(self.raw_storage, "store_stream", None)
        if callable(store_stream):
            return store_stream(
                stream,
                filename=filename,
                content_type=content_type,
            )
        return self.raw_storage.store(
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
        metadata: dict[str, NoteMetadataScalar] | None = None,
        client_capture_id: str | None = None,
        capture_context_id: UUID | None = None,
        status: NoteStatus = NoteStatus.STAGED,
        actor: AuthContext | None = None,
    ) -> Note:
        if self.raw_storage is None:
            raise ValidationError("Raw storage backend is not configured.")
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        resolved_client_capture_id = _normalize_client_capture_id(client_capture_id)
        if resolved_client_capture_id is not None:
            existing = self._find_client_capture_note(
                project_id,
                resolved_client_capture_id,
            )
            if existing is not None:
                if raw_asset is not None and owns_raw_asset:
                    self._delete_raw_asset(raw_asset)
                return existing
        asset = raw_asset
        created_asset = False
        if asset is None:
            if content is None:
                raise ValidationError("content must not be empty.")
            asset = self.raw_storage.store(
                content,
                filename=(filename or "").strip(),
                content_type=(content_type or "").strip(),
            )
            created_asset = True
        try:
            resolved_transcribed_text = transcribed_text.strip() if transcribed_text else None
            note = self.create_note(
                project_id=project_id,
                raw_content=None,
                raw_asset=asset,
                transcribed_text=resolved_transcribed_text,
                targets=targets,
                metadata=metadata,
                client_capture_id=resolved_client_capture_id,
                capture_context_id=capture_context_id,
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

    def find_note_by_client_capture_id(
        self,
        project_id: UUID,
        client_capture_id: str | None,
        *,
        actor: AuthContext | None = None,
    ) -> Note | None:
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        resolved_client_capture_id = _normalize_client_capture_id(client_capture_id)
        if resolved_client_capture_id is None:
            return None
        return self._find_client_capture_note(project_id, resolved_client_capture_id)

    def _find_client_capture_note(
        self,
        project_id: UUID,
        client_capture_id: str,
    ) -> Note | None:
        notes = self.query_from_repository(
            loader=lambda repository: repository.query_notes(
                project_id=project_id,
                client_capture_id=client_capture_id,
                limit=1,
                offset=0,
            ),
        )
        return notes[0] if notes else None

    def transcribe_voice_note(
        self,
        note_id: UUID,
        *,
        transcription_client: Any,
        prompt: str | None = None,
        actor: AuthContext | None = None,
    ) -> Note:
        note = self.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        if note.raw_asset is None:
            raise ValidationError("Voice transcription requires a note with a raw audio asset.")
        if not note.raw_asset.content_type.lower().startswith("audio/"):
            raise ValidationError("Voice transcription only supports audio note uploads.")
        try:
            raw_asset, audio_bytes = self.download_note_raw(note_id)
        except NotFoundError as exc:
            raise NotFoundError("Source audio file is unavailable.") from exc
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError("Source audio file could not be read.") from exc
        transcribe_audio = getattr(transcription_client, "transcribe_audio", None)
        if not callable(transcribe_audio):
            raise ValidationError("Configured transcription client does not support audio.")
        try:
            transcript = transcribe_audio(
                audio_bytes=audio_bytes,
                filename=raw_asset.filename,
                content_type=raw_asset.content_type,
                prompt=prompt,
            )
        except GraphDraftingError as exc:
            raise ValidationError(f"Voice transcription failed: {exc}") from exc
        text = _transcript_text(transcript)
        if not text:
            raise ValidationError("Voice transcription response did not include text.")
        metadata = dict(note.metadata)
        metadata.update(
            {
                "transcript_status": "ready",
                "transcript_provider": str(getattr(transcription_client, "provider", PROVIDER)),
                "transcript_model": str(
                    getattr(
                        transcription_client,
                        "transcription_model",
                        getattr(transcription_client, "model", "unknown"),
                    )
                ),
                "transcript_generated_at": utc_now().isoformat(),
                "transcript_source_storage_id": str(raw_asset.storage_id),
            }
        )
        note.transcribed_text = text
        note.metadata = metadata
        note.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.notes.save(note)
        return note

    def get_note(self, note_id: UUID) -> Note:
        return self.get_from_repository(
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
        return self.query_from_repository(
            loader=lambda repository: repository.query_notes(
                project_id=project_id,
                status=status.value if status is not None else None,
                target_entity_type=(
                    target_entity_type.value if target_entity_type is not None else None
                ),
                target_entity_id=target_entity_id,
                limit=None,
                offset=0,
            ),
        )

    def update_note(
        self,
        note_id: UUID,
        *,
        transcribed_text: str | None = None,
        targets: Iterable[EntityRef] | None = None,
        metadata: dict[str, NoteMetadataScalar] | None = None,
        status: NoteStatus | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Note:
        note = self.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        if transcribed_text is not None:
            note.transcribed_text = transcribed_text.strip() if transcribed_text else None
        if targets is not None:
            resolved_targets = list(targets)
            for target in resolved_targets:
                self.validate_target(target, note.project_id)
            note.targets = resolved_targets
        if metadata is not None:
            note.metadata = normalize_note_metadata(metadata)
        if status is not None:
            note.status = status
        if origin is not None:
            note.origin = origin
        if change_set_id is not None:
            note.change_set_id = change_set_id
        if origin_provider is not None:
            note.origin_provider = origin_provider
        if origin_model is not None:
            note.origin_model = origin_model
        if origin_prompt_version is not None:
            note.origin_prompt_version = origin_prompt_version
        note.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.notes.save(note)
        return note

    def archive_note(
        self,
        note_id: UUID,
        *,
        reason: NoteArchiveReason,
        actor: AuthContext | None = None,
    ) -> Note:
        """Set a captured note aside, recording why and by whom.

        Archiving always names a reason so a capture is never silently dropped:
        the record stays visible as archived (including
        ``archived_unreviewed``), so a skipped review degrades visible coverage
        rather than silent trust.
        """

        note = self.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        note.status = NoteStatus.ARCHIVED
        note.archived_reason = reason
        note.archived_at = utc_now()
        note.archived_by = actor_user_id(actor)
        note.archived_by_user_id = actor_user_fk(actor, self.repository)
        note.updated_at = note.archived_at
        with self.unit_of_work() as repository:
            repository.notes.save(note)
        return note

    def download_note_raw(self, note_id: UUID) -> tuple[NoteRawAsset, bytes]:
        note = self.get_note(note_id)
        if note.raw_asset is None:
            raise NotFoundError("Note does not have raw content.")
        if self.raw_storage is None:
            raise ValidationError("Raw storage backend is not configured.")
        content = self.raw_storage.read(note.raw_asset.storage_id)
        return note.raw_asset, content

    def delete_note(self, note_id: UUID, *, actor: AuthContext | None = None) -> Note:
        note = self.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        self._ensure_note_can_be_deleted(note)
        with self.unit_of_work() as repository:
            remove_goal_links_to_entity(
                repository,
                entity_type=EntityType.NOTE,
                entity_id=note_id,
            )
            repository.notes.delete(note_id)
        if note.raw_asset is not None:
            self.run_after_commit(
                lambda raw_asset=note.raw_asset: self._delete_raw_asset(raw_asset)
            )
        return note

    def _ensure_note_can_be_deleted(self, note: Note) -> None:
        change_sets = self.query_from_repository(
            loader=lambda repository: repository.query_graph_change_sets(
                project_id=note.project_id,
                limit=None,
                offset=0,
            ),
        )
        if any(
            change_set.source_note_id == note.note_id or note.note_id in change_set.source_note_ids
            for change_set in change_sets
        ):
            raise ValidationError("Note cannot be deleted while graph drafts reference it.")

    def validate_target(self, target: EntityRef, project_id: UUID) -> None:
        """Require a supported target that exists in the selected project."""

        entity_getters = {
            EntityType.PROJECT: self.projects.get_project,
            EntityType.QUESTION: self.questions.get_question,
            EntityType.DATASET: self.datasets.get_dataset,
            EntityType.NOTE: self.get_note,
            EntityType.SESSION: self.sessions.get_session,
            EntityType.ANALYSIS: self.analyses.get_analysis,
            EntityType.CLAIM: self.claims.get_claim,
            EntityType.VISUALIZATION: self.visualizations.get_visualization,
        }
        if self._goals_provider is not None:
            entity_getters[EntityType.GOAL] = self.goals.get_goal
        getter = entity_getters.get(target.entity_type)
        if getter is None:
            raise ValidationError("Unsupported target entity type.")
        entity = getter(target.entity_id)
        if target.entity_type == EntityType.VISUALIZATION:
            analysis = self.analyses.get_analysis(entity.analysis_id)
            if analysis.project_id != project_id:
                raise ValidationError("Target must belong to the same project.")
            return
        if hasattr(entity, "project_id") and entity.project_id != project_id:
            raise ValidationError("Target must belong to the same project.")

    def _apply_capture_context(
        self,
        *,
        project_id: UUID,
        targets: list[EntityRef],
        metadata: dict[str, str],
        capture_context_id: UUID,
        actor: AuthContext | None,
    ) -> tuple[list[EntityRef], dict[str, str]]:
        if self.capture_contexts is None:
            raise ValidationError("Capture context service is not configured.")
        context = self.capture_contexts.get_capture_context(capture_context_id, actor=actor)
        if context.project_id != project_id:
            raise ValidationError("Capture context must belong to the note project.")
        if context.revoked_at is not None:
            raise ValidationError("Capture context has been revoked.")
        merged_targets: list[EntityRef] = []
        seen: set[tuple[EntityType, UUID]] = set()
        for target in [*context.default_targets, *targets]:
            key = (target.entity_type, target.entity_id)
            if key in seen:
                continue
            seen.add(key)
            merged_targets.append(target)
        merged_metadata = dict(metadata)
        merged_metadata["capture_context_id"] = str(context.capture_context_id)
        merged_metadata["capture_context_label"] = context.label
        if context.site_label:
            merged_metadata["capture_site_label"] = context.site_label
        if context.place_label:
            merged_metadata["capture_place_label"] = context.place_label
        if context.default_hint:
            merged_metadata["capture_context_hint"] = context.default_hint
        return merged_targets, merged_metadata


def _transcript_text(transcript: Any) -> str:
    if isinstance(transcript, str):
        return transcript.strip()
    if isinstance(transcript, dict):
        text = transcript.get("text")
        if isinstance(text, str):
            return text.strip()
    return ""


def _normalize_client_capture_id(client_capture_id: str | None) -> str | None:
    if client_capture_id is None:
        return None
    value = client_capture_id.strip()
    if not value:
        return None
    if len(value) > 120:
        raise ValidationError("client_capture_id must be 120 characters or fewer.")
    return value
