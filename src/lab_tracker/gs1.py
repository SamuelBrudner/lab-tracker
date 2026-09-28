"""Pure parsing of GS1 element strings (GS1-128, GS1 DataMatrix, GS1 QR).

A GS1 symbol carries a concatenation of *elements*: an Application
Identifier (AI, 2-4 digits) followed by its data. Parsing needs two facts
from the GS1 General Specifications:

* The AI's digit count is fixed by its first two digits (``01`` is a
  2-digit AI, ``240`` a 3-digit one, ``3103`` a 4-digit one).
* Elements whose AI starts with one of the *predefined-length* prefixes
  (``00``-``04``, ``11``-``20``, ``31``-``36``, ``41``) have fixed-length,
  all-numeric data and need no separator. Every other element is
  variable-length and runs to the next FNC1 separator (transmitted as the
  ASCII group separator ``GS``, 0x1D) or the end of the string.

Both the transmitted form (``01<14 digits>17<6 digits>10<lot><GS>21...``,
optionally prefixed by a GS1 symbology identifier such as ``]C1``) and the
human-readable form (``(01)...(17)...(10)...``) are accepted. Nothing here
does I/O; :func:`parse_gs1_fields` is the one entry point the photo-code
decoder uses.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

GS = "\x1d"
# Other spellings of FNC1/GS that scanners and escaped text modes emit.
_SEPARATOR_ALIASES = ("<GS>", "␝")
# AIM symbology identifiers that announce GS1 data: GS1-128, GS1 DataBar,
# GS1 DataMatrix, GS1 QR, GS1 DotCode.
GS1_SYMBOLOGY_IDENTIFIERS = ("]C1", "]e0", "]d2", "]Q3", "]J1")

# AI digit count by the AI's first two digits (GS1 General Specifications,
# AI table). A prefix missing here is unassigned, so the string is not GS1.
_AI_LENGTH_BY_PREFIX: dict[str, int] = {
    **dict.fromkeys(("00", "01", "02", "03", "04"), 2),
    **{f"{value:02d}": 2 for value in range(10, 23)},
    **dict.fromkeys(("23", "24", "25"), 3),
    "30": 2,
    **dict.fromkeys(("31", "32", "33", "34", "35", "36"), 4),
    "37": 2,
    "39": 4,
    **dict.fromkeys(("40", "41", "42"), 3),
    "43": 4,
    "70": 4,
    "71": 3,
    "72": 4,
    **dict.fromkeys(("80", "81", "82"), 4),
    **{f"{value:02d}": 2 for value in range(90, 100)},
}
# Data length of the predefined-length (no separator needed) AIs, by prefix.
_PREDEFINED_DATA_LENGTH: dict[str, int] = {
    "00": 18,
    "01": 14,
    "02": 14,
    "03": 14,
    "04": 16,
    **{f"{value:02d}": 6 for value in range(11, 20)},
    "20": 2,
    **dict.fromkeys(("31", "32", "33", "34", "35", "36"), 6),
    "41": 13,
}
_HRI_AI = re.compile(r"\((\d{2,4})\)")

# The AIs Lab Tracker keeps, their note-metadata key, and their maximum
# data length (GS1 General Specifications).
GTIN_KEY = "barcode_gs1_gtin"
LOT_KEY = "barcode_gs1_lot"
EXPIRY_KEY = "barcode_gs1_expiry"
SERIAL_KEY = "barcode_gs1_serial"
CATALOG_KEY = "barcode_gs1_catalog"
GS1_METADATA_KEYS: tuple[str, ...] = (GTIN_KEY, LOT_KEY, EXPIRY_KEY, SERIAL_KEY, CATALOG_KEY)
_VARIABLE_FIELD_KEYS: dict[str, tuple[str, int]] = {
    "10": (LOT_KEY, 20),
    "21": (SERIAL_KEY, 20),
    "240": (CATALOG_KEY, 30),
}


class Gs1ParseError(ValueError):
    """The text is not a well-formed GS1 element string."""


@dataclass(frozen=True)
class Gs1Element:
    """One Application Identifier and its data."""

    ai: str
    data: str


def _ai_length(prefix: str) -> int:
    length = _AI_LENGTH_BY_PREFIX.get(prefix)
    if length is None:
        raise Gs1ParseError(f"unassigned GS1 AI prefix {prefix!r}")
    return length


def _strip_symbology_identifier(text: str) -> tuple[str, bool]:
    for identifier in GS1_SYMBOLOGY_IDENTIFIERS:
        if text.startswith(identifier):
            return text[len(identifier) :], True
    return text, False


def _parse_raw(text: str) -> tuple[Gs1Element, ...]:
    for alias in _SEPARATOR_ALIASES:
        text = text.replace(alias, GS)
    elements: list[Gs1Element] = []
    position = 0
    while position < len(text):
        if text[position] == GS:
            position += 1
            continue
        prefix = text[position : position + 2]
        if len(prefix) < 2 or not prefix.isdigit():
            raise Gs1ParseError("expected a numeric Application Identifier")
        ai_length = _ai_length(prefix)
        ai = text[position : position + ai_length]
        if len(ai) != ai_length or not ai.isdigit():
            raise Gs1ParseError(f"truncated Application Identifier {ai!r}")
        position += ai_length
        fixed = _PREDEFINED_DATA_LENGTH.get(prefix)
        if fixed is not None:
            data = text[position : position + fixed]
            if len(data) != fixed or not data.isdigit():
                raise Gs1ParseError(f"AI {ai} needs {fixed} digits")
            position += fixed
        else:
            end = text.find(GS, position)
            end = len(text) if end < 0 else end
            data = text[position:end]
            position = end
            if not data:
                raise Gs1ParseError(f"AI {ai} has no data")
        elements.append(Gs1Element(ai, data))
    if not elements:
        raise Gs1ParseError("no GS1 elements")
    return tuple(elements)


def _parse_human_readable(text: str) -> tuple[Gs1Element, ...]:
    parts = _HRI_AI.split(text)
    # re.split with one group: [before, ai1, data1, ai2, data2, ...]
    if len(parts) < 3 or parts[0].strip():
        raise Gs1ParseError("human-readable GS1 text must start with an (AI)")
    elements: list[Gs1Element] = []
    for ai, raw_data in zip(parts[1::2], parts[2::2], strict=True):
        if len(ai) != _ai_length(ai[:2]):
            raise Gs1ParseError(f"AI {ai!r} has the wrong length for its prefix")
        data = raw_data.strip()
        fixed = _PREDEFINED_DATA_LENGTH.get(ai[:2])
        if fixed is not None and (len(data) != fixed or not data.isdigit()):
            raise Gs1ParseError(f"AI {ai} needs {fixed} digits")
        if not data:
            raise Gs1ParseError(f"AI {ai} has no data")
        elements.append(Gs1Element(ai, data))
    return tuple(elements)


def parse_element_string(text: str) -> tuple[Gs1Element, ...]:
    """Parse a transmitted or human-readable GS1 element string.

    Raises :class:`Gs1ParseError` for anything that is not a complete,
    well-formed element string (unknown AI prefix, short fixed-length data,
    an AI without data, stray text before the first AI).
    """

    stripped = str(text or "").strip(" \t\r\n")
    stripped, _flagged = _strip_symbology_identifier(stripped)
    if stripped.startswith("("):
        return _parse_human_readable(stripped)
    return _parse_raw(stripped)


def gtin_check_digit(body: str) -> str:
    """The GS1 mod-10 check digit for a GTIN body (all digits but the last)."""

    if not body.isdigit():
        raise Gs1ParseError("GTIN digits must be numeric")
    total = sum(
        int(digit) * (3 if index % 2 == 0 else 1) for index, digit in enumerate(reversed(body))
    )
    return str((10 - total % 10) % 10)


def gs1_date_to_iso(yymmdd: str, *, today: date) -> str:
    """Convert a GS1 ``YYMMDD`` date to ``YYYY-MM-DD``.

    ``DD`` of ``00`` means the last day of the month. The century follows
    the GS1 rule: a year more than 50 years ahead of ``today`` belongs to
    the previous century, and one 50 or more years behind to the next.
    """

    if len(yymmdd) != 6 or not yymmdd.isdigit():
        raise Gs1ParseError("GS1 dates are six digits (YYMMDD)")
    yy, month, day = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    century = today.year // 100 * 100
    difference = yy - today.year % 100
    if 51 <= difference <= 99:
        century -= 100
    elif -99 <= difference <= -50:
        century += 100
    year = century + yy
    if not 1 <= month <= 12:
        raise Gs1ParseError(f"invalid GS1 month in {yymmdd}")
    last_day = calendar.monthrange(year, month)[1]
    if day == 0:
        day = last_day
    if not 1 <= day <= last_day:
        raise Gs1ParseError(f"invalid GS1 day in {yymmdd}")
    return date(year, month, day).isoformat()


def gs1_fields(elements: Iterable[Gs1Element], *, today: date) -> dict[str, str]:
    """Map the retained AIs to ``barcode_gs1_*`` metadata values.

    AI 01 is kept only with a valid check digit and AI 17 only as a valid
    date; an over-long lot, serial, or catalog number is malformed. Each of
    those raises :class:`Gs1ParseError`. A repeated AI keeps its first value;
    AIs Lab Tracker does not keep are ignored.
    """

    fields: dict[str, str] = {}
    for element in elements:
        if element.ai == "01":
            if gtin_check_digit(element.data[:-1]) != element.data[-1]:
                raise Gs1ParseError("GTIN check digit does not match")
            fields.setdefault(GTIN_KEY, element.data)
        elif element.ai == "17":
            fields.setdefault(EXPIRY_KEY, gs1_date_to_iso(element.data, today=today))
        elif element.ai in _VARIABLE_FIELD_KEYS:
            key, maximum = _VARIABLE_FIELD_KEYS[element.ai]
            if len(element.data) > maximum:
                raise Gs1ParseError(f"AI {element.ai} data exceeds {maximum} characters")
            fields.setdefault(key, element.data)
    return fields


def parse_gs1_fields(text: str, *, today: date, is_gs1: bool = False) -> dict[str, str] | None:
    """The ``barcode_gs1_*`` fields a decoded symbol carries, or ``None``.

    ``is_gs1`` is the decoder's own verdict (FNC1 in first position); a GS1
    symbology identifier prefix counts too. Without either, only the
    parenthesized human-readable form is treated as GS1, so an ordinary
    numeric barcode is never misread as an element string. ``None`` means
    not GS1, malformed GS1, or GS1 with none of the retained AIs.
    """

    stripped = str(text or "").strip()
    _rest, flagged = _strip_symbology_identifier(stripped)
    if not (is_gs1 or flagged or stripped.startswith("(")):
        return None
    try:
        fields = gs1_fields(parse_element_string(stripped), today=today)
    except Gs1ParseError:
        return None
    return fields or None


__all__ = [
    "CATALOG_KEY",
    "EXPIRY_KEY",
    "GS",
    "GS1_METADATA_KEYS",
    "GS1_SYMBOLOGY_IDENTIFIERS",
    "GTIN_KEY",
    "Gs1Element",
    "Gs1ParseError",
    "LOT_KEY",
    "SERIAL_KEY",
    "gs1_date_to_iso",
    "gs1_fields",
    "gtin_check_digit",
    "parse_element_string",
    "parse_gs1_fields",
]
