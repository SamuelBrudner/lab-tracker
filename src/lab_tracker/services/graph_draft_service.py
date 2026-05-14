"""Graph draft review service mixin."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError as PydanticValidationError

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.graph_drafting import GraphDraftingError, PROMPT_VERSION, PROVIDER
from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimStatus,
    Dataset,
    DatasetStatus,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    GraphDraftSemanticType,
    EntityRef,
    Note,
    NoteStatus,
    Project,
    ProjectStatus,
    Question,
    QuestionStatus,
    Session,
    Visualization,
    utc_now,
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
from lab_tracker.services.shared import WRITE_ROLES, _actor_user_id

EntityResult = Project | Question | Note | Session | Dataset | Analysis | Claim | Visualization
_REF_VALIDATION_PLACEHOLDER = "00000000-0000-0000-0000-000000000001"
_CREATE_SCHEMAS = {
    EntityType.PROJECT: ProjectCreate,
    EntityType.QUESTION: QuestionCreate,
    EntityType.NOTE: NoteCreate,
    EntityType.SESSION: SessionCreate,
    EntityType.DATASET: DatasetCreate,
    EntityType.ANALYSIS: AnalysisCreate,
    EntityType.CLAIM: ClaimCreate,
    EntityType.VISUALIZATION: VisualizationCreate,
}
_UPDATE_SCHEMAS = {
    EntityType.PROJECT: ProjectUpdate,
    EntityType.QUESTION: QuestionUpdate,
    EntityType.NOTE: NoteUpdate,
    EntityType.SESSION: SessionUpdate,
    EntityType.DATASET: DatasetUpdate,
    EntityType.ANALYSIS: AnalysisUpdate,
    EntityType.CLAIM: ClaimUpdate,
    EntityType.VISUALIZATION: VisualizationUpdate,
}
_RECENT_CONTEXT_LIMIT = 10
_QUESTION_CONTEXT_LIMIT = 50
_SEMANTIC_ALLOWED_TARGETS = {
    GraphDraftSemanticType.CREATE_NOTE: {(GraphChangeOp.CREATE, EntityType.NOTE)},
    GraphDraftSemanticType.LINK_NOTE_TO_QUESTION: {(GraphChangeOp.UPDATE, EntityType.NOTE)},
    GraphDraftSemanticType.LINK_NOTE_TO_SESSION: {(GraphChangeOp.UPDATE, EntityType.NOTE)},
    GraphDraftSemanticType.LINK_NOTE_TO_DATASET: {(GraphChangeOp.UPDATE, EntityType.NOTE)},
    GraphDraftSemanticType.LINK_NOTE_TO_ANALYSIS: {(GraphChangeOp.UPDATE, EntityType.NOTE)},
    GraphDraftSemanticType.SUGGEST_NEW_QUESTION: {(GraphChangeOp.CREATE, EntityType.QUESTION)},
    GraphDraftSemanticType.SUGGEST_NEW_DATASET: {(GraphChangeOp.CREATE, EntityType.DATASET)},
    GraphDraftSemanticType.SUGGEST_FOLLOWUP: {
        (GraphChangeOp.CREATE, EntityType.NOTE),
        (GraphChangeOp.CREATE, EntityType.QUESTION),
    },
    GraphDraftSemanticType.REQUEST_CLARIFICATION: {
        (GraphChangeOp.CREATE, EntityType.NOTE),
        (GraphChangeOp.UPDATE, EntityType.NOTE),
    },
}
_ENTITY_ID_FIELDS = {
    "project_id": EntityType.PROJECT,
    "question_id": EntityType.QUESTION,
    "primary_question_id": EntityType.QUESTION,
    "linked_question_id": EntityType.QUESTION,
    "session_id": EntityType.SESSION,
    "source_session_id": EntityType.SESSION,
    "dataset_id": EntityType.DATASET,
    "analysis_id": EntityType.ANALYSIS,
    "note_id": EntityType.NOTE,
    "viz_id": EntityType.VISUALIZATION,
    "visualization_id": EntityType.VISUALIZATION,
    "claim_id": EntityType.CLAIM,
}
_ENTITY_ID_LIST_FIELDS = {
    "parent_question_ids": EntityType.QUESTION,
    "secondary_question_ids": EntityType.QUESTION,
    "dataset_ids": EntityType.DATASET,
    "supported_by_dataset_ids": EntityType.DATASET,
    "supported_by_analysis_ids": EntityType.ANALYSIS,
    "related_claim_ids": EntityType.CLAIM,
    "note_ids": EntityType.NOTE,
}


class GraphDraftServiceMixin:
    def create_graph_draft_from_note(
        self,
        note_id: UUID,
        *,
        draft_client: Any,
        mode: GraphDraftMode = GraphDraftMode.GRAPH_CONTEXT,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        require_role(actor, WRITE_ROLES)
        note, raw_asset, image_bytes = self._prepare_image_note_for_graph_draft(note_id)
        cleaned_hint = user_hint.strip() if user_hint else None
        if mode == GraphDraftMode.GRAPH_CONTEXT:
            context_packet = self._build_graph_context_packet(
                note,
                user_hint=cleaned_hint,
                actor=actor,
            )
        elif mode == GraphDraftMode.IMAGE_ONLY:
            context_packet = self._image_only_context_packet(note, user_hint=cleaned_hint)
        else:
            raise ValidationError("Unsupported graph draft mode.")
        change_set = GraphChangeSet(
            change_set_id=uuid4(),
            project_id=note.project_id,
            source_note_id=note.note_id,
            source_checksum=raw_asset.checksum,
            source_content_type=raw_asset.content_type,
            source_filename=raw_asset.filename,
            provider=PROVIDER,
            model=getattr(draft_client, "model", "unknown"),
            prompt_version=PROMPT_VERSION,
            draft_mode=mode,
            context_packet=context_packet,
            created_by=_actor_user_id(actor),
        )
        self._save_graph_change_set(change_set)
        try:
            graph_patch = draft_client.draft_from_image(
                image_bytes=image_bytes,
                content_type=raw_asset.content_type,
                graph_context=context_packet,
                user_hint=cleaned_hint,
                draft_mode=mode.value,
            )
            _validate_graph_patch_top_level(graph_patch)
            change_set.operations = self._operations_from_graph_patch(change_set, graph_patch)
            change_set.summary = str(graph_patch.get("summary") or "")
            change_set.uncertain_fields = _string_list(graph_patch.get("uncertain_fields"))
            change_set.clarification_requests = _string_list(
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
        cached = self._get_cached_entity("graph_change_sets", change_set_id)
        if cached is not None:
            return cached
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            change_set = repository.graph_change_sets.get(change_set_id)
            if change_set is None:
                raise NotFoundError("Graph draft does not exist.")
            return self._cache_entity("graph_change_sets", change_set_id, change_set)
        raise NotFoundError("Graph draft does not exist.")

    def list_graph_change_sets(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphChangeSetStatus | None = None,
        source_note_id: UUID | None = None,
    ) -> list[GraphChangeSet]:
        change_sets = self._query_from_repository(
            attribute_name="graph_change_sets",
            loader=lambda repository: repository.query_graph_change_sets(
                project_id=project_id,
                status=status.value if status is not None else None,
                source_note_id=source_note_id,
                limit=None,
                offset=0,
            ),
            entity_id_getter=lambda change_set: change_set.change_set_id,
        )
        if change_sets is not None:
            return change_sets
        values = list(self._store.graph_change_sets.values())
        if project_id is not None:
            values = [item for item in values if item.project_id == project_id]
        if status is not None:
            values = [item for item in values if item.status == status]
        if source_note_id is not None:
            values = [item for item in values if item.source_note_id == source_note_id]
        return sorted(values, key=lambda item: (item.created_at, item.change_set_id), reverse=True)

    def update_graph_change_operation(
        self,
        change_set_id: UUID,
        operation_id: UUID,
        *,
        payload: dict[str, Any] | None = None,
        status: GraphChangeOperationStatus | None = None,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        require_role(actor, WRITE_ROLES)
        change_set = self.get_graph_change_set(change_set_id)
        if change_set.status == GraphChangeSetStatus.COMMITTED:
            raise ValidationError("Committed graph drafts cannot be edited.")
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
                _validate_graph_operation_payload(operation, operation.payload)
                self._ensure_graph_operation_references_exist(operation, operation.payload)
                _validate_semantic_operation_target(operation)
                operation.error_metadata = {}
            except ValidationError as exc:
                operation.error_metadata = {"message": str(exc)}
                if operation.status == GraphChangeOperationStatus.ACCEPTED:
                    operation.status = GraphChangeOperationStatus.PROPOSED
        operation.updated_at = utc_now()
        change_set.updated_at = utc_now()
        self._save_graph_change_set(change_set)
        return change_set

    def commit_graph_change_set(
        self,
        change_set_id: UUID,
        *,
        message: str,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        require_role(actor, WRITE_ROLES)
        if not message or not message.strip():
            raise ValidationError("message must not be empty.")
        change_set = self.get_graph_change_set(change_set_id)
        if change_set.status != GraphChangeSetStatus.READY:
            raise ValidationError("Only ready graph drafts can be committed.")
        ref_map: dict[str, UUID] = {}
        accepted = [
            operation
            for operation in sorted(change_set.operations, key=lambda item: item.sequence)
            if operation.status == GraphChangeOperationStatus.ACCEPTED
        ]
        if not accepted:
            raise ValidationError("At least one accepted operation is required to commit.")
        for operation in accepted:
            entity = self._apply_graph_operation(operation, ref_map=ref_map, actor=actor)
            entity_id = _entity_id(operation.entity_type, entity)
            if operation.client_ref:
                ref_map[operation.client_ref] = entity_id
            operation.status = GraphChangeOperationStatus.APPLIED
            operation.result_entity_id = entity_id
            operation.error_metadata = {}
            operation.updated_at = utc_now()
        change_set.status = GraphChangeSetStatus.COMMITTED
        change_set.commit_message = message.strip()
        change_set.committed_at = utc_now()
        change_set.committed_by = _actor_user_id(actor)
        change_set.updated_at = change_set.committed_at
        self._save_graph_change_set(change_set)
        return change_set

    def _save_graph_change_set(self, change_set: GraphChangeSet) -> None:
        self._remember_entity("graph_change_sets", change_set.change_set_id, change_set)
        self._run_repository_write(lambda repository: repository.graph_change_sets.save(change_set))

    def _operations_from_graph_patch(
        self,
        change_set: GraphChangeSet,
        graph_patch: dict[str, Any],
    ) -> list[GraphChangeOperation]:
        if not isinstance(graph_patch, dict):
            raise GraphDraftingError("GPT graph patch was not an object.")
        raw_operations = graph_patch.get("operations")
        if not isinstance(raw_operations, list):
            raise GraphDraftingError("GPT graph patch did not include an operations list.")
        operations: list[GraphChangeOperation] = []
        for index, item in enumerate(raw_operations, start=1):
            if not isinstance(item, dict):
                raise GraphDraftingError("GPT graph patch operation was not an object.")
            _validate_graph_patch_operation_shape(item)
            payload = _payload_from_json(item.get("payload_json"))
            try:
                operation = GraphChangeOperation(
                    operation_id=uuid4(),
                    change_set_id=change_set.change_set_id,
                    sequence=index,
                    op=GraphChangeOp(item.get("op")),
                    entity_type=EntityType(item.get("entity_type")),
                    semantic_type=GraphDraftSemanticType(item.get("semantic_type")),
                    target_entity_id=item.get("target_entity_id"),
                    client_ref=item.get("client_ref"),
                    payload=payload,
                    rationale=str(item.get("rationale") or ""),
                    confidence=item.get("confidence"),
                    source_refs=item.get("source_refs") or [],
                )
            except Exception as exc:
                raise GraphDraftingError("GPT graph patch operation was invalid.") from exc
            try:
                _validate_graph_operation_payload(operation, operation.payload)
                self._ensure_graph_operation_references_exist(operation, operation.payload)
                _validate_semantic_operation_target(operation)
            except ValidationError as exc:
                raise GraphDraftingError(str(exc)) from exc
            operations.append(operation)
        return operations

    def _find_graph_operation(
        self,
        change_set: GraphChangeSet,
        operation_id: UUID,
    ) -> GraphChangeOperation:
        for operation in change_set.operations:
            if operation.operation_id == operation_id:
                return operation
        raise NotFoundError("Graph draft operation does not exist.")

    def _apply_graph_operation(
        self,
        operation: GraphChangeOperation,
        *,
        ref_map: dict[str, UUID],
        actor: AuthContext | None,
    ) -> EntityResult:
        payload = _resolve_refs(operation.payload, ref_map)
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
            data = _validate_payload(ProjectCreate, payload)
            return self.create_project(
                data.name,
                description=data.description or "",
                status=data.status or ProjectStatus.ACTIVE,
                actor=actor,
            )
        if entity_type == EntityType.QUESTION:
            data = _validate_payload(QuestionCreate, payload)
            return self.create_question(
                project_id=data.project_id,
                text=data.text,
                question_type=data.question_type,
                hypothesis=data.hypothesis,
                status=data.status or QuestionStatus.STAGED,
                parent_question_ids=data.parent_question_ids,
                actor=actor,
            )
        if entity_type == EntityType.NOTE:
            data = _validate_payload(NoteCreate, payload)
            return self.create_note(
                project_id=data.project_id,
                raw_content=data.raw_content,
                transcribed_text=data.transcribed_text,
                targets=data.targets,
                metadata=data.metadata,
                status=data.status or NoteStatus.STAGED,
                actor=actor,
            )
        if entity_type == EntityType.SESSION:
            data = _validate_payload(SessionCreate, payload)
            return self.create_session(
                project_id=data.project_id,
                session_type=data.session_type,
                primary_question_id=data.primary_question_id,
                actor=actor,
            )
        if entity_type == EntityType.DATASET:
            data = _validate_payload(DatasetCreate, payload)
            return self.create_dataset(
                project_id=data.project_id,
                primary_question_id=data.primary_question_id,
                secondary_question_ids=data.secondary_question_ids,
                status=data.status or DatasetStatus.STAGED,
                commit_manifest=data.commit_manifest,
                commit_hash=data.commit_hash,
                actor=actor,
            )
        if entity_type == EntityType.ANALYSIS:
            data = _validate_payload(AnalysisCreate, payload)
            return self.create_analysis(
                project_id=data.project_id,
                dataset_ids=data.dataset_ids,
                method_hash=data.method_hash,
                code_version=data.code_version,
                environment_hash=data.environment_hash,
                status=data.status or AnalysisStatus.STAGED,
                actor=actor,
            )
        if entity_type == EntityType.CLAIM:
            data = _validate_payload(ClaimCreate, payload)
            return self.create_claim(
                project_id=data.project_id,
                statement=data.statement,
                confidence=data.confidence,
                status=data.status or ClaimStatus.PROPOSED,
                supported_by_dataset_ids=data.supported_by_dataset_ids,
                supported_by_analysis_ids=data.supported_by_analysis_ids,
                actor=actor,
            )
        if entity_type == EntityType.VISUALIZATION:
            data = _validate_payload(VisualizationCreate, payload)
            return self.create_visualization(
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
            data = _validate_payload(ProjectUpdate, payload)
            return self.update_project(
                entity_id,
                name=data.name,
                description=data.description,
                status=data.status,
                actor=actor,
            )
        if entity_type == EntityType.QUESTION:
            data = _validate_payload(QuestionUpdate, payload)
            return self.update_question(
                entity_id,
                text=data.text,
                question_type=data.question_type,
                hypothesis=data.hypothesis,
                status=data.status,
                parent_question_ids=data.parent_question_ids,
                actor=actor,
            )
        if entity_type == EntityType.NOTE:
            data = _validate_payload(NoteUpdate, payload)
            return self.update_note(
                entity_id,
                transcribed_text=data.transcribed_text,
                targets=data.targets,
                metadata=data.metadata,
                status=data.status,
                actor=actor,
            )
        if entity_type == EntityType.SESSION:
            data = _validate_payload(SessionUpdate, payload)
            return self.update_session(
                entity_id,
                status=data.status,
                ended_at=data.ended_at,
                actor=actor,
            )
        if entity_type == EntityType.DATASET:
            data = _validate_payload(DatasetUpdate, payload)
            return self.update_dataset(
                entity_id,
                status=data.status,
                question_links=data.question_links,
                commit_manifest=data.commit_manifest,
                commit_hash=data.commit_hash,
                actor=actor,
            )
        if entity_type == EntityType.ANALYSIS:
            data = _validate_payload(AnalysisUpdate, payload)
            return self.update_analysis(
                entity_id,
                status=data.status,
                environment_hash=data.environment_hash,
                actor=actor,
            )
        if entity_type == EntityType.CLAIM:
            data = _validate_payload(ClaimUpdate, payload)
            return self.update_claim(
                entity_id,
                statement=data.statement,
                confidence=data.confidence,
                status=data.status,
                supported_by_dataset_ids=data.supported_by_dataset_ids,
                supported_by_analysis_ids=data.supported_by_analysis_ids,
                actor=actor,
            )
        if entity_type == EntityType.VISUALIZATION:
            data = _validate_payload(VisualizationUpdate, payload)
            return self.update_visualization(
                entity_id,
                viz_type=data.viz_type,
                file_path=data.file_path,
                caption=data.caption,
                related_claim_ids=data.related_claim_ids,
                actor=actor,
            )
        raise ValidationError("Unsupported entity type.")

    def build_graph_context_for_note(
        self,
        note_id: UUID,
        *,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
    ) -> dict[str, Any]:
        note, _, _ = self._prepare_image_note_for_graph_draft(note_id)
        return self._build_graph_context_packet(
            note,
            user_hint=user_hint.strip() if user_hint else None,
            actor=actor,
        )

    def _prepare_image_note_for_graph_draft(self, note_id: UUID) -> tuple[Note, Any, bytes]:
        note = self.get_note(note_id)
        if note.raw_asset is None:
            raise ValidationError("Graph drafting requires a note with a raw image asset.")
        if not note.raw_asset.content_type.lower().startswith("image/"):
            raise ValidationError("Graph drafting only supports image note uploads.")
        try:
            raw_asset, image_bytes = self.download_note_raw(note_id)
        except NotFoundError as exc:
            raise NotFoundError("Source image file is unavailable.") from exc
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError("Source image file could not be read.") from exc
        if not image_bytes:
            raise ValidationError("Source image file is empty.")
        return note, raw_asset, image_bytes

    def _build_graph_context_packet(
        self,
        note: Note,
        *,
        user_hint: str | None,
        actor: AuthContext | None = None,
    ) -> dict[str, Any]:
        try:
            project = self.get_project(note.project_id)
        except NotFoundError as exc:
            raise ValidationError(
                "Graph context cannot be built because the note project does not exist."
            ) from exc
        questions = sorted(
            [
                question
                for question in self.list_questions(project_id=note.project_id)
                if question.status in {QuestionStatus.ACTIVE, QuestionStatus.STAGED}
            ],
            key=lambda question: (question.status.value, question.updated_at, question.question_id),
            reverse=True,
        )[:_QUESTION_CONTEXT_LIMIT]
        recent_notes = [
            item
            for item in sorted(
                self.list_notes(project_id=note.project_id),
                key=lambda candidate: candidate.created_at,
                reverse=True,
            )
            if item.note_id != note.note_id
        ][:_RECENT_CONTEXT_LIMIT]
        recent_sessions = sorted(
            self.list_sessions(project_id=note.project_id),
            key=lambda item: item.started_at,
            reverse=True,
        )[:_RECENT_CONTEXT_LIMIT]
        recent_datasets = sorted(
            self.list_datasets(project_id=note.project_id),
            key=lambda item: item.created_at,
            reverse=True,
        )[:_RECENT_CONTEXT_LIMIT]
        recent_analyses = sorted(
            self.list_analyses(project_id=note.project_id),
            key=lambda item: item.created_at,
            reverse=True,
        )[:_RECENT_CONTEXT_LIMIT]
        recent_claims = sorted(
            self.list_claims(project_id=note.project_id),
            key=lambda item: item.created_at,
            reverse=True,
        )[:_RECENT_CONTEXT_LIMIT]
        recent_visualizations = sorted(
            self.list_visualizations(project_id=note.project_id),
            key=lambda item: item.created_at,
            reverse=True,
        )[:_RECENT_CONTEXT_LIMIT]
        return {
            "mode": GraphDraftMode.GRAPH_CONTEXT.value,
            "user_hint": user_hint,
            "current_user": _compact_actor(actor),
            "source_note": _compact_note(note, include_raw_asset=True),
            "selected_targets": [
                self._compact_target_ref(target, note.project_id) for target in note.targets
            ],
            "project": {
                "id": str(project.project_id),
                "label": project.name,
                "status": project.status.value,
            },
            "active_or_staged_questions": [_compact_question(item) for item in questions],
            "recent_sessions": [_compact_session(item) for item in recent_sessions],
            "recent_datasets": [_compact_dataset(item) for item in recent_datasets],
            "recent_notes": [_compact_note(item) for item in recent_notes],
            "recent_analyses": [_compact_analysis(item) for item in recent_analyses],
            "recent_claims": [_compact_claim(item) for item in recent_claims],
            "recent_visualizations": [
                _compact_visualization(item) for item in recent_visualizations
            ],
            "unresolved_recent_captures": [
                _compact_note(item)
                for item in recent_notes
                if item.raw_asset is not None
                and item.raw_asset.content_type.lower().startswith("image/")
                and item.status == NoteStatus.STAGED
            ],
        }

    def _image_only_context_packet(self, note: Note, *, user_hint: str | None) -> dict[str, Any]:
        return {
            "mode": GraphDraftMode.IMAGE_ONLY.value,
            "user_hint": user_hint,
            "source_note": _compact_note(note, include_raw_asset=True),
            "warning": "Image-only draft was explicitly requested without graph context.",
        }

    def _compact_target_ref(self, target: EntityRef, project_id: UUID) -> dict[str, Any]:
        try:
            entity = self._get_graph_entity(target.entity_type, target.entity_id)
        except NotFoundError:
            return {
                "entity_type": target.entity_type.value,
                "entity_id": str(target.entity_id),
                "label": "(missing)",
            }
        if target.entity_type != EntityType.VISUALIZATION and hasattr(entity, "project_id"):
            if entity.project_id != project_id:
                raise ValidationError("Target must belong to the same project.")
        return {
            "entity_type": target.entity_type.value,
            "entity_id": str(target.entity_id),
            "label": _entity_label(target.entity_type, entity),
            "status": getattr(getattr(entity, "status", None), "value", None),
        }

    def _get_graph_entity(self, entity_type: EntityType, entity_id: UUID) -> EntityResult:
        getters = {
            EntityType.PROJECT: self.get_project,
            EntityType.QUESTION: self.get_question,
            EntityType.NOTE: self.get_note,
            EntityType.SESSION: self.get_session,
            EntityType.DATASET: self.get_dataset,
            EntityType.ANALYSIS: self.get_analysis,
            EntityType.CLAIM: self.get_claim,
            EntityType.VISUALIZATION: self.get_visualization,
        }
        getter = getters.get(entity_type)
        if getter is None:
            raise ValidationError("Unsupported entity type.")
        return getter(entity_id)

    def _ensure_graph_operation_references_exist(
        self,
        operation: GraphChangeOperation,
        payload: dict[str, Any],
    ) -> None:
        if operation.op == GraphChangeOp.UPDATE:
            if operation.target_entity_id is None:
                raise ValidationError("Update operations require target_entity_id.")
            try:
                self._get_graph_entity(operation.entity_type, operation.target_entity_id)
            except NotFoundError as exc:
                raise ValidationError(
                    "Operation references an unknown "
                    f"{operation.entity_type.value} ID: {operation.target_entity_id}."
                ) from exc
        try:
            refs = _iter_payload_entity_refs(payload)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Operation payload references an invalid entity ID.") from exc
        for entity_type, entity_id in refs:
            try:
                self._get_graph_entity(entity_type, entity_id)
            except NotFoundError as exc:
                raise ValidationError(
                    f"Operation references an unknown {entity_type.value} ID: {entity_id}."
                ) from exc


def _payload_from_json(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, str):
        raise GraphDraftingError("GPT graph patch payload_json must be a string.")
    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise GraphDraftingError("GPT graph patch payload_json was malformed.") from exc
    if not isinstance(parsed, dict):
        raise GraphDraftingError("GPT graph patch payload_json must decode to an object.")
    return parsed


def _validate_graph_patch_operation_shape(item: dict[str, Any]) -> None:
    required = {
        "client_ref",
        "op",
        "entity_type",
        "semantic_type",
        "target_entity_id",
        "payload_json",
        "rationale",
        "confidence",
        "source_refs",
    }
    missing = sorted(required - set(item))
    if missing:
        raise GraphDraftingError(
            f"GPT graph patch operation missing required fields: {', '.join(missing)}."
        )
    if not isinstance(item["rationale"], str):
        raise GraphDraftingError("GPT graph patch rationale must be a string.")
    confidence = item["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or confidence < 0
        or confidence > 1
    ):
        raise GraphDraftingError("GPT graph patch confidence must be between 0 and 1.")
    if not isinstance(item["source_refs"], list):
        raise GraphDraftingError("GPT graph patch source_refs must be a list.")


def _validate_graph_patch_top_level(graph_patch: dict[str, Any]) -> None:
    if not isinstance(graph_patch.get("summary"), str):
        raise GraphDraftingError("GPT graph patch summary must be a string.")
    if not isinstance(graph_patch.get("uncertain_fields"), list):
        raise GraphDraftingError("GPT graph patch uncertain_fields must be a list.")
    if not isinstance(graph_patch.get("clarification_requests"), list):
        raise GraphDraftingError("GPT graph patch clarification_requests must be a list.")


def _resolve_refs(value: Any, ref_map: dict[str, UUID]) -> Any:
    if isinstance(value, list):
        return [_resolve_refs(item, ref_map) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            ref_name = value["$ref"]
            if not isinstance(ref_name, str) or ref_name not in ref_map:
                raise ValidationError(f"Unknown graph draft ref: {ref_name}")
            return str(ref_map[ref_name])
        return {key: _resolve_refs(item, ref_map) for key, item in value.items()}
    return value


def _payload_for_review_validation(value: Any) -> Any:
    if isinstance(value, list):
        return [_payload_for_review_validation(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return _REF_VALIDATION_PLACEHOLDER
        return {key: _payload_for_review_validation(item) for key, item in value.items()}
    return value


def _validate_graph_operation_payload(
    operation: GraphChangeOperation,
    payload: dict[str, Any],
) -> None:
    candidate = _payload_for_review_validation(payload)
    if not isinstance(candidate, dict):
        raise ValidationError("Operation payload must be a JSON object.")
    if operation.op == GraphChangeOp.UPDATE and operation.target_entity_id is None:
        raise ValidationError("Update operations require target_entity_id.")
    schema_map = _CREATE_SCHEMAS if operation.op == GraphChangeOp.CREATE else _UPDATE_SCHEMAS
    schema_type = schema_map.get(operation.entity_type)
    if schema_type is None:
        raise ValidationError("Unsupported entity type.")
    _validate_payload(schema_type, candidate)


def _format_pydantic_error(exc: PydanticValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Operation payload failed API validation."
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", []))
    message = str(first.get("msg") or "invalid value")
    if location:
        return f"Operation payload failed API validation: {location}: {message}"
    return f"Operation payload failed API validation: {message}"


def _validate_payload(schema_type: Any, payload: dict[str, Any]) -> Any:
    try:
        return schema_type.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(_format_pydantic_error(exc)) from exc


def _validate_semantic_operation_target(operation: GraphChangeOperation) -> None:
    if operation.semantic_type is None:
        return
    if operation.semantic_type in {
        GraphDraftSemanticType.CREATE_ENTITY,
        GraphDraftSemanticType.UPDATE_ENTITY,
    }:
        return
    allowed = _SEMANTIC_ALLOWED_TARGETS.get(operation.semantic_type)
    if allowed is None:
        raise ValidationError(f"Unsupported semantic operation: {operation.semantic_type.value}.")
    candidate = (operation.op, operation.entity_type)
    if candidate not in allowed:
        raise ValidationError(
            f"Semantic operation {operation.semantic_type.value} cannot be used with "
            f"{operation.op.value} {operation.entity_type.value}."
        )


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _compact_actor(actor: AuthContext | None) -> dict[str, Any] | None:
    if actor is None:
        return None
    return {"id": str(actor.user_id), "role": actor.role.value}


def _compact_note(note: Note, *, include_raw_asset: bool = False) -> dict[str, Any]:
    preview = note.transcribed_text or note.raw_content or ""
    payload: dict[str, Any] = {
        "id": str(note.note_id),
        "project_id": str(note.project_id),
        "status": note.status.value,
        "created_at": note.created_at.isoformat(),
        "updated_at": note.updated_at.isoformat(),
        "preview": preview[:400],
        "targets": [
            {"entity_type": target.entity_type.value, "entity_id": str(target.entity_id)}
            for target in note.targets
        ],
        "metadata": dict(note.metadata),
    }
    if include_raw_asset and note.raw_asset is not None:
        payload["raw_asset"] = {
            "filename": note.raw_asset.filename,
            "content_type": note.raw_asset.content_type,
            "size_bytes": note.raw_asset.size_bytes,
            "checksum": note.raw_asset.checksum,
        }
    return payload


def _compact_question(question: Question) -> dict[str, Any]:
    return {
        "id": str(question.question_id),
        "label": question.text,
        "status": question.status.value,
        "question_type": question.question_type.value,
        "parent_question_ids": [str(item) for item in question.parent_question_ids],
        "updated_at": question.updated_at.isoformat(),
    }


def _compact_session(session: Session) -> dict[str, Any]:
    return {
        "id": str(session.session_id),
        "label": f"{session.session_type.value} session {session.started_at.date().isoformat()}",
        "status": session.status.value,
        "session_type": session.session_type.value,
        "primary_question_id": (
            str(session.primary_question_id) if session.primary_question_id else None
        ),
        "started_at": session.started_at.isoformat(),
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
    }


def _compact_dataset(dataset: Dataset) -> dict[str, Any]:
    return {
        "id": str(dataset.dataset_id),
        "label": f"Dataset {dataset.commit_hash[:12]}",
        "status": dataset.status.value,
        "primary_question_id": str(dataset.primary_question_id),
        "question_links": [
            {
                "question_id": str(link.question_id),
                "role": link.role.value,
                "outcome_status": link.outcome_status.value,
            }
            for link in dataset.question_links
        ],
        "source_session_id": (
            str(dataset.commit_manifest.source_session_id)
            if dataset.commit_manifest.source_session_id
            else None
        ),
        "created_at": dataset.created_at.isoformat(),
    }


def _compact_analysis(analysis: Analysis) -> dict[str, Any]:
    return {
        "id": str(analysis.analysis_id),
        "label": analysis.method_hash,
        "status": analysis.status.value,
        "dataset_ids": [str(item) for item in analysis.dataset_ids],
        "code_version": analysis.code_version,
        "created_at": analysis.created_at.isoformat(),
    }


def _compact_claim(claim: Claim) -> dict[str, Any]:
    return {
        "id": str(claim.claim_id),
        "label": claim.statement[:180],
        "status": claim.status.value,
        "confidence": claim.confidence,
        "supported_by_dataset_ids": [str(item) for item in claim.supported_by_dataset_ids],
        "supported_by_analysis_ids": [str(item) for item in claim.supported_by_analysis_ids],
        "created_at": claim.created_at.isoformat(),
    }


def _compact_visualization(visualization: Visualization) -> dict[str, Any]:
    return {
        "id": str(visualization.viz_id),
        "label": visualization.caption or visualization.file_path,
        "analysis_id": str(visualization.analysis_id),
        "viz_type": visualization.viz_type,
        "file_path": visualization.file_path,
        "related_claim_ids": [str(item) for item in visualization.related_claim_ids],
        "created_at": visualization.created_at.isoformat(),
    }


def _entity_label(entity_type: EntityType, entity: EntityResult) -> str:
    if entity_type == EntityType.PROJECT:
        return entity.name
    if entity_type == EntityType.QUESTION:
        return entity.text
    if entity_type == EntityType.NOTE:
        return entity.transcribed_text or entity.raw_content or "(binary note)"
    if entity_type == EntityType.SESSION:
        return f"{entity.session_type.value} session {entity.started_at.date().isoformat()}"
    if entity_type == EntityType.DATASET:
        return f"Dataset {entity.commit_hash[:12]}"
    if entity_type == EntityType.ANALYSIS:
        return entity.method_hash
    if entity_type == EntityType.CLAIM:
        return entity.statement[:180]
    if entity_type == EntityType.VISUALIZATION:
        return entity.caption or entity.file_path
    return str(_entity_id(entity_type, entity))


def _iter_payload_entity_refs(value: Any) -> list[tuple[EntityType, UUID]]:
    refs: list[tuple[EntityType, UUID]] = []
    if isinstance(value, list):
        for item in value:
            refs.extend(_iter_payload_entity_refs(item))
        return refs
    if not isinstance(value, dict):
        return refs
    if set(value) == {"$ref"}:
        return refs
    entity_type = value.get("entity_type")
    entity_id = value.get("entity_id")
    if isinstance(entity_type, str) and isinstance(entity_id, str):
        refs.append((EntityType(entity_type), UUID(entity_id)))
    for key, item in value.items():
        if key in _ENTITY_ID_FIELDS and isinstance(item, str):
            refs.append((_ENTITY_ID_FIELDS[key], UUID(item)))
        elif key in _ENTITY_ID_LIST_FIELDS and isinstance(item, list):
            for list_item in item:
                if isinstance(list_item, str):
                    refs.append((_ENTITY_ID_LIST_FIELDS[key], UUID(list_item)))
        else:
            refs.extend(_iter_payload_entity_refs(item))
    return refs


def _entity_id(entity_type: EntityType, entity: EntityResult) -> UUID:
    if entity_type == EntityType.PROJECT:
        return entity.project_id
    if entity_type == EntityType.QUESTION:
        return entity.question_id
    if entity_type == EntityType.NOTE:
        return entity.note_id
    if entity_type == EntityType.SESSION:
        return entity.session_id
    if entity_type == EntityType.DATASET:
        return entity.dataset_id
    if entity_type == EntityType.ANALYSIS:
        return entity.analysis_id
    if entity_type == EntityType.CLAIM:
        return entity.claim_id
    if entity_type == EntityType.VISUALIZATION:
        return entity.viz_id
    raise ValidationError("Unsupported entity type.")
