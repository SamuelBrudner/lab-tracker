"""Pure policies for graph-draft batch identity, windows, and scheduling."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.member_onboarding import (
    SCHEDULED_DRAFT_EXCLUDE,
    SCHEDULED_DRAFT_POLICY_KEY,
)
from lab_tracker.models import (
    GraphChangeSetStatus,
    GraphDraftBatchSettings,
    Note,
    NoteStatus,
    utc_now,
)
from lab_tracker.services.shared import actor_user_id


@dataclass(frozen=True)
class BatchReviewer:
    reviewer: str | None
    reviewer_user_id: UUID | None


BATCH_NOTE_LIMIT = 100
DEFAULT_BATCH_CADENCE_MINUTES = 24 * 60
DEFAULT_BATCH_RUN_TIME = "18:00"
DEFAULT_BATCH_TIMEZONE = "America/New_York"
ACTIVE_BATCH_CHANGE_SET_STATUSES = {
    GraphChangeSetStatus.DRAFTING,
    GraphChangeSetStatus.READY,
    GraphChangeSetStatus.SUBMITTED,
    GraphChangeSetStatus.CHANGES_REQUESTED,
    GraphChangeSetStatus.COMMITTING,
}


def make_batch_key(
    *,
    project_id: UUID,
    since: datetime,
    until: datetime,
    note_ids: list[UUID],
    review_assignee: str | None = None,
    review_assignee_user_id: UUID | None = None,
) -> str:
    payload = {
        "project_id": str(project_id),
        "since": as_utc(since).isoformat(),
        "until": as_utc(until).isoformat(),
        "note_ids": [str(note_id) for note_id in note_ids],
        "review_assignee": review_assignee,
        "review_assignee_user_id": (
            str(review_assignee_user_id) if review_assignee_user_id is not None else None
        ),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"batch:{digest[:48]}"


def make_reserved_batch_key(
    *,
    project_id: UUID,
    note_ids: list[UUID],
    review_assignee: str | None,
    review_assignee_user_id: UUID | None,
    generation_run_id: UUID | None,
) -> str:
    """Return a stable key for one reviewer-note reservation generation.

    Wall-clock window ends are intentionally absent: concurrent preparations
    that observe the same note set and generation must collide on one key. The
    caller advances ``generation_run_id`` only for a failed retry or an empty
    cursor window; successful non-empty explicit windows remain idempotent.
    """

    payload = {
        "project_id": str(project_id),
        "note_ids": [str(note_id) for note_id in note_ids],
        "review_assignee": review_assignee,
        "review_assignee_user_id": (
            str(review_assignee_user_id) if review_assignee_user_id is not None else None
        ),
        "generation_run_id": (
            str(generation_run_id) if generation_run_id is not None else None
        ),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"batch:{digest[:48]}"


def default_batch_settings(
    *,
    project_id: UUID,
    user_id: UUID | None = None,
    actor: AuthContext | None = None,
    inherit_from: GraphDraftBatchSettings | None = None,
) -> GraphDraftBatchSettings:
    return GraphDraftBatchSettings(
        settings_id=uuid4(),
        project_id=project_id,
        user_id=user_id,
        enabled=inherit_from.enabled if inherit_from is not None else False,
        cadence_minutes=(
            inherit_from.cadence_minutes
            if inherit_from is not None
            else DEFAULT_BATCH_CADENCE_MINUTES
        ),
        run_at_local_time=(
            inherit_from.run_at_local_time
            if inherit_from is not None
            else DEFAULT_BATCH_RUN_TIME
        ),
        timezone_name=(
            inherit_from.timezone_name if inherit_from is not None else DEFAULT_BATCH_TIMEZONE
        ),
        next_run_at=(
            inherit_from.next_run_at
            if inherit_from is not None and inherit_from.enabled
            else None
        ),
        updated_by=actor_user_id(actor),
    )


def validate_run_at_local_time(value: str) -> None:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValidationError("run_at_local_time must be HH:MM.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValidationError("run_at_local_time must be HH:MM.") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValidationError("run_at_local_time must be a valid 24-hour HH:MM time.")


def zoneinfo_for(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError(f"Unknown timezone: {timezone_name}") from exc


def next_run_at(
    *,
    cadence_minutes: int,
    run_at_local_time: str,
    timezone_name: str,
    now: datetime | None = None,
) -> datetime:
    validate_run_at_local_time(run_at_local_time)
    zone = zoneinfo_for(timezone_name)
    current = as_utc(now or utc_now()).astimezone(zone)
    hour, minute = (int(part) for part in run_at_local_time.split(":"))
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    cadence = timedelta(minutes=cadence_minutes)
    while candidate <= current:
        candidate += cadence
    return candidate.astimezone(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def staged_notes_in_window(
    notes: list[Note],
    *,
    since: datetime,
    until: datetime,
    include_start: bool = False,
    exclude_note_ids: set[UUID] | None = None,
    reviewer: BatchReviewer | None = None,
) -> list[Note]:
    start = as_utc(since)
    end = as_utc(until)
    excluded = exclude_note_ids or set()
    return sorted(
        [
            note
            for note in notes
            if note.status == NoteStatus.STAGED
            and note.metadata.get(SCHEDULED_DRAFT_POLICY_KEY)
            != SCHEDULED_DRAFT_EXCLUDE
            and note.note_id not in excluded
            and note_matches_reviewer(note, reviewer)
            and (
                start <= as_utc(note.created_at)
                if include_start
                else start < as_utc(note.created_at)
            )
            and as_utc(note.created_at) <= end
        ],
        key=lambda item: (item.created_at, str(item.note_id)),
    )


def reviewer_for_note(note: Note) -> BatchReviewer:
    if note.created_by_user_id is not None:
        return BatchReviewer(
            reviewer=str(note.created_by_user_id),
            reviewer_user_id=note.created_by_user_id,
        )
    return BatchReviewer(reviewer=note.created_by, reviewer_user_id=None)


def note_matches_reviewer(note: Note, reviewer: BatchReviewer | None) -> bool:
    if reviewer is None:
        return True
    if reviewer.reviewer_user_id is not None:
        return note.created_by_user_id == reviewer.reviewer_user_id
    if reviewer.reviewer is not None:
        return note.created_by == reviewer.reviewer
    return note.created_by is None and note.created_by_user_id is None


def limit_notes_to_draft(
    notes: list[Note],
    *,
    window_end: datetime,
) -> tuple[list[Note], datetime]:
    if len(notes) <= BATCH_NOTE_LIMIT:
        return notes, window_end
    limited = notes[:BATCH_NOTE_LIMIT]
    return limited, as_utc(limited[-1].created_at)
