"""Services for ARA-style exploration trajectory capture."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    EntityOrigin,
    EntityRef,
    EntityType,
    ExplorationNode,
    ExplorationNodeStatus,
    ExplorationNodeType,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.shared import actor_user_fk, actor_user_id, ensure_non_empty, unique_ids

_TARGET_TYPES = {
    EntityType.QUESTION,
    EntityType.DATASET,
    EntityType.ANALYSIS,
    EntityType.CLAIM,
}
_EVIDENCE_TYPES = {
    EntityType.QUESTION,
    EntityType.DATASET,
    EntityType.NOTE,
    EntityType.SESSION,
    EntityType.ANALYSIS,
    EntityType.CLAIM,
    EntityType.VISUALIZATION,
}
_EXPLORATION_STATUS_TRANSITIONS = {
    ExplorationNodeStatus.STAGED: {
        ExplorationNodeStatus.STAGED,
        ExplorationNodeStatus.COMMITTED,
        ExplorationNodeStatus.ARCHIVED,
    },
    ExplorationNodeStatus.COMMITTED: {
        ExplorationNodeStatus.COMMITTED,
        ExplorationNodeStatus.ARCHIVED,
    },
    ExplorationNodeStatus.ARCHIVED: {ExplorationNodeStatus.ARCHIVED},
}


class ExplorationService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.authorization = authorization

    def create_exploration_node(
        self,
        project_id: UUID,
        *,
        node_type: ExplorationNodeType,
        title: str,
        target: EntityRef,
        status: ExplorationNodeStatus = ExplorationNodeStatus.STAGED,
        choice: str | None = None,
        alternatives_considered: Iterable[str] | None = None,
        rationale: str | None = None,
        evidence_refs: Iterable[EntityRef] | None = None,
        hypothesis: str | None = None,
        failure_mode: str | None = None,
        lesson: str | None = None,
        tooling_context: str | None = None,
        trigger: str | None = None,
        invalidates_node_id: UUID | None = None,
        invalidates_claim_id: UUID | None = None,
        parent_node_ids: Iterable[UUID] | None = None,
        also_depends_on_node_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> ExplorationNode:
        self.authorization.require_contributor(project_id, actor=actor)
        if self.repository.projects.get(project_id) is None:
            raise NotFoundError("Project does not exist.")
        resolved_target = self._resolve_entity_ref(
            target,
            project_id=project_id,
            allowed_types=_TARGET_TYPES,
            field_name="target",
        )
        resolved_evidence_refs = [
            self._resolve_entity_ref(
                ref,
                project_id=project_id,
                allowed_types=_EVIDENCE_TYPES,
                field_name="evidence_refs",
            )
            for ref in evidence_refs or []
        ]
        node = ExplorationNode(
            node_id=uuid4(),
            project_id=project_id,
            node_type=node_type,
            title=title.strip(),
            target=resolved_target,
            status=status,
            choice=_normalize_optional_text(choice),
            alternatives_considered=_normalize_text_list(alternatives_considered),
            rationale=_normalize_optional_text(rationale),
            evidence_refs=resolved_evidence_refs,
            hypothesis=_normalize_optional_text(hypothesis),
            failure_mode=_normalize_optional_text(failure_mode),
            lesson=_normalize_optional_text(lesson),
            tooling_context=_normalize_optional_text(tooling_context),
            trigger=_normalize_optional_text(trigger),
            invalidates_node_id=invalidates_node_id,
            invalidates_claim_id=invalidates_claim_id,
            parent_node_ids=unique_ids(parent_node_ids),
            also_depends_on_node_ids=unique_ids(also_depends_on_node_ids),
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
        )
        self._validate_node(node)
        with self.unit_of_work() as repository:
            repository.exploration_nodes.save(node)
        return node

    def get_exploration_node(self, node_id: UUID) -> ExplorationNode:
        return self.get_from_repository(
            entity_id=node_id,
            label="Exploration node",
            loader=lambda repository: repository.exploration_nodes.get(node_id),
        )

    def list_exploration_nodes(
        self,
        *,
        project_id: UUID | None = None,
        node_type: ExplorationNodeType | None = None,
        status: ExplorationNodeStatus | None = None,
        target_entity_type: EntityType | None = None,
        target_entity_id: UUID | None = None,
    ) -> list[ExplorationNode]:
        if project_id is not None and self.repository.projects.get(project_id) is None:
            raise NotFoundError("Project does not exist.")
        return self.query_from_repository(
            loader=lambda repository: repository.query_exploration_nodes(
                project_id=project_id,
                node_type=node_type.value if node_type is not None else None,
                status=status.value if status is not None else None,
                target_entity_type=(
                    target_entity_type.value if target_entity_type is not None else None
                ),
                target_entity_id=target_entity_id,
                limit=None,
                offset=0,
            ),
        )

    def update_exploration_node(
        self,
        node_id: UUID,
        *,
        title: str | None = None,
        status: ExplorationNodeStatus | None = None,
        choice: str | None = None,
        alternatives_considered: Iterable[str] | None = None,
        rationale: str | None = None,
        evidence_refs: Iterable[EntityRef] | None = None,
        hypothesis: str | None = None,
        failure_mode: str | None = None,
        lesson: str | None = None,
        tooling_context: str | None = None,
        trigger: str | None = None,
        invalidates_node_id: UUID | None = None,
        invalidates_claim_id: UUID | None = None,
        parent_node_ids: Iterable[UUID] | None = None,
        also_depends_on_node_ids: Iterable[UUID] | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> ExplorationNode:
        node = self.get_exploration_node(node_id)
        self.authorization.require_contributor(node.project_id, actor=actor)
        next_status = status or node.status
        self._ensure_status_transition(node.status, next_status)
        content_edit = any(
            item is not None
            for item in (
                title,
                choice,
                alternatives_considered,
                rationale,
                evidence_refs,
                hypothesis,
                failure_mode,
                lesson,
                tooling_context,
                trigger,
                invalidates_node_id,
                invalidates_claim_id,
                parent_node_ids,
                also_depends_on_node_ids,
            )
        )
        if node.status != ExplorationNodeStatus.STAGED and content_edit:
            raise ValidationError("Only staged exploration nodes can be edited.")
        if title is not None:
            ensure_non_empty(title, "title")
            node.title = title.strip()
        if choice is not None:
            node.choice = _normalize_optional_text(choice)
        if alternatives_considered is not None:
            node.alternatives_considered = _normalize_text_list(alternatives_considered)
        if rationale is not None:
            node.rationale = _normalize_optional_text(rationale)
        if evidence_refs is not None:
            node.evidence_refs = [
                self._resolve_entity_ref(
                    ref,
                    project_id=node.project_id,
                    allowed_types=_EVIDENCE_TYPES,
                    field_name="evidence_refs",
                )
                for ref in evidence_refs
            ]
        if hypothesis is not None:
            node.hypothesis = _normalize_optional_text(hypothesis)
        if failure_mode is not None:
            node.failure_mode = _normalize_optional_text(failure_mode)
        if lesson is not None:
            node.lesson = _normalize_optional_text(lesson)
        if tooling_context is not None:
            node.tooling_context = _normalize_optional_text(tooling_context)
        if trigger is not None:
            node.trigger = _normalize_optional_text(trigger)
        # Providing exactly one invalidation target re-points the pivot and
        # clears the other kind, so a staged pivot can switch between
        # invalidating a node and a claim without tripping the exactly-one
        # rule mid-update. Providing both still fails validation below.
        if invalidates_node_id is not None:
            node.invalidates_node_id = invalidates_node_id
            if invalidates_claim_id is None:
                node.invalidates_claim_id = None
        if invalidates_claim_id is not None:
            node.invalidates_claim_id = invalidates_claim_id
            if invalidates_node_id is None:
                node.invalidates_node_id = None
        if parent_node_ids is not None:
            node.parent_node_ids = unique_ids(parent_node_ids)
        if also_depends_on_node_ids is not None:
            node.also_depends_on_node_ids = unique_ids(also_depends_on_node_ids)
        node.status = next_status
        if origin is not None:
            node.origin = origin
        if change_set_id is not None:
            node.change_set_id = change_set_id
        if origin_provider is not None:
            node.origin_provider = origin_provider
        if origin_model is not None:
            node.origin_model = origin_model
        if origin_prompt_version is not None:
            node.origin_prompt_version = origin_prompt_version
        node.updated_at = utc_now()
        if content_edit:
            self._validate_node(node)
        # Status-only transitions skip re-validation: the content was validated
        # on creation and on every staged edit, and the only drift since then is
        # external — a referenced node/claim deleted out from under the pivot
        # (invalidates_* are ondelete=SET NULL). Re-running the exactly-one
        # check there would strand a committed/archived pivot that can no
        # longer be re-pointed.
        with self.unit_of_work() as repository:
            repository.exploration_nodes.save(node)
        return node

    def delete_exploration_node(
        self,
        node_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> ExplorationNode:
        node = self.get_exploration_node(node_id)
        self.authorization.require_contributor(node.project_id, actor=actor)
        with self.unit_of_work() as repository:
            repository.exploration_nodes.delete(node_id)
        return node

    def _validate_node(self, node: ExplorationNode) -> None:
        ensure_non_empty(node.title, "title")
        if node.node_type == ExplorationNodeType.DECISION:
            ensure_non_empty(node.choice or "", "choice")
            ensure_non_empty(node.rationale or "", "rationale")
            if not node.alternatives_considered:
                raise ValidationError("Decision nodes require alternatives_considered.")
        elif node.node_type == ExplorationNodeType.DEAD_END:
            ensure_non_empty(node.hypothesis or "", "hypothesis")
            ensure_non_empty(node.failure_mode or "", "failure_mode")
            ensure_non_empty(node.lesson or "", "lesson")
        elif node.node_type == ExplorationNodeType.PIVOT:
            ensure_non_empty(node.trigger or "", "trigger")
            ensure_non_empty(node.rationale or "", "rationale")
            self._ensure_invalidated_refs(node)
        if node.node_type != ExplorationNodeType.PIVOT:
            self._ensure_no_invalidated_refs(node)
        self._resolve_invalidated_refs(node)
        self._resolve_edge_refs(node)
        self._ensure_dag(node)

    def _ensure_invalidated_refs(self, node: ExplorationNode) -> None:
        refs = [
            node.invalidates_node_id is not None,
            node.invalidates_claim_id is not None,
        ]
        if refs.count(True) != 1:
            raise ValidationError("Pivot nodes must invalidate exactly one node or claim.")

    def _ensure_no_invalidated_refs(self, node: ExplorationNode) -> None:
        if node.invalidates_node_id is not None or node.invalidates_claim_id is not None:
            raise ValidationError("Only pivot exploration nodes can invalidate nodes or claims.")

    def _resolve_invalidated_refs(self, node: ExplorationNode) -> None:
        if node.invalidates_node_id is not None:
            invalidated = self.repository.exploration_nodes.get(node.invalidates_node_id)
            if invalidated is None:
                raise NotFoundError("Invalidated exploration node does not exist.")
            if invalidated.project_id != node.project_id:
                raise ValidationError("Invalidated exploration nodes must belong to the project.")
            if invalidated.node_id == node.node_id:
                raise ValidationError("Exploration nodes cannot invalidate themselves.")
        if node.invalidates_claim_id is not None:
            claim = self.repository.claims.get(node.invalidates_claim_id)
            if claim is None:
                raise NotFoundError("Invalidated claim does not exist.")
            if claim.project_id != node.project_id:
                raise ValidationError("Invalidated claims must belong to the project.")

    def _resolve_edge_refs(self, node: ExplorationNode) -> None:
        parent_ids = set(node.parent_node_ids)
        dependency_ids = set(node.also_depends_on_node_ids)
        if node.node_id in parent_ids or node.node_id in dependency_ids:
            raise ValidationError("Exploration nodes cannot depend on themselves.")
        if parent_ids & dependency_ids:
            raise ValidationError("Exploration node parent and dependency links must be unique.")
        for related_id in sorted(parent_ids | dependency_ids, key=str):
            related = self.repository.exploration_nodes.get(related_id)
            if related is None:
                raise NotFoundError("Related exploration node does not exist.")
            if related.project_id != node.project_id:
                raise ValidationError("Exploration node edges must stay within a project.")

    def _ensure_dag(self, node: ExplorationNode) -> None:
        nodes, _ = self.repository.query_exploration_nodes(
            project_id=node.project_id,
            limit=None,
            offset=0,
        )
        adjacency: dict[UUID, set[UUID]] = {}
        for existing in nodes:
            if existing.node_id == node.node_id:
                continue
            for source_id in [*existing.parent_node_ids, *existing.also_depends_on_node_ids]:
                adjacency.setdefault(source_id, set()).add(existing.node_id)
        for source_id in [*node.parent_node_ids, *node.also_depends_on_node_ids]:
            adjacency.setdefault(source_id, set()).add(node.node_id)
        if _has_cycle(adjacency):
            raise ValidationError("Exploration node graph must be acyclic.")

    def _ensure_status_transition(
        self,
        current_status: ExplorationNodeStatus,
        next_status: ExplorationNodeStatus,
    ) -> None:
        allowed = _EXPLORATION_STATUS_TRANSITIONS.get(current_status, {current_status})
        if next_status not in allowed:
            raise ValidationError(
                "Exploration node status cannot transition "
                f"from {current_status.value} to {next_status.value}."
            )

    def _resolve_entity_ref(
        self,
        ref: EntityRef,
        *,
        project_id: UUID,
        allowed_types: set[EntityType],
        field_name: str,
    ) -> EntityRef:
        ref = EntityRef.model_validate(ref)
        if ref.entity_type not in allowed_types:
            allowed = ", ".join(sorted(item.value for item in allowed_types))
            raise ValidationError(f"{field_name} entity_type must be one of: {allowed}.")
        entity_project_id = self._entity_project_id(ref)
        if entity_project_id != project_id:
            raise ValidationError(f"{field_name} must belong to the same project.")
        return ref

    def _entity_project_id(self, ref: EntityRef) -> UUID:
        if ref.entity_type == EntityType.QUESTION:
            entity = self.repository.questions.get(ref.entity_id)
        elif ref.entity_type == EntityType.DATASET:
            entity = self.repository.datasets.get(ref.entity_id)
        elif ref.entity_type == EntityType.NOTE:
            entity = self.repository.notes.get(ref.entity_id)
        elif ref.entity_type == EntityType.SESSION:
            entity = self.repository.sessions.get(ref.entity_id)
        elif ref.entity_type == EntityType.ANALYSIS:
            entity = self.repository.analyses.get(ref.entity_id)
        elif ref.entity_type == EntityType.CLAIM:
            entity = self.repository.claims.get(ref.entity_id)
        elif ref.entity_type == EntityType.VISUALIZATION:
            visualization = self.repository.visualizations.get(ref.entity_id)
            if visualization is None:
                entity = None
            else:
                entity = self.repository.analyses.get(visualization.analysis_id)
        else:
            entity = None
        if entity is None:
            raise NotFoundError(f"{ref.entity_type.value} target does not exist.")
        return entity.project_id


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_text_list(values: Iterable[str] | None) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        cleaned = value.strip()
        if not cleaned:
            continue
        if cleaned in seen:
            raise ValidationError("Duplicate text value.")
        seen.add(cleaned)
        items.append(cleaned)
    return items


def _has_cycle(adjacency: dict[UUID, set[UUID]]) -> bool:
    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def visit(node_id: UUID) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for child_id in adjacency.get(node_id, set()):
            if visit(child_id):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in list(adjacency))
