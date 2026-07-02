"""Per-domain delegation mixins for LabTrackerAPI.

Split out of api.py to keep the facade file cohesive and give each domain
its own edit locality. These are mixins: LabTrackerAPI inherits them, so
``self`` exposes the composed services and the usage-telemetry helpers
(``_with_usage_event``, ``record_usage_event``) defined on the facade.
"""

from __future__ import annotations

from typing import Any


class UsageApiMixin:
    def query_usage_events(self, *args: Any, **kwargs: Any) -> Any:
        repository = self._service_context.active_repository()
        return repository.query_usage_events(*args, **kwargs)

    def usage_event_summary(self, *args: Any, **kwargs: Any) -> Any:
        repository = self._service_context.active_repository()
        return repository.usage_event_summary(*args, **kwargs)

    def rollup_usage_events_before(self, *args: Any, **kwargs: Any) -> Any:
        repository = self._service_context.active_repository()
        return repository.rollup_usage_events_before(*args, **kwargs)
