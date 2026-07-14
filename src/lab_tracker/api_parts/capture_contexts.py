"""Capture context API delegation mixin."""

from __future__ import annotations

from typing import Any

from lab_tracker.api_parts._base import _first_uuid
from lab_tracker.models import UsageEventResourceType, UsageEventVerb


class CaptureContextsApiMixin:
    def create_capture_context(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.capture_contexts.create_capture_context(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.CAPTURE_CONTEXT,
            actor=kwargs.get("actor"),
            resource_id_attr="capture_context_id",
        )

    def get_capture_context(self, *args: Any, **kwargs: Any) -> Any:
        return self.capture_contexts.get_capture_context(*args, **kwargs)

    def list_capture_contexts(self, *args: Any, **kwargs: Any) -> Any:
        return self.capture_contexts.list_capture_contexts(*args, **kwargs)

    def update_capture_context(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.capture_contexts.update_capture_context(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.CAPTURE_CONTEXT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="capture_context_id",
        )

    def revoke_capture_context(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.capture_contexts.revoke_capture_context(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.CAPTURE_CONTEXT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="capture_context_id",
        )
