"""The capture clock of a note.

A note carries up to three clocks: the client's composition time
(``metadata.captured_at``, stamped by the phone or web composer), the adapter's
observation time (``metadata.evidence_source_observed_at``, stamped by a
consumer-side adapter such as ``lt watch``), and the server's receipt time
(``created_at``). Batch drafting narrates the day in capture order, so it needs
one clock per note and must say which clock it used.

This module is pure: it reads only the note and never consults a wall clock,
so the same note always yields the same answer. A metadata clock that does not
parse, or is naive, is treated as absent (the next source is used and the
reported source says so); a metadata clock later than the server receipt is
clamped to ``created_at``, because a capture cannot be composed after the
server stored it. Window membership, reviewer watermarks and ``since``/``until``
filters stay on ``created_at`` so a note is never dropped from a batch by a
client clock; this clock is presentation only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from lab_tracker.models import Note

CLIENT_CAPTURED_AT_KEY = "captured_at"
ADAPTER_OBSERVED_AT_KEY = "evidence_source_observed_at"


class ObservedAtSource(str, Enum):
    """Which clock supplied a note's observed_at."""

    CLIENT = "client"
    ADAPTER = "adapter"
    SERVER = "server"


_METADATA_CLOCKS: tuple[tuple[str, ObservedAtSource], ...] = (
    (CLIENT_CAPTURED_AT_KEY, ObservedAtSource.CLIENT),
    (ADAPTER_OBSERVED_AT_KEY, ObservedAtSource.ADAPTER),
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_metadata_datetime(value: object) -> datetime | None:
    """Parse a metadata clock; mirrors the repository's metadata datetime rule.

    Only aware ISO 8601 strings count (``Z`` accepted). Anything else --
    non-strings, unparsable text, naive datetimes -- is absent, not an error:
    note metadata is a free-form bag the client controls.
    """

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def note_observed_at(note: Note) -> tuple[datetime, ObservedAtSource]:
    """Return the note's capture clock (aware UTC) and which source supplied it.

    Client ``captured_at`` wins over the adapter's ``evidence_source_observed_at``,
    which wins over the server's ``created_at``. A metadata clock later than
    ``created_at`` is clamped to ``created_at`` but keeps its source label, so
    a skewed client clock is visible rather than silently rewritten.
    """

    ceiling = _as_utc(note.created_at)
    for key, source in _METADATA_CLOCKS:
        parsed = _parse_metadata_datetime(note.metadata.get(key))
        if parsed is not None:
            return min(parsed, ceiling), source
    return ceiling, ObservedAtSource.SERVER
