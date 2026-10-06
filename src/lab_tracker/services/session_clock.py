"""Sessions as the clock: when a capture happened and which session was open.

Bench work happens in time, and an acquisition session is the unit that time
is cut into. Three deterministic features read the same two facts, so they
live here once:

* the time-window provenance detector (``provenance_time_windows``) proposes
  ``note -> session`` links for captures made inside exactly one session;
* session suggestions (``session_suggestions``) notice quiet open sessions,
  sessionless bench days, and uncovered instrument bookings;
* the day-log grouping stage (``graph_draft_day_log``) folds a session's short
  bench captures into one proposed log.

A capture's clock prefers the instrument's own acquisition time
(``format_acquired_at``, written by the file-format decoders) over the note's
observed time (client ``captured_at``, then the adapter's
``evidence_source_observed_at``, then the server's ``created_at``; see
:mod:`lab_tracker.services.note_observed_at`). Like the observed time, it is
clamped to ``created_at``: nothing is acquired after the server stored it.

A session's window is ``[started_at, ended_at]``, or ``[started_at, now]`` while
it is open, inclusive at both ends. Time only links a capture to a session its
own author ran: when both the note's and the session's author are known they
must match (a colleague's open session is not where your bench photo was
taken); when either is unknown (legacy rows, auth-disabled installs) any
session of the project qualifies.

Lookups go through :class:`SessionTimeline`, built once per batch of captures:
every window is computed once and all capture times are answered in one sweep,
so the cost grows with captures plus sessions, not their product. Everything
here is pure except :func:`resolve_capture_timezone`, which reads one settings
row.
"""

from __future__ import annotations

import heapq
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Final, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lab_tracker.models import (
    EntityType,
    GraphDraftBatchSettings,
    Note,
    Session,
    SessionStatus,
)
from lab_tracker.services.note_observed_at import note_observed_at
from lab_tracker.services.provenance_id_matches import ID_MATCH_SESSION_METADATA_KEYS

# Written by the file-format decoders (FCS, OME-TIFF, NWB headers): the
# instrument's acquisition time, ISO-8601 UTC. A naive value is read as UTC
# because the key's contract says UTC; an unparsable one is ignored.
FORMAT_ACQUIRED_AT_KEY: Final = "format_acquired_at"
FORMAT_CLOCK_SOURCE: Final = "format"

# Metadata that already names a session, beyond the exact-id detector's keys:
# a session resolved from a code decoded out of a photo, and the raw decoded
# link code itself. A note carrying any of these "has a session" for every
# rule here, so time never second-guesses an id.
SESSION_HINT_METADATA_KEYS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(
        (*ID_MATCH_SESSION_METADATA_KEYS, "photo_session_id", "decoded_session_link_code")
    )
)
# Metadata of the capture clients that point at a session by id (not by code).
SESSION_ID_METADATA_KEYS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys((*ID_MATCH_SESSION_METADATA_KEYS, "photo_session_id"))
)

# Instrument-calendar bookings (written by a calendar integration). A booking
# note describes a reserved window, not a capture made at its own clock time.
BOOKING_METADATA_KEYS: Final[tuple[str, ...]] = ("booking_uid", "booking_start", "booking_end")

UTC_ZONE_NAME: Final = "UTC"
# How the app shows and copies a session's link code.
SESSION_LINK_CODE_PREFIX: Final = "LT-"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_format_acquired_at(value: object) -> datetime | None:
    """Parse ``format_acquired_at``; ``None`` when absent or unparsable.

    The contract is ISO-8601 UTC, so a value without an offset is read as UTC
    (``Z`` is accepted). Non-strings and malformed text are absent, not errors:
    note metadata is a free-form bag the client controls.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


@dataclass(frozen=True)
class CaptureClock:
    """A note's capture time (aware UTC) and which clock supplied it."""

    at: datetime
    source: str


def capture_clock(note: Note) -> CaptureClock:
    """The capture time of ``note``: instrument acquisition time first.

    ``format_acquired_at`` wins when present and parseable; otherwise the
    note's observed time (client, then adapter, then server clock). Either way
    the value is clamped to ``created_at``.
    """

    ceiling = _as_utc(note.created_at)
    acquired = parse_format_acquired_at(note.metadata.get(FORMAT_ACQUIRED_AT_KEY))
    if acquired is not None:
        return CaptureClock(at=min(acquired, ceiling), source=FORMAT_CLOCK_SOURCE)
    observed, source = note_observed_at(note)
    return CaptureClock(at=observed, source=source.value)


def session_window(session: Session, *, now: datetime) -> tuple[datetime, datetime]:
    """``[started_at, ended_at]``, or ``[started_at, now]`` for an open session.

    A closed session that never recorded ``ended_at`` (legacy rows) ends when
    it was last updated, the moment it was closed.
    """

    start = _as_utc(session.started_at)
    if session.ended_at is not None:
        end = _as_utc(session.ended_at)
    elif session.status == SessionStatus.ACTIVE:
        end = _as_utc(now)
    else:
        end = _as_utc(session.updated_at)
    return start, end


class SessionTimeline:
    """A project's session windows ordered by start, for bounded time lookups.

    Each window is computed once. :meth:`containing_many` answers a batch of
    capture times with one sweep (a heap of open windows keyed by end), so it
    costs O((captures + sessions) log sessions) plus the size of each answer
    -- the sessions actually open at that moment -- instead of captures times
    sessions. :meth:`overlaps` is one bisect over a running maximum of ends.
    Windows ending before ``since`` are dropped up front, so a caller that
    knows its earliest capture pays nothing for older history.

    ``windows_examined`` counts the windows each sweep opens and closes (at
    most twice the number of sessions per sweep); it exists so tests and
    diagnostics can check the work stays bounded.
    """

    def __init__(
        self,
        sessions: Iterable[Session],
        *,
        now: datetime,
        since: datetime | None = None,
    ) -> None:
        floor = _as_utc(since) if since is not None else None
        windows: list[tuple[datetime, datetime, Session]] = []
        for session in sessions:
            start, end = session_window(session, now=now)
            if start > end or (floor is not None and end < floor):
                continue
            windows.append((start, end, session))
        windows.sort(key=lambda item: (item[0], str(item[2].session_id)))
        self._windows = windows
        self._starts = [start for start, _end, _session in windows]
        self._max_ends: list[datetime] = []
        for _start, end, _session in windows:
            latest = self._max_ends[-1] if self._max_ends else end
            self._max_ends.append(max(latest, end))
        self.windows_examined = 0

    def __len__(self) -> int:
        return len(self._windows)

    def containing_many(self, times: Sequence[datetime]) -> list[tuple[Session, ...]]:
        """For each time (in any order), the sessions whose window contains it."""

        moments = [_as_utc(moment) for moment in times]
        order = sorted(range(len(moments)), key=lambda index: moments[index])
        answers: list[tuple[Session, ...]] = [() for _ in moments]
        open_ends: list[tuple[datetime, int]] = []
        open_sessions: dict[int, Session] = {}
        next_window = 0
        for index in order:
            moment = moments[index]
            while next_window < len(self._windows) and self._windows[next_window][0] <= moment:
                _start, end, session = self._windows[next_window]
                heapq.heappush(open_ends, (end, next_window))
                open_sessions[next_window] = session
                next_window += 1
                self.windows_examined += 1
            while open_ends and open_ends[0][0] < moment:
                _end, closed = heapq.heappop(open_ends)
                del open_sessions[closed]
                self.windows_examined += 1
            answers[index] = tuple(open_sessions[key] for key in sorted(open_sessions))
        return answers

    def containing(self, at: datetime) -> tuple[Session, ...]:
        """The sessions whose window contains ``at`` (inclusive at both ends)."""

        return self.containing_many([at])[0]

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """True when any window overlaps ``[start, end]``."""

        count = bisect_right(self._starts, _as_utc(end))
        return count > 0 and self._max_ends[count - 1] >= _as_utc(start)


def author_key(entity: Note | Session) -> str | None:
    """Who made a note or started a session: the user id, else the stored actor id."""

    if entity.created_by_user_id is not None:
        return str(entity.created_by_user_id)
    created_by = (entity.created_by or "").strip()
    return created_by or None


def same_author_or_unknown(note: Note, session: Session) -> bool:
    """Whether time may tie ``note`` to ``session``: same author, or one unknown."""

    note_author = author_key(note)
    session_author = author_key(session)
    return note_author is None or session_author is None or note_author == session_author


def eligible_sessions(note: Note, containing: Iterable[Session]) -> list[Session]:
    """The sessions of the note's project, run by its author, among ``containing``."""

    return [
        session
        for session in containing
        if session.project_id == note.project_id and same_author_or_unknown(note, session)
    ]


def sessions_containing(
    at: datetime,
    sessions: Iterable[Session],
    *,
    now: datetime,
) -> list[Session]:
    """Every session whose window contains ``at`` (inclusive at both ends)."""

    return list(SessionTimeline(sessions, now=now).containing(at))


def unique_session_at(
    at: datetime,
    sessions: Iterable[Session],
    *,
    now: datetime,
) -> Session | None:
    """The one session open at ``at``; overlapping windows are ambiguous (None)."""

    matches = sessions_containing(at, sessions, now=now)
    return matches[0] if len(matches) == 1 else None


def windows_overlap(
    start: datetime,
    end: datetime,
    sessions: Iterable[Session],
    *,
    now: datetime,
) -> bool:
    """True when any session window overlaps ``[start, end]``."""

    return SessionTimeline(sessions, now=now).overlaps(start, end)


def is_booking_note(note: Note) -> bool:
    """True when the note carries instrument-booking metadata."""

    return any(str(note.metadata.get(key) or "").strip() for key in BOOKING_METADATA_KEYS)


def session_label(session: Session) -> str:
    """A person-readable session name: its type and ``LT-`` link code."""

    return f"{session.session_type.value} session {SESSION_LINK_CODE_PREFIX}{session.link_code}"


def session_targets(note: Note) -> list[UUID]:
    """Session ids the note declares as targets, in target order."""

    return [target.entity_id for target in note.targets if target.entity_type == EntityType.SESSION]


def carries_session(note: Note) -> bool:
    """True when the note already names a session: a target or any session hint."""

    if session_targets(note):
        return True
    return any(str(note.metadata.get(key) or "").strip() for key in SESSION_HINT_METADATA_KEYS)


def metadata_session_id(note: Note, known: set[UUID]) -> UUID | None:
    """The first session id the note's metadata names that is a known session."""

    for key in SESSION_ID_METADATA_KEYS:
        value = str(note.metadata.get(key) or "").strip()
        if not value:
            continue
        try:
            session_id = UUID(value)
        except ValueError:
            continue
        if session_id in known:
            return session_id
    return None


def _named_session(note: Note, known: set[UUID]) -> tuple[bool, UUID | None]:
    """``(decided, session)`` from what the capture itself says.

    ``decided`` is True when a target or metadata settles the question (even
    as "ambiguous" or "unknown to this project"), so time must not overrule it.
    """

    targeted = session_targets(note)
    declared = [session_id for session_id in targeted if session_id in known]
    if declared:
        return True, declared[0] if len(set(declared)) == 1 else None
    if targeted:
        return True, None
    named = metadata_session_id(note, known)
    if named is not None:
        return True, named
    if any(str(note.metadata.get(key) or "").strip() for key in SESSION_HINT_METADATA_KEYS):
        # It names a session this project does not know (or only by code).
        return True, None
    return False, None


def sessions_for_captures(
    notes: Sequence[Note],
    sessions: Sequence[Session],
    *,
    now: datetime,
) -> dict[UUID, UUID | None]:
    """The session each capture belongs to, by declaration, then id, then time.

    A single declared session target wins; several are ambiguous. Otherwise a
    session id in the metadata that names a project session; otherwise the one
    session of the note's author (see :func:`same_author_or_unknown`) whose
    window contains the capture time. Anything ambiguous or unmatched is
    ``None``. All time lookups share one :class:`SessionTimeline` sweep.
    """

    known = {session.session_id for session in sessions}
    resolved: dict[UUID, UUID | None] = {}
    timed: list[tuple[Note, datetime]] = []
    for note in notes:
        decided, session_id = _named_session(note, known)
        if decided:
            resolved[note.note_id] = session_id
        else:
            timed.append((note, capture_clock(note).at))
    if timed:
        earliest = min(at for _note, at in timed)
        timeline = SessionTimeline(sessions, now=now, since=earliest)
        containing = timeline.containing_many([at for _note, at in timed])
        for (note, _at), open_sessions in zip(timed, containing, strict=True):
            eligible = eligible_sessions(note, open_sessions)
            resolved[note.note_id] = eligible[0].session_id if len(eligible) == 1 else None
    return resolved


def session_for_capture(
    note: Note,
    sessions: Sequence[Session],
    *,
    now: datetime,
) -> UUID | None:
    """The session one capture belongs to (see :func:`sessions_for_captures`)."""

    return sessions_for_captures([note], sessions, now=now)[note.note_id]


class BatchSettingsReader(Protocol):
    def get_graph_draft_batch_settings_by_project(
        self,
        project_id: UUID,
        *,
        user_id: UUID | None = None,
    ) -> GraphDraftBatchSettings | None: ...


def zone_for_name(name: str | None) -> tuple[tzinfo, str]:
    """The zone named ``name``, or UTC when it is empty or unknown."""

    cleaned = (name or "").strip()
    if cleaned and cleaned.upper() != UTC_ZONE_NAME:
        try:
            return ZoneInfo(cleaned), cleaned
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc, UTC_ZONE_NAME


def resolve_capture_timezone(
    repository: BatchSettingsReader,
    project_id: UUID,
    user_id: UUID | None,
) -> tuple[tzinfo, str]:
    """The lab-local zone for day boundaries and ``HH:MM`` labels.

    The person's own daily-review settings row wins, then the project default
    row (both carry ``timezone_name``, the zone the daily review runs in, so a
    "day" here is the same day the review is cut on). A project with neither
    row uses UTC.
    """

    if user_id is not None:
        personal = repository.get_graph_draft_batch_settings_by_project(project_id, user_id=user_id)
        if personal is not None:
            return zone_for_name(personal.timezone_name)
    project_default = repository.get_graph_draft_batch_settings_by_project(project_id)
    if project_default is not None:
        return zone_for_name(project_default.timezone_name)
    return timezone.utc, UTC_ZONE_NAME


__all__ = [
    "BOOKING_METADATA_KEYS",
    "FORMAT_ACQUIRED_AT_KEY",
    "FORMAT_CLOCK_SOURCE",
    "SESSION_HINT_METADATA_KEYS",
    "SESSION_ID_METADATA_KEYS",
    "SESSION_LINK_CODE_PREFIX",
    "UTC_ZONE_NAME",
    "CaptureClock",
    "SessionTimeline",
    "author_key",
    "capture_clock",
    "carries_session",
    "eligible_sessions",
    "is_booking_note",
    "metadata_session_id",
    "parse_format_acquired_at",
    "resolve_capture_timezone",
    "same_author_or_unknown",
    "session_for_capture",
    "sessions_for_captures",
    "session_label",
    "session_targets",
    "session_window",
    "sessions_containing",
    "unique_session_at",
    "windows_overlap",
    "zone_for_name",
]
