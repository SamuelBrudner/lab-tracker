"""Instrument bookings: poll operator-declared ICS feeds into staged notes.

Each configured feed ``{project_id, url, instrument}`` is fetched over HTTPS
through the outbound HTTP egress policy (every redirect re-authorized, no TLS
downgrade), bounded in size and time, parsed with the RFC 5545 subset parser,
and every booking starting in ``[now - 1 day, now + 7 days]`` is upserted as one
staged note per (UID, instance start). The server fetched these records itself,
so the notes are authored by the ``SYSTEM`` principal -- never by the booking's
organizer or any other person -- and carry ``capture_channel=ics``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from lab_tracker.auth import AuthContext
from lab_tracker.capture_channels.common import (
    CAPTURE_CHANNEL_KEY,
    EVIDENCE_ADAPTER_KEY,
    EVIDENCE_CAPTURE_KIND_KEY,
    EVIDENCE_SOURCE_EXTERNAL_ID_KEY,
    EVIDENCE_SOURCE_OBSERVED_AT_KEY,
    EVIDENCE_SOURCE_PROVIDER_KEY,
    EVIDENCE_TITLE_KEY,
    MAX_TITLE_CHARS,
    bound_text,
    bound_value,
    stable_key,
)
from lab_tracker.capture_channels.ics import IcsEvent, parse_events
from lab_tracker.capture_channels.settings import BookingCalendar
from lab_tracker.models import EntityOrigin, NoteMetadataScalar, NoteStatus
from lab_tracker.outbound_http import (
    HTTP_REDIRECT_STATUS_CODES,
    OutboundHttpClient,
    OutboundHttpDeadline,
    OutboundHttpPolicy,
    OutboundHttpPolicyError,
    OutboundHttpTransportError,
    resolve_direct_http_redirect,
)

ICS_CHANNEL: Final = "ics"
ICS_ADAPTER: Final = "lab-tracker-ics-poller"
INSTRUMENT_BOOKING_KIND: Final = "instrument_booking"
MAX_CALENDAR_BYTES: Final = 2 * 1024 * 1024
MAX_CALENDAR_REDIRECTS: Final = 3
CALENDAR_DEADLINE_SECONDS: Final = 20.0
MAX_BOOKINGS_PER_FEED: Final = 200
MAX_BOOKING_TEXT_CHARS: Final = 2_000
BOOKING_WINDOW_PAST: Final = timedelta(days=1)
BOOKING_WINDOW_FUTURE: Final = timedelta(days=7)
BOOKING_METADATA_PREFIX: Final = "booking_"


class CalendarFetchError(RuntimeError):
    """A feed could not be fetched; the message never names the feed URL."""


CalendarFetcher = Callable[[str], bytes]


def policy_calendar_fetcher(
    policy: OutboundHttpPolicy,
    client: OutboundHttpClient,
    *,
    deadline_seconds: float = CALENDAR_DEADLINE_SECONDS,
    max_bytes: int = MAX_CALENDAR_BYTES,
) -> CalendarFetcher:
    """Fetch HTTPS feeds through the egress policy with one deadline and a byte cap."""

    def fetch(url: str) -> bytes:
        deadline = OutboundHttpDeadline.after(deadline_seconds)
        current = url
        try:
            for _hop in range(MAX_CALENDAR_REDIRECTS + 1):
                if urlsplit(current).scheme.lower() != "https":
                    raise CalendarFetchError("Calendar feeds must be fetched over HTTPS.")
                target = policy.authorize(current, deadline=deadline)
                with client.open("GET", target, deadline=deadline) as response:
                    if response.status_code in HTTP_REDIRECT_STATUS_CODES:
                        location = response.get_header("location")
                        next_url = (
                            resolve_direct_http_redirect(current, location) if location else None
                        )
                        if next_url is None:
                            raise CalendarFetchError("Calendar feed redirect was refused.")
                        current = next_url
                        continue
                    if response.status_code != 200:
                        raise CalendarFetchError(
                            f"Calendar feed answered HTTP {int(response.status_code)}."
                        )
                    return _bounded_body(response.iter_bytes(), max_bytes=max_bytes)
        except (OutboundHttpPolicyError, OutboundHttpTransportError, ValueError) as exc:
            raise CalendarFetchError("Calendar feed fetch failed or was denied.") from exc
        raise CalendarFetchError("Calendar feed redirected too many times.")

    return fetch


def _bounded_body(chunks: Any, *, max_bytes: int) -> bytes:
    collected = bytearray()
    for chunk in chunks:
        collected.extend(chunk)
        if len(collected) > max_bytes:
            raise CalendarFetchError(f"Calendar feed exceeds the {max_bytes}-byte limit.")
    return bytes(collected)


class BookingApi(Protocol):
    """The slice of ``LabTrackerAPI`` the booking poller uses."""

    def find_note_by_client_capture_id(self, *args: Any, **kwargs: Any) -> Any: ...

    def create_note_result(self, *args: Any, **kwargs: Any) -> Any: ...

    def update_note(self, *args: Any, **kwargs: Any) -> Any: ...


@dataclass
class BookingSyncResult:
    """Counts for one feed."""

    in_window: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    cancelled_not_captured: int = 0
    skipped_events: int = 0
    left_reviewed: int = 0


def sync_calendar(
    calendar: BookingCalendar,
    *,
    fetch: CalendarFetcher,
    api: BookingApi,
    actor: AuthContext,
    now: datetime,
) -> BookingSyncResult:
    """Fetch one feed and upsert its in-window bookings as staged notes."""

    raw = fetch(calendar.url)
    parsed = parse_events(
        raw.decode("utf-8", errors="replace"), default_zone=ZoneInfo(calendar.timezone)
    )
    result = BookingSyncResult(skipped_events=parsed.skipped)
    window_start, window_end = now - BOOKING_WINDOW_PAST, now + BOOKING_WINDOW_FUTURE
    in_window = [event for event in parsed.events if window_start <= event.start <= window_end]
    result.in_window = len(in_window)
    for event in in_window[:MAX_BOOKINGS_PER_FEED]:
        _upsert_booking(event, calendar=calendar, api=api, actor=actor, now=now, result=result)
    return result


def booking_metadata(
    event: IcsEvent, *, calendar: BookingCalendar
) -> dict[str, NoteMetadataScalar]:
    """The ``booking_*`` metadata contract (times are ISO-8601 UTC)."""

    metadata: dict[str, NoteMetadataScalar] = {
        "booking_uid": bound_value(event.uid, limit=255),
        "booking_start": _iso_utc(event.start),
        "booking_end": _iso_utc(event.end),
        "booking_instrument": calendar.instrument,
        "booking_summary": bound_value(event.summary, limit=MAX_TITLE_CHARS),
    }
    if event.organizer_email:
        metadata["booking_organizer"] = event.organizer_email
    if event.all_day:
        metadata["booking_all_day"] = True
    if event.status:
        metadata["booking_status"] = event.status.lower()
    if event.recurrence_id is not None:
        metadata["booking_recurrence_id"] = _iso_utc(event.recurrence_id)
    return metadata


def _upsert_booking(
    event: IcsEvent,
    *,
    calendar: BookingCalendar,
    api: BookingApi,
    actor: AuthContext,
    now: datetime,
    result: BookingSyncResult,
) -> None:
    key = stable_key("ics", calendar.instrument, event.uid, _iso_utc(event.instance_start))
    existing = api.find_note_by_client_capture_id(calendar.project_id, key, actor=actor)
    booking = booking_metadata(event, calendar=calendar)
    if existing is None:
        if event.cancelled:
            result.cancelled_not_captured += 1
            return
        api.create_note_result(
            project_id=calendar.project_id,
            raw_content=_booking_text(event, calendar=calendar),
            metadata={
                CAPTURE_CHANNEL_KEY: ICS_CHANNEL,
                EVIDENCE_SOURCE_PROVIDER_KEY: ICS_CHANNEL,
                EVIDENCE_ADAPTER_KEY: ICS_ADAPTER,
                EVIDENCE_CAPTURE_KIND_KEY: INSTRUMENT_BOOKING_KIND,
                EVIDENCE_SOURCE_EXTERNAL_ID_KEY: bound_value(event.uid, limit=255),
                EVIDENCE_SOURCE_OBSERVED_AT_KEY: _iso_utc(now),
                EVIDENCE_TITLE_KEY: bound_value(
                    f"{calendar.instrument}: {event.summary or 'booking'}", limit=MAX_TITLE_CHARS
                ),
                **booking,
            },
            client_capture_id=key,
            status=NoteStatus.STAGED,
            actor=actor,
            origin=EntityOrigin.USER,
            origin_provider=ICS_CHANNEL,
        )
        result.created += 1
        return
    if existing.status != NoteStatus.STAGED:
        # A person already reviewed this capture; the feed never rewrites it.
        result.left_reviewed += 1
        return
    current = {
        key_: value
        for key_, value in existing.metadata.items()
        if key_.startswith(BOOKING_METADATA_PREFIX) and key_ != "booking_updated_at"
    }
    normalized = {key_: str(value) for key_, value in booking.items()}
    if current == normalized:
        result.unchanged += 1
        return
    merged: dict[str, NoteMetadataScalar] = {
        key_: value
        for key_, value in existing.metadata.items()
        if not key_.startswith(BOOKING_METADATA_PREFIX)
    }
    merged.update(booking)
    merged["booking_updated_at"] = _iso_utc(now)
    api.update_note(existing.note_id, metadata=merged, actor=actor)
    result.updated += 1


def _booking_text(event: IcsEvent, *, calendar: BookingCalendar) -> str:
    lines = [
        f"Instrument booking: {calendar.instrument} — {event.summary or '(no title)'}",
        f"{_iso_utc(event.start)} to {_iso_utc(event.end)} (UTC)"
        + (" (all day)" if event.all_day else ""),
    ]
    if event.organizer_email:
        lines.append(f"Organizer: {event.organizer_email}")
    lines.append(
        "Captured from the instrument calendar feed by Lab Tracker; booking_* metadata "
        "tracks the feed while this note stays staged."
    )
    return bound_text("\n".join(lines), limit=MAX_BOOKING_TEXT_CHARS).text


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
