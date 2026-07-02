"""Per-domain delegation mixins for LabTrackerAPI.

Split out of api.py to keep the facade file cohesive and give each domain
its own edit locality. These are mixins: LabTrackerAPI inherits them, so
``self`` exposes the composed services and the usage-telemetry helpers
(``_with_usage_event``, ``record_usage_event``) defined on the facade.
"""

from __future__ import annotations

from typing import Any

from lab_tracker.api_parts._base import _first_uuid
from lab_tracker.models import (
    UsageEventResourceType,
    UsageEventVerb,
)


class ProjectsApiMixin:
    def create_project(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.create_project(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.PROJECT,
            actor=kwargs.get("actor"),
            resource_id_attr="project_id",
            project_id_attr="project_id",
        )

    def get_project(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project(*args, **kwargs)

    def list_projects(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_projects(*args, **kwargs)

    def update_project(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.update_project(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROJECT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id=_first_uuid(args),
            resource_id_attr="project_id",
            project_id_attr="project_id",
        )

    def delete_project(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.delete_project(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.PROJECT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id=_first_uuid(args),
            resource_id_attr="project_id",
            project_id_attr="project_id",
        )

    def create_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.create_project_group(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.PROJECT_GROUP,
            actor=kwargs.get("actor"),
            resource_id_attr="group_id",
            project_id_attr=None,
        )

    def get_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project_group(*args, **kwargs)

    def list_project_groups(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_project_groups(*args, **kwargs)

    def update_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.update_project_group(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROJECT_GROUP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="group_id",
            project_id_attr=None,
        )

    def delete_project_group(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.delete_project_group(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.PROJECT_GROUP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="group_id",
            project_id_attr=None,
        )

    def get_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project_membership(*args, **kwargs)

    def get_project_membership_for_user(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_project_membership_for_user(*args, **kwargs)

    def list_project_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_project_memberships(*args, **kwargs)

    def upsert_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.upsert_project_membership(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROJECT_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id_attr="membership_id",
        )

    def update_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.update_project_membership(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROJECT_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="membership_id",
        )

    def delete_project_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.delete_project_membership(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.PROJECT_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="membership_id",
        )

    def get_group_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_group_membership(*args, **kwargs)

    def get_group_membership_for_user(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.get_group_membership_for_user(*args, **kwargs)

    def list_group_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self.projects.list_group_memberships(*args, **kwargs)

    def upsert_group_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.upsert_group_membership(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GROUP_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id_attr="membership_id",
            project_id_attr=None,
        )

    def delete_group_membership(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.delete_group_membership(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.GROUP_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id_attr="membership_id",
            project_id_attr=None,
        )

    def upsert_group_project_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.upsert_group_project_memberships(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROJECT_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id_attr=None,
        )

    def delete_group_project_memberships(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.projects.delete_group_project_memberships(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.PROJECT_MEMBERSHIP,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id_attr=None,
        )

    def accessible_project_ids(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.accessible_project_ids(*args, **kwargs)

    def project_membership_role(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.membership_role(*args, **kwargs)

    def require_project_read(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.require_read(*args, **kwargs)

    def require_project_contributor(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.require_contributor(*args, **kwargs)

    def require_project_owner(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.require_owner(*args, **kwargs)

    def group_membership_role(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.group_membership_role(*args, **kwargs)

    def require_group_read(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.require_group_read(*args, **kwargs)

    def require_group_owner(self, *args: Any, **kwargs: Any) -> Any:
        return self.project_authorization.require_group_owner(*args, **kwargs)
