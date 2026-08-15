"""Graph-draft generation coordinator."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.config import Settings
from lab_tracker.errors import ValidationError
from lab_tracker.graph_drafting import (
    ANALYSIS_PROMPT_VERSION,
    BATCH_PROMPT_VERSION,
    PROMPT_VERSION,
    PROVIDER,
    GraphDraftClient,
    GraphDraftingError,
)
from lab_tracker.member_onboarding import is_member_checkpoint
from lab_tracker.models import (
    GraphChangeOperation,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    Note,
    NoteStatus,
    utc_now,
)
from lab_tracker.provider_error_redaction import provider_error_message
from lab_tracker.services import graph_draft_batch_policy as batch_policy
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.graph_draft_generation_ports import (
    DraftFromImageCallable,
    DraftFromNoteCallable,
    ReviewEmailEnqueuer,
)
from lab_tracker.services.graph_draft_generation_ports import (
    GeneratedDraftProposal as GeneratedDraftProposal,
)
from lab_tracker.services.graph_draft_generation_ports import (
    GenerationAuthorization as GenerationAuthorization,
)
from lab_tracker.services.graph_draft_generation_ports import (
    GenerationClaim as GenerationClaim,
)
from lab_tracker.services.graph_draft_generation_ports import (
    GenerationContextBuilder as GenerationContextBuilder,
)
from lab_tracker.services.graph_draft_generation_ports import (
    GenerationNotes as GenerationNotes,
)
from lab_tracker.services.graph_draft_generation_ports import (
    GenerationPatchValidator as GenerationPatchValidator,
)
from lab_tracker.services.graph_draft_generation_ports import (
    GenerationRecords as GenerationRecords,
)
from lab_tracker.services.graph_draft_validation import string_list
from lab_tracker.services.shared import UserExistenceReader, actor_user_fk, actor_user_id

DEFAULT_BATCH_RETRY_ATTEMPTS = 3
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 60.0
GENERATION_LEASE_MARGIN_SECONDS = 30


class _GenerationOwnershipLost(RuntimeError):
    """Internal control flow: never persist a stale provider result."""


def provider_generation_lease_seconds(draft_client: GraphDraftClient) -> int:
    """Return one provider attempt's timeout plus a completion margin."""

    raw_timeout = getattr(
        draft_client,
        "timeout_seconds",
        DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    )
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_PROVIDER_TIMEOUT_SECONDS
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        timeout_seconds = DEFAULT_PROVIDER_TIMEOUT_SECONDS
    return max(1, math.ceil(timeout_seconds)) + GENERATION_LEASE_MARGIN_SECONDS


def configured_provider_generation_lease_seconds(settings: Settings) -> int:
    """Resolve the active provider timeout even before client construction."""

    provider = (settings.graph_draft_provider or "openai").strip().lower()
    if provider in {"anthropic", "claude"}:
        timeout_seconds = settings.anthropic_timeout_seconds
    elif provider in {"google", "gemini"}:
        timeout_seconds = settings.google_timeout_seconds
    else:
        # OpenAI and the agentic OpenAI wrapper share the OpenAI transport.
        timeout_seconds = settings.openai_timeout_seconds
    return max(1, math.ceil(float(timeout_seconds))) + GENERATION_LEASE_MARGIN_SECONDS


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

    def claim_generation(
        self,
        candidate: GraphChangeSet,
        *,
        draft_client: GraphDraftClient,
        now: datetime | None = None,
    ) -> GenerationClaim:
        """Create/reclaim an attempt and return its unforgeable owner token."""

        claimed_at = now or utc_now()
        claim_token = uuid4()
        lease_until = claimed_at + timedelta(
            seconds=provider_generation_lease_seconds(draft_client)
        )
        change_set, acquired = self.records.claim_graph_change_set_generation(
            candidate,
            claimed_at=claimed_at,
            lease_until=lease_until,
            claim_token=claim_token,
        )
        return GenerationClaim(
            change_set=change_set,
            claim_token=claim_token,
            acquired=acquired,
        )

    def renew_generation_claim(
        self,
        change_set_id: UUID,
        claim_token: UUID,
        *,
        draft_client: GraphDraftClient,
        now: datetime | None = None,
    ) -> GraphChangeSet | None:
        renewed_at = now or utc_now()
        lease_until = renewed_at + timedelta(
            seconds=provider_generation_lease_seconds(draft_client)
        )
        return self.records.renew_graph_change_set_generation(
            change_set_id,
            claim_token,
            renewed_at=renewed_at,
            lease_until=lease_until,
        )

    def complete_generation_claim(
        self,
        change_set: GraphChangeSet,
        *,
        claim_token: UUID,
        now: datetime | None = None,
    ) -> GraphChangeSet | None:
        change_set.status = GraphChangeSetStatus.READY
        completed_at = now or utc_now()
        change_set.updated_at = completed_at
        return self.records.complete_graph_change_set_generation(
            change_set,
            claim_token,
            completed_at=completed_at,
        )

    def fail_generation_claim(
        self,
        change_set: GraphChangeSet,
        *,
        claim_token: UUID,
        now: datetime | None = None,
    ) -> GraphChangeSet | None:
        change_set.status = GraphChangeSetStatus.FAILED
        failed_at = now or utc_now()
        change_set.updated_at = failed_at
        return self.records.fail_graph_change_set_generation(
            change_set,
            claim_token,
            failed_at=failed_at,
        )

    def create_graph_draft_from_note(
        self,
        note_id: UUID,
        *,
        draft_client: GraphDraftClient,
        mode: GraphDraftMode = GraphDraftMode.GRAPH_CONTEXT,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
        max_attempts: int = DEFAULT_BATCH_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = 0.0,
    ) -> GraphChangeSet:
        prepared = self.context_builder.prepare_note_sources_for_graph_draft(note_id, mode=mode)
        note = prepared["source_note"]
        self.authorization.require_contributor(note.project_id, actor=actor)
        if any(is_member_checkpoint(item) for item in prepared["source_notes"]):
            raise ValidationError(
                "Member onboarding checkpoints can be drafted only through the "
                "dedicated acknowledged alignment workflow."
            )
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
            batch_key=_note_generation_key(
                note=note,
                source_notes=prepared["source_notes"],
                mode=mode,
                prompt_version=PROMPT_VERSION,
                user_hint=cleaned_hint,
                evidence_checksum=(raw_asset.checksum if raw_asset is not None else None),
            ),
            context_packet=context_packet,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.user_reader),
        )
        claim = self.claim_generation(change_set, draft_client=draft_client)
        if not claim.acquired:
            return claim.change_set
        change_set = claim.change_set
        try:
            generated = self._draft_validated_patch_with_retries(
                change_set=change_set,
                context_packet=context_packet,
                draft=lambda attempt_context: self._draft_graph_patch(
                    draft_client,
                    graph_context=attempt_context,
                    user_hint=cleaned_hint,
                    draft_mode=mode,
                    source_artifacts=prepared["source_artifacts"],
                    image_bytes=prepared["image_bytes"],
                    image_content_type=prepared["image_content_type"],
                ),
                max_attempts=max_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                before_attempt=lambda _attempt: self.renew_generation_claim(
                    change_set.change_set_id,
                    claim.claim_token,
                    draft_client=draft_client,
                )
                is not None,
            )
            if generated is None:
                return self._finish_failed_or_current(change_set, claim.claim_token)
            graph_patch, operations = generated
            change_set.operations = operations
            change_set.summary = str(graph_patch.get("summary") or "")
            change_set.uncertain_fields = string_list(graph_patch.get("uncertain_fields"))
            change_set.clarification_requests = string_list(
                graph_patch.get("clarification_requests")
            )
            change_set.error_metadata = {}
            completed = self.complete_generation_claim(
                change_set,
                claim_token=claim.claim_token,
            )
            return completed or self.records.get_graph_change_set(
                change_set.change_set_id
            )
        except _GenerationOwnershipLost:
            return self.records.get_graph_change_set(change_set.change_set_id)
        except Exception as exc:
            change_set.error_metadata = {
                "category": "runner_error",
                "message": provider_error_message(exc),
            }
            self._finish_failed_or_current(change_set, claim.claim_token)
            raise

    def create_analysis_graph_draft_from_note(
        self,
        note_id: UUID,
        *,
        draft_client: GraphDraftClient,
        actor: AuthContext | None = None,
        max_attempts: int = DEFAULT_BATCH_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = 0.0,
    ) -> GraphChangeSet:
        note = self.notes.get_note(note_id)
        self.authorization.require_contributor(note.project_id, actor=actor)
        if is_member_checkpoint(note):
            raise ValidationError(
                "Member onboarding checkpoints cannot use analysis drafting."
            )
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
            batch_key=_note_generation_key(
                note=note,
                source_notes=[note],
                mode=GraphDraftMode.GRAPH_CONTEXT,
                prompt_version=ANALYSIS_PROMPT_VERSION,
                user_hint=None,
                evidence_checksum=_text_checksum(evidence_text),
                kind="analysis",
            ),
            context_packet=context_packet,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.user_reader),
        )
        claim = self.claim_generation(change_set, draft_client=draft_client)
        if not claim.acquired:
            return claim.change_set
        change_set = claim.change_set
        try:
            generated = self._draft_validated_patch_with_retries(
                change_set=change_set,
                context_packet=context_packet,
                draft=lambda attempt_context: draft_client.draft_from_analysis_evidence(
                    evidence_text=evidence_text,
                    project_context=attempt_context,
                ),
                max_attempts=max_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                before_attempt=lambda _attempt: self.renew_generation_claim(
                    change_set.change_set_id,
                    claim.claim_token,
                    draft_client=draft_client,
                )
                is not None,
            )
            if generated is None:
                return self._finish_failed_or_current(change_set, claim.claim_token)
            graph_patch, operations = generated
            change_set.operations = operations
            change_set.summary = str(graph_patch.get("summary") or "")
            change_set.uncertain_fields = string_list(graph_patch.get("uncertain_fields"))
            change_set.clarification_requests = string_list(
                graph_patch.get("clarification_requests")
            )
            change_set.error_metadata = {}
            completed = self.complete_generation_claim(
                change_set,
                claim_token=claim.claim_token,
            )
            return completed or self.records.get_graph_change_set(
                change_set.change_set_id
            )
        except _GenerationOwnershipLost:
            return self.records.get_graph_change_set(change_set.change_set_id)
        except Exception as exc:
            change_set.error_metadata = {
                "category": "runner_error",
                "message": provider_error_message(exc),
            }
            self._finish_failed_or_current(change_set, claim.claim_token)
            raise

    def _draft_validated_patch_with_retries(
        self,
        *,
        change_set: GraphChangeSet,
        context_packet: dict[str, Any],
        draft: Callable[[dict[str, Any]], dict[str, Any]],
        max_attempts: int,
        retry_backoff_seconds: float,
        before_attempt: Callable[[int], bool] | None = None,
    ) -> tuple[dict[str, Any], list[GraphChangeOperation]] | None:
        """Generate a valid patch with bounded, trusted schema feedback."""

        attempts = max(1, max_attempts)
        attempt_context = context_packet
        last_error: GraphDraftingError | None = None
        last_error_category = "model_error"
        for attempt in range(1, attempts + 1):
            if before_attempt is not None and not before_attempt(attempt):
                raise _GenerationOwnershipLost
            try:
                graph_patch = draft(attempt_context)
            except GraphDraftingError as exc:
                last_error = exc
                last_error_category = "model_error"
            else:
                try:
                    self.patch_validator.validate_top_level(graph_patch)
                    operations = self.patch_validator.operations_from_graph_patch(
                        change_set,
                        graph_patch,
                    )
                except GraphDraftingError as exc:
                    last_error = exc
                    last_error_category = "validation_error"
                    attempt_context = {
                        **context_packet,
                        "generation_retry_feedback": {
                            "attempt": attempt,
                            "error": provider_error_message(exc),
                            "instruction": (
                                "Return a new complete graph patch whose operation "
                                "payload_json objects satisfy the trusted Lab Tracker "
                                "API payload contract."
                            ),
                        },
                    }
                else:
                    return graph_patch, operations
            if attempt < attempts and retry_backoff_seconds > 0:
                time.sleep(retry_backoff_seconds * attempt)

        change_set.error_metadata = {
            "category": last_error_category,
            "message": (
                provider_error_message(last_error)
                if last_error is not None
                else "Model did not return a patch."
            ),
            "attempts": attempts,
        }
        return None

    def _finish_failed_or_current(
        self,
        change_set: GraphChangeSet,
        claim_token: UUID,
    ) -> GraphChangeSet:
        failed = self.fail_generation_claim(
            change_set,
            claim_token=claim_token,
        )
        return failed or self.records.get_graph_change_set(change_set.change_set_id)

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
        before_attempt: Callable[[int], bool] | None = None,
    ) -> GraphChangeSet:
        batch_notes = sorted(notes, key=lambda item: (item.created_at, str(item.note_id)))
        if not batch_notes:
            raise ValidationError("Batch graph drafting requires at least one note.")
        project_ids = {note.project_id for note in batch_notes}
        if len(project_ids) != 1:
            raise ValidationError("Batch graph drafts must be scoped to one project.")
        project_id = next(iter(project_ids))
        self.authorization.require_contributor(project_id, actor=actor)
        if any(is_member_checkpoint(note) for note in batch_notes):
            raise ValidationError(
                "Member onboarding checkpoints cannot be included in generic batch drafting."
            )
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
        claim = self.claim_generation(change_set, draft_client=draft_client)
        if not claim.acquired:
            return claim.change_set
        change_set = claim.change_set

        def renew_all(attempt: int) -> bool:
            if before_attempt is not None and not before_attempt(attempt):
                return False
            return (
                self.renew_generation_claim(
                    change_set.change_set_id,
                    claim.claim_token,
                    draft_client=draft_client,
                )
                is not None
            )

        try:
            generated = self._draft_validated_patch_with_retries(
                change_set=change_set,
                context_packet=context_packet,
                draft=lambda attempt_context: draft_client.draft_from_batch(
                    batch_context=attempt_context,
                    user_hint=cleaned_hint,
                ),
                max_attempts=max_attempts,
                retry_backoff_seconds=retry_backoff_seconds,
                before_attempt=renew_all,
            )
            if generated is None:
                change_set.error_metadata["input_snapshot"] = _batch_input_snapshot(
                    context_packet
                )
                return self._finish_failed_or_current(change_set, claim.claim_token)
            graph_patch, operations = generated
            change_set.operations = operations
            change_set.summary = str(graph_patch.get("summary") or "")
            change_set.uncertain_fields = string_list(
                graph_patch.get("uncertain_fields")
            )
            change_set.clarification_requests = string_list(
                graph_patch.get("clarification_requests")
            )
            change_set.error_metadata = {}
            with self.application_transaction():
                completed = self.complete_generation_claim(
                    change_set,
                    claim_token=claim.claim_token,
                )
                if completed is not None and self.review_email_outbox is not None:
                    self.review_email_outbox.enqueue_ready_review(completed)
            return completed or self.records.get_graph_change_set(
                change_set.change_set_id
            )
        except _GenerationOwnershipLost:
            return self.records.get_graph_change_set(change_set.change_set_id)
        except Exception as exc:
            change_set.error_metadata = {
                "category": "runner_error",
                "message": provider_error_message(exc),
                "input_snapshot": _batch_input_snapshot(context_packet),
            }
            self._finish_failed_or_current(change_set, claim.claim_token)
            raise

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


def _note_generation_key(
    *,
    note: Note,
    source_notes: list[Note],
    mode: GraphDraftMode,
    prompt_version: str,
    user_hint: str | None,
    evidence_checksum: str | None,
    kind: str = "note",
) -> str:
    """Identify one exact note-source generation request across retries."""

    payload = {
        "version": "v1",
        "kind": kind,
        "project_id": str(note.project_id),
        "note_id": str(note.note_id),
        "source_versions": [
            {"note_id": str(item.note_id), "updated_at": item.updated_at.isoformat()}
            for item in source_notes
        ],
        "mode": mode.value,
        "prompt_version": prompt_version,
        "user_hint": user_hint,
        "evidence_checksum": evidence_checksum,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"generation:{digest[:48]}"


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
