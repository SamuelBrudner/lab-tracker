"""Ongoing-project member onboarding over retained-v1 notes and graph drafts."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ConflictError, NotFoundError, ValidationError
from lab_tracker.graph_drafting import PROVIDER, GraphDraftClient, GraphDraftingError
from lab_tracker.member_onboarding import (
    ALIGNMENT_MODE_KEY,
    ALIGNMENT_PAYLOAD_HASH_KEY,
    ALIGNMENT_RESOLUTIONS_KEY,
    ALIGNMENT_RESOLVED_AT_KEY,
    CHECKPOINT_AS_OF_KEY,
    CHECKPOINT_CONTENT_LIMIT,
    CHECKPOINT_COVERAGE,
    CHECKPOINT_COVERAGE_KEY,
    CHECKPOINT_CURRENT_OUTPUT_KEY,
    CHECKPOINT_INPUT_HASH_KEY,
    CHECKPOINT_LIVE_QUESTIONS_KEY,
    CHECKPOINT_NEXT_MOVE_KEY,
    CHECKPOINT_PAYLOAD_HASH_KEY,
    CHECKPOINT_PROMPT_VERSION,
    CHECKPOINT_RECENT_CONTEXT_KEY,
    CHECKPOINT_ROLE,
    CHECKPOINT_ROLE_KEY,
    CHECKPOINT_SCHEMA_VERSION,
    CHECKPOINT_SCHEMA_VERSION_KEY,
    CHECKPOINT_SOURCE_HASH_KEY,
    COMPLETED_AT_KEY,
    FIRST_CAPTURE_NOTE_ID_KEY,
    SCHEDULED_DRAFT_EXCLUDE,
    SCHEDULED_DRAFT_POLICY_KEY,
    checkpoint_client_capture_id,
    is_member_checkpoint,
    validate_member_alignment_operations,
)
from lab_tracker.models import (
    EntityOrigin,
    EntityRef,
    EntityType,
    GraphChangeOp,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    GraphDraftPurpose,
    Note,
    NoteStatus,
    ProjectMembershipRole,
    Question,
    QuestionStatus,
    QuestionType,
    UsageEventResourceType,
    UsageEventVerb,
    utc_now,
)
from lab_tracker.provider_error_redaction import provider_error_message
from lab_tracker.schemas import (
    MemberOnboardingAlignment,
    MemberOnboardingCapabilities,
    MemberOnboardingCheckpointRequest,
    MemberOnboardingGuidedFields,
    MemberOnboardingManualAlignmentRequest,
    MemberOnboardingMapItem,
    MemberOnboardingOwnerQueueItem,
    MemberOnboardingQuestionResolution,
    MemberOnboardingRead,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.graph_draft_generation import (
    DEFAULT_BATCH_RETRY_ATTEMPTS,
    GraphDraftGenerationCoordinator,
)
from lab_tracker.services.graph_draft_records import GraphDraftRecords
from lab_tracker.services.graph_draft_validation import GraphPatchValidator, string_list
from lab_tracker.services.note_service import NoteService
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.shared import actor_user_fk, actor_user_id

_WORD_RE = re.compile(r"[a-z0-9]{3,}")


@dataclass(frozen=True)
class MemberOnboardingCommandResult:
    onboarding: MemberOnboardingRead
    created: bool = False
    in_progress: bool = False
    retry_after_seconds: int | None = None


class MemberOnboardingService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        questions: QuestionService,
        notes: NoteService,
        authorization: ProjectAuthorizationPolicy,
        graph_records: GraphDraftRecords,
        graph_generation: GraphDraftGenerationCoordinator,
        graph_validator: GraphPatchValidator,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.questions = questions
        self.notes = notes
        self.authorization = authorization
        self.graph_records = graph_records
        self.graph_generation = graph_generation
        self.graph_validator = graph_validator

    def get_member_onboarding(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> MemberOnboardingRead:
        self.authorization.require_read(project_id, actor=actor)
        self.projects.get_project(project_id)
        role = self.authorization.membership_role(project_id, actor)
        if role is None:  # authorization above is deliberately authoritative
            raise NotFoundError("Project does not exist.")
        capabilities = self._capabilities(project_id, role=role, actor=actor)
        checkpoint = self._checkpoint_for_actor(project_id, actor=actor)
        if checkpoint is None:
            return MemberOnboardingRead(
                project_id=project_id,
                role=role,
                capabilities=capabilities,
                state="not_started",
            )
        fields = self._guided_fields(checkpoint)
        draft = self._alignment_draft(checkpoint)
        alignment = self._alignment(checkpoint, fields=fields, draft=draft)
        map_items = self._map_items(checkpoint, fields=fields, alignment=alignment)
        first_capture = self._first_capture(checkpoint)
        alignment_resolved = self._alignment_resolved(checkpoint, draft)
        member_complete = bool(alignment_resolved and first_capture is not None)
        owner_commit_pending = bool(
            draft is not None
            and draft.status == GraphChangeSetStatus.SUBMITTED
            and any(item.status == GraphChangeOperationStatus.ACCEPTED for item in draft.operations)
        )
        state = self._state(
            checkpoint=checkpoint,
            draft=draft,
            alignment_resolved=alignment_resolved,
            member_complete=member_complete,
        )
        return MemberOnboardingRead(
            project_id=project_id,
            role=role,
            capabilities=capabilities,
            state=state,
            checkpoint=checkpoint,
            guided_fields=fields,
            alignment=alignment,
            map_items=map_items,
            brief_markdown=self._brief_markdown(
                checkpoint,
                fields=fields,
                map_items=map_items,
                draft=draft,
                author_label=self._checkpoint_author_label(checkpoint),
            ),
            first_capture=first_capture,
            member_complete=member_complete,
            owner_commit_pending=owner_commit_pending,
        )

    def put_checkpoint(
        self,
        project_id: UUID,
        payload: MemberOnboardingCheckpointRequest,
        *,
        actor: AuthContext | None,
    ) -> MemberOnboardingCommandResult:
        self._require_interactive_contributor(project_id, actor=actor)
        existing = self._checkpoint_for_actor(project_id, actor=actor)
        effective_as_of = payload.as_of
        if effective_as_of is None and existing is not None:
            effective_as_of = _metadata_datetime(existing.metadata.get(CHECKPOINT_AS_OF_KEY))
        effective_as_of = effective_as_of or utc_now()
        normalized, content, payload_hash = self._canonical_checkpoint(
            payload,
            as_of=effective_as_of,
        )
        input_hash = self._checkpoint_input_hash(payload)
        if existing is not None:
            if not self._checkpoint_replays(
                existing,
                payload_hash=payload_hash,
                input_hash=input_hash,
            ):
                raise ConflictError(
                    "This member already has a different immutable tracking checkpoint "
                    "for the project."
                )
            return MemberOnboardingCommandResult(
                onboarding=self.get_member_onboarding(project_id, actor=actor),
                created=False,
            )
        metadata = {
            CHECKPOINT_ROLE_KEY: CHECKPOINT_ROLE,
            CHECKPOINT_SCHEMA_VERSION_KEY: CHECKPOINT_SCHEMA_VERSION,
            CHECKPOINT_AS_OF_KEY: normalized["as_of"],
            CHECKPOINT_COVERAGE_KEY: CHECKPOINT_COVERAGE,
            CHECKPOINT_PAYLOAD_HASH_KEY: payload_hash,
            CHECKPOINT_INPUT_HASH_KEY: input_hash,
            CHECKPOINT_CURRENT_OUTPUT_KEY: normalized["current_output_or_decision"],
            CHECKPOINT_LIVE_QUESTIONS_KEY: _canonical_json(normalized["live_questions"]),
            CHECKPOINT_RECENT_CONTEXT_KEY: normalized["strongest_recent_context"],
            CHECKPOINT_NEXT_MOVE_KEY: normalized["next_move"],
            SCHEDULED_DRAFT_POLICY_KEY: SCHEDULED_DRAFT_EXCLUDE,
        }
        if normalized["source_text"]:
            metadata[CHECKPOINT_SOURCE_HASH_KEY] = _sha256(normalized["source_text"])
        try:
            result = self.notes.create_note_result(
                project_id=project_id,
                raw_content=content,
                targets=[EntityRef(entity_type=EntityType.PROJECT, entity_id=project_id)],
                metadata=metadata,
                client_capture_id=checkpoint_client_capture_id(actor.user_id),
                status=NoteStatus.STAGED,
                actor=actor,
                allow_member_onboarding_reserved=True,
            )
        except ConflictError:
            # Concurrent first PUTs without a caller-provided ``as_of`` can
            # choose different server timestamps. Rejoin only an exact match
            # of the caller-controlled input; changed payloads remain 409s.
            winner = self._checkpoint_for_actor(project_id, actor=actor)
            if winner is None or not self._checkpoint_replays(
                winner,
                payload_hash=payload_hash,
                input_hash=input_hash,
            ):
                raise
            return MemberOnboardingCommandResult(
                onboarding=self.get_member_onboarding(project_id, actor=actor),
                created=False,
            )
        if result.created:
            self.record_usage_event(
                verb=UsageEventVerb.CREATE,
                resource_type=UsageEventResourceType.MEMBER_ONBOARDING,
                resource_id=result.entity.note_id,
                project_id=project_id,
                actor=actor,
            )
        return MemberOnboardingCommandResult(
            onboarding=self.get_member_onboarding(project_id, actor=actor),
            created=result.created,
        )

    def put_manual_alignment(
        self,
        project_id: UUID,
        payload: MemberOnboardingManualAlignmentRequest,
        *,
        actor: AuthContext | None,
    ) -> MemberOnboardingRead:
        self._require_interactive_contributor(project_id, actor=actor)
        checkpoint = self._require_checkpoint(project_id, actor=actor)
        fields = self._guided_fields(checkpoint)
        if len(payload.resolutions) != len(fields.live_questions) or {
            item.question_index for item in payload.resolutions
        } != set(range(len(fields.live_questions))):
            raise ValidationError(
                "Manual alignment requires exactly one resolution for every live question."
            )
        existing_hash = checkpoint.metadata.get(ALIGNMENT_PAYLOAD_HASH_KEY)
        normalized_input = [
            {
                "question_index": item.question_index,
                "action": item.action,
                "existing_question_id": (
                    str(item.existing_question_id)
                    if item.existing_question_id is not None
                    else None
                ),
            }
            for item in sorted(payload.resolutions, key=lambda item: item.question_index)
        ]
        payload_hash = _sha256(_canonical_json(normalized_input))
        if checkpoint.metadata.get(ALIGNMENT_MODE_KEY):
            if (
                checkpoint.metadata.get(ALIGNMENT_MODE_KEY) == "manual"
                and existing_hash == payload_hash
            ):
                return self.get_member_onboarding(project_id, actor=actor)
            raise ConflictError("The checkpoint alignment has already been finalized.")
        draft = self._alignment_draft(checkpoint)
        if draft is not None and draft.status != GraphChangeSetStatus.FAILED:
            raise ConflictError("An AI alignment already exists for this checkpoint.")

        resolved_at = utc_now()
        expected_updated_at = checkpoint.updated_at
        resolution_records: list[dict[str, Any]] = []
        added_targets: list[EntityRef] = []
        with self.application_transaction():
            # Use the same project-scoped question lock as delete, refactor,
            # terminal transitions, and AI commit. Re-read link targets below
            # after waiting so an accepted onboarding edge cannot race into a
            # dangling or terminal question.
            self.repository.lock_project_question_dag(project_id)
            checkpoint = self.notes.get_note(checkpoint.note_id)
            if checkpoint.metadata.get(ALIGNMENT_MODE_KEY):
                if (
                    checkpoint.metadata.get(ALIGNMENT_MODE_KEY) == "manual"
                    and checkpoint.metadata.get(ALIGNMENT_PAYLOAD_HASH_KEY) == payload_hash
                ):
                    return self.get_member_onboarding(project_id, actor=actor)
                raise ConflictError("The checkpoint alignment has already been finalized.")
            draft = self._alignment_draft(checkpoint)
            if draft is not None and draft.status != GraphChangeSetStatus.FAILED:
                raise ConflictError("An AI alignment already exists for this checkpoint.")
            expected_updated_at = checkpoint.updated_at
            for item in sorted(payload.resolutions, key=lambda value: value.question_index):
                question: Question | None = None
                if item.action == "link_existing":
                    assert item.existing_question_id is not None
                    question = self.questions.get_question_for_read(
                        item.existing_question_id,
                        actor=actor,
                    )
                    if question.project_id != project_id:
                        raise ValidationError(
                            "Manual alignment questions must belong to the checkpoint project."
                        )
                    if question.status not in {
                        QuestionStatus.ACTIVE,
                        QuestionStatus.STAGED,
                    }:
                        raise ValidationError(
                            "Manual alignment can link only active or staged questions."
                        )
                elif item.action == "create_staged":
                    question = self.questions.create_question_result(
                        project_id=project_id,
                        text=fields.live_questions[item.question_index],
                        question_type=QuestionType.OTHER,
                        status=QuestionStatus.STAGED,
                        client_capture_id=(
                            f"member-question:v1:{checkpoint.note_id}:{item.question_index}"
                        ),
                        actor=actor,
                        allow_member_onboarding_reserved=True,
                    ).entity
                if question is not None:
                    added_targets.append(
                        EntityRef(
                            entity_type=EntityType.QUESTION,
                            entity_id=question.question_id,
                        )
                    )
                resolution_records.append(
                    {
                        "question_index": item.question_index,
                        "action": item.action,
                        "question_id": (
                            str(question.question_id) if question is not None else None
                        ),
                    }
                )
            checkpoint.metadata = {
                **checkpoint.metadata,
                ALIGNMENT_MODE_KEY: "manual",
                ALIGNMENT_PAYLOAD_HASH_KEY: payload_hash,
                ALIGNMENT_RESOLVED_AT_KEY: resolved_at.isoformat(),
                ALIGNMENT_RESOLUTIONS_KEY: _canonical_json(resolution_records),
            }
            checkpoint.targets = _merge_targets(checkpoint.targets, added_targets)
            checkpoint.updated_at = resolved_at
            finalized = self.repository.notes.try_finalize_member_onboarding_alignment(
                checkpoint,
                expected_updated_at=expected_updated_at,
            )
            if finalized is None:
                current = self.notes.get_note(checkpoint.note_id)
                if (
                    current.metadata.get(ALIGNMENT_MODE_KEY) == "manual"
                    and current.metadata.get(ALIGNMENT_PAYLOAD_HASH_KEY) == payload_hash
                ):
                    return self.get_member_onboarding(project_id, actor=actor)
                raise ConflictError("The checkpoint alignment has already been finalized.")
            checkpoint = finalized
            self.record_usage_event(
                verb=UsageEventVerb.REVIEW,
                resource_type=UsageEventResourceType.MEMBER_ONBOARDING,
                resource_id=checkpoint.note_id,
                project_id=project_id,
                actor=actor,
            )
            self._mark_completed_if_ready(checkpoint, actor=actor)
            self.notes.schedule_member_onboarding_completion_reconciliation(
                checkpoint.note_id,
                actor=actor,
            )
        return self.get_member_onboarding(project_id, actor=actor)

    def start_ai_alignment(
        self,
        project_id: UUID,
        *,
        external_provider_acknowledged: bool,
        draft_client: GraphDraftClient,
        actor: AuthContext | None,
    ) -> MemberOnboardingCommandResult:
        self._require_interactive_contributor(project_id, actor=actor)
        if external_provider_acknowledged is not True:
            raise ValidationError(
                "AI alignment requires explicit external-provider acknowledgement."
            )
        checkpoint = self._require_checkpoint(project_id, actor=actor)
        if checkpoint.metadata.get(ALIGNMENT_MODE_KEY):
            raise ConflictError("The checkpoint already has a manual alignment.")
        acknowledged_at = utc_now()
        context_packet, source_artifacts = self._ai_context(
            checkpoint,
            actor=actor,
            acknowledged_at=acknowledged_at,
        )
        candidate = GraphChangeSet(
            change_set_id=uuid4(),
            project_id=project_id,
            source_note_id=checkpoint.note_id,
            source_note_ids=[checkpoint.note_id],
            source_checksum=_sha256(checkpoint.raw_content),
            source_content_type="text/markdown",
            source_filename="member-tracking-checkpoint.md",
            batch_key=f"member-onboarding:v1:{checkpoint.note_id}",
            provider=str(getattr(draft_client, "provider", PROVIDER)),
            model=str(getattr(draft_client, "model", "unknown")),
            prompt_version=CHECKPOINT_PROMPT_VERSION,
            draft_mode=GraphDraftMode.GRAPH_CONTEXT,
            purpose=GraphDraftPurpose.MEMBER_CHECKPOINT_ALIGNMENT,
            context_packet=context_packet,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
            review_assignee=actor_user_id(actor),
            review_assignee_user_id=actor_user_fk(actor, self.repository),
        )
        checkpoint = self.notes.get_note(checkpoint.note_id)
        if checkpoint.metadata.get(ALIGNMENT_MODE_KEY):
            raise ConflictError("The checkpoint already has a manual alignment.")
        claim = self.graph_generation.claim_generation(
            candidate,
            draft_client=draft_client,
        )
        if not claim.acquired:
            return MemberOnboardingCommandResult(
                onboarding=self.get_member_onboarding(project_id, actor=actor),
                in_progress=claim.change_set.status == GraphChangeSetStatus.DRAFTING,
                retry_after_seconds=(
                    _retry_after_seconds(claim.change_set)
                    if claim.change_set.status == GraphChangeSetStatus.DRAFTING
                    else None
                ),
            )

        change_set = claim.change_set
        checkpoint = self.notes.get_note(checkpoint.note_id)
        if checkpoint.metadata.get(ALIGNMENT_MODE_KEY):
            change_set.error_metadata = {
                "category": "alignment_conflict",
                "message": "Manual alignment finalized before AI generation began.",
            }
            self.graph_generation.fail_generation_claim(
                change_set,
                claim_token=claim.claim_token,
            )
            raise ConflictError("The checkpoint already has a manual alignment.")
        attempts = DEFAULT_BATCH_RETRY_ATTEMPTS
        attempt_context = dict(context_packet)
        last_error: Exception | None = None
        last_category = "model_error"
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                renewed = self.graph_generation.renew_generation_claim(
                    change_set.change_set_id,
                    claim.claim_token,
                    draft_client=draft_client,
                )
                if renewed is None:
                    return MemberOnboardingCommandResult(
                        onboarding=self.get_member_onboarding(project_id, actor=actor),
                        in_progress=True,
                        retry_after_seconds=1,
                    )
                change_set = renewed
            try:
                graph_patch = draft_client.draft_from_note(
                    graph_context=attempt_context,
                    user_hint=_ai_alignment_instruction(
                        len(self._guided_fields(checkpoint).live_questions)
                    ),
                    draft_mode=GraphDraftMode.GRAPH_CONTEXT.value,
                    source_artifacts=source_artifacts,
                    image_bytes=None,
                    image_content_type=None,
                    extra_images=[],
                )
                self.graph_validator.validate_top_level(graph_patch)
                change_set.operations = self.graph_validator.operations_from_graph_patch(
                    change_set,
                    graph_patch,
                )
                validate_member_alignment_operations(
                    change_set,
                    checkpoint,
                    get_question=self.questions.get_question,
                )
            except GraphDraftingError as exc:
                last_error = exc
                last_category = "model_error"
            except ValidationError as exc:
                last_error = exc
                last_category = "validation_error"
            except Exception as exc:  # provider SDKs do not share one base type
                last_error = exc
                last_category = "provider_error"
            else:
                change_set.summary = str(graph_patch.get("summary") or "")
                change_set.uncertain_fields = string_list(graph_patch.get("uncertain_fields"))
                change_set.clarification_requests = string_list(
                    graph_patch.get("clarification_requests")
                )
                change_set.error_metadata = {}
                completed = self.graph_generation.complete_generation_claim(
                    change_set,
                    claim_token=claim.claim_token,
                )
                if completed is None:
                    return MemberOnboardingCommandResult(
                        onboarding=self.get_member_onboarding(project_id, actor=actor),
                        in_progress=True,
                        retry_after_seconds=1,
                    )
                return MemberOnboardingCommandResult(
                    onboarding=self.get_member_onboarding(project_id, actor=actor)
                )
            if attempt < attempts:
                attempt_context = {
                    **context_packet,
                    "generation_retry_feedback": {
                        "attempt": attempt,
                        "error": provider_error_message(last_error),
                        "instruction": (
                            "Return exactly one allowed onboarding question operation "
                            "for each live question."
                        ),
                    },
                }
        change_set.operations = []
        change_set.error_metadata = {
            "category": last_category,
            "message": provider_error_message(last_error),
            "attempts": attempts,
        }
        failed = self.graph_generation.fail_generation_claim(
            change_set,
            claim_token=claim.claim_token,
        )
        return MemberOnboardingCommandResult(
            onboarding=self.get_member_onboarding(project_id, actor=actor),
            in_progress=failed is None,
            retry_after_seconds=1 if failed is None else None,
        )

    def owner_queue(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> list[MemberOnboardingOwnerQueueItem]:
        self.authorization.require_owner(project_id, actor=actor)
        drafts, _ = self.graph_records.query_graph_change_sets(
            project_id=project_id,
            status=GraphChangeSetStatus.SUBMITTED,
            purpose=GraphDraftPurpose.MEMBER_CHECKPOINT_ALIGNMENT,
            limit=None,
            offset=0,
        )
        items: list[MemberOnboardingOwnerQueueItem] = []
        for draft in drafts:
            checkpoint = self.notes.get_note(draft.source_note_id)
            if not is_member_checkpoint(checkpoint):
                continue
            accepted_count = sum(
                operation.status == GraphChangeOperationStatus.ACCEPTED
                for operation in draft.operations
            )
            if accepted_count == 0:
                continue
            items.append(
                MemberOnboardingOwnerQueueItem(
                    project_id=project_id,
                    checkpoint=checkpoint,
                    draft=draft,
                    member_user_id=checkpoint.created_by_user_id,
                    member_username=draft.created_by_username,
                    accepted_operation_count=accepted_count,
                )
            )
        return items

    def _checkpoint_for_actor(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Note | None:
        if actor is None:
            return None
        key = checkpoint_client_capture_id(actor.user_id)
        notes, _ = self.repository.query_notes(
            project_id=project_id,
            client_capture_id=key,
            limit=1,
            offset=0,
        )
        checkpoint = notes[0] if notes else None
        if checkpoint is not None and not is_member_checkpoint(checkpoint):
            raise ConflictError("The member checkpoint key is occupied by another note.")
        return checkpoint

    def _require_checkpoint(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Note:
        checkpoint = self._checkpoint_for_actor(project_id, actor=actor)
        if checkpoint is None:
            raise ValidationError("Create a member tracking checkpoint first.")
        return checkpoint

    def _require_interactive_contributor(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None:
        self.authorization.require_contributor(project_id, actor=actor)
        self.authorization.require_interactive(actor, action="Member onboarding")
        self.projects.get_project(project_id)

    def _capabilities(
        self,
        project_id: UUID,
        *,
        role: ProjectMembershipRole,
        actor: AuthContext | None,
    ) -> MemberOnboardingCapabilities:
        interactive = bool(actor is not None and actor.is_interactive)
        can_capture = interactive and role in {
            ProjectMembershipRole.CONTRIBUTOR,
            ProjectMembershipRole.OWNER,
        }
        # Paired devices are deliberately capture-only at the HTTP middleware.
        # Keep this resource's advertised capabilities aligned with that policy
        # so a phone never receives checkpoint, alignment, or commit affordances
        # that the request will reject before reaching the route.
        can_write = can_capture and bool(actor is not None and not actor.is_device)
        return MemberOnboardingCapabilities(
            can_read=True,
            can_create_checkpoint=can_write,
            can_align=can_write,
            can_capture=can_capture,
            can_commit=can_write and role == ProjectMembershipRole.OWNER,
        )

    def _guided_fields(self, checkpoint: Note) -> MemberOnboardingGuidedFields:
        questions = _json_list(checkpoint.metadata.get(CHECKPOINT_LIVE_QUESTIONS_KEY))
        return MemberOnboardingGuidedFields(
            current_output_or_decision=checkpoint.metadata.get(
                CHECKPOINT_CURRENT_OUTPUT_KEY,
                "",
            ),
            live_questions=[str(item) for item in questions],
            strongest_recent_context=checkpoint.metadata.get(
                CHECKPOINT_RECENT_CONTEXT_KEY,
                "",
            ),
            next_move=checkpoint.metadata.get(CHECKPOINT_NEXT_MOVE_KEY, ""),
            source_text_present=bool(checkpoint.metadata.get(CHECKPOINT_SOURCE_HASH_KEY)),
        )

    def _alignment_draft(self, checkpoint: Note) -> GraphChangeSet | None:
        drafts, _ = self.graph_records.query_graph_change_sets(
            project_id=checkpoint.project_id,
            source_note_id=checkpoint.note_id,
            purpose=GraphDraftPurpose.MEMBER_CHECKPOINT_ALIGNMENT,
            limit=1,
            offset=0,
        )
        return drafts[0] if drafts else None

    def _alignment(
        self,
        checkpoint: Note,
        *,
        fields: MemberOnboardingGuidedFields,
        draft: GraphChangeSet | None,
    ) -> MemberOnboardingAlignment:
        if checkpoint.metadata.get(ALIGNMENT_MODE_KEY) == "manual":
            records = _json_list(checkpoint.metadata.get(ALIGNMENT_RESOLUTIONS_KEY))
            resolutions: list[MemberOnboardingQuestionResolution] = []
            for item in records:
                if not isinstance(item, dict):
                    continue
                question_id = UUID(str(item["question_id"])) if item.get("question_id") else None
                status = "checkpoint_only"
                if question_id is not None:
                    try:
                        status = self.questions.get_question(question_id).status.value
                    except NotFoundError:
                        status = "missing"
                resolutions.append(
                    MemberOnboardingQuestionResolution(
                        question_index=int(item["question_index"]),
                        action=str(item["action"]),
                        question_id=question_id,
                        status=status,
                    )
                )
            return MemberOnboardingAlignment(
                mode="manual",
                resolved_at=_metadata_datetime(checkpoint.metadata.get(ALIGNMENT_RESOLVED_AT_KEY)),
                question_resolutions=resolutions,
            )
        if draft is None:
            return MemberOnboardingAlignment(mode="none")
        by_index = {
            _operation_question_index(operation.client_ref): operation
            for operation in draft.operations
            if _operation_question_index(operation.client_ref) is not None
        }
        resolutions: list[MemberOnboardingQuestionResolution] = []
        for index in range(len(fields.live_questions)):
            operation = by_index.get(index)
            if operation is None:
                continue
            question_id = _operation_question_id(operation)
            action = "create_staged" if operation.op == GraphChangeOp.CREATE else "link_existing"
            resolutions.append(
                MemberOnboardingQuestionResolution(
                    question_index=index,
                    action=action,
                    question_id=question_id,
                    operation_id=operation.operation_id,
                    status=operation.status.value,
                )
            )
        return MemberOnboardingAlignment(
            mode="ai",
            resolved_at=draft.submitted_at,
            question_resolutions=resolutions,
            draft=draft,
        )

    def _map_items(
        self,
        checkpoint: Note,
        *,
        fields: MemberOnboardingGuidedFields,
        alignment: MemberOnboardingAlignment,
    ) -> list[MemberOnboardingMapItem]:
        by_index = {item.question_index: item for item in alignment.question_resolutions}
        items: list[MemberOnboardingMapItem] = []
        for index, original_text in enumerate(fields.live_questions):
            resolution = by_index.get(index)
            if resolution is None:
                items.append(
                    MemberOnboardingMapItem(
                        question_index=index,
                        text=original_text,
                        source="pending",
                        status="unresolved",
                    )
                )
                continue
            text = original_text
            rejected = resolution.status == GraphChangeOperationStatus.REJECTED.value
            if resolution.question_id is not None and not rejected:
                with suppress(NotFoundError):
                    text = self.questions.get_question(resolution.question_id).text
            elif resolution.operation_id and alignment.draft is not None and not rejected:
                operation = next(
                    (
                        item
                        for item in alignment.draft.operations
                        if item.operation_id == resolution.operation_id
                    ),
                    None,
                )
                if operation is not None and operation.op == GraphChangeOp.CREATE:
                    text = str(operation.payload.get("text") or original_text)
            if alignment.mode == "manual":
                source = "personal" if resolution.action == "checkpoint_only" else "shared"
            else:
                owner_rejected = bool(
                    alignment.draft is not None
                    and alignment.draft.status == GraphChangeSetStatus.REJECTED
                )
                source = (
                    "shared"
                    if resolution.status == GraphChangeOperationStatus.APPLIED.value
                    else "personal"
                    if rejected or owner_rejected
                    else "pending"
                )
                if owner_rejected:
                    text = original_text
            items.append(
                MemberOnboardingMapItem(
                    question_index=index,
                    text=text,
                    source=source,
                    status=resolution.status or "resolved",
                    question_id=resolution.question_id,
                    operation_id=resolution.operation_id,
                )
            )
        return items

    def _first_capture(self, checkpoint: Note) -> Note | None:
        raw_id = checkpoint.metadata.get(FIRST_CAPTURE_NOTE_ID_KEY)
        if not raw_id:
            return None
        try:
            note = self.notes.get_note(UUID(raw_id))
        except (ValueError, NotFoundError):
            return None
        if (
            note.project_id != checkpoint.project_id
            or note.status not in {NoteStatus.STAGED, NoteStatus.COMMITTED}
            or note.origin != EntityOrigin.USER
            or note.change_set_id is not None
            or not any(
                target.entity_type == EntityType.NOTE and target.entity_id == checkpoint.note_id
                for target in note.targets
            )
        ):
            return None
        if checkpoint.created_by_user_id is not None:
            return note if note.created_by_user_id == checkpoint.created_by_user_id else None
        return note if note.created_by == checkpoint.created_by else None

    @staticmethod
    def _alignment_resolved(
        checkpoint: Note,
        draft: GraphChangeSet | None,
    ) -> bool:
        if checkpoint.metadata.get(ALIGNMENT_MODE_KEY) in {"manual", "ai"}:
            return True
        return bool(
            draft is not None
            and draft.status
            in {
                GraphChangeSetStatus.SUBMITTED,
                GraphChangeSetStatus.REJECTED,
                GraphChangeSetStatus.COMMITTED,
            }
        )

    @staticmethod
    def _state(
        *,
        checkpoint: Note,
        draft: GraphChangeSet | None,
        alignment_resolved: bool,
        member_complete: bool,
    ) -> str:
        if draft is not None:
            if draft.status == GraphChangeSetStatus.CHANGES_REQUESTED:
                return "changes_requested"
            if draft.status == GraphChangeSetStatus.SUBMITTED:
                has_owner_work = any(
                    operation.status == GraphChangeOperationStatus.ACCEPTED
                    for operation in draft.operations
                )
                if has_owner_work:
                    return "complete" if member_complete else "awaiting_owner"
                return "complete" if member_complete else "capture_pending"
            if draft.status == GraphChangeSetStatus.REJECTED:
                return "complete" if member_complete else "rejected"
            if draft.status == GraphChangeSetStatus.COMMITTED:
                return "complete" if member_complete else "committed"
            if draft.status == GraphChangeSetStatus.READY:
                return "alignment_ready"
        if alignment_resolved:
            return "complete" if member_complete else "capture_pending"
        return "checkpoint_ready"

    def _canonical_checkpoint(
        self,
        payload: MemberOnboardingCheckpointRequest,
        *,
        as_of: datetime,
    ) -> tuple[dict[str, Any], str, str]:
        normalized = {
            "current_output_or_decision": payload.current_output_or_decision.strip(),
            "live_questions": [item.strip() for item in payload.live_questions],
            "strongest_recent_context": payload.strongest_recent_context.strip(),
            "next_move": payload.next_move.strip(),
            "source_text": (payload.source_text or "").strip(),
            "as_of": as_of.astimezone(timezone.utc).isoformat(),
        }
        content = _checkpoint_markdown(normalized)
        if len(content) > CHECKPOINT_CONTENT_LIMIT:
            raise ValidationError(
                f"The complete tracking checkpoint must be {CHECKPOINT_CONTENT_LIMIT} "
                "characters or fewer; source text is never silently truncated."
            )
        return normalized, content, _sha256(_canonical_json(normalized))

    @staticmethod
    def _checkpoint_input_hash(payload: MemberOnboardingCheckpointRequest) -> str:
        return _sha256(
            _canonical_json(
                {
                    "current_output_or_decision": payload.current_output_or_decision.strip(),
                    "live_questions": [item.strip() for item in payload.live_questions],
                    "strongest_recent_context": payload.strongest_recent_context.strip(),
                    "next_move": payload.next_move.strip(),
                    "source_text": (payload.source_text or "").strip(),
                    "as_of": (
                        payload.as_of.astimezone(timezone.utc).isoformat()
                        if payload.as_of is not None
                        else None
                    ),
                }
            )
        )

    @staticmethod
    def _checkpoint_replays(
        checkpoint: Note,
        *,
        payload_hash: str,
        input_hash: str,
    ) -> bool:
        stored_input_hash = checkpoint.metadata.get(CHECKPOINT_INPUT_HASH_KEY)
        if stored_input_hash is not None:
            return stored_input_hash == input_hash
        return checkpoint.metadata.get(CHECKPOINT_PAYLOAD_HASH_KEY) == payload_hash

    def _mark_completed_if_ready(
        self,
        checkpoint: Note,
        *,
        actor: AuthContext,
    ) -> None:
        if checkpoint.metadata.get(COMPLETED_AT_KEY) or not checkpoint.metadata.get(
            FIRST_CAPTURE_NOTE_ID_KEY
        ):
            return
        checkpoint = self.repository.notes.try_mark_member_onboarding_completed(
            checkpoint.note_id,
            completed_at=utc_now(),
        )
        if checkpoint is None:
            return
        self.record_usage_event(
            verb=UsageEventVerb.SUBMIT,
            resource_type=UsageEventResourceType.MEMBER_ONBOARDING,
            resource_id=checkpoint.note_id,
            project_id=checkpoint.project_id,
            actor=actor,
        )

    def _ai_context(
        self,
        checkpoint: Note,
        *,
        actor: AuthContext,
        acknowledged_at: datetime,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        fields = self._guided_fields(checkpoint)
        candidates = self._lexical_question_candidates(
            checkpoint.project_id,
            fields.live_questions,
        )
        context = {
            "mode": GraphDraftMode.GRAPH_CONTEXT.value,
            "workflow": "member_checkpoint_alignment_v1",
            "project_id": str(checkpoint.project_id),
            "checkpoint_note_id": str(checkpoint.note_id),
            "checkpoint_markdown": checkpoint.raw_content,
            "live_questions": fields.live_questions,
            "candidate_questions": candidates,
            "external_provider_acknowledgement": {
                "acknowledged": True,
                "actor_user_id": str(actor.user_id),
                "acknowledged_at": acknowledged_at.isoformat(),
            },
        }
        source_artifacts = [
            {
                "type": "text",
                "note_id": str(checkpoint.note_id),
                "project_id": str(checkpoint.project_id),
                "status": checkpoint.status.value,
                "raw_content_preview": checkpoint.raw_content,
            }
        ]
        context["source_artifacts"] = source_artifacts
        return context, source_artifacts

    def _lexical_question_candidates(
        self,
        project_id: UUID,
        live_questions: list[str],
    ) -> list[dict[str, Any]]:
        questions = [
            *self.questions.list_questions(
                project_id=project_id,
                status=QuestionStatus.ACTIVE,
            ),
            *self.questions.list_questions(
                project_id=project_id,
                status=QuestionStatus.STAGED,
            ),
        ]
        selected: dict[UUID, Question] = {}
        for live_question in live_questions:
            terms = set(_WORD_RE.findall(live_question.casefold()))
            ranked = sorted(
                questions,
                key=lambda question: (
                    -len(terms & set(_WORD_RE.findall(question.text.casefold()))),
                    question.text.casefold(),
                    str(question.question_id),
                ),
            )
            for question in ranked[:10]:
                selected.setdefault(question.question_id, question)
        return [
            {
                "id": str(question.question_id),
                "text": question.text,
                "status": question.status.value,
                "question_type": question.question_type.value,
            }
            for question in list(selected.values())[:30]
        ]

    def _checkpoint_author_label(self, checkpoint: Note) -> str:
        if checkpoint.created_by_user_id is not None:
            membership = self.projects.get_project_membership_for_user(
                checkpoint.project_id,
                checkpoint.created_by_user_id,
            )
            if membership is not None and membership.username:
                return membership.username
        return "current member"

    @staticmethod
    def _brief_markdown(
        checkpoint: Note,
        *,
        fields: MemberOnboardingGuidedFields,
        map_items: list[MemberOnboardingMapItem],
        draft: GraphChangeSet | None,
        author_label: str,
    ) -> str:
        labels = {
            "shared": "shared project record",
            "pending": "pending map proposal",
            "personal": "your checkpoint",
        }
        question_lines = [
            f"- {item.text} _({labels[item.source]}; {item.status})_" for item in map_items
        ] or ["- No live questions recorded."]
        provenance_lines = [
            f"Checkpoint author: {author_label}",
            f"Checkpoint as of: {checkpoint.metadata.get(CHECKPOINT_AS_OF_KEY, '')}",
            f"Checkpoint captured at: {checkpoint.created_at.isoformat()}",
        ]
        if draft is not None and draft.committed_at is not None:
            provenance_lines.append(f"Shared graph commit at: {draft.committed_at.isoformat()}")
        return "\n".join(
            [
                "# Where this project stands",
                "",
                "## Current output or decision",
                fields.current_output_or_decision,
                "",
                "## Live questions",
                *question_lines,
                "",
                "## Strongest recent context",
                fields.strongest_recent_context,
                "",
                "## Next move",
                fields.next_move,
                "",
                "## Tracking boundary",
                (
                    "This attributed member checkpoint starts routine Lab Tracker "
                    "tracking here. Earlier project coverage is selective."
                ),
                *provenance_lines,
            ]
        )


def _checkpoint_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Member tracking checkpoint",
        "",
        f"As of: {payload['as_of']}",
        "Historical coverage: selective",
        "",
        "## Current output or decision",
        payload["current_output_or_decision"],
        "",
        "## Live questions",
        *[f"- {question}" for question in payload["live_questions"]],
        "",
        "## Strongest recent context",
        payload["strongest_recent_context"],
        "",
        "## Next move",
        payload["next_move"],
    ]
    if payload["source_text"]:
        lines.extend(["", "## Optional source context", payload["source_text"]])
    return "\n".join(lines).strip()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _metadata_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _merge_targets(
    current: list[EntityRef],
    added: list[EntityRef],
) -> list[EntityRef]:
    by_key = {(target.entity_type, target.entity_id): target for target in [*current, *added]}
    return [by_key[key] for key in sorted(by_key, key=lambda item: (item[0].value, str(item[1])))]


def _operation_question_index(client_ref: str | None) -> int | None:
    match = re.fullmatch(r"live_question_([0-2])", client_ref or "")
    return int(match.group(1)) if match else None


def _operation_question_id(operation) -> UUID | None:
    if operation.op == GraphChangeOp.CREATE and operation.result_entity_id is not None:
        return operation.result_entity_id
    if operation.op != GraphChangeOp.UPDATE:
        return None
    targets = operation.payload.get("targets")
    if not isinstance(targets, list) or not targets:
        return None
    try:
        return UUID(str(targets[0].get("entity_id")))
    except (AttributeError, TypeError, ValueError):
        return None


def _retry_after_seconds(change_set: GraphChangeSet) -> int:
    expires = change_set.generation_lease_expires_at
    if expires is None:
        return 1
    return max(1, int((expires - utc_now()).total_seconds()) + 1)


def _ai_alignment_instruction(question_count: int) -> str:
    return (
        "Align the member's live questions with the existing project question map. "
        f"Return exactly {question_count} operations, one for each live question index. "
        "Set client_ref to live_question_0, live_question_1, or live_question_2. "
        "Each operation must either (a) create one staged question with entity_type "
        "question, semantic_type suggest_new_question, question_type other, or (b) "
        "update the checkpoint note with semantic_type link_note_to_question and a "
        "targets payload containing exactly one existing active/staged question. "
        "Do not propose any other entity, field, parent link, activation, claim, goal, "
        "dataset, analysis, or arbitrary note update."
    )
