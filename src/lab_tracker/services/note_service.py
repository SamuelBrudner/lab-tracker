"""Note domain service."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, BinaryIO
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.graph_drafting import PROVIDER, GraphDraftingError
from lab_tracker.member_onboarding import (
    CHECKPOINT_CLIENT_KEY_PREFIX,
    COMPLETED_AT_KEY,
    FIRST_CAPTURE_NOTE_ID_KEY,
    has_reserved_capture_key,
    has_reserved_note_metadata,
    is_member_checkpoint,
)
from lab_tracker.models import (
    EntityOrigin,
    EntityRef,
    EntityType,
    GraphChangeSetStatus,
    GraphDraftPurpose,
    Note,
    NoteArchiveReason,
    NoteMetadataScalar,
    NoteRawAsset,
    NoteStatus,
    UsageEventResourceType,
    UsageEventVerb,
    utc_now,
)
from lab_tracker.patching import NOT_PROVIDED, PatchValue, is_provided
from lab_tracker.provider_error_redaction import provider_error_message
from lab_tracker.services.analysis_service import AnalysisService
from lab_tracker.services.base import BaseService, IdempotentCreateResult, ServiceContext
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
from lab_tracker.services.shared import (
    normalize_client_capture_id as _normalize_client_capture_id,
)
from lab_tracker.services.visualization_service import VisualizationService

if TYPE_CHECKING:
    from lab_tracker.services.goal_service import GoalService

_logger = logging.getLogger(__name__)

_TRANSCRIPTION_MUTATION_METADATA_KEYS = {
    "auto_transcription_claim_id",
    "auto_transcription_claimed_at",
    "transcript_generated_at",
    "transcript_model",
    "transcript_provider",
    "transcript_source_storage_id",
    "transcript_status",
}
_AUTO_TRANSCRIPTION_CLAIM_TTL = timedelta(minutes=30)


def _canonical_raw_asset(asset: NoteRawAsset | None) -> tuple[str, str, int, str] | None:
    if asset is None:
        return None
    # storage_id identifies this particular stored copy, not the uploaded
    # content. A replay writes a temporary second copy before the uniqueness
    # race is resolved, so compare the stable file identity instead.
    return (asset.filename, asset.content_type, asset.size_bytes, asset.checksum)


def _canonical_targets(targets: Iterable[EntityRef]) -> list[tuple[str, str]]:
    return sorted(
        (target.entity_type.value, str(target.entity_id))
        for target in targets
    )


def _canonical_capture_metadata(
    metadata: dict[str, NoteMetadataScalar],
) -> dict[str, NoteMetadataScalar]:
    # The upload route stamps ingestion time after storing each physical copy.
    # Transcription fields are a later server-derived mutation, so neither can
    # turn an exact replay of the original upload into a conflict.
    return {
        key: value
        for key, value in metadata.items()
        if key != "source_file_ingested_at"
        and key not in _TRANSCRIPTION_MUTATION_METADATA_KEYS
    }


def _has_provider_transcript(note: Note) -> bool:
    source_storage_id = note.metadata.get("transcript_source_storage_id")
    return bool(
        note.raw_asset is not None
        and (note.transcribed_text or "").strip()
        and note.metadata.get("transcript_generated_at")
        and source_storage_id
        and str(source_storage_id) == str(note.raw_asset.storage_id)
    )


class NoteService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
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
        status: NoteStatus = NoteStatus.STAGED,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
        allow_member_onboarding_reserved: bool = False,
    ) -> Note:
        return self.create_note_result(
            project_id,
            raw_content,
            raw_asset=raw_asset,
            transcribed_text=transcribed_text,
            targets=targets,
            metadata=metadata,
            client_capture_id=client_capture_id,
            status=status,
            actor=actor,
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
            allow_member_onboarding_reserved=allow_member_onboarding_reserved,
        ).entity

    def create_note_result(
        self,
        project_id: UUID,
        raw_content: str | None = None,
        *,
        raw_asset: NoteRawAsset | None = None,
        transcribed_text: str | None = None,
        targets: Iterable[EntityRef] | None = None,
        metadata: dict[str, NoteMetadataScalar] | None = None,
        client_capture_id: str | None = None,
        status: NoteStatus = NoteStatus.STAGED,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
        allow_member_onboarding_reserved: bool = False,
    ) -> IdempotentCreateResult[Note]:
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        raw_text = raw_content.strip() if raw_content else ""
        if not raw_text and raw_asset is None:
            raise ValidationError("raw_content or raw_asset must be provided.")
        resolved_transcribed_text = transcribed_text.strip() if transcribed_text else None
        resolved_targets = list(targets or [])
        for target in resolved_targets:
            self.validate_target(target, project_id)
        if not allow_member_onboarding_reserved:
            if has_reserved_note_metadata(metadata):
                raise ValidationError(
                    "Member-onboarding metadata is reserved for the guided workflow."
                )
            if has_reserved_capture_key(client_capture_id):
                raise ValidationError(
                    f"client_capture_id values beginning {CHECKPOINT_CLIENT_KEY_PREFIX!r} "
                    "are reserved for the guided workflow."
                )
        resolved_metadata = normalize_note_metadata(metadata)
        resolved_client_capture_id = _normalize_client_capture_id(client_capture_id)
        if resolved_client_capture_id is not None:
            existing = self._find_client_capture_note(
                project_id,
                resolved_client_capture_id,
            )
            if existing is not None:
                self._ensure_matching_capture_note(
                    existing,
                    raw_content=raw_text,
                    raw_asset=raw_asset,
                    transcribed_text=resolved_transcribed_text,
                    targets=resolved_targets,
                    metadata=resolved_metadata,
                    status=status,
                    origin=origin,
                    change_set_id=change_set_id,
                    origin_provider=origin_provider,
                    origin_model=origin_model,
                    origin_prompt_version=origin_prompt_version,
                    client_capture_id=resolved_client_capture_id,
                )
                # A prior first-capture projection can lose a CAS to a
                # simultaneous trusted checkpoint update.  Exact replay is a
                # safe opportunity to repair that projection without emitting
                # duplicate events (the checkpoint CAS remains first-only).
                with self.application_transaction():
                    self._mark_first_member_onboarding_capture(existing, actor=actor)
                return IdempotentCreateResult("reused", existing)
        note = Note(
            note_id=uuid4(),
            project_id=project_id,
            raw_content=raw_text,
            raw_asset=raw_asset,
            transcribed_text=resolved_transcribed_text,
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
        with self.application_transaction():
            try:
                unit_of_work = (
                    self.recoverable_unit_of_work
                    if resolved_client_capture_id is not None
                    else self.unit_of_work
                )
                with unit_of_work() as repository:
                    repository.notes.save(note)
            except IntegrityError as exc:
                if resolved_client_capture_id is None:
                    raise
                existing = self._find_client_capture_note(
                    project_id,
                    resolved_client_capture_id,
                )
                if existing is None:
                    raise
                self._ensure_matching_capture_note(
                    existing,
                    raw_content=raw_text,
                    raw_asset=raw_asset,
                    transcribed_text=resolved_transcribed_text,
                    targets=resolved_targets,
                    metadata=resolved_metadata,
                    status=status,
                    origin=origin,
                    change_set_id=change_set_id,
                    origin_provider=origin_provider,
                    origin_model=origin_model,
                    origin_prompt_version=origin_prompt_version,
                    client_capture_id=resolved_client_capture_id,
                    cause=exc,
                )
                self._mark_first_member_onboarding_capture(existing, actor=actor)
                return IdempotentCreateResult("reused", existing)
            self._mark_first_member_onboarding_capture(note, actor=actor)
        return IdempotentCreateResult("created", note)

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
        status: NoteStatus = NoteStatus.STAGED,
        actor: AuthContext | None = None,
        allow_member_onboarding_reserved: bool = False,
    ) -> Note:
        return self.upload_note_raw_result(
            project_id,
            content,
            filename=filename,
            content_type=content_type,
            raw_asset=raw_asset,
            owns_raw_asset=owns_raw_asset,
            transcribed_text=transcribed_text,
            targets=targets,
            metadata=metadata,
            client_capture_id=client_capture_id,
            status=status,
            actor=actor,
            allow_member_onboarding_reserved=allow_member_onboarding_reserved,
        ).entity

    def upload_note_raw_result(
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
        status: NoteStatus = NoteStatus.STAGED,
        actor: AuthContext | None = None,
        allow_member_onboarding_reserved: bool = False,
    ) -> IdempotentCreateResult[Note]:
        asset = raw_asset
        created_asset = False
        try:
            if self.raw_storage is None:
                raise ValidationError("Raw storage backend is not configured.")
            self.authorization.require_contributor(project_id, actor=actor)
            self.projects.get_project(project_id)
            resolved_client_capture_id = _normalize_client_capture_id(client_capture_id)
            if asset is None:
                if content is None:
                    raise ValidationError("content must not be empty.")
                asset = self.raw_storage.store(
                    content,
                    filename=(filename or "").strip(),
                    content_type=(content_type or "").strip(),
                )
                created_asset = True
            resolved_transcribed_text = transcribed_text.strip() if transcribed_text else None
            result = self.create_note_result(
                project_id=project_id,
                raw_content=None,
                raw_asset=asset,
                transcribed_text=resolved_transcribed_text,
                targets=targets,
                metadata=metadata,
                client_capture_id=resolved_client_capture_id,
                status=status,
                actor=actor,
                allow_member_onboarding_reserved=allow_member_onboarding_reserved,
            )
        except Exception:
            if asset is not None and (created_asset or owns_raw_asset):
                self._delete_raw_asset(asset)
            raise
        if result.reused and asset is not None and (created_asset or owns_raw_asset):
            self._delete_raw_asset(asset)
        if result.created and asset is not None and (created_asset or owns_raw_asset):
            self.run_after_rollback(lambda asset=asset: self._delete_raw_asset(asset))
        return result

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

    @staticmethod
    def _ensure_matching_capture_note(
        existing: Note,
        *,
        raw_content: str,
        raw_asset: NoteRawAsset | None,
        transcribed_text: str | None,
        targets: Iterable[EntityRef],
        metadata: dict[str, NoteMetadataScalar],
        status: NoteStatus,
        origin: EntityOrigin,
        change_set_id: UUID | None,
        origin_provider: str | None,
        origin_model: str | None,
        origin_prompt_version: str | None,
        client_capture_id: str,
        cause: Exception | None = None,
    ) -> None:
        supplied = {
            "raw_content": raw_content,
            "raw_asset": _canonical_raw_asset(raw_asset),
            "transcribed_text": transcribed_text,
            "targets": _canonical_targets(targets),
            "metadata": _canonical_capture_metadata(metadata),
            "status": status,
            "origin": origin,
            "change_set_id": change_set_id,
            "origin_provider": origin_provider,
            "origin_model": origin_model,
            "origin_prompt_version": origin_prompt_version,
        }
        stored = {
            "raw_content": existing.raw_content,
            "raw_asset": _canonical_raw_asset(existing.raw_asset),
            "transcribed_text": (
                None
                if transcribed_text is None and _has_provider_transcript(existing)
                else existing.transcribed_text
            ),
            "targets": _canonical_targets(existing.targets),
            "metadata": _canonical_capture_metadata(existing.metadata),
            "status": existing.status,
            "origin": existing.origin,
            "change_set_id": existing.change_set_id,
            "origin_provider": existing.origin_provider,
            "origin_model": existing.origin_model,
            "origin_prompt_version": existing.origin_prompt_version,
        }
        conflicts = [field for field, value in supplied.items() if stored[field] != value]
        if conflicts:
            error = ConflictError(
                "Note client_capture_id "
                f"{client_capture_id!r} was already used with different "
                f"field(s): {', '.join(conflicts)}."
            )
            if cause is not None:
                raise error from cause
            raise error

    def auto_transcribe_voice_note(
        self,
        note_id: UUID,
        *,
        transcription_client: Any,
        actor: AuthContext | None = None,
        on_provider_call: Callable[[Note], None] | None = None,
    ) -> tuple[Note, bool]:
        """Claim, call, and merge one opt-in automatic transcription.

        The claim and final merge use separate short transactions. Raw audio
        leaves the instance only for the claim winner, and the provider prompt
        comes from that latest claimed note instead of the upload response.
        """

        note = self.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        if is_member_checkpoint(note):
            raise ValidationError(
                "Member onboarding checkpoints cannot be transcribed outside "
                "the guided workflow."
            )
        claimed_at = utc_now()
        claim_id = uuid4()
        with self.unit_of_work() as repository:
            claimed = repository.notes.try_claim_auto_transcription(
                note_id,
                claim_id=claim_id,
                claimed_at=claimed_at,
                stale_before=claimed_at - _AUTO_TRANSCRIPTION_CLAIM_TTL,
            )
        if claimed is None:
            return self.get_note(note_id), False

        capture_hint = claimed.metadata.get("capture_hint")
        prompt = str(capture_hint).strip() if capture_hint is not None else ""
        try:
            text, transcript_metadata, completed_at = (
                self._request_voice_transcript(
                    note=claimed,
                    transcription_client=transcription_client,
                    prompt=prompt or None,
                    on_provider_call=on_provider_call,
                )
            )
            with self.unit_of_work() as repository:
                current = repository.notes.apply_auto_transcription_result(
                    note_id,
                    claim_id=claim_id,
                    claimed_updated_at=claimed.updated_at,
                    text=text,
                    metadata_updates=transcript_metadata,
                    updated_at=completed_at,
                )
        except Exception:
            try:
                with self.unit_of_work() as repository:
                    repository.notes.release_auto_transcription_claim(
                        note_id,
                        claim_id=claim_id,
                        updated_at=utc_now(),
                    )
            except Exception:
                _logger.exception(
                    "Automatic transcription claim cleanup failed for note %s.",
                    note_id,
                )
            raise
        if current is None:
            raise NotFoundError("Note does not exist.")
        return current, True

    def transcribe_voice_note(
        self,
        note_id: UUID,
        *,
        transcription_client: Any,
        prompt: str | None = None,
        actor: AuthContext | None = None,
    ) -> Note:
        """Transcribe one audio note without restoring stale note state."""

        note = self.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        if is_member_checkpoint(note):
            raise ValidationError(
                "Member onboarding checkpoints cannot be transcribed outside "
                "the guided workflow."
            )
        text, transcript_metadata, completed_at = self._request_voice_transcript(
            note=note,
            transcription_client=transcription_client,
            prompt=prompt,
        )
        with self.unit_of_work() as repository:
            current = repository.notes.apply_transcription_result(
                note_id,
                text=text,
                metadata_updates=transcript_metadata,
                updated_at=completed_at,
            )
        if current is None:
            raise NotFoundError("Note does not exist.")
        return current

    def _request_voice_transcript(
        self,
        *,
        note: Note,
        transcription_client: Any,
        prompt: str | None,
        on_provider_call: Callable[[Note], None] | None = None,
    ) -> tuple[str, dict[str, NoteMetadataScalar], datetime]:
        if note.raw_asset is None:
            raise ValidationError("Voice transcription requires a note with a raw audio asset.")
        if not note.raw_asset.content_type.lower().startswith("audio/"):
            raise ValidationError("Voice transcription only supports audio note uploads.")
        raw_asset = note.raw_asset
        try:
            if self.raw_storage is None:
                raise ValidationError("Raw storage backend is not configured.")
            audio_bytes = self.raw_storage.read(raw_asset.storage_id)
        except NotFoundError as exc:
            raise NotFoundError("Source audio file is unavailable.") from exc
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError("Source audio file could not be read.") from exc
        transcribe_audio = getattr(transcription_client, "transcribe_audio", None)
        if not callable(transcribe_audio):
            raise ValidationError("Configured transcription client does not support audio.")
        if on_provider_call is not None:
            on_provider_call(note)
        try:
            transcript = transcribe_audio(
                audio_bytes=audio_bytes,
                filename=raw_asset.filename,
                content_type=raw_asset.content_type,
                prompt=prompt,
            )
        except GraphDraftingError as exc:
            raise ValidationError(
                f"Voice transcription failed: {provider_error_message(exc)}"
            ) from exc
        text = _transcript_text(transcript)
        if not text:
            raise ValidationError("Voice transcription response did not include text.")
        completed_at = utc_now()
        transcript_metadata: dict[str, NoteMetadataScalar] = {
            "transcript_status": "ready",
            "transcript_provider": str(getattr(transcription_client, "provider", PROVIDER)),
            "transcript_model": str(
                getattr(
                    transcription_client,
                    "transcription_model",
                    getattr(transcription_client, "model", "unknown"),
                )
            ),
            "transcript_generated_at": completed_at.isoformat(),
            "transcript_source_storage_id": str(raw_asset.storage_id),
        }
        return text, transcript_metadata, completed_at

    def get_note(self, note_id: UUID) -> Note:
        return self.get_from_repository(
            entity_id=note_id,
            label="Note",
            loader=lambda repository: repository.notes.get(note_id),
        )

    def get_note_for_read(
        self,
        note_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Note:
        note = self.get_note(note_id)
        if not self.authorization.can_read(note.project_id, actor=actor):
            raise NotFoundError("Note does not exist.")
        return note

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
        transcribed_text: PatchValue[str | None] = NOT_PROVIDED,
        targets: PatchValue[Iterable[EntityRef] | None] = NOT_PROVIDED,
        metadata: PatchValue[dict[str, NoteMetadataScalar] | None] = NOT_PROVIDED,
        status: PatchValue[NoteStatus | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
        allow_member_onboarding_checkpoint: bool = False,
    ) -> Note:
        note = self.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        designated_checkpoint = self._member_onboarding_checkpoint_for_capture(note)
        reserved_update = is_provided(metadata) and has_reserved_note_metadata(metadata)
        if not allow_member_onboarding_checkpoint and (
            is_member_checkpoint(note)
            or has_reserved_note_metadata(note.metadata)
            or reserved_update
        ):
            raise ValidationError(
                "Member-onboarding metadata and checkpoints are immutable outside "
                "the guided workflow."
            )
        before = note.model_copy(deep=True)
        if designated_checkpoint is not None:
            if is_provided(targets):
                proposed_targets = list(targets or [])
                if not any(
                    target.entity_type == EntityType.NOTE
                    and target.entity_id == designated_checkpoint.note_id
                    for target in proposed_targets
                ):
                    raise ValidationError(
                        "The designated first capture must remain attached to its "
                        "member onboarding checkpoint."
                    )
                targets = proposed_targets
            if is_provided(status) and status not in {
                NoteStatus.STAGED,
                NoteStatus.COMMITTED,
            }:
                raise ValidationError(
                    "The designated first capture must remain staged or committed."
                )
            if (
                origin not in {None, EntityOrigin.USER}
                or change_set_id is not None
            ):
                raise ValidationError(
                    "The designated first capture must remain a direct human record."
                )
        if is_provided(transcribed_text):
            note.transcribed_text = transcribed_text.strip() if transcribed_text else None
        if is_provided(targets):
            if targets is None:
                raise ValidationError("targets must not be null.")
            resolved_targets = list(targets)
            for target in resolved_targets:
                self.validate_target(target, note.project_id)
            note.targets = resolved_targets
        if is_provided(metadata):
            if metadata is None:
                raise ValidationError("metadata must not be null.")
            note.metadata = normalize_note_metadata(metadata)
        if is_provided(status):
            if status is None:
                raise ValidationError("status must not be null.")
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
        if note == before:
            return note
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
        allow_member_onboarding_checkpoint: bool = False,
    ) -> Note:
        """Set a captured note aside, recording why and by whom.

        Archiving always names a reason so a capture is never silently dropped:
        the record stays visible as archived (including
        ``archived_unreviewed``), so a skipped review degrades visible coverage
        rather than silent trust.
        """

        note = self.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        if is_member_checkpoint(note) and not allow_member_onboarding_checkpoint:
            raise ValidationError(
                "Member onboarding checkpoints cannot be archived outside the guided workflow."
            )
        if self._member_onboarding_checkpoint_for_capture(note) is not None:
            raise ValidationError(
                "The designated first member-onboarding capture cannot be archived."
            )
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

    def delete_note(
        self,
        note_id: UUID,
        *,
        actor: AuthContext | None = None,
        allow_member_onboarding_checkpoint: bool = False,
    ) -> Note:
        note = self.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        if is_member_checkpoint(note) and not allow_member_onboarding_checkpoint:
            raise ValidationError(
                "Member onboarding checkpoints cannot be deleted."
            )
        if self._member_onboarding_checkpoint_for_capture(note) is not None:
            raise ValidationError(
                "The designated first member-onboarding capture cannot be deleted."
            )
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

    def add_member_onboarding_question_target(
        self,
        checkpoint_id: UUID,
        *,
        question_id: UUID,
        actor: AuthContext | None,
    ) -> Note:
        """Add one trusted alignment edge without replacing checkpoint state."""

        checkpoint = self.get_note(checkpoint_id)
        self.authorization.require_contributor(checkpoint.project_id, actor=actor)
        if not is_member_checkpoint(checkpoint):
            raise ValidationError("Onboarding link target must be a member checkpoint.")
        question = self.questions.get_question(question_id)
        if question.project_id != checkpoint.project_id:
            raise ValidationError("Onboarding question link must stay in one project.")
        with self.unit_of_work() as repository:
            updated = repository.notes.add_member_onboarding_question_target(
                checkpoint_id,
                question_id=question_id,
            )
        if updated is None:
            raise NotFoundError("Note does not exist.")
        return updated

    def _mark_first_member_onboarding_capture(
        self,
        note: Note,
        *,
        actor: AuthContext | None,
    ) -> None:
        """Project one genuine forward capture onto its author's checkpoint.

        The generic capture remains the durable evidence record.  These trusted
        checkpoint markers only make the onboarding funnel idempotently
        queryable; they contain no research content.
        """

        if (
            actor is None
            or not actor.is_interactive
            or note.status not in {NoteStatus.STAGED, NoteStatus.COMMITTED}
            or note.origin != EntityOrigin.USER
            or note.change_set_id is not None
        ):
            return

        for target in note.targets:
            if target.entity_type != EntityType.NOTE or target.entity_id == note.note_id:
                continue
            checkpoint = self.repository.notes.get(target.entity_id)
            if checkpoint is None or not is_member_checkpoint(checkpoint):
                continue
            if checkpoint.project_id != note.project_id:
                continue
            if checkpoint.created_by_user_id is not None:
                if checkpoint.created_by_user_id != note.created_by_user_id:
                    continue
            elif checkpoint.created_by != note.created_by:
                continue
            if checkpoint.metadata.get(FIRST_CAPTURE_NOTE_ID_KEY):
                continue
            captured_at = utc_now()
            completed = self._checkpoint_alignment_is_resolved(checkpoint)
            checkpoint = self.repository.notes.try_mark_member_onboarding_first_capture(
                checkpoint.note_id,
                capture_note_id=note.note_id,
                captured_at=captured_at,
                completed=completed,
            )
            if checkpoint is None:
                continue
            self.record_usage_event(
                verb=UsageEventVerb.UPLOAD,
                resource_type=UsageEventResourceType.MEMBER_ONBOARDING,
                resource_id=checkpoint.note_id,
                project_id=checkpoint.project_id,
                actor=actor,
            )
            if completed:
                self.record_usage_event(
                    verb=UsageEventVerb.SUBMIT,
                    resource_type=UsageEventResourceType.MEMBER_ONBOARDING,
                    resource_id=checkpoint.note_id,
                    project_id=checkpoint.project_id,
                    actor=actor,
                )
            else:
                self.schedule_member_onboarding_completion_reconciliation(
                    checkpoint.note_id,
                    actor=actor,
                )
            return

    def _member_onboarding_checkpoint_for_capture(
        self,
        note: Note,
    ) -> Note | None:
        """Return the checkpoint that durably designated this capture, if any."""

        checkpoints, _ = self.repository.query_notes(
            project_id=note.project_id,
            target_entity_type=EntityType.PROJECT.value,
            target_entity_id=note.project_id,
            limit=None,
            offset=0,
        )
        capture_id = str(note.note_id)
        return next(
            (
                checkpoint
                for checkpoint in checkpoints
                if is_member_checkpoint(checkpoint)
                and checkpoint.metadata.get(FIRST_CAPTURE_NOTE_ID_KEY) == capture_id
            ),
            None,
        )

    def schedule_member_onboarding_completion_reconciliation(
        self,
        checkpoint_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None:
        """Close the rare alignment/capture commit-order race after commit."""

        self.run_after_commit(
            lambda: self._reconcile_member_onboarding_completion(
                checkpoint_id,
                actor=actor,
            )
        )

    def _reconcile_member_onboarding_completion(
        self,
        checkpoint_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None:
        checkpoint = self.repository.notes.get(checkpoint_id)
        if (
            checkpoint is None
            or checkpoint.metadata.get(COMPLETED_AT_KEY)
            or not checkpoint.metadata.get(FIRST_CAPTURE_NOTE_ID_KEY)
            or not self._checkpoint_alignment_is_resolved(checkpoint)
        ):
            return
        completed = self.repository.notes.try_mark_member_onboarding_completed(
            checkpoint_id,
            completed_at=utc_now(),
        )
        if completed is None:
            return
        # Request middleware has already committed before running this hook;
        # publish the CAS before scheduling the fail-soft usage event.
        self.repository.commit()
        self.record_usage_event(
            verb=UsageEventVerb.SUBMIT,
            resource_type=UsageEventResourceType.MEMBER_ONBOARDING,
            resource_id=completed.note_id,
            project_id=completed.project_id,
            actor=actor,
        )

    def _checkpoint_alignment_is_resolved(self, checkpoint: Note) -> bool:
        if checkpoint.metadata.get("member_onboarding_alignment_mode") in {
            "manual",
            "ai",
        }:
            return True
        drafts, _ = self.repository.query_graph_change_sets(
            project_id=checkpoint.project_id,
            source_note_id=checkpoint.note_id,
            purpose=GraphDraftPurpose.MEMBER_CHECKPOINT_ALIGNMENT.value,
            limit=None,
            offset=0,
        )
        return any(
            draft.status
            in {
                GraphChangeSetStatus.SUBMITTED,
                GraphChangeSetStatus.REJECTED,
                GraphChangeSetStatus.COMMITTED,
            }
            for draft in drafts
        )

    def validate_target(self, target: EntityRef, project_id: UUID) -> None:
        """Validate one note target and project scope without writing."""
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


def _transcript_text(transcript: Any) -> str:
    if isinstance(transcript, str):
        return transcript.strip()
    if isinstance(transcript, dict):
        text = transcript.get("text")
        if isinstance(text, str):
            return text.strip()
    return ""
