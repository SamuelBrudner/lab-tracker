"""Distinguish a capture's original source from the person who reviews it."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from lab_tracker.auth import LOCAL_AUTH_USER_ID
from lab_tracker.models import EntityType, Note, Session

AssignmentScope = Literal["note", "session"]
RoutingStatus = Literal[
    "assigned", "needs_session", "ambiguous_session", "needs_operator", "operator_unavailable"
]
LEGACY_MACHINE_CHANNELS = frozenset({"ics", "store_scan"})


class ReviewOperatorChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID | None


class ReviewOperatorAssignment(BaseModel):
    scope: AssignmentScope
    work_id: UUID
    user_id: UUID | None
    assigned_by_user_id: UUID | None
    assigned_at: datetime


class CaptureRoute(BaseModel):
    note_id: UUID
    status: RoutingStatus
    session_ids: tuple[UUID, ...] = ()
    operator: ReviewOperatorAssignment | None = None


class UnassignedMachineCapture(BaseModel):
    note_id: UUID
    title: str
    source: str
    captured_at: datetime
    route: CaptureRoute
    clock_sessions: list[UUID] = Field(default_factory=list)


def is_machine_capture(note: Note) -> bool:
    if note.machine_captured is not None:
        return note.machine_captured
    # The fixed local automation ID was shared with the auth-disabled user.
    # Recognize legacy machine captures only when a server integration is also named.
    return note.created_by == str(LOCAL_AUTH_USER_ID) and (
        note.origin_provider in LEGACY_MACHINE_CHANNELS
        or note.metadata.get("capture_channel") in LEGACY_MACHINE_CHANNELS
    )


def machine_capture_source(note: Note) -> str:
    return (
        note.capture_source
        or note.metadata.get("capture_channel")
        or note.origin_provider
        or "automation"
    )[:80]


def session_targets(note: Note) -> tuple[UUID, ...]:
    return tuple(
        sorted(
            {
                target.entity_id
                for target in note.targets
                if target.entity_type == EntityType.SESSION
            }
        )
    )


def review_session_for_capture(note: Note) -> UUID | None:
    if note.review_session_id is not None:
        return note.review_session_id
    if note.machine_captured is None and note.change_set_id is None:
        legacy = session_targets(note)
        if len(legacy) == 1:
            return legacy[0]
    return None


def overlapping_sessions(observed_at: datetime, sessions: list[Session]) -> list[Session]:
    observed_at = _with_timezone(observed_at)
    return [
        session
        for session in sessions
        if _with_timezone(session.started_at) <= observed_at
        and (session.ended_at is None or observed_at <= _with_timezone(session.ended_at))
    ]


def _with_timezone(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
