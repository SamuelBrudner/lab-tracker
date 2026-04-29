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


class GraphDraftServiceMixin:
    def create_graph_draft_from_note(
        self,
        note_id: UUID,
        *,
        draft_client: Any,
        actor: AuthContext | None = None,
    ) -> GraphChangeSet:
        require_role(actor, WRITE_ROLES)
        note = self.get_note(note_id)
        if note.raw_asset is None:
            raise ValidationError("Graph drafting requires a note with a raw image asset.")
        if not note.raw_asset.content_type.lower().startswith("image/"):
            raise ValidationError("Graph drafting only supports image note uploads.")
        raw_asset, image_bytes = self.download_note_raw(note_id)
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
            created_by=_actor_user_id(actor),
        )
        self._save_graph_change_set(change_set)
        try:
            graph_patch = draft_client.draft_from_image(
                image_bytes=image_bytes,
                content_type=raw_asset.content_type,
                project_context=self._graph_draft_project_context(note.project_id),
            )
            change_set.operations = self._operations_from_graph_patch(change_set, graph_patch)
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
                    target_entity_id=item.get("target_entity_id"),
                    client_ref=item.get("client_ref"),
                    payload=payload,
                    rationale=str(item.get("rationale") or ""),
                    confidence=item.get("confidence"),
                    source_refs=item.get("source_refs") or [],
                )
            except Exception as exc:
                raise GraphDraftingError("GPT graph patch operation was invalid.") from exc
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

    def _graph_draft_project_context(self, project_id: UUID) -> dict[str, Any]:
        project = self.get_project(project_id)
        notes = sorted(
            self.list_notes(project_id=project_id),
            key=lambda note: note.created_at,
            reverse=True,
        )[:20]
        return {
            "project": project.model_dump(mode="json"),
            "questions": [
                item.model_dump(mode="json") for item in self.list_questions(project_id=project_id)
            ],
            "recent_notes": [item.model_dump(mode="json") for item in notes],
            "sessions": [
                item.model_dump(mode="json") for item in self.list_sessions(project_id=project_id)
            ],
            "datasets": [
                item.model_dump(mode="json") for item in self.list_datasets(project_id=project_id)
            ],
            "analyses": [
                item.model_dump(mode="json") for item in self.list_analyses(project_id=project_id)
            ],
            "claims": [
                item.model_dump(mode="json") for item in self.list_claims(project_id=project_id)
            ],
            "visualizations": [
                item.model_dump(mode="json")
                for item in self.list_visualizations(project_id=project_id)
            ],
        }


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
