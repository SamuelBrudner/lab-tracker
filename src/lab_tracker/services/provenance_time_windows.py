"""Rule-based provenance-link proposals from when a capture was made.

A photo of a gel, a typed "added 5 ml buffer", or an FCS file acquired at
10:14 carries no session id, yet if exactly one acquisition session of the
same project was open at 10:14 the capture almost certainly belongs to it.
This module turns that into a proposed ``was_derived_from`` note -> session
link with ``basis=time_window_match``, on the same human-gated review surface
as the content-hash and exact-id detectors: time does the linking, a person
still decides.

The rule is deliberately narrow:

* only notes with no session target and no session id or link code in their
  metadata (a capture that names its session is the exact-id detector's, and
  time never second-guesses an id), and never an instrument-booking note (it
  describes a reserved window, not a capture made when it was synced);
* the capture time is ``format_acquired_at`` when present and parseable, else
  the note's observed time (see :mod:`lab_tracker.services.session_clock`);
* the time must fall inside exactly one session window of the project;
  overlapping windows are ambiguous and propose nothing;
* only notes created in the last :data:`TIME_WINDOW_LOOKBACK_DAYS` days are
  scanned (the repository filters in SQL), so an old project costs a bounded
  scan per batch run.

Everything here is pure: the caller supplies candidate notes and sessions.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from lab_tracker.member_onboarding import is_member_checkpoint
from lab_tracker.models import EntityOrigin, Note, NoteStatus, Session
from lab_tracker.services.session_clock import (
    BOOKING_METADATA_KEYS,
    SESSION_HINT_METADATA_KEYS,
    capture_clock,
    carries_session,
    is_booking_note,
    unique_session_at,
)

# How far back (by note creation) the detector looks on each batch run.
TIME_WINDOW_LOOKBACK_DAYS: Final = 14
# Only captures a person or their agent made. A note the review path created
# (ai_suggested, user_revised) is a product of review, not a capture.
TIME_WINDOW_NOTE_ORIGINS: Final = frozenset({EntityOrigin.USER, EntityOrigin.AI_EXECUTED})
# The metadata keys whose presence excludes a note in SQL.
TIME_WINDOW_EXCLUDED_METADATA_KEYS: Final[tuple[str, ...]] = (
    *SESSION_HINT_METADATA_KEYS,
    *BOOKING_METADATA_KEYS,
)


def time_window_lookback_start(now: datetime) -> datetime:
    """The earliest note creation time the detector scans at ``now``."""

    return now - timedelta(days=TIME_WINDOW_LOOKBACK_DAYS)


@dataclass(frozen=True)
class TimeWindowMatch:
    """One capture whose capture time falls inside exactly one session window."""

    note_id: UUID
    session_id: UUID
    captured_at: datetime
    clock_source: str


def is_time_window_candidate(note: Note) -> bool:
    """True for a live capture that does not already name a session."""

    return (
        note.status != NoteStatus.ARCHIVED
        and note.origin in TIME_WINDOW_NOTE_ORIGINS
        and not is_member_checkpoint(note)
        and not is_booking_note(note)
        and not carries_session(note)
    )


def time_window_match(
    note: Note,
    sessions: Sequence[Session],
    *,
    now: datetime,
) -> TimeWindowMatch | None:
    """The session open at the note's capture time, when there is exactly one."""

    if not is_time_window_candidate(note):
        return None
    clock = capture_clock(note)
    session = unique_session_at(
        clock.at,
        [item for item in sessions if item.project_id == note.project_id],
        now=now,
    )
    if session is None:
        return None
    return TimeWindowMatch(
        note_id=note.note_id,
        session_id=session.session_id,
        captured_at=clock.at,
        clock_source=clock.source,
    )


def time_window_matches(
    notes: Iterable[Note],
    sessions: Sequence[Session],
    *,
    now: datetime,
) -> list[TimeWindowMatch]:
    """Time-window matches for ``notes``, in input order."""

    matches: list[TimeWindowMatch] = []
    for note in notes:
        match = time_window_match(note, sessions, now=now)
        if match is not None:
            matches.append(match)
    return matches


__all__ = [
    "TIME_WINDOW_EXCLUDED_METADATA_KEYS",
    "TIME_WINDOW_LOOKBACK_DAYS",
    "TIME_WINDOW_NOTE_ORIGINS",
    "TimeWindowMatch",
    "is_time_window_candidate",
    "time_window_lookback_start",
    "time_window_match",
    "time_window_matches",
]
