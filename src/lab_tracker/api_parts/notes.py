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
from lab_tracker.models import (
    Note,
    UsageEventResourceType,
    UsageEventVerb,
)


class NotesApiMixin:
    def create_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.create_note(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id_attr="note_id",
        )

    def create_note_result(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.create_note_result(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id_attr="note_id",
        )

    def store_note_raw_asset(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.store_note_raw_asset(*args, **kwargs)

    def find_note_by_client_capture_id(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.find_note_by_client_capture_id(*args, **kwargs)

    def upload_note_raw(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.upload_note_raw(*args, **kwargs),
            verb=UsageEventVerb.UPLOAD,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id_attr="note_id",
        )

    def upload_note_raw_result(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.upload_note_raw_result(*args, **kwargs),
            verb=UsageEventVerb.UPLOAD,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id_attr="note_id",
        )

    def transcribe_voice_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.transcribe_voice_note(*args, **kwargs),
            verb=UsageEventVerb.TRANSCRIBE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id_attr="note_id",
        )

    def get_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.get_note(*args, **kwargs)

    def list_notes(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.list_notes(*args, **kwargs)

    def update_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.update_note(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="note_id",
        )

    def archive_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.archive_note(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="note_id",
        )

    def download_note_raw(self, *args: Any, **kwargs: Any) -> Any:
        return self.notes.download_note_raw(*args, **kwargs)

    def delete_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.notes.delete_note(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.NOTE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="note_id",
        )

    def search_notes(
        self,
        query: str,
        *,
        project_id: UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Note]:
        repository = self._service_context.active_repository()
        notes, _ = repository.query_notes(
            project_id=project_id,
            search=query,
            limit=limit,
            offset=offset,
        )
        return notes
