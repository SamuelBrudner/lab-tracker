"""Instrument-file header sniffers used by ``lt watch``: bounded, fail-soft, pure.

Fixture files are written programmatically (FCS HEADER/TEXT bytes, minimal
classic and BigTIFF files with OME-XML); no binaries are committed. h5py is
not a dependency, so NWB is exercised both without it and with a stand-in
module that mimics the few ``h5py.File`` calls the sniffer makes.
"""

from __future__ import annotations

import importlib
import json
import random
import re
import struct
import sys
import types
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from lab_tracker_client import LabTracker
from lab_tracker_client import format_sniffers as sniffers
from lab_tracker_client.format_sniffers import (
    parse_fcs_text,
    parse_iso_datetime,
    sniff_format,
    watch_format_fields,
)
from lab_tracker_client.watch import (
    _event_metadata,
    init_config,
    read_event,
    scan_watch,
    sync_outbox,
)

PLUS_TWO = timezone(timedelta(hours=2))
MINUS_FIVE = timezone(timedelta(hours=-5))


# --- fixture builders -----------------------------------------------------------


def _fcs(
    keywords: dict[str, str],
    *,
    version: str = "FCS3.1",
    delimiter: str = "/",
    text_start: int = 256,
    data: bytes = b"\x00" * 16,
) -> bytes:
    def escape(value: str) -> str:
        return value.replace(delimiter, delimiter * 2)

    text = delimiter + "".join(
        f"{escape(key)}{delimiter}{escape(value)}{delimiter}" for key, value in keywords.items()
    )
    text_bytes = text.encode("utf-8")
    text_end = text_start + len(text_bytes) - 1
    data_start = text_end + 1
    data_end = data_start + len(data) - 1
    header = (
        f"{version}    {text_start:>8}{text_end:>8}{data_start:>8}{data_end:>8}{0:>8}{0:>8}"
    ).encode("ascii")
    assert len(header) == 58
    return header + b" " * (text_start - 58) + text_bytes + data


def _tiff(description: bytes | None, *, big: bool = False, order: str = "<") -> bytes:
    """A one-IFD TIFF (classic or BigTIFF) with an optional ImageDescription."""

    marker = b"II" if order == "<" else b"MM"
    short_inline = struct.pack(order + "H", 8)
    entries: list[tuple[int, int, int, bytes]] = [
        (256, 3, 1, short_inline),  # ImageWidth
        (257, 3, 1, short_inline),  # ImageLength
    ]
    if description is not None:
        entries.append((270, 2, len(description) + 1, description + b"\x00"))
    if big:
        header = marker + struct.pack(order + "HHHQ", 43, 8, 0, 16)
        ifd_offset, count_format, entry_size, value_format, inline_size = 16, "Q", 20, "Q", 8
    else:
        header = marker + struct.pack(order + "HI", 42, 8)
        ifd_offset, count_format, entry_size, value_format, inline_size = 8, "H", 12, "I", 4
    count_size = struct.calcsize(count_format)
    data_offset = ifd_offset + count_size + entry_size * len(entries) + inline_size
    ifd = struct.pack(order + count_format, len(entries))
    payload = b""
    for tag, field_type, count, value in entries:
        ifd += struct.pack(order + "HH", tag, field_type) + struct.pack(order + value_format, count)
        if len(value) <= inline_size:
            ifd += value.ljust(inline_size, b"\x00")
        else:
            ifd += struct.pack(order + value_format, data_offset + len(payload))
            payload += value
    ifd += b"\x00" * inline_size  # next IFD: none
    return header + ifd + payload


OME_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06" Creator="test">
  <Instrument ID="Instrument:0">
    <Microscope Manufacturer="Zeiss" Model="LSM 980"/>
    <Objective ID="Objective:0" Manufacturer="Zeiss" Model="Plan-Apochromat 63x/1.40 Oil"
      NominalMagnification="63"/>
  </Instrument>
  <Instrument ID="Instrument:1"><Microscope Model="Second scope"/></Instrument>
  <Image ID="Image:0" Name="slice_01">
    <AcquisitionDate>{date}</AcquisitionDate>
    <Pixels ID="Pixels:0" DimensionOrder="XYZCT" Type="uint16"
      SizeX="512" SizeY="256" SizeZ="10" SizeC="2" SizeT="1">
      <Channel ID="Channel:0:0"/>
      <TiffData/>
    </Pixels>
  </Image>
  <Image ID="Image:1" Name="second"><Pixels SizeX="1" SizeY="1"/></Image>
</OME>
"""


def _write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


FCS_KEYWORDS = {
    "$BEGINANALYSIS": "0",
    "$DATE": "05-MAR-2024",
    "$BTIM": "14:22:10.25",
    "$ETIM": "14:31:02",
    "$CYT": "BD LSRFortessa",
    "$FIL": "tube_004.fcs",
    "$TOT": "250000",
    "$PAR": "12",
    "$SRC": "mouse 7 / spleen",
    "$OP": "sbrudner",
    "$P1N": "FSC-A",
}


# --- FCS -----------------------------------------------------------------------------


def test_fcs_31_header_and_text_become_format_metadata(tmp_path: Path) -> None:
    path = _write(tmp_path, "tube_004.fcs", _fcs(FCS_KEYWORDS))

    assert sniff_format(path, local_tz=PLUS_TWO) == {
        "format_kind": "fcs",
        "format_version": "FCS3.1",
        "format_date": "05-MAR-2024",
        "format_begin_time": "14:22:10.25",
        "format_end_time": "14:31:02",
        "format_instrument": "BD LSRFortessa",
        "format_original_filename": "tube_004.fcs",
        "format_source": "mouse 7 / spleen",
        "format_operator": "sbrudner",
        "format_event_count": 250000,
        "format_parameter_count": 12,
        "format_acquired_at": "2024-03-05T12:22:10+00:00",
        "format_acquired_at_timezone": "local:+02:00",
    }


def test_fcs_text_unescapes_doubled_delimiters_and_upper_cases_keywords() -> None:
    raw = b"|$cyt|Aria||II|$SRC|a||b|||$OP|me|"

    assert parse_fcs_text(raw) == {"$CYT": "Aria|II", "$SRC": "a|b|", "$OP": "me"}
    # Missing trailing delimiter and an unpaired last token are tolerated.
    assert parse_fcs_text(b"/$TOT/5/$PAR") == {"$TOT": "5"}


def test_fcs_20_two_digit_years_and_60ths_of_a_second(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "old.fcs",
        _fcs({"$DATE": "05-mar-99", "$BTIM": "09:00:01:30"}, version="FCS2.0", delimiter="\\"),
    )

    fields = sniff_format(path, local_tz=MINUS_FIVE)

    assert fields["format_version"] == "FCS2.0"
    assert fields["format_acquired_at"] == "1999-03-05T14:00:01+00:00"
    assert fields["format_acquired_at_timezone"] == "local:-05:00"


def test_fcs_32_begin_datetime_with_an_offset_is_exact(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "new.fcs",
        _fcs(
            {
                "$BEGINDATETIME": "2025-01-02T03:04:05+01:00",
                "$DATE": "01-JAN-2000",
                "$BTIM": "00:00:00",
            },
            version="FCS3.2",
        ),
    )

    fields = sniff_format(path, local_tz=MINUS_FIVE)

    assert fields["format_acquired_at"] == "2025-01-02T02:04:05+00:00"
    assert fields["format_acquired_at_timezone"] == "header"


def test_fcs_without_a_parseable_clock_has_no_acquired_at(tmp_path: Path) -> None:
    path = _write(tmp_path, "x.fcs", _fcs({"$DATE": "sometime", "$BTIM": "14:22:10"}))

    fields = sniff_format(path, local_tz=PLUS_TWO)

    assert fields["format_date"] == "sometime"
    assert "format_acquired_at" not in fields


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (b"FCS3.1    ", "truncated"),
        (b"FCSx.y" + b" " * 60, "no version"),
        (b"FCS3.1    " + b"       0" * 6, "invalid TEXT offsets"),
        (b"FCS3.1    " + b"     100      90" + b"       0" * 4, "invalid TEXT offsets"),
        (b"FCS3.1    " + b"     100    5000" + b"       0" * 4 + b" " * 60, "truncated"),
    ],
)
def test_malformed_fcs_headers_record_a_sniff_error(
    tmp_path: Path, content: bytes, error: str
) -> None:
    fields = sniff_format(_write(tmp_path, "bad.fcs", content))

    assert fields["format_kind"] == "fcs"
    assert error in str(fields["format_sniff_error"])


def test_an_oversized_fcs_text_segment_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sniffers, "MAX_FCS_TEXT_BYTES", 64)
    path = _write(tmp_path, "big.fcs", _fcs(FCS_KEYWORDS))

    fields = sniff_format(path)

    assert fields["format_version"] == "FCS3.1"
    assert "limit 64" in str(fields["format_sniff_error"])
    assert "format_instrument" not in fields


def test_the_total_read_budget_bounds_every_sniff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sniffers, "MAX_SNIFF_BYTES", 300)
    keywords = {**FCS_KEYWORDS, "$COM": "x" * 1000}
    reads: list[int] = []
    original = sniffers._BoundedReader.read_at

    def spy(self: Any, offset: int, size: int) -> bytes:
        data = original(self, offset, size)
        reads.append(len(data))
        return data

    monkeypatch.setattr(sniffers._BoundedReader, "read_at", spy)

    fields = sniff_format(_write(tmp_path, "c.fcs", _fcs(keywords)))

    assert "more than 300 bytes" in str(fields["format_sniff_error"])
    assert sum(reads) <= 300


# --- OME-TIFF --------------------------------------------------------------------------


EXPECTED_OME = {
    "format_kind": "ome_tiff",
    "format_version": "2016-06",
    "format_instrument": "Zeiss LSM 980",
    "format_objective": "Plan-Apochromat 63x/1.40 Oil",
    "format_objective_magnification": "63",
    "format_image_name": "slice_01",
    "format_acquired_at": "2024-03-05T14:22:10.123000+00:00",
    "format_acquired_at_timezone": "header",
    "format_size_x": 512,
    "format_size_y": 256,
    "format_size_z": 10,
    "format_size_c": 2,
    "format_size_t": 1,
    "format_pixel_type": "uint16",
}


@pytest.mark.parametrize(("big", "order"), [(False, "<"), (False, ">"), (True, "<"), (True, ">")])
def test_ome_tiff_image_description_is_parsed(tmp_path: Path, big: bool, order: str) -> None:
    xml = OME_XML.format(date="2024-03-05T14:22:10.123Z").encode("utf-8")
    path = _write(tmp_path, "stack.ome.tif", _tiff(xml, big=big, order=order))

    assert sniff_format(path, local_tz=PLUS_TWO) == EXPECTED_OME


def test_a_naive_ome_acquisition_date_is_read_as_local_time(tmp_path: Path) -> None:
    xml = OME_XML.format(date="2024-03-05T14:22:10").encode("utf-8")
    # Detected by content, whatever the file is called.
    path = _write(tmp_path, "renamed.tiff", _tiff(xml))

    fields = sniff_format(path, local_tz=MINUS_FIVE)

    assert fields["format_acquired_at"] == "2024-03-05T19:22:10+00:00"
    assert fields["format_acquired_at_timezone"] == "local:-05:00"


def test_plain_tiffs_are_not_ome(tmp_path: Path) -> None:
    assert sniff_format(_write(tmp_path, "scan.tif", _tiff(b"ImageJ=1.54f"))) == {}
    assert sniff_format(_write(tmp_path, "scan2.tif", _tiff(None))) == {}
    assert (
        sniff_format(_write(tmp_path, "other.tif", _tiff(b"<svg xmlns='x'><OMEGA/></svg>"))) == {}
    )
    # A TIFF that is malformed but does not claim OME is simply not sniffed.
    assert sniff_format(_write(tmp_path, "broken.tif", b"II*\x00\xff\xff\xff\x7f")) == {}


def test_an_ome_named_tiff_without_ome_xml_records_why(tmp_path: Path) -> None:
    fields = sniff_format(_write(tmp_path, "empty.ome.tif", _tiff(b"just text")))
    truncated = sniff_format(_write(tmp_path, "cut.ome.tiff", _tiff(b"<OME>" * 50)[:40]))

    assert fields == {
        "format_kind": "ome_tiff",
        "format_sniff_error": "no OME-XML ImageDescription in the first IFD",
    }
    assert truncated["format_kind"] == "ome_tiff"
    assert truncated["format_sniff_error"]


@pytest.mark.parametrize(
    "xml",
    [
        # Billion laughs.
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image Name="&lol2;"/></OME>',
        # External entity (XXE).
        '<!DOCTYPE OME [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image Name="&xxe;"/></OME>',
        # Lower-case and spaced markup is still DTD markup.
        '<! doctype OME><OME><Image Name="x"/></OME>',
    ],
)
def test_ome_xml_with_dtd_markup_is_refused(tmp_path: Path, xml: str) -> None:
    fields = sniff_format(_write(tmp_path, "evil.ome.tif", _tiff(xml.encode("utf-8"))))

    assert fields["format_kind"] == "ome_tiff"
    assert "refused" in str(fields["format_sniff_error"])
    assert "format_image_name" not in fields


def test_undefined_entities_are_a_parse_error_not_an_expansion() -> None:
    fields = sniffers.parse_ome_xml(
        b'<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        b'<Image Name="a&undefined;"/></OME>',
        truncated=False,
        zone=PLUS_TWO,
    )

    assert "malformed" in str(fields["format_sniff_error"])


def test_ome_xml_over_the_cap_keeps_what_it_read_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xml = OME_XML.format(date="2024-03-05T14:22:10Z").encode("utf-8")
    cut = xml.index(b"<Pixels") - 2
    monkeypatch.setattr(sniffers, "MAX_OME_XML_BYTES", cut)

    fields = sniff_format(_write(tmp_path, "long.ome.tif", _tiff(xml)), local_tz=PLUS_TWO)

    assert fields["format_image_name"] == "slice_01"
    assert fields["format_acquired_at"] == "2024-03-05T14:22:10+00:00"
    assert "format_size_x" not in fields
    assert f"longer than {cut} bytes" in str(fields["format_sniff_error"])


def test_an_ifd_with_too_many_entries_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sniffers, "MAX_TIFF_IFD_ENTRIES", 2)
    xml = OME_XML.format(date="2024-03-05T14:22:10Z").encode("utf-8")

    fields = sniff_format(_write(tmp_path, "many.ome.tif", _tiff(xml)))

    assert "limit 2" in str(fields["format_sniff_error"])


def test_random_headers_never_raise(tmp_path: Path) -> None:
    rng = random.Random(1234)
    xml = OME_XML.format(date="2024-03-05T14:22:10Z").encode("utf-8")
    seeds = [_fcs(FCS_KEYWORDS), _tiff(xml), _tiff(xml, big=True, order=">")]
    for index in range(300):
        content = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(1, 12)):
            position = rng.randrange(4, len(content))
            content[position] = rng.randrange(256)
        if rng.random() < 0.3:
            content = content[: rng.randint(4, len(content))]
        name = rng.choice(["x.fcs", "x.ome.tif", "x.tif"])
        fields = sniff_format(_write(tmp_path, f"{index}-{name}", bytes(content)))
        assert fields == {} or fields["format_kind"] in {"fcs", "ome_tiff"}
        assert all(isinstance(value, (str, int)) for value in fields.values())
        assert all(len(str(value)) <= sniffers.MAX_VALUE_CHARS for value in fields.values())


# --- NWB ---------------------------------------------------------------------------------


def test_nwb_without_h5py_is_labelled_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "h5py", None)  # makes `import h5py` fail
    hdf5 = b"\x89HDF\r\n\x1a\n" + b"\x00" * 64

    assert sniff_format(_write(tmp_path, "session.nwb", hdf5)) == {
        "format_kind": "nwb",
        "format_sniff_error": "h5py not installed",
    }
    # A generic HDF5 file cannot be told apart without h5py: nothing.
    assert sniff_format(_write(tmp_path, "data.h5", hdf5)) == {}


def test_the_module_never_imports_h5py_at_import_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "h5py", None)

    reloaded = importlib.reload(sniffers)

    assert reloaded.sniff_format is not None


class _FakeDataset:
    def __init__(self, value: Any) -> None:
        self.value = value
        self.shape = ()

    def __getitem__(self, key: Any) -> Any:
        assert key == ()
        return self.value


class _FakeGroup:
    def __init__(self, nodes: dict[str, Any], attrs: dict[str, Any] | None = None) -> None:
        self.nodes = nodes
        self.attrs = attrs or {}

    def get(self, name: str) -> Any:
        node: Any = self
        for part in name.split("/"):
            node = node.nodes.get(part) if isinstance(node, _FakeGroup) else None
            if node is None:
                return None
        return node


def _fake_h5py(root: _FakeGroup, opened: list[str]) -> types.ModuleType:
    module = types.ModuleType("h5py")

    class File(_FakeGroup):
        def __init__(self, path: str, mode: str) -> None:
            assert mode == "r"
            opened.append(path)
            if Path(path).read_bytes()[:4] != b"\x89HDF":
                raise OSError("Unable to open file (file signature not found)")
            super().__init__(root.nodes, root.attrs)

        def __enter__(self) -> File:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    module.File = File  # type: ignore[attr-defined]
    return module


def test_nwb_with_h5py_reads_session_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _FakeGroup(
        {
            "session_start_time": _FakeDataset(b"2023-11-02T09:15:00-04:00"),
            "identifier": _FakeDataset("mouse7-day3"),
            "session_description": _FakeDataset("x" * 1000),
            "general": _FakeGroup({"subject": _FakeGroup({"subject_id": _FakeDataset(b"M7")})}),
        },
        {"nwb_version": b"2.6.0", "neurodata_type": "NWBFile"},
    )
    opened: list[str] = []
    monkeypatch.setitem(sys.modules, "h5py", _fake_h5py(root, opened))
    path = _write(tmp_path, "rec.nwb", b"\x89HDF\r\n\x1a\n" + b"\x00" * 64)

    fields = sniff_format(path, local_tz=PLUS_TWO)

    assert fields == {
        "format_kind": "nwb",
        "format_version": "2.6.0",
        "format_session_start_time": "2023-11-02T09:15:00-04:00",
        "format_identifier": "mouse7-day3",
        "format_session_description": "x" * (sniffers.MAX_VALUE_CHARS - 1) + "…",
        "format_subject_id": "M7",
        "format_acquired_at": "2023-11-02T13:15:00+00:00",
        "format_acquired_at_timezone": "header",
    }
    assert opened == [str(path)]


def test_nwb_1x_style_root_attributes_are_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _FakeGroup({}, {"session_start_time": "2020-01-01T00:00:00", "identifier": "abc"})
    monkeypatch.setitem(sys.modules, "h5py", _fake_h5py(root, []))

    fields = sniff_format(_write(tmp_path, "legacy.nwb", b"\x89HDF\r\n\x1a\n"), local_tz=MINUS_FIVE)

    assert fields["format_identifier"] == "abc"
    assert fields["format_acquired_at"] == "2020-01-01T05:00:00+00:00"


def test_nwb_that_h5py_cannot_open_records_the_error_and_other_hdf5_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "h5py", _fake_h5py(_FakeGroup({}, {}), []))

    broken = sniff_format(_write(tmp_path, "broken.nwb", b"not hdf5 at all"))
    generic = sniff_format(_write(tmp_path, "array.h5", b"\x89HDF\r\n\x1a\n"))

    assert broken["format_kind"] == "nwb"
    assert "signature not found" in str(broken["format_sniff_error"])
    assert generic == {}


# --- general ----------------------------------------------------------------------------


def test_unrecognized_and_missing_files_yield_nothing(tmp_path: Path) -> None:
    assert sniff_format(_write(tmp_path, "notes.md", b"# hello")) == {}
    assert sniff_format(_write(tmp_path, "empty.fcs", b"")) == {}
    assert sniff_format(tmp_path / "missing.fcs") == {}


def test_iso_datetimes_parse_with_fractions_offsets_and_z() -> None:
    assert parse_iso_datetime("2024-03-05T14:22:10.1234567Z") is not None
    assert parse_iso_datetime("2024-03-05 14:22") is not None
    parsed = parse_iso_datetime("2024-03-05T14:22:10+0530")
    assert parsed is not None and parsed.utcoffset() == timedelta(hours=5, minutes=30)
    assert parse_iso_datetime("2024-02-30T00:00:00") is None
    assert parse_iso_datetime("yesterday") is None


def test_the_kill_switch_disables_watch_sniffing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, "t.fcs", _fcs(FCS_KEYWORDS))

    monkeypatch.setenv("LAB_TRACKER_WATCH_FORMAT_SNIFF", "off")
    assert watch_format_fields(path) == {}
    monkeypatch.setenv("LAB_TRACKER_WATCH_FORMAT_SNIFF", "1")
    assert watch_format_fields(path)["format_kind"] == "fcs"


# --- lt watch: header metadata reaches the synced note -------------------------------------


def _uploaded_metadata(config: Any) -> dict[str, dict[str, Any]]:
    """Sync the outbox; return each uploaded file's note metadata by filename."""

    uploads: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/notes":
            return httpx.Response(
                200, json={"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and request.url.path == "/notes/upload-file":
            body = request.content.decode("utf-8", errors="replace")
            filename = re.search(r'filename="([^"]+)"', body)
            metadata = re.search(r'name="metadata"\r\n\r\n(.*?)\r\n--', body, re.DOTALL)
            assert filename is not None and metadata is not None
            uploads[filename.group(1)] = json.loads(metadata.group(1))
            return httpx.Response(201, json={"data": {"note_id": f"note-{len(uploads)}"}})
        return httpx.Response(500, json={"error": {"message": "unexpected request"}})

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        summary = sync_outbox(lt, config)
    assert summary["errors"] == []
    return uploads


def test_watched_instrument_files_carry_format_metadata_into_the_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LAB_TRACKER_SESSION_ID", raising=False)
    monkeypatch.delenv("LAB_TRACKER_WATCH_FORMAT_SNIFF", raising=False)
    monkeypatch.setitem(sys.modules, "h5py", None)
    monkeypatch.setattr(sniffers, "_local_timezone", lambda: PLUS_TWO)
    monkeypatch.chdir(tmp_path)
    config = init_config(project_id="project-1")
    inbox = tmp_path / "cytometer"
    inbox.mkdir()
    xml = OME_XML.format(date="2024-03-05T14:22:10Z").encode("utf-8")
    _write(inbox, "tube_004.fcs", _fcs(FCS_KEYWORDS))
    _write(inbox, "stack.ome.tif", _tiff(xml))
    _write(inbox, "rec.nwb", b"\x89HDF\r\n\x1a\n")
    _write(inbox, "bad.fcs", b"FCS3.1  truncated")
    _write(inbox, "readme.md", b"plain notes")

    summary = scan_watch(config, mode="files", root=inbox)

    assert summary["errors"] == []
    events = {
        read_event(path)["source"]["relative_path"]: read_event(path)
        for path in config.outbox_path().glob("*.json")
    }
    fcs_metadata = _event_metadata(events["tube_004.fcs"], project_id="project-1")
    assert fcs_metadata["format_kind"] == "fcs"
    assert fcs_metadata["format_acquired_at"] == "2024-03-05T12:22:10+00:00"
    assert "format_kind" not in events["readme.md"]["source"]

    uploads = _uploaded_metadata(config)

    assert uploads["tube_004.fcs"]["format_kind"] == "fcs"
    assert uploads["tube_004.fcs"]["format_instrument"] == "BD LSRFortessa"
    assert uploads["tube_004.fcs"]["format_event_count"] == 250000
    assert uploads["tube_004.fcs"]["format_acquired_at_timezone"] == "local:+02:00"
    assert uploads["stack.ome.tif"]["format_kind"] == "ome_tiff"
    assert uploads["stack.ome.tif"]["format_size_z"] == 10
    assert uploads["stack.ome.tif"]["format_acquired_at"] == "2024-03-05T14:22:10+00:00"
    assert uploads["rec.nwb"]["format_kind"] == "nwb"
    assert uploads["rec.nwb"]["format_sniff_error"] == "h5py not installed"
    assert uploads["bad.fcs"]["format_kind"] == "fcs"
    assert uploads["bad.fcs"]["format_sniff_error"]
    assert not any(key.startswith("format_") for key in uploads["readme.md"])
