"""Bounded header sniffers for instrument files that ``lt watch`` observes.

Three formats carry acquisition facts in their headers, and reading them is
deterministic decoding of machine-readable structure (not OCR, not a model):

* FCS 2.0/3.0/3.1/3.2 flow cytometry files: the HEADER's TEXT offsets and
  the delimiter-separated TEXT segment (``$DATE``, ``$BTIM``, ``$ETIM``,
  ``$CYT``, ``$FIL``, ``$TOT``, ``$PAR``, ``$SRC``, ``$OP``).
* OME-TIFF: the first IFD's ImageDescription holds OME-XML, parsed with DTDs
  refused (so no external entities and no entity expansion) for the first
  image's name, ``AcquisitionDate`` and ``Pixels`` sizes and the first
  instrument's microscope and objective.
* NWB (HDF5): with ``h5py`` importable, ``session_start_time``,
  ``identifier``, ``session_description`` and ``/general/subject/subject_id``.
  ``h5py`` is imported only when an NWB candidate is sniffed; without it the
  file is still labelled ``nwb`` with ``format_sniff_error``.

:func:`sniff_format` returns flat ``format_*`` metadata (``format_kind`` is
``fcs``, ``ome_tiff`` or ``nwb``) and never raises: a malformed header yields
``format_kind`` plus ``format_sniff_error``, an unrecognized file ``{}``. No
more than :data:`MAX_SNIFF_BYTES` are read from any file (h5py reads only the
HDF5 metadata it is asked for), and every stored value is bounded to
:data:`MAX_VALUE_CHARS`.

``format_acquired_at`` is ISO-8601 UTC. A header clock with a UTC offset is
converted exactly (``format_acquired_at_timezone`` = ``header``); a clock
without one (FCS ``$DATE``/``$BTIM``, a naive OME ``AcquisitionDate``) is read
as local time on the machine running ``lt watch`` -- usually the acquisition
workstation -- and labelled ``local:+HH:MM`` with the offset used.
"""

from __future__ import annotations

import importlib
import os
import re
import struct
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, BinaryIO

FORMAT_SNIFF_ENV = "LAB_TRACKER_WATCH_FORMAT_SNIFF"
FORMAT_KIND_KEY = "format_kind"
FORMAT_ACQUIRED_AT_KEY = "format_acquired_at"
FORMAT_ACQUIRED_AT_TIMEZONE_KEY = "format_acquired_at_timezone"
FORMAT_SNIFF_ERROR_KEY = "format_sniff_error"
KIND_FCS = "fcs"
KIND_OME_TIFF = "ome_tiff"
KIND_NWB = "nwb"

# Total bytes any one sniff may read from a file.
MAX_SNIFF_BYTES = 2 * 1024 * 1024
MAX_FCS_TEXT_BYTES = 1024 * 1024
MAX_OME_XML_BYTES = 1024 * 1024
MAX_TIFF_IFD_ENTRIES = 4096
MAX_VALUE_CHARS = 256

MetadataValue = str | int
FormatFields = dict[str, MetadataValue]

_DISABLED_VALUES = {"0", "false", "no", "off"}
_HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
_TIFF_MAGICS = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
_OME_SUFFIXES = (".ome.tif", ".ome.tiff", ".ome.tf2", ".ome.tf8", ".ome.btf")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_DTD_MARKUP = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
_ISO_DATETIME = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
    r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?\s*$",
    re.IGNORECASE,
)
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
_FCS_KEYWORDS = {
    "$DATE": "format_date",
    "$BTIM": "format_begin_time",
    "$ETIM": "format_end_time",
    "$CYT": "format_instrument",
    "$FIL": "format_original_filename",
    "$SRC": "format_source",
    "$OP": "format_operator",
}
_FCS_COUNT_KEYWORDS = {"$TOT": "format_event_count", "$PAR": "format_parameter_count"}


class SniffError(ValueError):
    """A recognized format whose header is malformed or over a bound."""


class _NotThisFormat(Exception):
    """The file is not the candidate format after all."""


class _BoundedReader:
    """Positioned reads from an open file under a total byte budget."""

    def __init__(self, handle: BinaryIO, *, budget: int | None = None) -> None:
        self._handle = handle
        self._budget = MAX_SNIFF_BYTES if budget is None else budget
        self._remaining = self._budget

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0:
            raise SniffError("negative offset or length in header")
        if size > self._remaining:
            raise SniffError(f"header would need more than {self._budget} bytes")
        self._handle.seek(offset)
        data = self._handle.read(size)
        self._remaining -= len(data)
        return data


def _local_timezone() -> tzinfo | None:
    """The zone naive header clocks are read in; ``None`` means the system zone."""

    return None


def sniff_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """False when ``LAB_TRACKER_WATCH_FORMAT_SNIFF`` is set to 0/false/no/off."""

    value = (environ if environ is not None else os.environ).get(FORMAT_SNIFF_ENV, "")
    return value.strip().lower() not in _DISABLED_VALUES


def watch_format_fields(path: str | Path) -> FormatFields:
    """``format_*`` event-source fields for a watched file (``{}`` when disabled)."""

    if not sniff_enabled():
        return {}
    return sniff_format(path)


def sniff_format(path: str | Path, *, local_tz: tzinfo | None = None) -> FormatFields:
    """Sniff one file's header; never raises (see the module docstring)."""

    resolved = Path(path)
    zone = local_tz if local_tz is not None else _local_timezone()
    kind: str | None = None
    try:
        with resolved.open("rb") as handle:
            reader = _BoundedReader(handle)
            head = reader.read_at(0, 16)
            candidate = _candidate(head, resolved.name)
            if candidate is None:
                return {}
            kind, sniffer = candidate
            fields = sniffer(reader, resolved, zone)
    except _NotThisFormat:
        return {}
    except Exception as exc:
        if kind is None:
            return {}
        return {FORMAT_KIND_KEY: kind, FORMAT_SNIFF_ERROR_KEY: _error_text(exc)}
    return {FORMAT_KIND_KEY: kind, **fields}


Sniffer = Callable[[_BoundedReader, Path, tzinfo | None], FormatFields]


def _candidate(head: bytes, name: str) -> tuple[str, Sniffer] | None:
    lowered = name.lower()
    if head[:3] == b"FCS":
        return KIND_FCS, _sniff_fcs
    if head[:4] in _TIFF_MAGICS:
        return KIND_OME_TIFF, _sniff_ome_tiff
    if lowered.endswith(".nwb") or head[:8] == _HDF5_SIGNATURE:
        return KIND_NWB, _sniff_nwb
    return None


def _error_text(exc: BaseException) -> str:
    return _bounded(str(exc) or type(exc).__name__)


def _bounded(value: object) -> str:
    text = _CONTROL_CHARACTERS.sub(" ", str(value)).strip()
    if len(text) > MAX_VALUE_CHARS:
        return text[: MAX_VALUE_CHARS - 1] + "…"
    return text


def _count(value: str) -> int | None:
    text = value.strip()
    return int(text) if text.isdigit() else None


# --- time -----------------------------------------------------------------------


def _offset_label(offset: timedelta | None) -> str:
    minutes = int((offset or timedelta()).total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    hours, remainder = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{remainder:02d}"


def _acquired_at(moment: datetime | None, zone: tzinfo | None) -> FormatFields:
    """UTC ISO-8601 plus how its timezone was decided; ``{}`` when not convertible."""

    if moment is None:
        return {}
    try:
        if moment.tzinfo is not None:
            basis = "header"
            aware = moment
        else:
            aware = moment.replace(tzinfo=zone) if zone is not None else moment.astimezone()
            basis = f"local:{_offset_label(aware.utcoffset())}"
        return {
            FORMAT_ACQUIRED_AT_KEY: aware.astimezone(timezone.utc).isoformat(),
            FORMAT_ACQUIRED_AT_TIMEZONE_KEY: basis,
        }
    except (OverflowError, OSError, ValueError):
        # Out-of-range clocks (e.g. before 1970 on Windows) are left out.
        return {}


def parse_iso_datetime(text: str) -> datetime | None:
    """An ISO-8601 / xsd:dateTime value (naive when it has no offset), or ``None``."""

    match = _ISO_DATETIME.match(text or "")
    if match is None:
        return None
    year, month, day, hour, minute = (int(match.group(index)) for index in range(1, 6))
    second = int(match.group(6) or 0)
    micro = int((match.group(7) or "0")[:6].ljust(6, "0"))
    zone: tzinfo | None = None
    designator = match.group(8)
    if designator:
        if designator.upper() == "Z":
            zone = timezone.utc
        else:
            digits = designator[1:].replace(":", "")
            delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
            zone = timezone(-delta if designator[0] == "-" else delta)
    try:
        return datetime(year, month, day, hour, minute, second, micro, tzinfo=zone)
    except ValueError:
        return None


def _fcs_date(text: str) -> tuple[int, int, int] | None:
    value = text.strip()
    match = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{2}|\d{4})", value)
    if match:
        month = _MONTHS.get(match.group(2).upper())
        if month is None:
            return None
        year = int(match.group(3))
        if len(match.group(3)) == 2:
            # FCS 2.0 two-digit years: 1900s from 70, otherwise 2000s.
            year += 1900 if year >= 70 else 2000
        return year, month, int(match.group(1))
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return None


def _fcs_time(text: str) -> tuple[int, int, int] | None:
    # hh:mm:ss, optionally followed by :tt (1/60 s, FCS 3.0) or .cc (FCS 3.1).
    match = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})(?:[:.]\d+)?", text.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


# --- FCS ------------------------------------------------------------------------


def _fcs_offset(raw: bytes) -> int | None:
    text = raw.decode("ascii", errors="replace").strip()
    return int(text) if text.isdigit() else None


def parse_fcs_text(raw: bytes) -> dict[str, str]:
    """Keyword/value pairs of an FCS TEXT segment, keywords upper-cased.

    The first byte is the delimiter; a doubled delimiter inside a keyword or
    value is an escaped literal delimiter. The first occurrence of a keyword
    wins, and a trailing unpaired token is ignored.
    """

    text = raw.decode("utf-8", errors="replace")
    if len(text) < 2:
        raise SniffError("FCS TEXT segment is empty")
    delimiter = text[0]
    tokens: list[str] = []
    current: list[str] = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == delimiter:
            if index + 1 < len(text) and text[index + 1] == delimiter:
                current.append(delimiter)
                index += 2
                continue
            tokens.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    if current:
        tokens.append("".join(current))
    keywords: dict[str, str] = {}
    for key, value in zip(tokens[0::2], tokens[1::2], strict=False):
        keywords.setdefault(key.strip().upper(), value)
    return keywords


def _sniff_fcs(reader: _BoundedReader, _path: Path, zone: tzinfo | None) -> FormatFields:
    header = reader.read_at(0, 58)
    if len(header) < 58:
        raise SniffError("FCS HEADER is truncated")
    version = header[:6].decode("ascii", errors="replace")
    if not re.fullmatch(r"FCS\d\.\d", version):
        raise SniffError("FCS HEADER has no version")
    fields: FormatFields = {"format_version": version}
    text_start = _fcs_offset(header[10:18])
    text_end = _fcs_offset(header[18:26])
    if text_start is None or text_end is None or text_start < 58 or text_end < text_start:
        raise SniffError("FCS HEADER has invalid TEXT offsets")
    length = text_end - text_start + 1
    if length > MAX_FCS_TEXT_BYTES:
        return {
            **fields,
            FORMAT_SNIFF_ERROR_KEY: f"FCS TEXT segment is {length} bytes "
            f"(limit {MAX_FCS_TEXT_BYTES})",
        }
    raw = reader.read_at(text_start, length)
    if len(raw) < length:
        raise SniffError("FCS TEXT segment is truncated")
    keywords = parse_fcs_text(raw)
    for keyword, key in _FCS_KEYWORDS.items():
        if keywords.get(keyword, "").strip():
            fields[key] = _bounded(keywords[keyword])
    for keyword, key in _FCS_COUNT_KEYWORDS.items():
        parsed = _count(keywords.get(keyword, ""))
        if parsed is not None:
            fields[key] = parsed
    fields.update(_fcs_acquired_at(keywords, zone))
    return fields


def _fcs_acquired_at(keywords: Mapping[str, str], zone: tzinfo | None) -> FormatFields:
    # FCS 3.2 records an ISO-8601 start, possibly with an offset.
    begin = parse_iso_datetime(keywords.get("$BEGINDATETIME", ""))
    if begin is None:
        day = _fcs_date(keywords.get("$DATE", ""))
        clock = _fcs_time(keywords.get("$BTIM", ""))
        if day is None or clock is None:
            return {}
        try:
            begin = datetime(*day, *clock)
        except ValueError:
            return {}
    return _acquired_at(begin, zone)


# --- OME-TIFF -------------------------------------------------------------------


def _image_description(reader: _BoundedReader) -> tuple[bytes, bool] | None:
    """The first IFD's ImageDescription and whether it was cut at the cap."""

    head = reader.read_at(0, 16)
    order = {b"II": "<", b"MM": ">"}.get(head[:2])
    if order is None or len(head) < 8:
        raise SniffError("TIFF header is truncated")
    magic = struct.unpack(order + "H", head[2:4])[0]
    big = magic == 43
    if big:
        if len(head) < 16 or struct.unpack(order + "H", head[4:6])[0] != 8:
            raise SniffError("BigTIFF header is malformed")
        ifd_offset = struct.unpack(order + "Q", head[8:16])[0]
        # BigTIFF: 8-byte entry count; 20-byte entries with 8-byte counts/offsets.
        count_format, count_size, entry_size, value_format = "Q", 8, 20, "Q"
    else:
        ifd_offset = struct.unpack(order + "I", head[4:8])[0]
        # Classic TIFF: 2-byte entry count; 12-byte entries with 4-byte counts/offsets.
        count_format, count_size, entry_size, value_format = "H", 2, 12, "I"
    value_size = struct.calcsize(value_format)
    raw_count = reader.read_at(ifd_offset, count_size)
    if len(raw_count) < count_size:
        raise SniffError("TIFF IFD offset is past the end of the file")
    count = struct.unpack(order + count_format, raw_count)[0]
    if count > MAX_TIFF_IFD_ENTRIES:
        raise SniffError(f"TIFF IFD has {count} entries (limit {MAX_TIFF_IFD_ENTRIES})")
    entries = reader.read_at(ifd_offset + count_size, count * entry_size)
    if len(entries) < count * entry_size:
        raise SniffError("TIFF IFD is truncated")
    for index in range(count):
        entry = entries[index * entry_size : (index + 1) * entry_size]
        tag, field_type = struct.unpack(order + "HH", entry[:4])
        if tag != 270:  # ImageDescription
            continue
        if field_type not in (1, 2, 7):  # BYTE, ASCII, UNDEFINED
            raise SniffError("TIFF ImageDescription has an unexpected type")
        length = struct.unpack(order + value_format, entry[4 : 4 + value_size])[0]
        inline = entry[4 + value_size :]
        if length <= len(inline):
            return inline[:length].rstrip(b"\x00"), False
        offset = struct.unpack(order + value_format, inline)[0]
        wanted = min(length, MAX_OME_XML_BYTES)
        data = reader.read_at(offset, wanted)
        if len(data) < wanted:
            raise SniffError("TIFF ImageDescription is truncated")
        return data.rstrip(b"\x00"), length > MAX_OME_XML_BYTES
    return None


class _StopParsing(Exception):
    """Everything wanted from the OME-XML has been read."""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _OmeTarget:
    """Collect the first image and instrument facts, stopping at the first Pixels."""

    def __init__(self) -> None:
        self.root: str | None = None
        self.namespace = ""
        self.fields: FormatFields = {}
        self.acquisition_date: str | None = None
        self._stack: list[str] = []
        self._images = 0
        self._instruments = 0
        self._date_parts: list[str] | None = None

    def start(self, tag: str, attrib: Mapping[str, str]) -> None:
        name = _local_name(tag)
        if self.root is None:
            self.root = name
            if name != "OME":
                raise _NotThisFormat()
            self.namespace = tag[1:].split("}", 1)[0] if tag.startswith("{") else ""
        parent = self._stack[-1] if self._stack else None
        grandparent = self._stack[-2] if len(self._stack) > 1 else None
        self._stack.append(name)
        if name == "Instrument" and parent == "OME":
            self._instruments += 1
        elif parent == "Instrument" and grandparent == "OME" and self._instruments == 1:
            self._instrument_part(name, attrib)
        elif name == "Image" and parent == "OME":
            self._images += 1
            if self._images == 1 and attrib.get("Name"):
                self.fields["format_image_name"] = _bounded(attrib["Name"])
        elif name == "AcquisitionDate" and parent == "Image" and self._images == 1:
            self._date_parts = []
        elif name == "Pixels" and parent == "Image" and self._images == 1:
            self._pixels(attrib)
            raise _StopParsing()

    def _instrument_part(self, name: str, attrib: Mapping[str, str]) -> None:
        label = " ".join(part for part in (attrib.get("Manufacturer"), attrib.get("Model")) if part)
        if name == "Microscope" and label:
            self.fields.setdefault("format_instrument", _bounded(label))
        elif name == "Objective" and "format_objective" not in self.fields:
            if label:
                self.fields["format_objective"] = _bounded(attrib.get("Model") or label)
            if attrib.get("NominalMagnification"):
                self.fields["format_objective_magnification"] = _bounded(
                    attrib["NominalMagnification"]
                )

    def _pixels(self, attrib: Mapping[str, str]) -> None:
        for axis in ("X", "Y", "Z", "C", "T"):
            parsed = _count(attrib.get(f"Size{axis}", ""))
            if parsed is not None:
                self.fields[f"format_size_{axis.lower()}"] = parsed
        if attrib.get("Type"):
            self.fields["format_pixel_type"] = _bounded(attrib["Type"])

    def end(self, tag: str) -> None:
        name = self._stack.pop() if self._stack else _local_name(tag)
        if name == "AcquisitionDate" and self._date_parts is not None:
            self.acquisition_date = "".join(self._date_parts).strip()[:MAX_VALUE_CHARS]
            self._date_parts = None

    def data(self, text: str) -> None:
        if self._date_parts is not None:
            self._date_parts.append(text)

    def close(self) -> None:
        return None


def _xml_parser(target: _OmeTarget) -> Any:
    """defusedxml's parser when installed; otherwise the stdlib parser.

    Either way the caller has already refused any document with DTD markup,
    so there are no entity declarations to expand or fetch.
    """

    try:
        defused = importlib.import_module("defusedxml.ElementTree")
    except ImportError:
        from xml.etree import ElementTree

        return ElementTree.XMLParser(target=target)
    return defused.DefusedXMLParser(
        target=target, forbid_dtd=True, forbid_entities=True, forbid_external=True
    )


def parse_ome_xml(data: bytes, *, truncated: bool, zone: tzinfo | None) -> FormatFields:
    """``format_*`` fields from OME-XML bytes; raises ``_NotThisFormat`` for other XML."""

    # Imported here so `lt` startup (hooks run on every commit) never pays for it.
    from xml.etree import ElementTree

    text = data.decode("utf-8", errors="replace")
    if "<OME" not in text[:65536] and ":OME" not in text[:65536]:
        raise _NotThisFormat()
    if _DTD_MARKUP.search(text):
        raise SniffError("OME-XML with a DTD or entity declarations is refused")
    target = _OmeTarget()
    parser = _xml_parser(target)
    error = ""
    try:
        parser.feed(_XML_DECLARATION.sub("", text, count=1).encode("utf-8"))
        parser.close()
    except _StopParsing:
        pass
    except ElementTree.ParseError as exc:
        error = (
            f"OME-XML is longer than {MAX_OME_XML_BYTES} bytes; its first image was not reached"
            if truncated
            else f"OME-XML is malformed: {exc}"
        )
    if target.root is None and not error:
        raise _NotThisFormat()
    fields: FormatFields = {}
    version = target.namespace.rstrip("/").rsplit("/", 1)[-1]
    if version:
        fields["format_version"] = _bounded(version)
    fields.update(target.fields)
    if target.acquisition_date:
        fields.update(_acquired_at(parse_iso_datetime(target.acquisition_date), zone))
    if error:
        fields[FORMAT_SNIFF_ERROR_KEY] = _bounded(error)
    return fields


def _sniff_ome_tiff(reader: _BoundedReader, path: Path, zone: tzinfo | None) -> FormatFields:
    named_ome = path.name.lower().endswith(_OME_SUFFIXES)
    try:
        description = _image_description(reader)
        if description is None:
            raise _NotThisFormat()
        return parse_ome_xml(description[0], truncated=description[1], zone=zone)
    except _NotThisFormat:
        if named_ome:
            return {FORMAT_SNIFF_ERROR_KEY: "no OME-XML ImageDescription in the first IFD"}
        raise
    except SniffError:
        if named_ome:
            raise
        # A malformed TIFF that does not claim to be OME is just not sniffed.
        raise _NotThisFormat() from None


# --- NWB ------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    item = getattr(value, "item", None)
    if callable(item) and getattr(value, "shape", None) == ():
        return _text(item())
    return None


def _scalar(group: Any, name: str) -> str | None:
    """A scalar dataset under ``group`` (NWB 2.x), else a same-named attribute."""

    try:
        node = group.get(name)
    except Exception:
        node = None
    if node is not None and getattr(node, "shape", None) == ():
        return _text(node[()])
    try:
        return _text(group.attrs.get(name))
    except Exception:
        return None


def _sniff_nwb(_reader: _BoundedReader, path: Path, zone: tzinfo | None) -> FormatFields:
    named_nwb = path.name.lower().endswith(".nwb")
    try:
        h5py = importlib.import_module("h5py")
    except ImportError:
        if named_nwb:
            return {FORMAT_SNIFF_ERROR_KEY: "h5py not installed"}
        raise _NotThisFormat() from None
    with h5py.File(str(path), "r") as handle:
        attrs = handle.attrs
        neurodata_type = _text(attrs.get("neurodata_type"))
        nwb_version = _text(attrs.get("nwb_version"))
        if not (named_nwb or neurodata_type == "NWBFile" or nwb_version):
            raise _NotThisFormat()
        fields: FormatFields = {}
        if nwb_version:
            fields["format_version"] = _bounded(nwb_version)
        for name in ("session_start_time", "identifier", "session_description"):
            value = _scalar(handle, name)
            if value and value.strip():
                fields[f"format_{name}"] = _bounded(value)
        subject = handle.get("general/subject")
        subject_id = _scalar(subject, "subject_id") if subject is not None else None
        if subject_id and subject_id.strip():
            fields["format_subject_id"] = _bounded(subject_id)
    start = parse_iso_datetime(str(fields.get("format_session_start_time", "")))
    fields.update(_acquired_at(start, zone))
    return fields


__all__ = [
    "FORMAT_ACQUIRED_AT_KEY",
    "FORMAT_ACQUIRED_AT_TIMEZONE_KEY",
    "FORMAT_KIND_KEY",
    "FORMAT_SNIFF_ENV",
    "FORMAT_SNIFF_ERROR_KEY",
    "KIND_FCS",
    "KIND_NWB",
    "KIND_OME_TIFF",
    "MAX_FCS_TEXT_BYTES",
    "MAX_OME_XML_BYTES",
    "MAX_SNIFF_BYTES",
    "MAX_TIFF_IFD_ENTRIES",
    "MAX_VALUE_CHARS",
    "SniffError",
    "parse_fcs_text",
    "parse_iso_datetime",
    "parse_ome_xml",
    "sniff_enabled",
    "sniff_format",
    "watch_format_fields",
]
