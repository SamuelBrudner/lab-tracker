"""Session suggestions: computed on read, applied only by a person.

Bench work happens in time and a session is the unit it is cut into, but
starting and closing sessions is exactly the bookkeeping a busy person forgets.
This module looks at what the project already recorded and suggests the
missing session bookkeeping; nothing here writes anything. A person applies a
suggestion from the app through the ordinary session create/update APIs (and,
only on their click, the ordinary note-target API), or dismisses it on their
device.

Three deterministic kinds, each with a stable id so a dismissal sticks:

``close_quiet_session``
    An active session whose most recent capture (a note targeting it, or the
    source of a proposed or accepted provenance link to it) is older than
    :data:`QUIET_SESSION_THRESHOLD`: suggest ending it at that capture's time.
    A session with no captures at all is left alone -- there is no honest
    time to end it at. Id: session plus last capture, so new captures after a
    dismissal raise a fresh suggestion.
``start_session_from_captures``
    A local day with at least :data:`MIN_CAPTURES_FOR_SESSION` of one person's
    staged captures that name no session and fall inside no window of a
    session that person ran: suggest a session spanning the first to the last
    of them. Only the reader's own captures are offered (applying records the
    session as the reader, and time only ties a capture to its own author's
    session); captures with no recorded author count as anyone's. Id: project,
    local day, and author, so a dismissed day stays dismissed as more captures
    arrive.
``start_session_from_booking``
    An instrument booking note (``booking_start``/``booking_end`` metadata,
    written by a calendar integration) that has begun, within the lookback,
    and whose window no session overlaps: suggest a session for the booking
    window, listing the booking note and the sessionless captures made in it.
    Id: project plus booking uid (else the booking note id).

Days are local days in the zone the project's daily review runs in (the
person's own review settings, then the project default, else UTC). Captures
are read with the same clock as the time-window detector:
``format_acquired_at`` first, then the note's observed time.

Every read is bounded: candidate captures from the last
:data:`SUGGESTION_LOOKBACK_DAYS` days (filtered in SQL), booking notes synced in
the last :data:`BOOKING_NOTE_LOOKBACK_DAYS` days, only the provenance links
that target an active session, and one
:class:`~lab_tracker.services.session_clock.SessionTimeline` sweep for every
window lookup.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from enum import Enum
from typing import Final
from uuid import UUID

from pydantic import BaseModel, Field

from lab_tracker.auth import AuthContext
from lab_tracker.member_onboarding import is_member_checkpoint
from lab_tracker.models import (
    EntityType,
    Note,
    NoteStatus,
    ProvenanceLink,
    ProvenanceLinkStatus,
    Session,
    SessionStatus,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.provenance_time_windows import TIME_WINDOW_NOTE_ORIGINS
from lab_tracker.services.session_clock import (
    SESSION_HINT_METADATA_KEYS,
    SessionTimeline,
    author_key,
    capture_clock,
    carries_session,
    eligible_sessions,
    is_booking_note,
    parse_format_acquired_at,
    resolve_capture_timezone,
    session_label,
)
from lab_tracker.services.shared import actor_user_fk, actor_user_id

# An open session with no capture for this long reads as forgotten.
QUIET_SESSION_THRESHOLD: Final = timedelta(hours=4)
# How far back (by note creation, and by booking start) suggestions look.
SUGGESTION_LOOKBACK_DAYS: Final = 14
# How long before its start a booking note may have been synced and still be
# read: calendar integrations sync a rolling window of upcoming bookings.
BOOKING_NOTE_LOOKBACK_DAYS: Final = 90
# Sessionless captures on one local day before a session is suggested.
MIN_CAPTURES_FOR_SESSION: Final = 3
# Response bounds: suggestions per read, capture ids listed per suggestion,
# and the most recent notes read per active session.
MAX_SUGGESTIONS: Final = 50
MAX_LISTED_CAPTURES: Final = 200
MAX_NOTES_PER_ACTIVE_SESSION: Final = 500

BOOKING_UID_KEY: Final = "booking_uid"
BOOKING_START_KEY: Final = "booking_start"
BOOKING_END_KEY: Final = "booking_end"
BOOKING_INSTRUMENT_KEY: Final = "booking_instrument"
BOOKING_SUMMARY_KEY: Final = "booking_summary"


class SessionSuggestionKind(str, Enum):
    """What a session suggestion asks a person to do."""

    CLOSE_QUIET_SESSION = "close_quiet_session"
    START_SESSION_FROM_CAPTURES = "start_session_from_captures"
    START_SESSION_FROM_BOOKING = "start_session_from_booking"


class SessionSuggestion(BaseModel):
    """One deterministic session suggestion; applying it is a person's click."""

    suggestion_id: str = Field(
        description="Stable id for this suggestion; a dismissal is keyed by it."
    )
    kind: SessionSuggestionKind
    title: str
    detail: str
    session_id: UUID | None = Field(
        default=None, description="The active session to close (close_quiet_session)."
    )
    start_at: datetime | None = Field(
        default=None, description="Suggested session start (start_* kinds)."
    )
    end_at: datetime | None = Field(
        default=None,
        description=(
            "Suggested session end. A start_* suggestion whose end is still in the "
            "future (an ongoing booking) is applied as an open session."
        ),
    )
    local_date: date | None = Field(
        default=None, description="The local day the captures fall on (captures kind)."
    )
    capture_count: int = 0
    capture_note_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "Captures the suggestion is based on (at most 200), in capture order. "
            "For start_* kinds a person may choose to attach them to the new session."
        ),
    )
    booking_note_id: UUID | None = None
    booking_uid: str | None = None
    booking_instrument: str | None = None
    booking_summary: str | None = None


class SessionSuggestionReport(BaseModel):
    """The session suggestions for one project, computed at ``generated_at``."""

    project_id: UUID
    generated_at: datetime
    timezone: str = Field(description="Zone used for local days and clock labels.")
    quiet_threshold_minutes: int
    lookback_days: int
    min_captures_per_day: int
    suggestions: list[SessionSuggestion] = Field(default_factory=list)


@dataclass(frozen=True)
class _Capture:
    note: Note
    at: datetime


@dataclass(frozen=True)
class _Booking:
    note: Note
    uid: str | None
    start: datetime
    end: datetime
    instrument: str | None
    summary: str | None


def _stable_id(kind: SessionSuggestionKind, *parts: object) -> str:
    return ":".join([kind.value, *(str(part) for part in parts)])


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _clock(value: datetime, zone: tzinfo) -> str:
    return value.astimezone(zone).strftime("%H:%M")


def _day_label(value: datetime, zone: tzinfo) -> str:
    return value.astimezone(zone).date().isoformat()


def _captures(notes: Iterable[Note]) -> list[_Capture]:
    return sorted(
        (_Capture(note=note, at=capture_clock(note).at) for note in notes),
        key=lambda item: (item.at, str(item.note.note_id)),
    )


def _metadata_text(note: Note, key: str) -> str | None:
    value = str(note.metadata.get(key) or "").strip()
    return value or None


def booking_from_note(note: Note) -> _Booking | None:
    """The booking a note describes, or None when its window does not parse."""

    start = parse_format_acquired_at(note.metadata.get(BOOKING_START_KEY))
    end = parse_format_acquired_at(note.metadata.get(BOOKING_END_KEY))
    if start is None or end is None or end <= start:
        return None
    return _Booking(
        note=note,
        uid=_metadata_text(note, BOOKING_UID_KEY),
        start=start,
        end=end,
        instrument=_metadata_text(note, BOOKING_INSTRUMENT_KEY),
        summary=_metadata_text(note, BOOKING_SUMMARY_KEY),
    )


def is_sessionless_capture(note: Note) -> bool:
    """A staged capture a person (or their agent) made that names no session."""

    return (
        note.status == NoteStatus.STAGED
        and note.origin in TIME_WINDOW_NOTE_ORIGINS
        and not is_member_checkpoint(note)
        and not is_booking_note(note)
        and not carries_session(note)
    )


def close_quiet_session_suggestions(
    sessions: Sequence[Session],
    captures_by_session: dict[UUID, list[Note]],
    *,
    now: datetime,
    zone: tzinfo,
    threshold: timedelta = QUIET_SESSION_THRESHOLD,
) -> list[SessionSuggestion]:
    """Active sessions whose last capture is older than ``threshold``."""

    suggestions: list[SessionSuggestion] = []
    active = sorted(
        (session for session in sessions if session.status == SessionStatus.ACTIVE),
        key=lambda session: (session.started_at, str(session.session_id)),
    )
    for session in active:
        captures = _captures(captures_by_session.get(session.session_id, []))
        if not captures:
            continue
        last = captures[-1]
        if now - last.at < threshold:
            continue
        end_at = max(last.at, session.started_at)
        quiet_hours = int((now - last.at).total_seconds() // 3600)
        suggestions.append(
            SessionSuggestion(
                suggestion_id=_stable_id(
                    SessionSuggestionKind.CLOSE_QUIET_SESSION,
                    session.session_id,
                    last.note.note_id,
                ),
                kind=SessionSuggestionKind.CLOSE_QUIET_SESSION,
                title=(
                    f"End the {session_label(session)} "
                    f"at {_clock(end_at, zone)} on {_day_label(end_at, zone)}"
                ),
                detail=(
                    f"Still open, but its last capture was {quiet_hours} h ago "
                    f"({len(captures)} capture{'s' if len(captures) != 1 else ''} in all)."
                ),
                session_id=session.session_id,
                end_at=end_at,
                capture_count=1,
                capture_note_ids=[last.note.note_id],
            )
        )
    return suggestions


def booking_suggestions(
    bookings: Iterable[_Booking],
    timeline: SessionTimeline,
    sessionless: Sequence[_Capture],
    *,
    project_id: UUID,
    now: datetime,
    zone: tzinfo,
    lookback_start: datetime,
) -> list[SessionSuggestion]:
    """Bookings that have begun and that no session window overlaps.

    One suggestion per booking uid (the most recently created note wins when a
    calendar sync wrote the same booking twice).
    """

    latest: dict[str, _Booking] = {}
    for booking in bookings:
        key = booking.uid or str(booking.note.note_id)
        current = latest.get(key)
        if current is None or (booking.note.created_at, str(booking.note.note_id)) > (
            current.note.created_at,
            str(current.note.note_id),
        ):
            latest[key] = booking
    suggestions: list[SessionSuggestion] = []
    for key, booking in sorted(latest.items(), key=lambda item: (item[1].start, item[0])):
        if booking.start > now or booking.start < lookback_start:
            continue
        if carries_session(booking.note):
            continue
        if timeline.overlaps(booking.start, booking.end):
            continue
        inside = [
            capture.note.note_id
            for capture in sessionless
            if booking.start <= capture.at <= booking.end
        ]
        listed = [booking.note.note_id, *inside]
        instrument = booking.instrument or "instrument"
        suggestions.append(
            SessionSuggestion(
                suggestion_id=_stable_id(
                    SessionSuggestionKind.START_SESSION_FROM_BOOKING,
                    project_id,
                    _short_hash(key),
                ),
                kind=SessionSuggestionKind.START_SESSION_FROM_BOOKING,
                title=(
                    f"Start a session for the {instrument} booking "
                    f"{_clock(booking.start, zone)}-{_clock(booking.end, zone)} "
                    f"on {_day_label(booking.start, zone)}"
                ),
                detail=(
                    "No session covers this booking"
                    + (f" ({booking.summary})" if booking.summary else "")
                    + (
                        f"; {len(inside)} sessionless capture"
                        f"{'s' if len(inside) != 1 else ''} fall inside it."
                        if inside
                        else "."
                    )
                ),
                start_at=booking.start,
                end_at=booking.end,
                capture_count=len(listed),
                capture_note_ids=listed[:MAX_LISTED_CAPTURES],
                booking_note_id=booking.note.note_id,
                booking_uid=booking.uid,
                booking_instrument=booking.instrument,
                booking_summary=booking.summary,
            )
        )
    return suggestions


def capture_day_suggestions(
    sessionless: Sequence[_Capture],
    *,
    project_id: UUID,
    zone: tzinfo,
    covered: Sequence[tuple[datetime, datetime]] = (),
    min_captures: int = MIN_CAPTURES_FOR_SESSION,
    viewer: str | None = None,
) -> list[SessionSuggestion]:
    """Local days with at least ``min_captures`` of one person's sessionless captures.

    Captures are grouped per day and author (a capture with no recorded author
    joins ``viewer``'s group), so one person's day never borrows a colleague's
    captures. Captures inside a ``covered`` window (a booking already being
    suggested) are left to that suggestion.
    """

    by_day: dict[tuple[date, str], list[_Capture]] = defaultdict(list)
    for capture in sessionless:
        if any(start <= capture.at <= end for start, end in covered):
            continue
        author = author_key(capture.note) or viewer or ""
        by_day[(capture.at.astimezone(zone).date(), author)].append(capture)
    suggestions: list[SessionSuggestion] = []
    for day, author in sorted(by_day):
        captures = by_day[(day, author)]
        if len(captures) < min_captures:
            continue
        first, last = captures[0], captures[-1]
        suggestions.append(
            SessionSuggestion(
                suggestion_id=_stable_id(
                    SessionSuggestionKind.START_SESSION_FROM_CAPTURES,
                    project_id,
                    day.isoformat(),
                    *((author,) if author else ()),
                ),
                kind=SessionSuggestionKind.START_SESSION_FROM_CAPTURES,
                title=(
                    f"Record a session for {day.isoformat()} "
                    f"{_clock(first.at, zone)}-{_clock(last.at, zone)}"
                ),
                detail=(f"{len(captures)} captures that day are in no session and name none."),
                start_at=first.at,
                end_at=last.at,
                local_date=day,
                capture_count=len(captures),
                capture_note_ids=[
                    capture.note.note_id for capture in captures[:MAX_LISTED_CAPTURES]
                ],
            )
        )
    return suggestions


def suggest_sessions(
    *,
    project_id: UUID,
    sessions: Sequence[Session],
    captures_by_session: dict[UUID, list[Note]],
    candidate_notes: Iterable[Note],
    booking_notes: Iterable[Note],
    now: datetime,
    zone: tzinfo,
    viewer: str | None = None,
) -> list[SessionSuggestion]:
    """Every suggestion for one project, deterministic and bounded.

    ``candidate_notes`` are recent notes that may be sessionless captures
    (they are re-checked here); ``booking_notes`` carry booking metadata.
    ``viewer`` (the reader's author id) limits capture-day suggestions and the
    captures listed with a booking to the reader's own captures (plus those
    with no recorded author); ``None`` offers everyone's, grouped per author.
    """

    lookback_start = now - timedelta(days=SUGGESTION_LOOKBACK_DAYS)
    candidates = [
        capture
        for capture in _captures(note for note in candidate_notes if is_sessionless_capture(note))
        if viewer is None or author_key(capture.note) in (None, viewer)
    ]
    earliest = min([lookback_start, *(capture.at for capture in candidates)])
    timeline = SessionTimeline(sessions, now=now, since=earliest)
    sessionless = [
        capture
        for capture, open_sessions in zip(
            candidates,
            timeline.containing_many([capture.at for capture in candidates]),
            strict=True,
        )
        if not eligible_sessions(capture.note, open_sessions)
    ]
    bookings = [
        booking
        for note in booking_notes
        if note.status != NoteStatus.ARCHIVED and (booking := booking_from_note(note)) is not None
    ]
    closing = close_quiet_session_suggestions(sessions, captures_by_session, now=now, zone=zone)
    booked = booking_suggestions(
        bookings,
        timeline,
        sessionless,
        project_id=project_id,
        now=now,
        zone=zone,
        lookback_start=lookback_start,
    )
    days = capture_day_suggestions(
        sessionless,
        project_id=project_id,
        zone=zone,
        viewer=viewer,
        covered=[
            (suggestion.start_at, suggestion.end_at)
            for suggestion in booked
            if suggestion.start_at is not None and suggestion.end_at is not None
        ],
    )
    return [*closing, *booked, *days][:MAX_SUGGESTIONS]


def _linked_session_notes(
    links: Iterable[ProvenanceLink],
    session_ids: set[UUID],
) -> dict[UUID, set[UUID]]:
    """Note ids that a proposed or accepted link ties to each session."""

    linked: dict[UUID, set[UUID]] = defaultdict(set)
    for link in links:
        if (
            link.status != ProvenanceLinkStatus.REJECTED
            and link.source.entity_type == EntityType.NOTE
            and link.target.entity_type == EntityType.SESSION
            and link.target.entity_id in session_ids
        ):
            linked[link.target.entity_id].add(link.source.entity_id)
    return linked


class SessionSuggestionService(BaseService):
    """Read-only: gathers a project's records and computes its suggestions."""

    def __init__(self, context: ServiceContext, *, projects: ProjectService) -> None:
        super().__init__(context)
        self.projects = projects

    def suggest_sessions(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
        now: datetime | None = None,
    ) -> SessionSuggestionReport:
        """The project's session suggestions; needs project read access.

        A project the actor cannot read is indistinguishable from a missing
        one. Nothing is written.
        """

        project = self.projects.get_project_for_read(project_id, actor=actor)
        current = now or utc_now()
        repository = self.repository
        zone, zone_name = resolve_capture_timezone(
            repository, project.project_id, actor_user_fk(actor, repository)
        )
        sessions, _total = repository.query_sessions(
            project_id=project.project_id, limit=None, offset=0
        )
        captures_by_session = self._captures_by_active_session(project.project_id, sessions)
        candidate_notes = repository.provenance_links.list_time_window_candidates(
            project.project_id,
            created_since=current - timedelta(days=SUGGESTION_LOOKBACK_DAYS),
            excluded_metadata_keys=SESSION_HINT_METADATA_KEYS,
            origins=sorted(origin.value for origin in TIME_WINDOW_NOTE_ORIGINS),
        )
        booking_notes = repository.provenance_links.list_identifier_carriers(
            project.project_id,
            (BOOKING_START_KEY,),
            created_since=current - timedelta(days=BOOKING_NOTE_LOOKBACK_DAYS),
        )
        suggestions = suggest_sessions(
            project_id=project.project_id,
            sessions=sessions,
            captures_by_session=captures_by_session,
            candidate_notes=candidate_notes,
            booking_notes=booking_notes,
            now=current,
            zone=zone,
            viewer=actor_user_id(actor),
        )
        return SessionSuggestionReport(
            project_id=project.project_id,
            generated_at=current,
            timezone=zone_name,
            quiet_threshold_minutes=int(QUIET_SESSION_THRESHOLD.total_seconds() // 60),
            lookback_days=SUGGESTION_LOOKBACK_DAYS,
            min_captures_per_day=MIN_CAPTURES_FOR_SESSION,
            suggestions=suggestions,
        )

    def _captures_by_active_session(
        self,
        project_id: UUID,
        sessions: Sequence[Session],
    ) -> dict[UUID, list[Note]]:
        """Each active session's targeted and linked notes (most recent, bounded)."""

        active_ids = {
            session.session_id for session in sessions if session.status == SessionStatus.ACTIVE
        }
        if not active_ids:
            return {}
        repository = self.repository
        linked = _linked_session_notes(
            repository.provenance_links.list_by_project(
                project_id, target_ids=sorted(active_ids, key=str)
            ),
            active_ids,
        )
        captures: dict[UUID, list[Note]] = {}
        for session_id in sorted(active_ids, key=str):
            targeted, _total = repository.query_notes(
                project_id=project_id,
                target_entity_type=EntityType.SESSION.value,
                target_entity_id=session_id,
                limit=MAX_NOTES_PER_ACTIVE_SESSION,
                offset=0,
                recent_first=True,
            )
            notes = {note.note_id: note for note in targeted}
            missing = linked.get(session_id, set()) - set(notes)
            if missing:
                linked_notes, _total = repository.query_notes(
                    project_id=project_id, note_ids=missing, limit=None, offset=0
                )
                notes.update({note.note_id: note for note in linked_notes})
            captures[session_id] = [
                note for note in notes.values() if note.status != NoteStatus.ARCHIVED
            ]
        return captures


__all__ = [
    "BOOKING_NOTE_LOOKBACK_DAYS",
    "MAX_LISTED_CAPTURES",
    "MAX_SUGGESTIONS",
    "MIN_CAPTURES_FOR_SESSION",
    "QUIET_SESSION_THRESHOLD",
    "SUGGESTION_LOOKBACK_DAYS",
    "SessionSuggestion",
    "SessionSuggestionKind",
    "SessionSuggestionReport",
    "SessionSuggestionService",
    "booking_from_note",
    "booking_suggestions",
    "capture_day_suggestions",
    "close_quiet_session_suggestions",
    "is_booking_note",
    "is_sessionless_capture",
    "suggest_sessions",
]
