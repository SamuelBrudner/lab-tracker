"""Per-domain delegation mixins for LabTrackerAPI.

Split out of api.py to keep the facade file cohesive and give each domain
its own edit locality. These are mixins: LabTrackerAPI inherits them, so
``self`` exposes the composed services and the usage-telemetry helpers
(``_with_usage_event``, ``record_usage_event``) defined on the facade.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lab_tracker.api_parts._base import _first_uuid
from lab_tracker.auth import AuthContext
from lab_tracker.models import (
    ExplorationNode,
    UsageEventResourceType,
    UsageEventVerb,
)


class ExplorationApiMixin:
    def create_exploration_node(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.exploration.create_exploration_node(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.EXPLORATION_NODE,
            actor=kwargs.get("actor"),
            resource_id_attr="node_id",
        )

    def get_exploration_node(self, *args: Any, **kwargs: Any) -> Any:
        return self.exploration.get_exploration_node(*args, **kwargs)

    def get_exploration_node_for_read(
        self,
        node_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> ExplorationNode:
        return self.exploration.get_exploration_node_for_read(
            node_id,
            actor=actor,
        )

    def list_exploration_nodes(self, *args: Any, **kwargs: Any) -> Any:
        return self.exploration.list_exploration_nodes(*args, **kwargs)

    def update_exploration_node(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.exploration.update_exploration_node(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.EXPLORATION_NODE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="node_id",
        )

    def delete_exploration_node(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.exploration.delete_exploration_node(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.EXPLORATION_NODE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="node_id",
        )
