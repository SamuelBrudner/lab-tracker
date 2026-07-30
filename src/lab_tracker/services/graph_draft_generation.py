"""Graph-draft generation coordinator."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.graph_drafting import (
    ANALYSIS_PROMPT_VERSION,
    BATCH_PROMPT_VERSION,
    PROMPT_VERSION,
    PROVIDER,
    GraphDraftClient,
    GraphDraftingError,
)
from lab_tracker.models import (
    GraphChangeOperation,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    Note,
    NoteRawAsset,
    NoteStatus,
    utc_now,
)
from lab_tracker.provider_error_redaction import provider_error_message
from lab_tracker.services import graph_draft_batch_policy as batch_policy
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.graph_draft_validation import string_list
from lab_tracker.services.shared import UserExistenceReader, actor_user_fk, actor_user_id

DEFAULT_BATCH_RETRY_ATTEMPTS = 3


class GenerationRecords(Protocol):
    def save_graph_change_set(self, change_set: GraphChangeSet) -> None: ...

    def list_graph_change_sets(
        self,
        *,
        draft_mode: GraphDraftMode | None = None,
        batch_key: str | None = None,
    ) -> list[GraphChangeSet]: ...


class GenerationNotes(Protocol):
    def get_note(self, note_id: UUID) -> Note: ...

    def download_note_raw(self, note_id: UUID) -> tuple[NoteRawAsset, bytes]: ...


class GenerationAuthorization(Protocol):
    def require_contributor(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...


class GenerationContextBuilder(Protocol):
    def prepare_note_sources_for_graph_draft(
        self,
        note_id: UUID,
        *,
        mode: GraphDraftMode,
        source_note_ids: list[UUID] | None = None,
    ) -> dict[str, Any]: ...

    def build_graph_context_packet(
        self,
        note: Note,
        *,
        source_notes: list[Note],
        user_hint: str | None,
        actor: AuthContext | None,
    ) -> dict[str, Any]: ...

    def image_only_context_packet(
        self,
        note: Note,
        *,
        source_notes: list[Note],
        user_hint: str | None,
    ) -> dict[str, Any]: ...

    def build_batch_graph_context(
        self,
        notes: list[Note],
        *,
        window: tuple[datetime, datetime] | None,
        actor: AuthContext | None,
        batch_note_limit: int,
    ) -> dict[str, Any]: ...


class GenerationPatchValidator(Protocol):
    def validate_top_level(self, graph_patch: dict[str, Any]) -> None: ...

    def operations_from_graph_patch(
        self,
        change_set: GraphChangeSet,
        graph_patch: dict[str, Any],
    ) -> list[GraphChangeOperation]: ...


class ReviewEmailEnqueuer(Protocol):
    def enqueue_ready_review(
        self,
        change_set: GraphChangeSet,
    ) -> object | None: ...


class DraftFromNoteCallable(Protocol):
    def __call__(
        self,
        *,
        graph_context: dict[str, Any],
        user_hint: str | None,
        draft_mode: str,
        source_artifacts: list[dict[str, Any]],
        image_bytes: bytes | None,
        image_content_type: str | None,
        extra_images: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class DraftFromImageCallable(Protocol):
    def __call__(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        graph_context: dict[str, Any],
        user_hint: str | None,
        draft_mode: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GeneratedDraftProposal:
    """A validated, non-persisted proposal returned to another lifecycle owner."""

    context_packet: dict[str, Any]
    operations: list[GraphChangeOperation]
    summary: str
    uncertain_fields: list[str]
    clarification_requests: list[str]


class GraphDraftGenerationCoordinator(BaseService):
    """Generate and validate proposals without owning review or commit state."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        records: GenerationRecords,
        notes: GenerationNotes,
        authorization: GenerationAuthorization,
        context_builder: GenerationContextBuilder,
        patch_validator: GenerationPatchValidator,
        review_email_outbox: ReviewEmailEnqueuer | None = None,
    ) -> None:
        super().__init__(context)
        self.records = records
        self.notes = notes
        self.authorization = authorization
        self.context_builder = context_builder
        self.patch_validator = patch_validator
        self.review_email_outbox = review_email_outbox

    @property
    def user_reader(self) -> UserExistenceReader:
        """Expose only the lookup needed to attribute generated proposals."""

        return self._context.active_repository()

    def create_graph_draft_from_note(
        self,
        note_id: UUID,
        *,
        draft_client: GraphDraftClient,
        mode: GraphDraftMode = GraphDraftMode.GRAPH_CONTEXT,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        prepared = self.context_builder.prepare_note_sources_for_graph_draft(note_id, mode=mode)
        note = prepared["source_note"]
        self.authorization.require_contributor(note.project_id, actor=actor)
        raw_asset = prepared["primary_raw_asset"]
        cleaned_hint = user_hint.strip() if user_hint else None
        if mode == GraphDraftMode.GRAPH_CONTEXT:
            context_packet = self.context_builder.build_graph_context_packet(
                note,
                source_notes=prepared["source_notes"],
                user_hint=cleaned_hint,
                actor=actor,
            )
        elif mode == GraphDraftMode.IMAGE_ONLY:
            context_packet = self.context_builder.image_only_context_packet(
                note,
                source_notes=prepared["source_notes"],
                user_hint=cleaned_hint,
            )
        else:
            raise ValidationError("Unsupported graph draft mode.")
        change_set = GraphChangeSet(
            change_set_id=uuid4(),
            project_id=note.project_id,
            source_note_id=note.note_id,
            source_note_ids=[item.note_id for item in prepared["source_notes"]],
            source_checksum=raw_asset.checksum if raw_asset is not None else None,
            source_content_type=raw_asset.content_type if raw_asset is not None else None,
            source_filename=raw_asset.filename if raw_asset is not None else None,
            provider=getattr(draft_client, "provider", PROVIDER),
            model=getattr(draft_client, "model", "unknown"),
            prompt_version=PROMPT_VERSION,
            draft_mode=mode,
            context_packet=context_packet,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.user_reader),
        )
        self.records.save_graph_change_set(change_set)
        try:
            graph_patch = self._draft_graph_patch(
                draft_client,
                graph_context=context_packet,
                user_hint=cleaned_hint,
                draft_mode=mode,
                source_artifacts=prepared["source_artifacts"],
                image_bytes=prepared["image_bytes"],
                image_content_type=prepared["image_content_type"],
            )
            self.patch_validator.validate_top_level(graph_patch)
            change_set.operations = self.patch_validator.operations_from_graph_patch(
                change_set,
                graph_patch,
            )
            change_set.summary = str(graph_patch.get("summary") or "")
            change_set.uncertain_fields = string_list(graph_patch.get("uncertain_fields"))
            change_set.clarification_requests = string_list(
                graph_patch.get("clarification_requests")
            )
            change_set.status = GraphChangeSetStatus.READY
            change_set.error_metadata = {}
        except GraphDraftingError as exc:
            change_set.status = GraphChangeSetStatus.FAILED
            change_set.error_metadata = {
                "message": provider_error_message(exc),
            }
        finally:
            change_set.updated_at = utc_now()
            self.records.save_graph_change_set(change_set)
        return change_set

    def create_analysis_graph_draft_from_note(
        self,
        note_id: UUID,
        *,
        draft_client: GraphDraftClient,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        note = self.notes.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        evidence_text = self._analysis_evidence_from_note(note)
        context_packet = self.context_builder.build_graph_context_packet(
            note,
            source_notes=[note],
            user_hint=None,
            actor=actor,
        )
        change_set = GraphChangeSet(
            change_set_id=uuid4(),
            project_id=note.project_id,
            source_note_id=note.note_id,
            source_note_ids=[note.note_id],
            source_checksum=_text_checksum(evidence_text),
            source_content_type="text/markdown",
            source_filename=(
                note.raw_asset.filename
                if note.raw_asset is not None
                else "analysis-evidence-note.md"
            ),
            provider=getattr(draft_client, "provider", PROVIDER),
            model=getattr(draft_client, "model", "unknown"),
            prompt_version=ANALYSIS_PROMPT_VERSION,
            draft_mode=GraphDraftMode.GRAPH_CONTEXT,
            context_packet=context_packet,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.user_reader),
        )
        self.records.save_graph_change_set(change_set)
        try:
            graph_patch = draft_client.draft_from_analysis_evidence(
                evidence_text=evidence_text,
                project_context=context_packet,
            )
            self.patch_validator.validate_top_level(graph_patch)
            change_set.operations = self.patch_validator.operations_from_graph_patch(
                change_set,
                graph_patch,
            )
            change_set.summary = str(graph_patch.get("summary") or "")
            change_set.uncertain_fields = string_list(graph_patch.get("uncertain_fields"))
            change_set.clarification_requests = string_list(
                graph_patch.get("clarification_requests")
            )
            change_set.status = GraphChangeSetStatus.READY
            change_set.error_metadata = {}
        except GraphDraftingError as exc:
            change_set.status = GraphChangeSetStatus.FAILED
            change_set.error_metadata = {
                "message": provider_error_message(exc),
            }
        finally:
            change_set.updated_at = utc_now()
            self.records.save_graph_change_set(change_set)
        return change_set

    def create_batch_graph_draft(
        self,
        notes: list[Note],
        *,
        draft_client: GraphDraftClient,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
        window: tuple[datetime, datetime] | None = None,
        batch_key: str | None = None,
        review_assignee: str | None = None,
        review_assignee_user_id: UUID | None = None,
        max_attempts: int = DEFAULT_BATCH_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = 0.0,
    ) -> GraphChangeSet:
        batch_notes = sorted(notes, key=lambda item: (item.created_at, str(item.note_id)))
        if not batch_notes:
            raise ValidationError("Batch graph drafting requires at least one note.")
        project_ids = {note.project_id for note in batch_notes}
        if len(project_ids) != 1:
            raise ValidationError("Batch graph drafts must be scoped to one project.")
        project_id = next(iter(project_ids))
        self.authorization.require_contributor(project_id, actor=actor)
        non_staged = [note.note_id for note in batch_notes if note.status != NoteStatus.STAGED]
        if non_staged:
            raise ValidationError("Batch graph drafts can only include staged notes.")
        cleaned_hint = user_hint.strip() if user_hint else None
        primary_note = batch_notes[0]
        primary_raw_asset = primary_note.raw_asset
        note_ids = [note.note_id for note in batch_notes]
        if batch_key is None:
            since, until = window if window is not None else (batch_notes[0].created_at, utc_now())
            batch_key = batch_policy.make_batch_key(
                project_id=project_id,
                since=since,
                until=until,
                note_ids=note_ids,
                review_assignee=review_assignee,
                review_assignee_user_id=review_assignee_user_id,
            )
        existing = self.records.list_graph_change_sets(
            draft_mode=GraphDraftMode.GRAPH_BATCH,
            batch_key=batch_key,
        )
        active_existing = [
            change_set
            for change_set in existing
            if change_set.status in batch_policy.ACTIVE_BATCH_CHANGE_SET_STATUSES
        ]
        if active_existing:
            return active_existing[0]
        self._ensure_draft_client_allowed_here(draft_client, actor=actor)
        context_packet = self.context_builder.build_batch_graph_context(
            batch_notes,
            window=window,
            actor=actor,
            batch_note_limit=batch_policy.BATCH_NOTE_LIMIT,
        )
        if cleaned_hint:
            context_packet["user_hint"] = cleaned_hint
        change_set = GraphChangeSet(
            change_set_id=uuid4(),
            project_id=project_id,
            source_note_id=primary_note.note_id,
            source_note_ids=note_ids,
            source_checksum=primary_raw_asset.checksum if primary_raw_asset is not None else None,
            source_content_type=(
                primary_raw_asset.content_type if primary_raw_asset is not None else None
            ),
            source_filename=primary_raw_asset.filename if primary_raw_asset is not None else None,
            batch_key=batch_key,
            batch_window_start=window[0] if window is not None else None,
            batch_window_end=window[1] if window is not None else None,
            provider=getattr(draft_client, "provider", PROVIDER),
            model=getattr(draft_client, "model", "unknown"),
            prompt_version=BATCH_PROMPT_VERSION,
            draft_mode=GraphDraftMode.GRAPH_BATCH,
            context_packet=context_packet,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.user_reader),
            review_assignee=review_assignee,
            review_assignee_user_id=review_assignee_user_id,
        )
        self.records.save_graph_change_set(change_set)

        attempts = max(1, max_attempts)
        last_error: GraphDraftingError | None = None
        last_error_category = "model_error"
        graph_patch: dict[str, Any] | None = None
        operations: list[GraphChangeOperation] | None = None
        attempt_context = context_packet
        for attempt in range(1, attempts + 1):
            try:
                graph_patch = draft_client.draft_from_batch(
                    batch_context=attempt_context,
                    user_hint=cleaned_hint,
                )
            except GraphDraftingError as exc:
                last_error = exc
                last_error_category = "model_error"
                if attempt >= attempts:
                    change_set.status = GraphChangeSetStatus.FAILED
                    change_set.error_metadata = {
                        "category": "model_error",
                        "message": provider_error_message(exc),
                        "attempts": attempt,
                        "input_snapshot": _batch_input_snapshot(context_packet),
                    }
                    change_set.updated_at = utc_now()
                    self.records.save_graph_change_set(change_set)
                    return change_set
                if retry_backoff_seconds > 0:
                    time.sleep(retry_backoff_seconds * attempt)
                continue
            try:
                self.patch_validator.validate_top_level(graph_patch)
                operations = self.patch_validator.operations_from_graph_patch(
                    change_set,
                    graph_patch,
                )
                break
            except GraphDraftingError as exc:
                last_error = exc
                last_error_category = "validation_error"
                if attempt >= attempts:
                    change_set.status = GraphChangeSetStatus.FAILED
                    change_set.error_metadata = {
                        "category": "validation_error",
                        "message": provider_error_message(exc),
                        "attempts": attempt,
                        "input_snapshot": _batch_input_snapshot(context_packet),
                    }
                    change_set.updated_at = utc_now()
                    self.records.save_graph_change_set(change_set)
                    return change_set
                attempt_context = {
                    **context_packet,
                    "generation_retry_feedback": {
                        "attempt": attempt,
                        "error": provider_error_message(exc),
                        "instruction": (
                            "Return a new complete graph patch whose operation payload_json "
                            "objects satisfy the Lab Tracker API request schemas."
                        ),
                    },
                }
                if retry_backoff_seconds > 0:
                    time.sleep(retry_backoff_seconds * attempt)
        else:
            message = (
                provider_error_message(last_error)
                if last_error is not None
                else "Model did not return a patch."
            )
            change_set.status = GraphChangeSetStatus.FAILED
            change_set.error_metadata = {
                "category": last_error_category,
                "message": message,
                "attempts": attempts,
                "input_snapshot": _batch_input_snapshot(context_packet),
            }
            change_set.updated_at = utc_now()
            self.records.save_graph_change_set(change_set)
            return change_set

        assert graph_patch is not None
        assert operations is not None
        change_set.operations = operations
        change_set.summary = str(graph_patch.get("summary") or "")
        change_set.uncertain_fields = string_list(graph_patch.get("uncertain_fields"))
        change_set.clarification_requests = string_list(graph_patch.get("clarification_requests"))
        change_set.status = GraphChangeSetStatus.READY
        change_set.error_metadata = {}
        change_set.updated_at = utc_now()
        with self.application_transaction():
            self.records.save_graph_change_set(change_set)
            if self.review_email_outbox is not None:
                self.review_email_outbox.enqueue_ready_review(change_set)
        return change_set

    @staticmethod
    def _ensure_draft_client_allowed_here(
        draft_client: GraphDraftClient,
        *,
        actor: AuthContext | None,
    ) -> None:
        if not getattr(draft_client, "requires_background_worker", False):
            return
        if actor is not None and actor.is_system:
            return
        raise GraphDraftingError(
            "The configured graph draft client only runs inside the background worker."
        )

    def propose_note_revision(
        self,
        change_set: GraphChangeSet,
        *,
        user_hint: str,
        draft_client: GraphDraftClient,
        actor: AuthContext | None,
        extra_images: list[dict[str, Any]],
    ) -> GeneratedDraftProposal:
        """Generate a complete replacement proposal without mutating persistence."""

        mode = change_set.draft_mode
        prior_context = change_set.context_packet
        captured_source_artifacts = (
            [
                dict(item)
                for item in prior_context.get("source_artifacts", [])
                if isinstance(item, dict)
            ]
            if isinstance(prior_context, dict)
            else []
        )
        prepared = self.context_builder.prepare_note_sources_for_graph_draft(
            change_set.source_note_id,
            mode=mode,
            source_note_ids=list(change_set.source_note_ids or [change_set.source_note_id]),
        )
        if not captured_source_artifacts:
            captured_source_artifacts = prepared["source_artifacts"]
        note = prepared["source_note"]
        if mode == GraphDraftMode.GRAPH_CONTEXT:
            context_packet = self.context_builder.build_graph_context_packet(
                note,
                source_notes=prepared["source_notes"],
                user_hint=user_hint,
                actor=actor,
            )
        elif mode == GraphDraftMode.IMAGE_ONLY:
            context_packet = self.context_builder.image_only_context_packet(
                note,
                source_notes=prepared["source_notes"],
                user_hint=user_hint,
            )
        else:
            raise ValidationError("Unsupported graph draft mode.")
        context_packet["source_artifacts"] = captured_source_artifacts
        graph_patch = self._draft_graph_patch(
            draft_client,
            graph_context=context_packet,
            user_hint=user_hint,
            draft_mode=mode,
            source_artifacts=captured_source_artifacts,
            image_bytes=prepared["image_bytes"],
            image_content_type=prepared["image_content_type"],
            extra_images=extra_images,
        )
        self.patch_validator.validate_top_level(graph_patch)
        operations = self.patch_validator.operations_from_graph_patch(change_set, graph_patch)
        return GeneratedDraftProposal(
            context_packet=context_packet,
            operations=operations,
            summary=str(graph_patch.get("summary") or ""),
            uncertain_fields=string_list(graph_patch.get("uncertain_fields")),
            clarification_requests=string_list(graph_patch.get("clarification_requests")),
        )

    def _analysis_evidence_from_note(self, note: Note) -> str:
        parts: list[str] = []
        if note.raw_content.strip():
            parts.append("## Note raw content\n\n" + note.raw_content.strip())
        if note.transcribed_text and note.transcribed_text.strip():
            parts.append("## Note transcribed text\n\n" + note.transcribed_text.strip())
        if note.raw_asset is not None:
            raw_asset, content = self.notes.download_note_raw(note.note_id)
            parts.append(
                "\n".join(
                    [
                        "## Raw asset metadata",
                        "",
                        f"- filename: {raw_asset.filename}",
                        f"- content_type: {raw_asset.content_type}",
                        f"- checksum: {raw_asset.checksum}",
                        f"- size_bytes: {raw_asset.size_bytes}",
                    ]
                )
            )
            if _is_text_asset(raw_asset.content_type):
                try:
                    raw_text = content.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    raise ValidationError(
                        "Analysis graph drafting requires UTF-8 text evidence."
                    ) from exc
                if raw_text:
                    parts.append("## Raw asset text\n\n" + raw_text)
        evidence_text = "\n\n".join(parts).strip()
        if not evidence_text:
            raise ValidationError("Analysis graph drafting requires text evidence on the note.")
        return evidence_text

    def build_graph_context_for_note(
        self,
        note_id: UUID,
        *,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
    ) -> dict[str, Any]:
        prepared = self.context_builder.prepare_note_sources_for_graph_draft(
            note_id,
            mode=GraphDraftMode.GRAPH_CONTEXT,
        )
        return self.context_builder.build_graph_context_packet(
            prepared["source_note"],
            source_notes=prepared["source_notes"],
            user_hint=user_hint.strip() if user_hint else None,
            actor=actor,
        )

    def build_batch_graph_context(
        self,
        notes: list[Note],
        *,
        window: tuple[datetime, datetime] | None = None,
        actor: AuthContext | None = None,
    ) -> dict[str, Any]:
        return self.context_builder.build_batch_graph_context(
            notes,
            window=window,
            actor=actor,
            batch_note_limit=batch_policy.BATCH_NOTE_LIMIT,
        )

    @staticmethod
    def _draft_graph_patch(
        draft_client: GraphDraftClient,
        *,
        graph_context: dict[str, Any],
        user_hint: str | None,
        draft_mode: GraphDraftMode,
        source_artifacts: list[dict[str, Any]],
        image_bytes: bytes | None,
        image_content_type: str | None,
        extra_images: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        draft_from_note: DraftFromNoteCallable | None = getattr(
            draft_client,
            "draft_from_note",
            None,
        )
        if callable(draft_from_note):
            return draft_from_note(
                graph_context=graph_context,
                user_hint=user_hint,
                draft_mode=draft_mode.value,
                source_artifacts=source_artifacts,
                image_bytes=image_bytes,
                image_content_type=image_content_type,
                extra_images=extra_images or [],
            )
        draft_from_image: DraftFromImageCallable | None = getattr(
            draft_client,
            "draft_from_image",
            None,
        )
        if callable(draft_from_image) and image_bytes and image_content_type:
            return draft_from_image(
                image_bytes=image_bytes,
                content_type=image_content_type,
                graph_context=graph_context,
                user_hint=user_hint,
                draft_mode=draft_mode.value,
            )
        raise GraphDraftingError("Configured draft client does not support this note source.")


def _text_checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_text_asset(content_type: str) -> bool:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    return normalized.startswith("text/") or normalized in {
        "application/json",
        "application/ld+json",
        "application/markdown",
        "application/x-ndjson",
        "application/xml",
    }


def _batch_input_snapshot(context_packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_window": context_packet.get("batch_window"),
        "context_summary": context_packet.get("context_summary"),
        "source_note_ids": [
            item.get("id")
            for item in context_packet.get("batch_notes", [])
            if isinstance(item, dict)
        ],
    }
