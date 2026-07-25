"""Goal domain service."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import ValidationError as PydanticValidationError

from lab_tracker.auth import AuthContext
from lab_tracker.errors import (
    AuthError,
    NotFoundError,
    OpaqueTargetNotFoundError,
    ValidationError,
)
from lab_tracker.goals_attributes import validate_goal_attributes
from lab_tracker.models import (
    EntityOrigin,
    EntityRef,
    EntityType,
    Goal,
    GoalLink,
    GoalLinkStatus,
    GoalRelation,
    GoalStatus,
    GoalType,
    utc_now,
)
from lab_tracker.patching import NOT_PROVIDED, PatchValue, is_provided
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.dataset_service import DatasetService
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.shared import actor_user_fk, actor_user_id, ensure_non_empty

if TYPE_CHECKING:
    from lab_tracker.services.analysis_service import AnalysisService
    from lab_tracker.services.claim_service import ClaimService
    from lab_tracker.services.note_service import NoteService
    from lab_tracker.services.session_service import SessionService
    from lab_tracker.services.visualization_service import VisualizationService


GOAL_LINK_TARGET_TYPES = {
    EntityType.PROJECT,
    EntityType.QUESTION,
    EntityType.EXPERIMENT,
    EntityType.NOTE,
    EntityType.SESSION,
    EntityType.DATASET,
    EntityType.ANALYSIS,
    EntityType.CLAIM,
    EntityType.VISUALIZATION,
}


class GoalService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        questions: QuestionService,
        datasets: DatasetService,
        sessions_provider: Callable[[], SessionService],
        analyses_provider: Callable[[], AnalysisService],
        claims_provider: Callable[[], ClaimService],
        visualizations_provider: Callable[[], VisualizationService],
        notes_provider: Callable[[], NoteService],
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.questions = questions
        self.datasets = datasets
        self._sessions_provider = sessions_provider
        self._analyses_provider = analyses_provider
        self._claims_provider = claims_provider
        self._visualizations_provider = visualizations_provider
        self._notes_provider = notes_provider
        self.authorization = authorization

    @property
    def sessions(self) -> SessionService:
        return self._sessions_provider()

    @property
    def analyses(self) -> AnalysisService:
        return self._analyses_provider()

    @property
    def claims(self) -> ClaimService:
        return self._claims_provider()

    @property
    def visualizations(self) -> VisualizationService:
        return self._visualizations_provider()

    @property
    def notes(self) -> NoteService:
        return self._notes_provider()

    def create_goal(
        self,
        project_id: UUID | None,
        *,
        goal_type: GoalType,
        title: str,
        summary: str | None = None,
        status: GoalStatus = GoalStatus.PLANNED,
        target_date: date | None = None,
        external_ref: str | None = None,
        attributes: dict[str, object] | None = None,
        links: Iterable[GoalLinkSpec] | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Goal:
        if project_id is not None:
            self.projects.get_project(project_id)
        ensure_non_empty(title, "title")
        normalized_attributes = self._validate_attributes(goal_type, attributes)
        goal = Goal(
            goal_id=uuid4(),
            project_id=project_id,
            goal_type=goal_type,
            title=title.strip(),
            summary=(summary or "").strip(),
            status=status,
            target_date=target_date,
            external_ref=external_ref.strip() if external_ref else None,
            attributes=normalized_attributes,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
        )
        for link in links or []:
            self._ensure_target_exists(link.target, goal.project_id)
            self._upsert_goal_link(
                goal,
                target=link.target,
                relation=link.relation,
                link_status=link.link_status,
                slot=link.slot,
                actor=actor,
            )
        self._ensure_goal_has_scope(goal)
        self._require_goal_contributor(goal, actor=actor)
        with self.unit_of_work() as repository:
            repository.goals.save(goal)
        return goal

    def get_goal(self, goal_id: UUID) -> Goal:
        return self.get_from_repository(
            entity_id=goal_id,
            label="Goal",
            loader=lambda repository: repository.goals.get(goal_id),
        )

    def list_goals(
        self,
        *,
        project_id: UUID | None = None,
        goal_type: GoalType | None = None,
        status: GoalStatus | None = None,
    ) -> list[Goal]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_goals(
                project_id=project_id,
                goal_type=goal_type.value if goal_type is not None else None,
                status=status.value if status is not None else None,
                limit=None,
                offset=0,
            ),
        )

    def list_visible_goals(
        self,
        *,
        project_id: UUID | None = None,
        goal_type: GoalType | None = None,
        status: GoalStatus | None = None,
        actor: AuthContext | None = None,
    ) -> list[Goal]:
        if project_id is not None:
            self.authorization.require_read(project_id, actor=actor)
            project_ids = None
        else:
            project_ids = self.authorization.accessible_project_ids(actor)
            if project_ids == set():
                return []
        goals = self.query_from_repository(
            loader=lambda repository: repository.query_goals(
                project_id=project_id,
                project_ids=project_ids,
                goal_type=goal_type.value if goal_type is not None else None,
                status=status.value if status is not None else None,
                limit=None,
                offset=0,
            ),
        )
        return [goal for goal in goals if self.can_read_goal(goal, actor=actor)]

    def can_read_goal(self, goal: Goal, *, actor: AuthContext | None = None) -> bool:
        try:
            self._require_goal_read(goal, actor=actor)
        except AuthError:
            return False
        return True

    def require_goal_read(
        self,
        goal: Goal,
        *,
        actor: AuthContext | None = None,
    ) -> set[UUID]:
        """Require the complete goal scope without exposing inaccessible goals."""

        try:
            return self._require_goal_read(goal, actor=actor)
        except AuthError as exc:
            raise OpaqueTargetNotFoundError("Goal does not exist.") from exc
        except NotFoundError as exc:
            # A linked target can disappear while resolving the goal scope.
            # Scope resolution happens before linked-project authorization, so
            # classify the failure from authority that does not depend on the
            # missing target.
            can_read_stable_scope = (
                self.authorization.can_read(goal.project_id, actor=actor)
                if goal.project_id is not None
                else self.authorization.has_global_read(actor)
            )
            if not can_read_stable_scope:
                raise OpaqueTargetNotFoundError("Goal does not exist.") from exc
            raise NotFoundError("Goal does not exist.") from exc

    def update_goal(
        self,
        goal_id: UUID,
        *,
        goal_type: PatchValue[GoalType | None] = NOT_PROVIDED,
        title: PatchValue[str | None] = NOT_PROVIDED,
        summary: PatchValue[str | None] = NOT_PROVIDED,
        status: PatchValue[GoalStatus | None] = NOT_PROVIDED,
        target_date: PatchValue[date | None] = NOT_PROVIDED,
        external_ref: PatchValue[str | None] = NOT_PROVIDED,
        attributes: PatchValue[dict[str, object] | None] = NOT_PROVIDED,
        links: PatchValue[Iterable[GoalLinkSpec] | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Goal:
        goal = self.get_goal(goal_id)
        self._require_goal_contributor(goal, actor=actor)
        before = goal.model_copy(deep=True)
        for field_name, value in (
            ("goal_type", goal_type),
            ("title", title),
            ("summary", summary),
            ("status", status),
            ("attributes", attributes),
            ("links", links),
        ):
            if is_provided(value) and value is None:
                raise ValidationError(f"{field_name} must not be null.")
        next_goal_type = goal_type if is_provided(goal_type) else goal.goal_type
        if is_provided(title):
            ensure_non_empty(title, "title")
            goal.title = title.strip()
        if is_provided(summary):
            goal.summary = summary.strip()
        if is_provided(status):
            goal.status = status
        if is_provided(goal_type):
            goal.goal_type = goal_type
        if is_provided(target_date):
            goal.target_date = target_date
        if is_provided(external_ref):
            goal.external_ref = external_ref.strip() if external_ref else None
        if is_provided(attributes) or is_provided(goal_type):
            goal.attributes = self._validate_attributes(
                next_goal_type,
                attributes if is_provided(attributes) else goal.attributes,
            )
        if is_provided(links):
            for link in links:
                self._ensure_target_exists(link.target, goal.project_id)
                self._upsert_goal_link(
                    goal,
                    target=link.target,
                    relation=link.relation,
                    link_status=link.link_status,
                    slot=link.slot,
                    actor=actor,
                )
        if origin is not None:
            goal.origin = origin
        if change_set_id is not None:
            goal.change_set_id = change_set_id
        if origin_provider is not None:
            goal.origin_provider = origin_provider
        if origin_model is not None:
            goal.origin_model = origin_model
        if origin_prompt_version is not None:
            goal.origin_prompt_version = origin_prompt_version
        self._ensure_unique_goal_links(goal.links)
        self._ensure_goal_has_scope(goal)
        self._require_goal_contributor(goal, actor=actor)
        if goal == before:
            return goal
        goal.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.goals.save(goal)
        return goal

    def delete_goal(self, goal_id: UUID, *, actor: AuthContext | None = None) -> Goal:
        goal = self.get_goal(goal_id)
        self._require_goal_owner(goal, actor=actor)
        with self.unit_of_work() as repository:
            repository.goals.delete(goal_id)
        return goal

    def link_node_to_goal(
        self,
        goal_id: UUID,
        *,
        target: EntityRef,
        relation: GoalRelation,
        link_status: GoalLinkStatus = GoalLinkStatus.CANDIDATE,
        slot: str | None = None,
        actor: AuthContext | None = None,
    ) -> GoalLink:
        goal = self.get_goal(goal_id)
        self._require_goal_contributor(goal, actor=actor)
        self._ensure_target_exists(target, goal.project_id)
        link = self._upsert_goal_link(
            goal,
            target=target,
            relation=relation,
            link_status=link_status,
            slot=slot,
            actor=actor,
        )
        goal.updated_at = utc_now()
        self._ensure_unique_goal_links(goal.links)
        self._ensure_goal_has_scope(goal)
        self._require_goal_contributor(goal, actor=actor)
        with self.unit_of_work() as repository:
            repository.goals.save(goal)
        return link

    def update_goal_link(
        self,
        goal_id: UUID,
        link_id: UUID,
        *,
        relation: PatchValue[GoalRelation | None] = NOT_PROVIDED,
        link_status: PatchValue[GoalLinkStatus | None] = NOT_PROVIDED,
        slot: PatchValue[str | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
    ) -> GoalLink:
        goal = self.get_goal(goal_id)
        self._require_goal_contributor(goal, actor=actor)
        link = self._find_goal_link(goal, link_id)
        before = goal.model_copy(deep=True)
        if is_provided(relation):
            if relation is None:
                raise ValidationError("relation must not be null.")
            link.relation = relation
        if is_provided(link_status):
            if link_status is None:
                raise ValidationError("link_status must not be null.")
            link.link_status = link_status
        if is_provided(slot):
            link.slot = self._clean_slot(slot) if slot is not None else None
        self._ensure_unique_goal_links(goal.links)
        self._ensure_goal_has_scope(goal)
        self._require_goal_contributor(goal, actor=actor)
        if goal == before:
            return link
        goal.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.goals.save(goal)
        return link

    def delete_goal_link(
        self,
        goal_id: UUID,
        link_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> GoalLink:
        goal = self.get_goal(goal_id)
        self._require_goal_contributor(goal, actor=actor)
        link = self._find_goal_link(goal, link_id)
        goal.links = [item for item in goal.links if item.link_id != link_id]
        goal.updated_at = utc_now()
        self._ensure_goal_has_scope(goal)
        self._require_goal_contributor(goal, actor=actor)
        with self.unit_of_work() as repository:
            repository.goals.save(goal)
        return link

    def list_node_goals(
        self,
        *,
        project_id: UUID,
        target: EntityRef,
    ) -> list[Goal]:
        self._ensure_target_exists(target, project_id)
        return self.query_from_repository(
            loader=lambda repository: repository.query_goals(
                project_id=project_id,
                target_entity_type=target.entity_type.value,
                target_entity_id=target.entity_id,
                limit=None,
                offset=0,
            ),
        )

    def _validate_attributes(
        self,
        goal_type: GoalType,
        attributes: dict[str, object] | None,
    ) -> dict[str, object]:
        try:
            return validate_goal_attributes(goal_type, attributes)
        except (PydanticValidationError, ValueError) as exc:
            raise ValidationError(f"Goal attributes are invalid: {exc}") from exc

    def _ensure_target_exists(
        self,
        target: EntityRef,
        project_id: UUID | None,
    ) -> UUID:
        if target.entity_type not in GOAL_LINK_TARGET_TYPES:
            raise ValidationError("Unsupported goal link target entity type.")
        if target.entity_type == EntityType.PROJECT:
            self.projects.get_project(target.entity_id)
            if project_id is not None and target.entity_id != project_id:
                raise ValidationError(
                    "Project goal links must match the goal project; "
                    "create a projectless goal to span projects."
                )
            return target.entity_id
        entity_getters = {
            EntityType.QUESTION: self.questions.get_question,
            EntityType.EXPERIMENT: self._get_experiment_target,
            EntityType.NOTE: self.notes.get_note,
            EntityType.SESSION: self.sessions.get_session,
            EntityType.DATASET: self.datasets.get_dataset,
            EntityType.ANALYSIS: self.analyses.get_analysis,
            EntityType.CLAIM: self.claims.get_claim,
            EntityType.VISUALIZATION: self.visualizations.get_visualization,
        }
        entity = entity_getters[target.entity_type](target.entity_id)
        if target.entity_type == EntityType.VISUALIZATION:
            analysis = self.analyses.get_analysis(entity.analysis_id)
            if project_id is not None and analysis.project_id != project_id:
                raise ValidationError("Goal link target must belong to the same project.")
            return analysis.project_id
        target_project_id = getattr(entity, "project_id", None)
        if project_id is not None and target_project_id != project_id:
            raise ValidationError("Goal link target must belong to the same project.")
        return target_project_id

    def _get_experiment_target(self, experiment_id: UUID):
        experiment = self.repository.experiments.get(experiment_id)
        if experiment is None:
            raise ValidationError("Experiment goal link target does not exist.")
        return experiment

    def _target_project_id(self, target: EntityRef) -> UUID:
        return self._ensure_target_exists(target, None)

    def _goal_scope_project_ids(self, goal: Goal) -> set[UUID]:
        project_ids: set[UUID] = set()
        if goal.project_id is not None:
            project_ids.add(goal.project_id)
        for link in goal.links:
            project_ids.add(self._target_project_id(link.target))
        return project_ids

    def _ensure_goal_has_scope(self, goal: Goal) -> None:
        if goal.project_id is not None:
            return
        if any(link.target.entity_type == EntityType.PROJECT for link in goal.links):
            return
        raise ValidationError("Projectless goals must include at least one project link.")

    def _require_goal_read(
        self,
        goal: Goal,
        *,
        actor: AuthContext | None = None,
    ) -> set[UUID]:
        project_ids = self._goal_scope_project_ids(goal)
        if not project_ids and not self.authorization.has_global_read(actor):
            raise AuthError("Project access required.")
        for project_id in project_ids:
            self.authorization.require_read(project_id, actor=actor)
        return project_ids

    def _require_goal_contributor(
        self,
        goal: Goal,
        *,
        actor: AuthContext | None = None,
    ) -> None:
        project_ids = self._goal_scope_project_ids(goal)
        if not project_ids and not self.authorization.has_global_write(actor):
            raise AuthError("Project contributor access required.")
        for project_id in project_ids:
            self.authorization.require_contributor(project_id, actor=actor)

    def _require_goal_owner(self, goal: Goal, *, actor: AuthContext | None = None) -> None:
        project_ids = self._goal_scope_project_ids(goal)
        if not project_ids and not self.authorization.has_global_admin(actor):
            raise AuthError("Project owner access required.")
        for project_id in project_ids:
            self.authorization.require_owner(project_id, actor=actor)

    def _upsert_goal_link(
        self,
        goal: Goal,
        *,
        target: EntityRef,
        relation: GoalRelation,
        link_status: GoalLinkStatus | None,
        slot: str | None,
        actor: AuthContext | None,
    ) -> GoalLink:
        cleaned_slot = self._clean_slot(slot)
        status = link_status or GoalLinkStatus.CANDIDATE
        for link in goal.links:
            if self._link_key(link) == (
                target.entity_type,
                target.entity_id,
                relation,
                cleaned_slot,
            ):
                link.link_status = status
                return link
        link = GoalLink(
            link_id=uuid4(),
            goal_id=goal.goal_id,
            target=target,
            relation=relation,
            link_status=status,
            slot=cleaned_slot,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        goal.links.append(link)
        return link

    def _find_goal_link(self, goal: Goal, link_id: UUID) -> GoalLink:
        for link in goal.links:
            if link.link_id == link_id:
                return link
        raise ValidationError("Goal link does not exist.")

    def _ensure_unique_goal_links(self, links: list[GoalLink]) -> None:
        seen: set[tuple[EntityType, UUID, GoalRelation, str | None]] = set()
        for link in links:
            key = self._link_key(link)
            if key in seen:
                raise ValidationError("Duplicate goal link.")
            seen.add(key)

    def _link_key(
        self,
        link: GoalLink,
    ) -> tuple[EntityType, UUID, GoalRelation, str | None]:
        return (
            link.target.entity_type,
            link.target.entity_id,
            link.relation,
            self._clean_slot(link.slot),
        )

    @staticmethod
    def _clean_slot(slot: str | None) -> str | None:
        if slot is None:
            return None
        cleaned = slot.strip()
        return cleaned or None


class GoalLinkSpec:
    def __init__(
        self,
        *,
        target: EntityRef,
        relation: GoalRelation,
        link_status: GoalLinkStatus | None = None,
        slot: str | None = None,
    ) -> None:
        self.target = target
        self.relation = relation
        self.link_status = link_status
        self.slot = slot
