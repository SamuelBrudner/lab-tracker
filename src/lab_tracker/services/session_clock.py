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
it is open, inclusive at both ends. Everything here is pure except
:func:`resolve_capture_timezone`, which reads one settings row.
"""

from __future__ import annotations

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


def sessions_containing(
    at: datetime,
    sessions: Iterable[Session],
    *,
    now: datetime,
) -> list[Session]:
    """Every session whose window contains ``at`` (inclusive at both ends)."""

    moment = _as_utc(at)
    matches: list[Session] = []
    for session in sessions:
        start, end = session_window(session, now=now)
        if start <= moment <= end:
            matches.append(session)
    return matches


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

    lower = _as_utc(start)
    upper = _as_utc(end)
    for session in sessions:
        session_start, session_end = session_window(session, now=now)
        if session_start <= upper and lower <= session_end:
            return True
    return False


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


def session_for_capture(
    note: Note,
    sessions: Sequence[Session],
    *,
    now: datetime,
) -> UUID | None:
    """The session a capture belongs to, by declaration, then id, then time.

    A single declared session target wins; several are ambiguous. Otherwise a
    session id in the metadata that names a project session; otherwise the one
    session whose window contains the capture time. Anything ambiguous or
    unmatched is ``None``.
    """

    known = {session.session_id for session in sessions}
    declared = [session_id for session_id in session_targets(note) if session_id in known]
    if declared:
        return declared[0] if len(set(declared)) == 1 else None
    if session_targets(note):
        return None
    named = metadata_session_id(note, known)
    if named is not None:
        return named
    if any(str(note.metadata.get(key) or "").strip() for key in SESSION_HINT_METADATA_KEYS):
        # It names a session this project does not know (or only by code):
        # time must not overrule what the capture itself said.
        return None
    match = unique_session_at(capture_clock(note).at, sessions, now=now)
    return match.session_id if match is not None else None


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
    "capture_clock",
    "carries_session",
    "is_booking_note",
    "metadata_session_id",
    "parse_format_acquired_at",
    "resolve_capture_timezone",
    "session_for_capture",
    "session_label",
    "session_targets",
    "session_window",
    "sessions_containing",
    "unique_session_at",
    "windows_overlap",
    "zone_for_name",
]
