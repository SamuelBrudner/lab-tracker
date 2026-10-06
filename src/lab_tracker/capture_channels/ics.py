"""A small, dependency-free RFC 5545 subset parser for instrument booking feeds.

Supported: line unfolding; content-line parameters (quoted values included);
``VEVENT`` components (nested ``VALARM`` and friends are skipped); ``UID``,
``SUMMARY``, ``ORGANIZER`` (``mailto:`` only), ``STATUS``, ``RECURRENCE-ID``,
``DTSTART``/``DTEND``/``DURATION`` as UTC (``Z``), ``TZID``-qualified local times
resolved through ``zoneinfo`` (with the common Windows and Mozilla ``TZID``
spellings mapped), floating times in the feed's configured zone, and all-day
``VALUE=DATE`` dates. Not supported: ``RRULE``/``RDATE`` expansion (a
recurring master yields only its own ``DTSTART`` instance) and ``VTIMEZONE``
definitions (``TZID`` must name a zone ``zoneinfo`` knows).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_EVENTS: Final = 5_000
MAX_LINE_CHARS: Final = 10_000
_DATE_RE = re.compile(r"(\d{4})(\d{2})(\d{2})\Z")
_DATETIME_RE = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)\Z")
_DURATION_RE = re.compile(
    r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?\Z"
)
_EMAIL_RE = re.compile(r"[^@\s:]{1,64}@[A-Za-z0-9.-]{1,253}\Z")
# Windows zone names Outlook/Exchange feeds put in TZID, mapped to IANA zones.
_WINDOWS_ZONES: Final = {
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Central Standard Time": "America/Chicago",
    "China Standard Time": "Asia/Shanghai",
    "Coordinated Universal Time": "UTC",
    "E. Europe Standard Time": "Europe/Chisinau",
    "Eastern Standard Time": "America/New_York",
    "GMT Standard Time": "Europe/London",
    "India Standard Time": "Asia/Kolkata",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "Romance Standard Time": "Europe/Paris",
    "Tokyo Standard Time": "Asia/Tokyo",
    "UTC": "UTC",
    "W. Europe Standard Time": "Europe/Berlin",
}


class IcsParseError(ValueError):
    """One property could not be interpreted; its event is skipped."""


@dataclass(frozen=True, slots=True)
class IcsEvent:
    """One booking instance, all times in UTC."""

    uid: str
    start: datetime
    end: datetime
    all_day: bool
    summary: str
    organizer_email: str | None
    status: str
    recurrence_id: datetime | None

    @property
    def cancelled(self) -> bool:
        return self.status == "CANCELLED"

    @property
    def instance_start(self) -> datetime:
        """The instance identity: ``RECURRENCE-ID`` when present, else ``DTSTART``."""

        return self.recurrence_id or self.start


@dataclass(frozen=True, slots=True)
class IcsParseResult:
    events: tuple[IcsEvent, ...]
    skipped: int


def unfold_lines(text: str) -> list[str]:
    """Split into logical content lines, joining RFC 5545 folded continuations."""

    lines: list[str] = []
    for physical in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if physical[:1] in {" ", "\t"} and lines:
            lines[-1] += physical[1:]
        elif physical:
            lines.append(physical)
    return lines


def parse_content_line(line: str) -> tuple[str, dict[str, str], str]:
    """Split ``NAME;P1=V1;P2="V:2":value`` into name, params, and raw value."""

    in_quotes = False
    split_at = -1
    for index, character in enumerate(line):
        if character == '"':
            in_quotes = not in_quotes
        elif character == ":" and not in_quotes:
            split_at = index
            break
    if split_at < 0:
        raise IcsParseError("Content line has no value.")
    head, value = line[:split_at], line[split_at + 1 :]
    parts = _split_unquoted(head, ";")
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        key, _, param_value = part.partition("=")
        params[key.strip().upper()] = param_value.strip().strip('"')
    return name, params, value


def parse_events(
    text: str,
    *,
    default_zone: tzinfo = timezone.utc,
    max_events: int = MAX_EVENTS,
) -> IcsParseResult:
    """Parse every ``VEVENT``; malformed events are counted and skipped."""

    events: dict[tuple[str, datetime], IcsEvent] = {}
    skipped = 0
    current: dict[str, tuple[dict[str, str], str]] | None = None
    nested = 0
    for line in unfold_lines(text):
        if len(line) > MAX_LINE_CHARS:
            continue
        try:
            name, params, value = parse_content_line(line)
        except IcsParseError:
            continue
        upper_value = value.strip().upper()
        if name == "BEGIN":
            if current is None and upper_value == "VEVENT":
                current, nested = {}, 0
            elif current is not None:
                nested += 1
            continue
        if name == "END":
            if current is not None and nested:
                nested -= 1
            elif current is not None and upper_value == "VEVENT":
                try:
                    event = _event(current, default_zone)
                except (IcsParseError, OverflowError, ValueError):
                    # e.g. DURATION:P999999999W or a year-1 date east of UTC
                    # overflows datetime; skip that event, never the feed.
                    skipped += 1
                else:
                    key = (event.uid, event.instance_start)
                    # An override (RECURRENCE-ID) replaces the master's instance.
                    if key not in events or event.recurrence_id is not None:
                        events[key] = event
                current = None
                if len(events) >= max_events:
                    break
            continue
        if current is not None and not nested and name not in current:
            current[name] = (params, value)
    ordered = sorted(events.values(), key=lambda event: (event.start, event.uid))
    return IcsParseResult(tuple(ordered), skipped)


def parse_ics_datetime(
    value: str,
    params: dict[str, str],
    *,
    default_zone: tzinfo,
) -> tuple[datetime, bool]:
    """Return ``(UTC datetime, is_all_day)`` for a DATE or DATE-TIME value."""

    raw = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or _DATE_RE.fullmatch(raw):
        match = _DATE_RE.fullmatch(raw)
        if match is None:
            raise IcsParseError("Invalid DATE value.")
        day = _date(match)
        return datetime.combine(day, time(), tzinfo=default_zone).astimezone(timezone.utc), True
    match = _DATETIME_RE.fullmatch(raw)
    if match is None:
        raise IcsParseError("Invalid DATE-TIME value.")
    try:
        year, month, day_of_month, hour, minute, second = (
            int(match.group(index)) for index in range(1, 7)
        )
        naive = datetime(year, month, day_of_month, hour, minute, second)
    except ValueError as exc:
        raise IcsParseError("Invalid DATE-TIME value.") from exc
    if match.group(7) == "Z":
        return naive.replace(tzinfo=timezone.utc), False
    zone = resolve_tzid(params["TZID"]) if params.get("TZID") else default_zone
    return naive.replace(tzinfo=zone).astimezone(timezone.utc), False


def resolve_tzid(tzid: str) -> tzinfo:
    """Resolve a ``TZID`` to a zone, accepting Windows and ``/vendor/.../Area/City`` forms."""

    cleaned = tzid.strip().strip('"')
    candidates = [cleaned, _WINDOWS_ZONES.get(cleaned, "")]
    parts = [part for part in cleaned.split("/") if part]
    candidates.extend("/".join(parts[index:]) for index in range(1, len(parts)))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    raise IcsParseError("Unknown TZID.")


def parse_duration(value: str) -> timedelta:
    """Parse an RFC 5545 DURATION such as ``PT1H30M`` or ``P1D``."""

    match = _DURATION_RE.fullmatch(value.strip().upper())
    if match is None or value.strip().upper() in {"P", "PT", "+P", "-P"}:
        raise IcsParseError("Invalid DURATION value.")
    sign, weeks, days, hours, minutes, seconds = match.groups()
    delta = timedelta(
        weeks=int(weeks or 0),
        days=int(days or 0),
        hours=int(hours or 0),
        minutes=int(minutes or 0),
        seconds=int(seconds or 0),
    )
    return -delta if sign == "-" else delta


def unescape_text(value: str) -> str:
    """Undo RFC 5545 TEXT escaping (``\\n``, ``\\,``, ``\\;``, ``\\\\``)."""

    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            following = value[index + 1]
            result.append("\n" if following in "nN" else following)
            index += 2
            continue
        result.append(character)
        index += 1
    return "".join(result)


def _event(properties: dict[str, tuple[dict[str, str], str]], zone: tzinfo) -> IcsEvent:
    uid = properties.get("UID", ({}, ""))[1].strip()
    if not uid or "DTSTART" not in properties:
        raise IcsParseError("VEVENT lacks UID or DTSTART.")
    start_params, start_value = properties["DTSTART"]
    start, all_day = parse_ics_datetime(start_value, start_params, default_zone=zone)
    if "DTEND" in properties:
        end_params, end_value = properties["DTEND"]
        end, _ = parse_ics_datetime(end_value, end_params, default_zone=zone)
    elif "DURATION" in properties:
        end = start + parse_duration(properties["DURATION"][1])
    else:
        end = start + timedelta(days=1) if all_day else start
    if end < start:
        raise IcsParseError("DTEND precedes DTSTART.")
    recurrence_id = None
    if "RECURRENCE-ID" in properties:
        recurrence_params, recurrence_value = properties["RECURRENCE-ID"]
        recurrence_id, _ = parse_ics_datetime(
            recurrence_value, recurrence_params, default_zone=zone
        )
    return IcsEvent(
        uid=uid,
        start=start,
        end=end,
        all_day=all_day,
        summary=unescape_text(properties.get("SUMMARY", ({}, ""))[1]).strip(),
        organizer_email=_organizer_email(properties.get("ORGANIZER", ({}, ""))[1]),
        status=properties.get("STATUS", ({}, ""))[1].strip().upper(),
        recurrence_id=recurrence_id,
    )


def _organizer_email(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned.lower().startswith("mailto:"):
        return None
    address = cleaned[len("mailto:") :].strip()
    return address.lower() if _EMAIL_RE.fullmatch(address) else None


def _date(match: re.Match[str]) -> date:
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError as exc:
        raise IcsParseError("Invalid DATE value.") from exc


def _split_unquoted(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for character in text:
        if character == '"':
            in_quotes = not in_quotes
        if character == separator and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return parts
