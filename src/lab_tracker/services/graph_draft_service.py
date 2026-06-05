"""Graph draft review service."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.graph_drafting import PROMPT_VERSION, PROVIDER, GraphDraftingError
from lab_tracker.models import (
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    Note,
    utc_now,
)
from lab_tracker.services.analysis_service import AnalysisService
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.claim_service import ClaimService
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.graph_draft_applier import GraphPatchApplier
from lab_tracker.services.graph_draft_context import (
    GraphContextBuilder,
)
from lab_tracker.services.graph_draft_context import (
    entity_id as graph_entity_id,
)
from lab_tracker.services.graph_draft_validation import GraphPatchValidator, string_list
from lab_tracker.services.note_service import NoteService
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.session_service import SessionService
from lab_tracker.services.shared import actor_user_id
from lab_tracker.services.visualization_service import VisualizationService

_BATCH_NOTE_LIMIT = 100


class GraphDraftService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        questions: QuestionService,
        notes: NoteService,
        sessions: SessionService,
        datasets: DatasetService,
        analyses: AnalysisService,
        claims: ClaimService,
        visualizations: VisualizationService,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.questions = questions
        self.notes = notes
        self.sessions = sessions
        self.datasets = datasets
        self.analyses = analyses
        self.claims = claims
        self.visualizations = visualizations
        self.authorization = authorization
        self.context_builder = GraphContextBuilder(
            projects=projects,
            questions=questions,
            notes=notes,
            sessions=sessions,
            datasets=datasets,
            analyses=analyses,
            claims=claims,
            visualizations=visualizations,
        )
        self.patch_validator = GraphPatchValidator(
            get_graph_entity=self.context_builder.get_graph_entity,
        )
        self.patch_applier = GraphPatchApplier(
            projects=projects,
            questions=questions,
            notes=notes,
            sessions=sessions,
            datasets=datasets,
            analyses=analyses,
            claims=claims,
            visualizations=visualizations,
        )

    def create_graph_draft_from_note(
        self,
        note_id: UUID,
        *,
        draft_client: Any,
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
            source_checksum=raw_asset.checksum if raw_asset is not None else None,
            source_content_type=raw_asset.content_type if raw_asset is not None else None,
            source_filename=raw_asset.filename if raw_asset is not None else None,
            provider=PROVIDER,
            model=getattr(draft_client, "model", "unknown"),
            prompt_version=PROMPT_VERSION,
            draft_mode=mode,
            context_packet=context_packet,
            created_by=actor_user_id(actor),
        )
        self._save_graph_change_set(change_set)
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
            change_set.error_metadata = {"message": str(exc)}
        finally:
            change_set.updated_at = utc_now()
            self._save_graph_change_set(change_set)
        return change_set

    def get_graph_change_set(self, change_set_id: UUID) -> GraphChangeSet:
        change_set = self.repository.graph_change_sets.get(change_set_id)
        if change_set is None:
            raise NotFoundError("Graph draft does not exist.")
        return change_set

    def list_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
    ) -> list[GraphChangeSet]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_graph_change_sets(
                project_id=project_id,
                status=status.value if status is not None else None,
                source_note_id=source_note_id,
                limit=None,
                offset=0,
            ),
        )

    def update_graph_change_operation(
        self,
        change_set_id: UUID,
        operation_id: UUID,
        *,
        payload: dict[str, Any] | None = None,
        status: GraphChangeOperationStatus | None = None,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        change_set = self.get_graph_change_set(change_set_id)
        self._ensure_graph_change_set_editable(change_set, actor=actor)
        operation = self._find_graph_operation(change_set, operation_id)
        if payload is not None:
            if not isinstance(payload, dict):
                raise ValidationError("payload must be a JSON object.")
            operation.payload = payload
            operation.error_metadata = {}
        if status is not None:
            if status not in {
                GraphChangeOperationStatus.PROPOSED,
                GraphChangeOperationStatus.ACCEPTED,
                GraphChangeOperationStatus.REJECTED,
            }:
                raise ValidationError("Operation status must be proposed, accepted, or rejected.")
            operation.status = status
        if operation.status == GraphChangeOperationStatus.REJECTED:
            operation.error_metadata = {}
        else:
            try:
                self.patch_validator.validate_operation(operation, operation.payload)
                operation.error_metadata = {}
            except ValidationError as exc:
                operation.error_metadata = {"message": str(exc)}
                if operation.status == GraphChangeOperationStatus.ACCEPTED:
                    operation.status = GraphChangeOperationStatus.PROPOSED
        operation.updated_at = utc_now()
        change_set.updated_at = utc_now()
        self._save_graph_change_set(change_set)
        return change_set

    def submit_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        change_set = self.get_graph_change_set(change_set_id)
        self.authorization.require_contributor(change_set.project_id, actor=actor)
        if not self._is_graph_change_set_author(
            change_set, actor
        ) and not self.authorization.has_global_write(actor):
            raise ValidationError("Only the graph draft author can submit this draft.")
        if change_set.status not in {
            GraphChangeSetStatus.READY,
            GraphChangeSetStatus.CHANGES_REQUESTED,
        }:
            raise ValidationError("Only ready or changes-requested graph drafts can be submitted.")
        change_set.status = GraphChangeSetStatus.SUBMITTED
        change_set.submitted_at = utc_now()
        change_set.submitted_by = actor_user_id(actor)
        change_set.reviewed_at = None
        change_set.reviewed_by = None
        change_set.review_note = None
        change_set.updated_at = change_set.submitted_at
        self._save_graph_change_set(change_set)
        return change_set

    def review_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        status: GraphChangeSetStatus,
        note: str | None = None,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        change_set = self.get_graph_change_set(change_set_id)
        self.authorization.require_owner(change_set.project_id, actor=actor)
        if status not in {
            GraphChangeSetStatus.CHANGES_REQUESTED,
            GraphChangeSetStatus.REJECTED,
        }:
            raise ValidationError("Review status must be changes_requested or rejected.")
        if change_set.status != GraphChangeSetStatus.SUBMITTED:
            raise ValidationError("Only submitted graph drafts can be reviewed.")
        change_set.status = status
        change_set.reviewed_at = utc_now()
        change_set.reviewed_by = actor_user_id(actor)
        change_set.review_note = note.strip() if note else None
        change_set.updated_at = change_set.reviewed_at
        self._save_graph_change_set(change_set)
        return change_set

    def revise_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        feedback: str,
        draft_client: Any,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        """Regenerate the whole proposed operation set from reviewer feedback.

        Reuses the same model + validation + persistence path as the initial
        draft, but seeds the model with the current operations and the reviewer's
        feedback. A model/validation failure leaves the existing draft intact.
        """
        change_set = self.get_graph_change_set(change_set_id)
        self._ensure_graph_change_set_editable(change_set, actor=actor)
        cleaned = (feedback or "").strip()
        if not cleaned:
            raise ValidationError("Reviewer feedback is required to revise a draft.")
        mode = change_set.draft_mode
        prepared = self.context_builder.prepare_note_sources_for_graph_draft(
            change_set.source_note_id,
            mode=mode,
        )
        note = prepared["source_note"]
        revise_hint = self._compose_revise_hint(change_set.operations, cleaned)
        if mode == GraphDraftMode.GRAPH_CONTEXT:
            context_packet = self.context_builder.build_graph_context_packet(
                note,
                source_notes=prepared["source_notes"],
                user_hint=revise_hint,
                actor=actor,
            )
        elif mode == GraphDraftMode.IMAGE_ONLY:
            context_packet = self.context_builder.image_only_context_packet(
                note,
                source_notes=prepared["source_notes"],
                user_hint=revise_hint,
            )
        else:
            raise ValidationError("Unsupported graph draft mode.")
        try:
            graph_patch = self._draft_graph_patch(
                draft_client,
                graph_context=context_packet,
                user_hint=revise_hint,
                draft_mode=mode,
                source_artifacts=prepared["source_artifacts"],
                image_bytes=prepared["image_bytes"],
                image_content_type=prepared["image_content_type"],
            )
            self.patch_validator.validate_top_level(graph_patch)
            # Build the new operations before mutating change_set so a model or
            # validation failure does not destroy the existing draft.
            new_operations = self.patch_validator.operations_from_graph_patch(
                change_set,
                graph_patch,
            )
        except GraphDraftingError as exc:
            raise ValidationError(f"Could not revise the draft: {exc}") from exc
        revisions: list[dict[str, Any]] = []
        if isinstance(change_set.context_packet, dict):
            revisions = list(change_set.context_packet.get("reviewer_revisions") or [])
        revisions.append({"feedback": cleaned, "at": utc_now().isoformat()})
        change_set.operations = new_operations
        change_set.summary = str(graph_patch.get("summary") or "")
        change_set.uncertain_fields = string_list(graph_patch.get("uncertain_fields"))
        change_set.clarification_requests = string_list(
            graph_patch.get("clarification_requests")
        )
        change_set.context_packet = context_packet
        if isinstance(change_set.context_packet, dict):
            change_set.context_packet["reviewer_revisions"] = revisions
        change_set.status = GraphChangeSetStatus.READY
        change_set.error_metadata = {}
        change_set.updated_at = utc_now()
        self._save_graph_change_set(change_set)
        return change_set

    @staticmethod
    def _compose_revise_hint(
        operations: list[GraphChangeOperation],
        feedback: str,
    ) -> str:
        lines = []
        for operation in operations:
            semantic = (
                operation.semantic_type.value
                if operation.semantic_type
                else operation.op.value
            )
            try:
                payload_text = json.dumps(operation.payload, default=str)
            except (TypeError, ValueError):
                payload_text = str(operation.payload)
            lines.append(
                f"- [{operation.status.value}] {semantic} "
                f"on {operation.entity_type.value}: {payload_text}"
            )
        prior = "\n".join(lines) if lines else "(none)"
        return (
            "REVISION REQUEST. You previously proposed the graph operations below. "
            "Return a complete, corrected operation set (not a diff) that honors the "
            "reviewer's feedback while staying grounded in the note and graph context."
            f"\n\nPreviously proposed operations:\n{prior}\n\nReviewer feedback: {feedback}"
        )

    def commit_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        message: str,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        if not message or not message.strip():
            raise ValidationError("message must not be empty.")
        change_set = self.get_graph_change_set(change_set_id)
        self.authorization.require_owner(change_set.project_id, actor=actor)
        if change_set.status not in {
            GraphChangeSetStatus.READY,
            GraphChangeSetStatus.SUBMITTED,
        }:
            raise ValidationError("Only ready or submitted graph drafts can be committed.")
        ref_map: dict[str, UUID] = {}
        accepted = [
            operation
            for operation in sorted(change_set.operations, key=lambda item: item.sequence)
            if operation.status == GraphChangeOperationStatus.ACCEPTED
        ]
        if not accepted:
            raise ValidationError("At least one accepted operation is required to commit.")
        for operation in accepted:
            entity = self.patch_applier.apply_graph_operation(
                operation,
                ref_map=ref_map,
                actor=actor,
            )
            resolved_entity_id = graph_entity_id(operation.entity_type, entity)
            if operation.client_ref:
                ref_map[operation.client_ref] = resolved_entity_id
            operation.status = GraphChangeOperationStatus.APPLIED
            operation.result_entity_id = resolved_entity_id
            operation.error_metadata = {}
            operation.updated_at = utc_now()
        change_set.status = GraphChangeSetStatus.COMMITTED
        change_set.commit_message = message.strip()
        change_set.committed_at = utc_now()
        change_set.committed_by = actor_user_id(actor)
        change_set.updated_at = change_set.committed_at
        self._save_graph_change_set(change_set)
        return change_set

    def _is_graph_change_set_author(
        self,
        change_set: GraphChangeSet,
        actor: AuthContext | None,
    ) -> bool:
        return actor is not None and change_set.created_by == str(actor.user_id)

    def _ensure_graph_change_set_editable(
        self,
        change_set: GraphChangeSet,
        *,
        actor: AuthContext | None,
    ) -> None:
        if change_set.status in {
            GraphChangeSetStatus.COMMITTED,
            GraphChangeSetStatus.REJECTED,
            GraphChangeSetStatus.FAILED,
        }:
            raise ValidationError("This graph draft cannot be edited.")
        if self.authorization.has_global_write(actor):
            return
        self.authorization.require_contributor(change_set.project_id, actor=actor)
        if not self._is_graph_change_set_author(change_set, actor):
            raise ValidationError("Only the graph draft author can edit this draft.")
        if change_set.status not in {
            GraphChangeSetStatus.READY,
            GraphChangeSetStatus.CHANGES_REQUESTED,
        }:
            raise ValidationError("Submitted graph drafts cannot be edited by contributors.")

    def _save_graph_change_set(self, change_set: GraphChangeSet) -> None:
        with self.unit_of_work() as repository:
            repository.graph_change_sets.save(change_set)

    def _find_graph_operation(
        self,
        change_set: GraphChangeSet,
        operation_id: UUID,
    ) -> GraphChangeOperation:
        for operation in change_set.operations:
            if operation.operation_id == operation_id:
                return operation
        raise NotFoundError("Graph draft operation does not exist.")

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
        window: tuple[Any, Any] | None = None,
        actor: AuthContext | None = None,
    ) -> dict[str, Any]:
        return self.context_builder.build_batch_graph_context(
            notes,
            window=window,
            actor=actor,
            batch_note_limit=_BATCH_NOTE_LIMIT,
        )

    def _draft_graph_patch(
        self,
        draft_client: Any,
        *,
        graph_context: dict[str, Any],
        user_hint: str | None,
        draft_mode: GraphDraftMode,
        source_artifacts: list[dict[str, Any]],
        image_bytes: bytes | None,
        image_content_type: str | None,
    ) -> dict[str, Any]:
        draft_from_note = getattr(draft_client, "draft_from_note", None)
        if callable(draft_from_note):
            return draft_from_note(
                graph_context=graph_context,
                user_hint=user_hint,
                draft_mode=draft_mode.value,
                source_artifacts=source_artifacts,
                image_bytes=image_bytes,
                image_content_type=image_content_type,
            )
        draft_from_image = getattr(draft_client, "draft_from_image", None)
        if callable(draft_from_image) and image_bytes and image_content_type:
            return draft_from_image(
                image_bytes=image_bytes,
                content_type=image_content_type,
                graph_context=graph_context,
                user_hint=user_hint,
                draft_mode=draft_mode.value,
            )
        raise GraphDraftingError("Configured draft client does not support this note source.")
