"""Per-domain delegation mixins for LabTrackerAPI.

Split out of api.py to keep the facade file cohesive and give each domain
its own edit locality. These are mixins: LabTrackerAPI inherits them, so
``self`` exposes the composed services and the usage-telemetry helpers
(``_with_usage_event``, ``record_usage_event``) defined on the facade.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from lab_tracker.api_parts._base import _first_uuid
from lab_tracker.auth import AuthContext
from lab_tracker.models import (
    Goal,
    UsageEventResourceType,
    UsageEventVerb,
)

if TYPE_CHECKING:
    from lab_tracker.services import GoalService

UsageResultT = TypeVar("UsageResultT")


class GoalsApiMixin:
    if TYPE_CHECKING:
        goals: GoalService

        def _with_usage_event(
            self,
            action: Callable[[], UsageResultT],
            *,
            verb: UsageEventVerb,
            resource_type: UsageEventResourceType,
            actor: AuthContext | None = None,
            resource_id: UUID | None = None,
            project_id: UUID | None = None,
            resource_id_attr: str | None = None,
            project_id_attr: str | None = "project_id",
        ) -> UsageResultT: ...

    def create_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.create_goal(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GOAL,
            actor=kwargs.get("actor"),
            resource_id_attr="goal_id",
        )

    def get_goal(self, goal_id: UUID) -> Goal:
        return self.goals.get_goal(goal_id)

    def require_goal_read(
        self,
        goal: Goal,
        *,
        actor: AuthContext | None = None,
    ) -> set[UUID]:
        return self.goals.require_goal_read(goal, actor=actor)

    def list_goals(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.list_goals(*args, **kwargs)

    def update_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.update_goal(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GOAL,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="goal_id",
        )

    def delete_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.delete_goal(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.GOAL,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="goal_id",
        )

    def link_node_to_goal(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.link_node_to_goal(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GOAL_LINK,
            actor=kwargs.get("actor"),
            resource_id_attr="link_id",
            project_id_attr=None,
        )

    def update_goal_link(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.update_goal_link(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GOAL_LINK,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="link_id",
            project_id_attr=None,
        )

    def delete_goal_link(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.goals.delete_goal_link(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.GOAL_LINK,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="link_id",
            project_id_attr=None,
        )

    def list_node_goals(self, *args: Any, **kwargs: Any) -> Any:
        return self.goals.list_node_goals(*args, **kwargs)
