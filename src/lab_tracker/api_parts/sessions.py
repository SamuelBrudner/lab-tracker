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
    Session,
    UsageEventResourceType,
    UsageEventVerb,
)

if TYPE_CHECKING:
    from lab_tracker.services import SessionService

UsageResultT = TypeVar("UsageResultT")


class SessionsApiMixin:
    if TYPE_CHECKING:
        sessions: SessionService

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

    def create_session(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.create_session(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.SESSION,
            actor=kwargs.get("actor"),
            resource_id_attr="session_id",
        )

    def get_session(self, session_id: UUID) -> Session:
        return self.sessions.get_session(session_id)

    def get_session_by_link_code(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.get_session_by_link_code(*args, **kwargs)

    def list_sessions(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.list_sessions(*args, **kwargs)

    def update_session(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.update_session(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.SESSION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="session_id",
        )

    def delete_session(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.delete_session(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.SESSION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="session_id",
        )

    def register_acquisition_output(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.register_acquisition_output(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.ACQUISITION_OUTPUT,
            actor=kwargs.get("actor"),
            resource_id_attr="output_id",
            project_id_attr=None,
        )

    def list_acquisition_outputs(self, *args: Any, **kwargs: Any) -> Any:
        return self.sessions.list_acquisition_outputs(*args, **kwargs)

    def delete_acquisition_output(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.delete_acquisition_output(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.ACQUISITION_OUTPUT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="output_id",
            project_id_attr=None,
        )

    def promote_operational_session(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.promote_operational_session(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.SESSION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="session_id",
        )

    def promote_operational_session_to_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.sessions.promote_operational_session_to_dataset(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.DATASET,
            actor=kwargs.get("actor"),
            resource_id_attr="dataset_id",
        )
