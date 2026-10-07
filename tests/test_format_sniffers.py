"""Instrument-file header sniffers used by ``lt watch``: bounded, fail-soft, pure.

Fixture files are written programmatically (FCS HEADER/TEXT bytes, minimal
classic and BigTIFF files with OME-XML, and HDF5 files through h5py); no
binaries are committed. NWB is exercised without h5py and, where it is
installed (the ``test`` extra installs it), against real HDF5 files, including
crafted ones that point outside the file or claim oversized strings.
"""

from __future__ import annotations

import importlib
import json
import os
import random
import re
import struct
import subprocess
import sys
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


def _h5py() -> Any:
    return pytest.importorskip("h5py")


def _nwb(
    path: Path,
    *,
    libver: str = "earliest",
    userblock_size: int = 0,
    padding_attributes: int = 0,
    nwb_version: str = "2.6.0",
) -> Path:
    """An NWB 2.x file laid out as pynwb writes one (variable-length UTF-8 strings)."""

    h5py = _h5py()
    with h5py.File(path, "w", libver=libver, userblock_size=userblock_size) as handle:
        _fill_nwb(handle, padding_attributes=padding_attributes, nwb_version=nwb_version)
    return path


def _fill_nwb(handle: Any, *, padding_attributes: int = 0, nwb_version: str = "2.6.0") -> None:
    for index in range(padding_attributes):
        handle.attrs[f"padding{index:03d}"] = "x" * 50
    handle.attrs["namespace"] = "core"
    handle.attrs["neurodata_type"] = "NWBFile"
    handle.attrs["nwb_version"] = nwb_version
    handle["session_start_time"] = "2023-11-02T09:15:00-04:00"
    handle["identifier"] = "mouse7-day3"
    handle["session_description"] = "x" * 1000
    # Fixed-length, as some writers store short strings.
    handle.create_dataset("general/subject/subject_id", data=b"M7", dtype="S2")


NWB_FIELDS = {
    "format_kind": "nwb",
    "format_version": "2.6.0",
    "format_session_start_time": "2023-11-02T09:15:00-04:00",
    "format_identifier": "mouse7-day3",
    "format_session_description": "x" * (sniffers.MAX_VALUE_CHARS - 1) + "…",
    "format_subject_id": "M7",
    "format_acquired_at": "2023-11-02T13:15:00+00:00",
    "format_acquired_at_timezone": "header",
}
SECRET = "SECRET-7f3a-not-for-upload"


def _secret_hdf5(path: Path) -> Path:
    """Another local HDF5 file whose values must never be sniffed.

    Fixed-length, so following a link to one would read it through h5py.
    """

    h5py = _h5py()
    with h5py.File(path, "w") as handle:
        for name in ("identifier", "general/subject/subject_id"):
            handle.create_dataset(name, data=SECRET.encode(), dtype=f"S{len(SECRET)}")
    return path


@pytest.fixture
def h5py_reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """(file, name) of each dataset or attribute value h5py is asked to read."""

    h5py = _h5py()
    reads: list[tuple[str, str]] = []
    read_dataset = h5py.Dataset.__getitem__
    read_attribute = h5py.AttributeManager.__getitem__

    def dataset(self: Any, *args: Any) -> Any:
        reads.append((self.file.filename, self.name))
        return read_dataset(self, *args)

    def attribute(self: Any, name: str) -> Any:
        reads.append((h5py.h5f.get_name(self._id).decode(), f"@{name}"))
        return read_attribute(self, name)

    monkeypatch.setattr(h5py.Dataset, "__getitem__", dataset)
    monkeypatch.setattr(h5py.AttributeManager, "__getitem__", attribute)
    return reads


@pytest.mark.parametrize(
    ("libver", "userblock_size", "padding_attributes"),
    [
        ("earliest", 0, 0),  # version 1 object headers, as pynwb writes
        ("earliest", 512, 40),  # a user block, and attributes in continuation chunks
        ("latest", 0, 0),  # version 2 object headers
        ("latest", 1024, 4),
    ],
)
def test_nwb_with_h5py_reads_session_facts(
    tmp_path: Path, libver: str, userblock_size: int, padding_attributes: int
) -> None:
    path = _nwb(
        tmp_path / "rec.nwb",
        libver=libver,
        userblock_size=userblock_size,
        padding_attributes=padding_attributes,
    )

    assert sniff_format(path, local_tz=PLUS_TWO) == NWB_FIELDS


@pytest.mark.parametrize(("libver", "padding_attributes"), [("earliest", 40), ("latest", 4)])
@pytest.mark.parametrize(
    "sizes",
    [(8, 8), (4, 4), (8, 4), (4, 8), (2, 2), (8, 2)],
    ids=["8-8", "4-4", "8-4", "4-8", "2-2", "8-2"],  # offset size, length size
)
def test_nwb_with_narrow_offsets_and_lengths_reads_session_facts(
    tmp_path: Path, libver: str, padding_attributes: int, sizes: tuple[int, int]
) -> None:
    h5py = _h5py()
    # File offsets and lengths are 8 bytes by default; a writer may choose 2 or 4.
    path = tmp_path / "rec.nwb"
    fapl = h5py.h5p.create(h5py.h5p.FILE_ACCESS)
    fapl.set_libver_bounds(getattr(h5py.h5f, f"LIBVER_{libver.upper()}"), h5py.h5f.LIBVER_LATEST)
    fcpl = h5py.h5p.create(h5py.h5p.FILE_CREATE)
    try:
        fcpl.set_sizes(*sizes)
        file_id = h5py.h5f.create(str(path).encode(), h5py.h5f.ACC_TRUNC, fcpl=fcpl, fapl=fapl)
    except ValueError as exc:
        pytest.skip(f"HDF5 refuses offset and length sizes {sizes}: {exc}")
    with h5py.File(file_id) as handle:
        assert handle.id.get_create_plist().get_sizes() == sizes
        _fill_nwb(handle, padding_attributes=padding_attributes)

    assert sniff_format(path, local_tz=PLUS_TWO) == NWB_FIELDS


def test_nwb_is_opened_without_locking_and_on_h5py_without_the_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h5py = _h5py()
    path = _nwb(tmp_path / "rec.nwb")
    real_file = h5py.File
    opened: list[dict[str, Any]] = []

    def old_file(name: str, mode: str, **options: Any) -> Any:
        opened.append(options)
        if options:  # h5py < 3.5 has no ``locking`` argument.
            raise TypeError("__init__() got an unexpected keyword argument 'locking'")
        return real_file(name, mode)

    monkeypatch.setattr(h5py, "File", old_file)

    assert sniff_format(path)["format_identifier"] == "mouse7-day3"
    # Tried without HDF5 file locking first, so a scan never blocks the writer.
    assert opened == [{"locking": False}, {}]


def test_nwb_1x_style_root_attributes_are_a_fallback(tmp_path: Path) -> None:
    h5py = _h5py()
    path = tmp_path / "legacy.nwb"
    with h5py.File(path, "w") as handle:
        handle.attrs["session_start_time"] = "2020-01-01T00:00:00"
        handle.attrs["identifier"] = "abc"

    fields = sniff_format(path, local_tz=MINUS_FIVE)

    assert fields["format_identifier"] == "abc"
    assert fields["format_acquired_at"] == "2020-01-01T05:00:00+00:00"


def test_nwb_that_h5py_cannot_open_records_the_error_and_other_hdf5_is_skipped(
    tmp_path: Path,
) -> None:
    h5py = _h5py()
    generic = tmp_path / "array.h5"
    with h5py.File(generic, "w") as handle:
        handle["data"] = [1, 2, 3]

    broken = sniff_format(_write(tmp_path, "broken.nwb", b"not hdf5 at all"))

    assert broken["format_kind"] == "nwb"
    assert "signature not found" in str(broken["format_sniff_error"])
    assert sniff_format(generic) == {}


def test_nwb_links_out_of_the_file_are_not_followed(
    tmp_path: Path, h5py_reads: list[tuple[str, str]]
) -> None:
    h5py = _h5py()
    other = str(_secret_hdf5(tmp_path / "other.h5"))
    path = _nwb(tmp_path / "rec.nwb")
    with h5py.File(path, "a") as handle:
        del handle["identifier"], handle["session_description"], handle["general"]
        handle["identifier"] = h5py.ExternalLink(other, "/identifier")
        # An external link part-way down a path, and a soft link routed through one.
        handle["general"] = h5py.ExternalLink(other, "/general")
        handle["elsewhere"] = h5py.ExternalLink(other, "/")
        handle["session_description"] = h5py.SoftLink("/elsewhere/identifier")

    fields = sniff_format(path, local_tz=PLUS_TWO)

    assert SECRET not in json.dumps(fields)
    assert fields["format_session_start_time"] == "2023-11-02T09:15:00-04:00"
    for key in ("format_identifier", "format_session_description", "format_subject_id"):
        assert key not in fields
    assert {file for file, _name in h5py_reads} <= {str(path)}


def test_nwb_raw_data_stored_outside_the_file_is_not_read(
    tmp_path: Path, h5py_reads: list[tuple[str, str]]
) -> None:
    h5py = _h5py()
    secret = _write(tmp_path, "token.txt", SECRET.encode())
    other = str(_secret_hdf5(tmp_path / "other.h5"))
    path = _nwb(tmp_path / "rec.nwb")
    with h5py.File(path, "a") as handle:
        del handle["identifier"], handle["session_description"]
        # HDF5 external storage reads any local file -- here not even an HDF5 one.
        # (Low-level: h5py's create_dataset(external=...) ignores it for a scalar.)
        plist = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
        plist.set_layout(h5py.h5d.CONTIGUOUS)
        plist.set_external(str(secret).encode(), 0, len(SECRET))
        string = h5py.h5t.C_S1.copy()
        string.set_size(len(SECRET))
        scalar = h5py.h5s.create(h5py.h5s.SCALAR)
        h5py.h5d.create(handle.id, b"session_description", string, scalar, dcpl=plist)
        assert handle["session_description"].external
        # A virtual dataset maps another file's dataset.
        layout = h5py.VirtualLayout(shape=(), dtype=f"S{len(SECRET)}")
        layout[...] = h5py.VirtualSource(other, "identifier", shape=())
        handle.create_virtual_dataset("identifier", layout)

    fields = sniff_format(path, local_tz=PLUS_TWO)

    assert SECRET not in json.dumps(fields)
    assert "format_session_description" not in fields
    assert "format_identifier" not in fields
    assert fields["format_subject_id"] == "M7"
    assert not {name for _file, name in h5py_reads} & {"/identifier", "/session_description"}


def _forge_vlen_length(path: Path, offset: int, claimed: int) -> None:
    """Overwrite a variable-length element's 4-byte length (HDF5 trusts it)."""

    with path.open("r+b") as raw:
        raw.seek(offset)
        raw.write(struct.pack("<I", claimed))


def _vlen_attribute_element(path: Path, value: str) -> int:
    """File offset of the root attribute element pointing at ``value`` in a global heap."""

    data = path.read_bytes()
    for collection in re.finditer(b"GCOL\x01", data):
        element = struct.pack("<IQ", len(value.encode()), collection.start())
        if data.count(element) == 1:
            return data.index(element)
    raise AssertionError(f"no element for {value!r}")


def _oversized_nwb(tmp_path: Path) -> Path:
    """Every NWB string over the read limit: declared, forged, or genuinely long."""

    h5py = _h5py()
    path = _nwb(tmp_path / "oversized.nwb", nwb_version="2.6.0-forged")
    with h5py.File(path, "a") as handle:
        del handle["identifier"], handle["session_description"]
        # 300 MB declared by a 6 KB file: never-written storage reads as its fill.
        handle.create_dataset("identifier", shape=(), dtype="S300000000")
        handle["session_description"] = "y" * (sniffers.MAX_HDF5_STRING_BYTES + 1)
        handle["general/subject"].attrs["subject_id"] = b"z" * 16384
        del handle["general/subject/subject_id"]
        offset = handle["session_start_time"].id.get_offset()
    _forge_vlen_length(path, offset, 1 << 30)
    _forge_vlen_length(path, _vlen_attribute_element(path, "2.6.0-forged"), 1 << 30)
    return path


def test_nwb_strings_over_the_limit_are_not_read(
    tmp_path: Path, h5py_reads: list[tuple[str, str]]
) -> None:
    path = _oversized_nwb(tmp_path)

    assert path.stat().st_size < 64 * 1024
    assert sniff_format(path) == {"format_kind": "nwb"}
    assert h5py_reads == []


_CHILD_SNIFF = """
import json, resource, sys
import h5py
from lab_tracker_client.format_sniffers import sniff_format
def peak():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024
before = peak()
fields = sniff_format(sys.argv[1])
print(json.dumps({"growth": peak() - before, "fields": fields}))
"""


def _sniff_in_child(path: Path) -> dict[str, Any]:
    """Sniff in a fresh interpreter: its peak-RSS growth, and a timeout if it blocks."""

    pytest.importorskip("resource")
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD_SNIFF, str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    report: dict[str, Any] = json.loads(completed.stdout)
    return report


def test_oversized_nwb_strings_allocate_nothing(tmp_path: Path) -> None:
    report = _sniff_in_child(_oversized_nwb(tmp_path))

    assert report["fields"] == {"format_kind": "nwb"}
    # Read as declared, these strings would allocate 300 MB and 1 GiB.
    assert report["growth"] < 64 * 1024 * 1024


def test_a_virtual_dataset_never_opens_its_source_files(tmp_path: Path) -> None:
    pytest.importorskip("resource")  # POSIX: os.mkfifo as well
    h5py = _h5py()
    # A virtual dataset with unlimited mappings opens its sources to compute its
    # shape; a FIFO there would block the scan until something wrote to it.
    fifo = tmp_path / "source.h5"
    os.mkfifo(fifo)
    path = _nwb(tmp_path / "rec.nwb")
    with h5py.File(path, "a") as handle:
        del handle["identifier"]
        layout = h5py.VirtualLayout(shape=(1,), maxshape=(None,), dtype="S8")
        source = h5py.VirtualSource(str(fifo), "data", shape=(1,), maxshape=(None,))
        layout[0 : h5py.h5s.UNLIMITED] = source[0 : h5py.h5s.UNLIMITED]
        handle.create_virtual_dataset("identifier", layout)

    fields = _sniff_in_child(path)["fields"]

    assert "format_identifier" not in fields
    assert fields["format_session_start_time"] == "2023-11-02T09:15:00-04:00"


@pytest.mark.parametrize(
    ("missing", "links"),
    [
        ("format_identifier", {"identifier": ("external", "/identifier")}),
        ("format_subject_id", {"general": ("external", "/general")}),
        (
            "format_identifier",
            {"elsewhere": ("external", "/"), "identifier": ("soft", "/elsewhere/identifier")},
        ),
    ],
    ids=["leaf", "group", "soft-through-external"],
)
def test_an_external_link_never_opens_its_target_file(
    tmp_path: Path, missing: str, links: dict[str, tuple[str, str]]
) -> None:
    pytest.importorskip("resource")  # POSIX: os.mkfifo as well
    h5py = _h5py()
    # Traversing an external link opens its file; a FIFO there would block the
    # scan until something wrote to it, so the link must be refused unopened.
    fifo = tmp_path / "other.h5"
    os.mkfifo(fifo)
    path = _nwb(tmp_path / "rec.nwb")
    with h5py.File(path, "a") as handle:
        for name, (kind, target) in links.items():
            if name in handle:
                del handle[name]
            if kind == "external":
                handle[name] = h5py.ExternalLink(str(fifo), target)
            else:
                handle[name] = h5py.SoftLink(target)

    fields = _sniff_in_child(path)["fields"]

    assert fields == {key: value for key, value in NWB_FIELDS.items() if key != missing}


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
