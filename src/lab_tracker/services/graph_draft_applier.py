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
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    NoteStatus,
    ProjectStatus,
    QuestionStatus,
)
from lab_tracker.schemas import (
    AnalysisCreate,
    AnalysisUpdate,
    ClaimCreate,
    ClaimUpdate,
    DatasetCreate,
    DatasetUpdate,
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
    ) -> None:
        self.projects = projects
        self.questions = questions
        self.notes = notes
        self.sessions = sessions
        self.datasets = datasets
        self.analyses = analyses
        self.claims = claims
        self.visualizations = visualizations

    def apply_graph_operation(
        self,
        operation: GraphChangeOperation,
        *,
        ref_map: dict[str, UUID],
        actor: AuthContext | None,
    ) -> EntityResult:
        payload = resolve_refs(operation.payload, ref_map)
        if not isinstance(payload, dict):
            raise ValidationError("Resolved operation payload must be a JSON object.")
        if operation.op == GraphChangeOp.CREATE:
            return self._create_graph_entity(operation.entity_type, payload, actor=actor)
        if operation.target_entity_id is None:
            raise ValidationError("Update operations require target_entity_id.")
        return self._update_graph_entity(
            operation.entity_type,
            operation.target_entity_id,
            payload,
            actor=actor,
        )

    def _create_graph_entity(
        self,
        entity_type: EntityType,
        payload: dict[str, Any],
        *,
        actor: AuthContext | None,
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
                parent_question_ids=data.parent_question_ids,
                actor=actor,
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
            )
        if entity_type == EntityType.SESSION:
            data = validate_payload(SessionCreate, payload)
            return self.sessions.create_session(
                project_id=data.project_id,
                session_type=data.session_type,
                primary_question_id=data.primary_question_id,
                actor=actor,
            )
        if entity_type == EntityType.DATASET:
            data = validate_payload(DatasetCreate, payload)
            return self.datasets.create_dataset(
                project_id=data.project_id,
                primary_question_id=data.primary_question_id,
                secondary_question_ids=data.secondary_question_ids,
                status=data.status or DatasetStatus.STAGED,
                commit_manifest=data.commit_manifest,
                commit_hash=data.commit_hash,
                actor=actor,
            )
        if entity_type == EntityType.ANALYSIS:
            data = validate_payload(AnalysisCreate, payload)
            return self.analyses.create_analysis(
                project_id=data.project_id,
                dataset_ids=data.dataset_ids,
                method_hash=data.method_hash,
                code_version=data.code_version,
                environment_hash=data.environment_hash,
                status=data.status or AnalysisStatus.STAGED,
                actor=actor,
            )
        if entity_type == EntityType.CLAIM:
            data = validate_payload(ClaimCreate, payload)
            return self.claims.create_claim(
                project_id=data.project_id,
                statement=data.statement,
                confidence=data.confidence,
                status=data.status or ClaimStatus.PROPOSED,
                supported_by_dataset_ids=data.supported_by_dataset_ids,
                supported_by_analysis_ids=data.supported_by_analysis_ids,
                actor=actor,
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
            )
        raise ValidationError("Unsupported entity type.")

    def _update_graph_entity(
        self,
        entity_type: EntityType,
        entity_id: UUID,
        payload: dict[str, Any],
        *,
        actor: AuthContext | None,
    ) -> EntityResult:
        if entity_type == EntityType.PROJECT:
            data = validate_payload(ProjectUpdate, payload)
            return self.projects.update_project(
                entity_id,
                name=data.name,
                description=data.description,
                status=data.status,
                actor=actor,
            )
        if entity_type == EntityType.QUESTION:
            data = validate_payload(QuestionUpdate, payload)
            return self.questions.update_question(
                entity_id,
                text=data.text,
                question_type=data.question_type,
                hypothesis=data.hypothesis,
                status=data.status,
                parent_question_ids=data.parent_question_ids,
                actor=actor,
            )
        if entity_type == EntityType.NOTE:
            data = validate_payload(NoteUpdate, payload)
            return self.notes.update_note(
                entity_id,
                transcribed_text=data.transcribed_text,
                targets=data.targets,
                metadata=data.metadata,
                status=data.status,
                actor=actor,
            )
        if entity_type == EntityType.SESSION:
            data = validate_payload(SessionUpdate, payload)
            return self.sessions.update_session(
                entity_id,
                status=data.status,
                ended_at=data.ended_at,
                actor=actor,
            )
        if entity_type == EntityType.DATASET:
            data = validate_payload(DatasetUpdate, payload)
            return self.datasets.update_dataset(
                entity_id,
                status=data.status,
                question_links=data.question_links,
                commit_manifest=data.commit_manifest,
                commit_hash=data.commit_hash,
                actor=actor,
            )
        if entity_type == EntityType.ANALYSIS:
            data = validate_payload(AnalysisUpdate, payload)
            return self.analyses.update_analysis(
                entity_id,
                status=data.status,
                environment_hash=data.environment_hash,
                actor=actor,
            )
        if entity_type == EntityType.CLAIM:
            data = validate_payload(ClaimUpdate, payload)
            return self.claims.update_claim(
                entity_id,
                statement=data.statement,
                confidence=data.confidence,
                status=data.status,
                supported_by_dataset_ids=data.supported_by_dataset_ids,
                supported_by_analysis_ids=data.supported_by_analysis_ids,
                actor=actor,
            )
        if entity_type == EntityType.VISUALIZATION:
            data = validate_payload(VisualizationUpdate, payload)
            return self.visualizations.update_visualization(
                entity_id,
                viz_type=data.viz_type,
                file_path=data.file_path,
                caption=data.caption,
                related_claim_ids=data.related_claim_ids,
                actor=actor,
            )
        raise ValidationError("Unsupported entity type.")
