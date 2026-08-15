"""Graph draft patch application."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    EntityOrigin,
    EntityRef,
    EntityType,
    GoalLinkStatus,
    GoalStatus,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeSet,
    GraphDraftPurpose,
    GraphDraftSemanticType,
    NoteStatus,
    ProjectStatus,
    QuestionStatus,
)
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import (
    AnalysisCreate,
    AnalysisUpdate,
    ClaimCreate,
    ClaimUpdate,
    DatasetCreate,
    DatasetUpdate,
    GoalCreate,
    GoalUpdate,
    NoteCreate,
    NoteUpdate,
    ProjectCreate,
    ProjectUpdate,
    QuestionCreate,
    QuestionUpdate,
    SessionCreate,
    SessionUpdate,
    VisualizationCreate,
    VisualizationUpdate,
)
from lab_tracker.services.analysis_service import AnalysisService
from lab_tracker.services.claim_service import ClaimService
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.goal_service import GoalLinkSpec, GoalService
from lab_tracker.services.graph_draft_context import EntityResult
from lab_tracker.services.graph_draft_validation import resolve_refs, validate_payload
from lab_tracker.services.note_service import NoteService
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.session_service import SessionService
from lab_tracker.services.visualization_service import VisualizationService


class GraphPatchApplier:
    def __init__(
        self,
        *,
        projects: ProjectService,
        questions: QuestionService,
        notes: NoteService,
        sessions: SessionService,
        datasets: DatasetService,
        analyses: AnalysisService,
        claims: ClaimService,
        visualizations: VisualizationService,
        goals: GoalService | None = None,
    ) -> None:
        self.projects = projects
        self.questions = questions
        self.notes = notes
        self.sessions = sessions
        self.datasets = datasets
        self.analyses = analyses
        self.claims = claims
        self.visualizations = visualizations
        self.goals = goals

    def apply_graph_operation(
        self,
        operation: GraphChangeOperation,
        *,
        ref_map: dict[str, UUID],
        actor: AuthContext | None,
        change_set: GraphChangeSet,
        dataset_locks_held: bool = False,
    ) -> EntityResult:
        payload = resolve_refs(operation.payload, ref_map)
        if not isinstance(payload, dict):
            raise ValidationError("Resolved operation payload must be a JSON object.")
        origin_kwargs = _graph_draft_origin_kwargs(change_set, operation)
        if operation.op == GraphChangeOp.CREATE:
            return self._create_graph_entity(
                operation.entity_type,
                payload,
                actor=actor,
                origin_kwargs=origin_kwargs,
            )
        if operation.target_entity_id is None:
            raise ValidationError("Update operations require target_entity_id.")
        if (
            change_set.purpose == GraphDraftPurpose.MEMBER_CHECKPOINT_ALIGNMENT
            and operation.op == GraphChangeOp.UPDATE
            and operation.entity_type == EntityType.NOTE
            and operation.semantic_type == GraphDraftSemanticType.LINK_NOTE_TO_QUESTION
        ):
            return self._add_member_onboarding_note_targets(
                operation.target_entity_id,
                payload,
                actor=actor,
            )
        return self._update_graph_entity(
            operation.entity_type,
            operation.target_entity_id,
            payload,
            operation_semantic=operation.semantic_type,
            actor=actor,
            origin_kwargs=origin_kwargs,
            dataset_locks_held=dataset_locks_held,
        )

    def _add_member_onboarding_note_targets(
        self,
        note_id: UUID,
        payload: dict[str, Any],
        *,
        actor: AuthContext | None,
    ) -> EntityResult:
        """Add links without rewriting the human checkpoint or its provenance."""

        raw_targets = payload.get("targets")
        if not isinstance(raw_targets, list):
            raise ValidationError("Onboarding note links require targets.")
        added: list[EntityRef] = []
        for raw_target in raw_targets:
            try:
                added.append(EntityRef.model_validate(raw_target))
            except Exception as exc:
                raise ValidationError("Onboarding note link target is invalid.") from exc
        if len(added) != 1 or added[0].entity_type != EntityType.QUESTION:
            raise ValidationError("Onboarding note links require one question target.")
        return self.notes.add_member_onboarding_question_target(
            note_id,
            question_id=added[0].entity_id,
            actor=actor,
        )

    def _create_graph_entity(
        self,
        entity_type: EntityType,
        payload: dict[str, Any],
        *,
        actor: AuthContext | None,
        origin_kwargs: dict[str, Any],
    ) -> EntityResult:
        if entity_type == EntityType.PROJECT:
            data = validate_payload(ProjectCreate, payload)
            return self.projects.create_project(
                data.name,
                description=data.description or "",
                status=data.status or ProjectStatus.ACTIVE,
                actor=actor,
            )
        if entity_type == EntityType.QUESTION:
            data = validate_payload(QuestionCreate, payload)
            return self.questions.create_question(
                project_id=data.project_id,
                text=data.text,
                question_type=data.question_type,
                hypothesis=data.hypothesis,
                status=data.status or QuestionStatus.STAGED,
                terminal_reason=data.terminal_reason,
                parent_question_ids=data.parent_question_ids,
                actor=actor,
                **origin_kwargs,
            )
        if entity_type == EntityType.NOTE:
            data = validate_payload(NoteCreate, payload)
            return self.notes.create_note(
                project_id=data.project_id,
                raw_content=data.raw_content,
                transcribed_text=data.transcribed_text,
                targets=data.targets,
                metadata=data.metadata,
                status=data.status or NoteStatus.STAGED,
                actor=actor,
                **origin_kwargs,
            )
        if entity_type == EntityType.SESSION:
            data = validate_payload(SessionCreate, payload)
            return self.sessions.create_session(
                project_id=data.project_id,
                session_type=data.session_type,
                primary_question_id=data.primary_question_id,
                actor=actor,
                **origin_kwargs,
            )
        if entity_type == EntityType.DATASET:
            data = validate_payload(DatasetCreate, payload)
            return self.datasets.create_dataset(
                project_id=data.project_id,
                primary_question_id=data.primary_question_id,
                secondary_question_ids=data.secondary_question_ids,
                status=data.status or DatasetStatus.STAGED,
                terminal_reason=data.terminal_reason,
                commit_manifest=data.commit_manifest,
                commit_hash=data.commit_hash,
                actor=actor,
                **origin_kwargs,
            )
        if entity_type == EntityType.ANALYSIS:
            data = validate_payload(AnalysisCreate, payload)
            return self.analyses.create_analysis(
                project_id=data.project_id,
                dataset_ids=data.dataset_ids,
                method_hash=data.method_hash,
                code_version=data.code_version,
                environment_hash=data.environment_hash,
                external_artifacts=data.external_artifacts,
                status=data.status or AnalysisStatus.STAGED,
                terminal_reason=data.terminal_reason,
                actor=actor,
                **origin_kwargs,
            )
        if entity_type == EntityType.CLAIM:
            data = validate_payload(ClaimCreate, payload)
            return self.claims.create_claim(
                project_id=data.project_id,
                statement=data.statement,
                confidence=data.confidence,
                status=data.status or ClaimStatus.PROPOSED,
                terminal_reason=data.terminal_reason,
                falsification_criteria=data.falsification_criteria,
                verification_plan=data.verification_plan,
                refuting_outcome=data.refuting_outcome,
                supported_by_dataset_ids=data.supported_by_dataset_ids,
                supported_by_analysis_ids=data.supported_by_analysis_ids,
                answers_question_ids=data.answers_question_ids,
                external_citations=data.external_citations,
                actor=actor,
                **origin_kwargs,
            )
        if entity_type == EntityType.GOAL:
            if self.goals is None:
                raise ValidationError("Goal service is not configured.")
            data = validate_payload(GoalCreate, payload)
            return self.goals.create_goal(
                project_id=data.project_id,
                goal_type=data.goal_type,
                title=data.title,
                summary=data.summary,
                status=data.status or GoalStatus.PLANNED,
                target_date=data.target_date,
                external_ref=data.external_ref,
                attributes=data.attributes,
                links=[
                    GoalLinkSpec(
                        target=EntityRef(
                            entity_type=link.entity_type,
                            entity_id=link.entity_id,
                        ),
                        relation=link.relation,
                        link_status=link.link_status,
                        slot=link.slot,
                    )
                    for link in data.links or []
                ],
                actor=actor,
                **origin_kwargs,
            )
        if entity_type == EntityType.VISUALIZATION:
            data = validate_payload(VisualizationCreate, payload)
            return self.visualizations.create_visualization(
                analysis_id=data.analysis_id,
                viz_type=data.viz_type,
                file_path=data.file_path,
                caption=data.caption,
                related_claim_ids=data.related_claim_ids,
                actor=actor,
                **origin_kwargs,
            )
        raise ValidationError("Unsupported entity type.")

    def _update_graph_entity(
        self,
        entity_type: EntityType,
        entity_id: UUID,
        payload: dict[str, Any],
        *,
        operation_semantic: GraphDraftSemanticType | None,
        actor: AuthContext | None,
        origin_kwargs: dict[str, Any],
        dataset_locks_held: bool,
    ) -> EntityResult:
        if not payload:
            raise ValidationError("Update operation payload must include at least one field.")
        if entity_type == EntityType.PROJECT:
            data = validate_payload(ProjectUpdate, payload)
            return self.projects.update_project(
                entity_id,
                actor=actor,
                **provided_fields(data),
            )
        if entity_type == EntityType.QUESTION:
            data = validate_payload(QuestionUpdate, payload)
            return self.questions.update_question(
                entity_id,
                actor=actor,
                **origin_kwargs,
                **provided_fields(data),
            )
        if entity_type == EntityType.NOTE:
            data = validate_payload(NoteUpdate, payload)
            return self.notes.update_note(
                entity_id,
                actor=actor,
                **origin_kwargs,
                **provided_fields(data),
            )
        if entity_type == EntityType.SESSION:
            data = validate_payload(SessionUpdate, payload)
            return self.sessions.update_session(
                entity_id,
                actor=actor,
                **origin_kwargs,
                **provided_fields(data),
            )
        if entity_type == EntityType.DATASET:
            data = validate_payload(DatasetUpdate, payload)
            return self.datasets.update_dataset(
                entity_id,
                actor=actor,
                experiment_dataset_locks_held=dataset_locks_held,
                **origin_kwargs,
                **provided_fields(data),
            )
        if entity_type == EntityType.ANALYSIS:
            data = validate_payload(AnalysisUpdate, payload)
            return self.analyses.update_analysis(
                entity_id,
                actor=actor,
                **origin_kwargs,
                **provided_fields(data),
            )
        if entity_type == EntityType.CLAIM:
            data = validate_payload(ClaimUpdate, payload)
            return self.claims.update_claim(
                entity_id,
                actor=actor,
                **origin_kwargs,
                **provided_fields(data),
            )
        if entity_type == EntityType.GOAL:
            if self.goals is None:
                raise ValidationError("Goal service is not configured.")
            data = validate_payload(GoalUpdate, payload)
            updates = provided_fields(data)
            if "links" in updates:
                updates["links"] = [
                    GoalLinkSpec(
                        target=EntityRef(
                            entity_type=link.entity_type,
                            entity_id=link.entity_id,
                        ),
                        relation=link.relation,
                        link_status=_applied_goal_link_status(
                            operation_semantic=operation_semantic,
                            payload_status=link.link_status,
                        ),
                        slot=link.slot,
                    )
                    for link in data.links
                ]
            return self.goals.update_goal(
                entity_id,
                actor=actor,
                **origin_kwargs,
                **updates,
            )
        if entity_type == EntityType.VISUALIZATION:
            data = validate_payload(VisualizationUpdate, payload)
            return self.visualizations.update_visualization(
                entity_id,
                actor=actor,
                **origin_kwargs,
                **provided_fields(data),
            )
        raise ValidationError("Unsupported entity type.")


def _graph_draft_origin_kwargs(
    change_set: GraphChangeSet,
    operation: GraphChangeOperation,
) -> dict[str, Any]:
    return {
        "origin": (
            EntityOrigin.USER_REVISED
            if _operation_was_human_edited(operation)
            else EntityOrigin.AI_SUGGESTED
        ),
        "change_set_id": change_set.change_set_id,
        "origin_provider": change_set.provider,
        "origin_model": change_set.model,
        "origin_prompt_version": change_set.prompt_version,
    }


def _operation_was_human_edited(operation: GraphChangeOperation) -> bool:
    """True iff a reviewer edited this operation's payload before accepting it.

    Keyed on ``error_metadata['edited_at']``, which ``update_graph_change_operation``
    sets unconditionally whenever the incoming payload differs from the stored
    one. We deliberately do NOT key on ``edited_by``: it is ``None`` for an
    anonymous actor and is dropped by the validation-cleanup whitelist, whereas
    ``edited_at`` always survives to apply time. Stamping ``user_revised`` only on
    a genuine human edit keeps the PROV-O export honest -- an operation that was
    accepted (or bulk-accepted) unedited is an AI suggestion, not a human
    revision, so it must not materialize a ``prov:wasRevisionOf`` edge.
    """
    return (operation.error_metadata or {}).get("edited_at") is not None


def _applied_goal_link_status(
    *,
    operation_semantic: GraphDraftSemanticType | None,
    payload_status: GoalLinkStatus | None,
) -> GoalLinkStatus | None:
    if (
        operation_semantic == GraphDraftSemanticType.LINK_NODE_TO_GOAL
        and payload_status in {None, GoalLinkStatus.CANDIDATE}
    ):
        return GoalLinkStatus.COMMITTED
    return payload_status
