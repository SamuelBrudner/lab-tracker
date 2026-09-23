"""Per-project capture health: which automated capture paths are still alive.

Automated capture fails quietly: a scheduler entry disappears after an OS
upgrade, a token expires, a watched folder moves, and nothing complains. The
review queue simply gets emptier. This report makes the silence visible by
grouping a project's recent notes by the adapter and host that captured them
and flagging any source that used to capture but has gone quiet.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.models import (
    CaptureHealthReport,
    CaptureHealthSource,
    Note,
    NoteStatus,
    Project,
)
from lab_tracker.services.base import BaseService, ServiceContext

DEFAULT_WINDOW_DAYS = 30
RECENT_DAYS = 7
MANUAL_SOURCE = "manual"


class ProjectReadAccess(Protocol):
    def get_project_for_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Project: ...


def capture_source_key(note: Note) -> tuple[str, str]:
    """(adapter, host label) a note was captured through.

    Client adapters stamp ``evidence_adapter``; the phone and share target
    stamp ``capture_source``; anything else is a manual browser or API note.
    """

    metadata = note.metadata or {}
    adapter = (
        str(metadata.get("evidence_adapter") or "").strip()
        or str(metadata.get("capture_source") or "").strip()
        or MANUAL_SOURCE
    )
    host = str(metadata.get("capture_host_label") or "").strip()
    return adapter, host


class CaptureHealthService(BaseService):
    def __init__(self, context: ServiceContext, *, projects: ProjectReadAccess) -> None:
        super().__init__(context)
        self.projects = projects

    def report(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None,
    ) -> CaptureHealthReport:
        # Keeps the opaque project boundary: an unreadable project is a 404.
        self.projects.get_project_for_read(project_id, actor=actor)
        generated_at = _as_utc(now or datetime.now(timezone.utc))
        window_days = max(1, int(window_days))
        since = generated_at - timedelta(days=window_days)
        recent_cutoff = generated_at - timedelta(days=RECENT_DAYS)
        notes, _total = self.repository.query_notes(
            project_id=project_id,
            since=since,
            limit=None,
            offset=0,
        )
        grouped: dict[tuple[str, str], list[Note]] = defaultdict(list)
        for note in notes:
            grouped[capture_source_key(note)].append(note)
        sources: list[CaptureHealthSource] = []
        for (adapter, host), items in grouped.items():
            last = max(_as_utc(item.created_at) for item in items)
            sources.append(
                CaptureHealthSource(
                    adapter=adapter,
                    host_label=host,
                    last_captured_at=last,
                    captured_recent=sum(
                        1 for item in items if _as_utc(item.created_at) >= recent_cutoff
                    ),
                    captured_window=len(items),
                    staged_unreviewed=sum(
                        1 for item in items if item.status == NoteStatus.STAGED
                    ),
                    # Captured earlier in the window but nothing lately: the
                    # path may have stalled, and only a person can tell.
                    quiet=adapter != MANUAL_SOURCE and last < recent_cutoff,
                )
            )
        sources.sort(key=lambda item: item.last_captured_at, reverse=True)
        return CaptureHealthReport(
            project_id=project_id,
            generated_at=generated_at,
            window_days=window_days,
            recent_days=RECENT_DAYS,
            sources=sources,
            captured_window=len(notes),
            staged_unreviewed=sum(1 for note in notes if note.status == NoteStatus.STAGED),
            quiet_sources=sum(1 for source in sources if source.quiet),
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["CaptureHealthService", "DEFAULT_WINDOW_DAYS", "RECENT_DAYS", "capture_source_key"]
