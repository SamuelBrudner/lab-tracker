"""Decoded QR codes and barcodes on photo uploads: metadata only, never blocking.

Unit tests drive the classifier and the bounded decoder with fake readers;
the zxing-cpp round trips (``decode`` extra, also in ``test``) generate
symbols programmatically, so no binary fixtures are committed.
"""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Sequence
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from lab_tracker import photo_codes
from lab_tracker.gs1 import GS
from lab_tracker.models import encode_session_link_code
from lab_tracker.photo_codes import (
    DECODED_CODE_METADATA_KEYS,
    MAX_BARCODE_TEXT_CHARS,
    DecodedCode,
    PhotoCodeDecoder,
    PhotoCodeLimitError,
    decoded_upload_metadata,
    photo_code_metadata,
    session_references,
)
from lab_tracker.services.provenance_id_matches import ID_MATCH_SESSION_METADATA_KEYS

TODAY = date(2026, 9, 28)
GTIN = "09506000134352"
SESSION_A = UUID("3d4f6a1e-9c2b-4a8e-8f01-2b3c4d5e6f70")
SESSION_B = UUID("9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d")


def _lt(session_id: UUID) -> str:
    return f"LT-{encode_session_link_code(session_id)}"


def _only(*session_ids: UUID):
    allowed = set(session_ids)
    return lambda session_id: session_id in allowed


# --- session references ---------------------------------------------------------


def test_session_references_accept_lt_codes_and_capture_links_only() -> None:
    code = encode_session_link_code(SESSION_A)
    capture_url = f"https://lab.example.org/app/capture?project_id={uuid4()}&session_id={SESSION_B}"

    assert [ref.session_id for ref in session_references(f"tube 4 {_lt(SESSION_A)}")] == [SESSION_A]
    assert session_references(f"LT-{code.lower()}")[0].link_code == code
    assert [ref.session_id for ref in session_references(capture_url)] == [SESSION_B]
    # No prefix, a longer run, a non-canonical code, or another app path: nothing.
    assert session_references(code) == []
    assert session_references(f"X{_lt(SESSION_A)}") == []
    assert session_references(f"{_lt(SESSION_A)}7") == []
    assert session_references("LT-" + code[:-1] + ("B" if code[-1] == "A" else "A")) == []
    assert session_references(f"https://lab.example.org/app/graph?session_id={SESSION_B}") == []
    assert session_references("https://lab.example.org/app/capture?session_id=nope") == []


def test_server_link_code_rule_matches_the_client_watcher() -> None:
    from lab_tracker_client.session_context import LINK_CODE_PREFIX, find_session_link_code

    assert photo_codes.LINK_CODE_PREFIX == LINK_CODE_PREFIX
    code = encode_session_link_code(SESSION_A)
    samples = [
        f"run_{_lt(SESSION_A)}",
        f"LT-{code.lower()}",
        code,
        f"supplementary{_lt(SESSION_A)}",
        "LT-" + "A" * 26,
        f"{_lt(SESSION_A)}/x",
    ]
    for sample in samples:
        client_found = find_session_link_code(sample)
        server_found = session_references(sample)
        assert (client_found[1] if client_found else None) == (
            str(server_found[0].session_id) if server_found else None
        ), sample


# --- classification into metadata -------------------------------------------------


def test_an_lt_code_for_a_session_in_the_notes_project_is_resolved() -> None:
    metadata = photo_code_metadata(
        [DecodedCode(_lt(SESSION_A), "QRCode")],
        session_in_project=_only(SESSION_A),
        today=TODAY,
    )

    assert metadata == {
        "barcode_count": 1,
        "decoded_session_link_code": _lt(SESSION_A),
        "decoded_session_link_code_count": 1,
        "photo_session_id": str(SESSION_A),
    }


def test_an_lt_code_for_another_projects_session_is_recorded_but_not_resolved() -> None:
    metadata = photo_code_metadata(
        [DecodedCode(_lt(SESSION_A), "QRCode")], session_in_project=_only(), today=TODAY
    )

    assert metadata["decoded_session_link_code"] == _lt(SESSION_A)
    assert "photo_session_id" not in metadata


def test_two_sessions_in_the_project_are_ambiguous_and_resolve_nothing() -> None:
    codes = [DecodedCode(_lt(SESSION_A), "QRCode"), DecodedCode(_lt(SESSION_B), "QRCode")]

    ambiguous = photo_code_metadata(
        codes, session_in_project=_only(SESSION_A, SESSION_B), today=TODAY
    )
    one_in_project = photo_code_metadata(codes, session_in_project=_only(SESSION_B), today=TODAY)

    assert "photo_session_id" not in ambiguous
    assert ambiguous["decoded_session_link_code"] == _lt(SESSION_A)
    assert ambiguous["decoded_session_link_code_count"] == 2
    # Exactly one of the decoded sessions is the note's own: that one resolves.
    assert one_in_project["photo_session_id"] == str(SESSION_B)
    assert one_in_project["decoded_session_link_code"] == _lt(SESSION_B)


def test_the_same_session_twice_is_not_ambiguous() -> None:
    capture_url = f"https://lab.example.org/app/capture?session_id={SESSION_A}"
    metadata = photo_code_metadata(
        [DecodedCode(_lt(SESSION_A), "QRCode"), DecodedCode(capture_url, "QRCode")],
        session_in_project=_only(SESSION_A),
        today=TODAY,
    )

    assert metadata["photo_session_id"] == str(SESSION_A)
    assert metadata["decoded_session_link_code_count"] == 1
    assert metadata["barcode_count"] == 2
    assert "barcode_text" not in metadata


def test_a_failing_session_lookup_leaves_the_code_unresolved() -> None:
    def broken(_session_id: UUID) -> bool:
        raise RuntimeError("database away")

    metadata = photo_code_metadata(
        [DecodedCode(_lt(SESSION_A), "QRCode")], session_in_project=broken, today=TODAY
    )

    assert metadata["decoded_session_link_code"] == _lt(SESSION_A)
    assert "photo_session_id" not in metadata


def test_gs1_symbols_fill_the_gs1_keys_and_merge_first_value_wins() -> None:
    metadata = photo_code_metadata(
        [
            DecodedCode(f"01{GTIN}1727010010LOT-7{GS}21SN1", "DataMatrix", is_gs1=True),
            DecodedCode("(10)OTHER(240)CAT-9", "Code128"),
        ],
        session_in_project=_only(),
        today=TODAY,
    )

    assert metadata == {
        "barcode_count": 2,
        "barcode_gs1_gtin": GTIN,
        "barcode_gs1_expiry": "2027-01-31",
        "barcode_gs1_lot": "LOT-7",
        "barcode_gs1_serial": "SN1",
        "barcode_gs1_catalog": "CAT-9",
    }


def test_other_codes_become_bounded_barcode_text() -> None:
    long_text = "x" * (MAX_BARCODE_TEXT_CHARS + 50)
    metadata = photo_code_metadata(
        [
            DecodedCode(f"FREEZER{GS}B\x00-3\n", "Code128"),
            DecodedCode(long_text, "QRCode"),
            DecodedCode(f"FREEZER{GS}B\x00-3\n", "Code128"),  # duplicate
        ],
        session_in_project=_only(),
        today=TODAY,
    )

    assert metadata == {
        "barcode_count": 2,
        "barcode_text": "FREEZER<GS>B -3",
        "barcode_text_format": "Code128",
    }
    bounded = photo_code_metadata(
        [DecodedCode(long_text, "QRCode")], session_in_project=_only(), today=TODAY
    )
    assert len(str(bounded["barcode_text"])) == MAX_BARCODE_TEXT_CHARS


def test_a_malformed_gs1_symbol_falls_back_to_barcode_text() -> None:
    metadata = photo_code_metadata(
        [DecodedCode("0512345", "Code128", is_gs1=True)], session_in_project=_only(), today=TODAY
    )

    assert metadata["barcode_text"] == "0512345"
    assert not any(key.startswith("barcode_gs1_") for key in metadata)


def test_no_codes_yield_no_metadata() -> None:
    assert photo_code_metadata([], session_in_project=_only(), today=TODAY) == {}
    assert (
        photo_code_metadata([DecodedCode("", "QRCode")], session_in_project=_only(), today=TODAY)
        == {}
    )


def test_every_stamped_key_is_reserved_and_photo_session_id_feeds_the_detector() -> None:
    metadata = photo_code_metadata(
        [
            DecodedCode(_lt(SESSION_A), "QRCode"),
            DecodedCode(f"(01){GTIN}(10)L(17)270101(21)S(240)C", "QRCode"),
            DecodedCode("plain", "QRCode"),
        ],
        session_in_project=_only(SESSION_A),
        today=TODAY,
    )

    assert set(metadata) == set(DECODED_CODE_METADATA_KEYS)
    assert "photo_session_id" in ID_MATCH_SESSION_METADATA_KEYS


# --- the bounded decoder ------------------------------------------------------------


def test_decoder_returns_the_readers_codes() -> None:
    decoder = PhotoCodeDecoder(reader=lambda data: [DecodedCode(data.decode(), "QRCode")])
    try:
        assert decoder.decode(b"hello", timeout_seconds=5) == [DecodedCode("hello", "QRCode")]
    finally:
        decoder.close()


@pytest.mark.parametrize("error", [RuntimeError("zxing crashed"), PhotoCodeLimitError("big")])
def test_decoder_swallows_reader_failures(error: Exception) -> None:
    def reader(_data: bytes) -> Sequence[DecodedCode]:
        raise error

    decoder = PhotoCodeDecoder(reader=reader)
    try:
        assert decoder.decode(b"x", timeout_seconds=5) is None
        # The failed decode released its slot.
        assert decoder.decode(b"x", timeout_seconds=5) is None
    finally:
        decoder.close()


def test_decoder_abandons_a_slow_decode_and_skips_while_every_slot_is_busy() -> None:
    release = threading.Event()
    calls: list[bytes] = []

    def reader(data: bytes) -> Sequence[DecodedCode]:
        calls.append(data)
        if data == b"slow":
            release.wait(timeout=30)
        return [DecodedCode(data.decode(), "QRCode")]

    decoder = PhotoCodeDecoder(reader=reader, max_concurrent=1)
    try:
        assert decoder.decode(b"slow", timeout_seconds=0.05) is None
        # The abandoned decode still holds the only slot: skipped, not queued.
        assert decoder.decode(b"next", timeout_seconds=5) is None
        assert calls == [b"slow"]
        release.set()
        # Wait for the abandoned worker to finish and free its slot.
        decoder._pool().submit(lambda: None).result(timeout=30)
        assert decoder.decode(b"next", timeout_seconds=5) == [DecodedCode("next", "QRCode")]
    finally:
        release.set()
        decoder.close()


class _Settings:
    def __init__(self, enabled: bool = True, timeout: float = 5.0) -> None:
        self.decode_photo_codes = enabled
        self.decode_photo_codes_timeout_seconds = timeout


def _counting_decoder(codes: list[DecodedCode]) -> tuple[PhotoCodeDecoder, list[bytes]]:
    seen: list[bytes] = []

    def reader(data: bytes) -> Sequence[DecodedCode]:
        seen.append(data)
        return codes

    return PhotoCodeDecoder(reader=reader), seen


def test_upload_metadata_honours_the_kill_switch_type_and_size_limits() -> None:
    decoder, seen = _counting_decoder([DecodedCode("plain", "QRCode")])

    def run(**overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "content_type": "image/png",
            "size_bytes": 5,
            "settings": _Settings(),
            "session_in_project": _only(),
            "decoder": decoder,
            "today": TODAY,
        }
        arguments.update(overrides)
        return dict(decoded_upload_metadata(io.BytesIO(b"image"), **arguments))

    try:
        assert run()["barcode_text"] == "plain"
        assert seen == [b"image"]
        assert run(settings=_Settings(enabled=False)) == {}
        assert run(content_type="image/tiff") == {}
        assert run(content_type="audio/webm") == {}
        assert run(size_bytes=photo_codes.MAX_DECODE_BYTES + 1) == {}
        assert seen == [b"image"]
    finally:
        decoder.close()


def test_upload_metadata_without_the_extra_or_with_a_broken_stream_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenStream(io.BytesIO):
        def seek(self, *_args: Any, **_kwargs: Any) -> int:
            raise OSError("spooled file vanished")

    decoder, seen = _counting_decoder([DecodedCode("plain", "QRCode")])
    monkeypatch.setattr(photo_codes, "decoder_available", lambda: False)
    try:
        assert (
            decoded_upload_metadata(
                io.BytesIO(b"image"),
                content_type="image/jpeg",
                size_bytes=5,
                settings=_Settings(),
                session_in_project=_only(),
            )
            == {}
        )
        assert (
            decoded_upload_metadata(
                BrokenStream(b"image"),
                content_type="image/jpeg",
                size_bytes=5,
                settings=_Settings(),
                session_in_project=_only(),
                decoder=decoder,
            )
            == {}
        )
        assert seen == []
    finally:
        decoder.close()


# --- zxing-cpp round trips ----------------------------------------------------------


def _symbol_png(content: str, symbology: str = "QRCode", *, gs1: bool = False) -> bytes:
    zxingcpp = pytest.importorskip("zxingcpp")
    image_module = pytest.importorskip("PIL.Image")
    barcode = zxingcpp.create_barcode(
        content, getattr(zxingcpp.BarcodeFormat, symbology), **({"gs1": True} if gs1 else {})
    )
    view = memoryview(zxingcpp.write_barcode_to_image(barcode, scale=4))
    symbol = image_module.frombytes("L", (view.shape[1], view.shape[0]), view.tobytes())
    # Place the symbol on a larger light "photo" so detection is not trivial.
    photo = image_module.new("RGB", (symbol.width * 3, symbol.height * 3), (228, 226, 220))
    photo.paste(symbol.convert("RGB"), (symbol.width, symbol.height))
    buffer = io.BytesIO()
    photo.save(buffer, format="PNG")
    return buffer.getvalue()


def test_zxing_reads_an_lt_code_and_a_gs1_datamatrix() -> None:
    qr = photo_codes.read_image_codes(_symbol_png(_lt(SESSION_A)))
    datamatrix = photo_codes.read_image_codes(
        _symbol_png(f"(01){GTIN}(17)261231(10)ABC123", "DataMatrix", gs1=True)
    )

    assert qr == [DecodedCode(_lt(SESSION_A), "QRCode", is_gs1=False)]
    assert len(datamatrix) == 1 and datamatrix[0].is_gs1
    assert photo_code_metadata(datamatrix, session_in_project=_only(), today=TODAY) == {
        "barcode_count": 1,
        "barcode_gs1_gtin": GTIN,
        "barcode_gs1_expiry": "2026-12-31",
        "barcode_gs1_lot": "ABC123",
    }


def test_zxing_reader_enforces_the_pixel_limit_and_rejects_garbage() -> None:
    png = _symbol_png("hello")

    with pytest.raises(PhotoCodeLimitError):
        photo_codes.read_image_codes(png, max_pixels=100)
    # A PNG must be decoded at full size, so its lower cap applies...
    with pytest.raises(PhotoCodeLimitError):
        photo_codes.read_image_codes(png, max_full_decode_pixels=100)
    # ...while a JPEG of the same size is decoded at a reduced scale.
    image_module = pytest.importorskip("PIL.Image")
    jpeg = io.BytesIO()
    image_module.open(io.BytesIO(png)).convert("RGB").save(jpeg, format="JPEG", quality=95)
    assert [
        code.text
        for code in photo_codes.read_image_codes(jpeg.getvalue(), max_full_decode_pixels=100)
    ] == ["hello"]
    decoder = PhotoCodeDecoder()
    try:
        assert decoder.decode(b"not an image at all", timeout_seconds=5) is None
    finally:
        decoder.close()


def test_zxing_reader_downscales_a_large_jpeg_before_decoding() -> None:
    image_module = pytest.importorskip("PIL.Image")
    symbol = image_module.open(io.BytesIO(_symbol_png(_lt(SESSION_B))))
    photo = image_module.new("RGB", (6000, 4000), (230, 230, 230))
    photo.paste(symbol.resize((symbol.width * 4, symbol.height * 4)), (2500, 1500))
    buffer = io.BytesIO()
    photo.save(buffer, format="JPEG", quality=90)

    codes = photo_codes.read_image_codes(buffer.getvalue(), max_side=2048)

    assert [code.text for code in codes] == [_lt(SESSION_B)]


# --- the upload routes ------------------------------------------------------------------


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": f"Decode {uuid4().hex[:6]}"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _session(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["session_id"]


def _upload(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    content: bytes,
    *,
    route: str = "/notes/upload-file",
    metadata: dict[str, Any] | None = None,
    client_capture_id: str | None = None,
    content_type: str = "image/png",
) -> Any:
    data: dict[str, str] = {"project_id": project_id}
    if metadata is not None:
        data["metadata"] = json.dumps(metadata)
    if client_capture_id is not None:
        data["client_capture_id"] = client_capture_id
    return client.post(
        route,
        data=data,
        files={"file": ("bench.png", content, content_type)},
        headers=headers,
    )


def _fake_decoder(client: TestClient, reader: Any) -> PhotoCodeDecoder:
    decoder = PhotoCodeDecoder(reader=reader)
    client.app.state.photo_code_decoder = decoder
    return decoder


def test_uploaded_photo_of_a_session_label_is_stamped_and_proposed_for_review(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    pytest.importorskip("zxingcpp")
    project_id = _project(client, admin_auth_headers)
    session_id = _session(client, admin_auth_headers, project_id)
    label = f"LT-{encode_session_link_code(UUID(session_id))}"

    response = _upload(client, admin_auth_headers, project_id, _symbol_png(f"Rack 3 {label}"))

    assert response.status_code == 201, response.text
    note = response.json()["data"]
    assert note["metadata"]["decoded_session_link_code"] == label
    assert note["metadata"]["photo_session_id"] == session_id
    assert note["metadata"]["barcode_count"] == "1"
    assert note["targets"] == []  # metadata only: the link is proposed, never applied

    client.app.state.graph_draft_client_factory = lambda settings: _FakeBatchClient()
    run = client.post(
        "/batches/run-now", json={"project_id": project_id}, headers=admin_auth_headers
    )
    assert run.status_code == 201, run.text
    links = client.get(
        f"/provenance-links?project_id={project_id}&status=proposed", headers=admin_auth_headers
    ).json()["data"]
    assert [(link["source"]["entity_id"], link["target"]) for link in links] == [
        (note["note_id"], {"entity_type": "session", "entity_id": session_id})
    ]
    assert links[0]["basis"] == "exact_id_match"


def test_a_session_label_from_another_project_is_not_resolved(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    other_session = _session(client, admin_auth_headers, _project(client, admin_auth_headers))
    label = f"LT-{encode_session_link_code(UUID(other_session))}"
    decoder = _fake_decoder(client, lambda _data: [DecodedCode(label, "QRCode")])
    try:
        response = _upload(client, admin_auth_headers, project_id, b"photo-bytes")
    finally:
        decoder.close()

    assert response.status_code == 201, response.text
    metadata = response.json()["data"]["metadata"]
    assert metadata["decoded_session_link_code"] == label
    assert "photo_session_id" not in metadata


def test_quick_capture_photos_are_decoded_too(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    decoder = _fake_decoder(
        client,
        lambda _data: [DecodedCode(f"01{GTIN}10LOT9", "Code128", is_gs1=True)],
    )
    try:
        response = _upload(
            client, admin_auth_headers, project_id, b"photo", route="/notes/quick-capture"
        )
    finally:
        decoder.close()

    assert response.status_code == 201, response.text
    metadata = response.json()["data"]["metadata"]
    assert metadata["barcode_gs1_gtin"] == GTIN
    assert metadata["barcode_gs1_lot"] == "LOT9"


def test_a_decode_failure_never_fails_the_upload(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)

    def reader(_data: bytes) -> Sequence[DecodedCode]:
        raise RuntimeError("decoder exploded")

    decoder = _fake_decoder(client, reader)
    try:
        response = _upload(client, admin_auth_headers, project_id, b"photo")
    finally:
        decoder.close()

    assert response.status_code == 201, response.text
    metadata = response.json()["data"]["metadata"]
    assert not set(metadata) & DECODED_CODE_METADATA_KEYS
    assert metadata["source_file_content_type"] == "image/png"


def test_garbage_image_bytes_upload_without_decoded_metadata(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)

    response = _upload(client, admin_auth_headers, project_id, b"\x89PNG broken")

    assert response.status_code == 201, response.text
    assert not set(response.json()["data"]["metadata"]) & DECODED_CODE_METADATA_KEYS


def test_the_kill_switch_skips_decoding(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    calls: list[bytes] = []

    def reader(data: bytes) -> Sequence[DecodedCode]:
        calls.append(data)
        return [DecodedCode("plain", "QRCode")]

    decoder = _fake_decoder(client, reader)
    client.app.state.settings.decode_photo_codes = False
    try:
        response = _upload(client, admin_auth_headers, project_id, b"photo")
    finally:
        client.app.state.settings.decode_photo_codes = True
        decoder.close()

    assert response.status_code == 201, response.text
    assert "barcode_text" not in response.json()["data"]["metadata"]
    assert calls == []


def test_client_supplied_decoded_keys_are_rejected(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)

    for route in ("/notes/upload-file", "/notes/quick-capture"):
        response = _upload(
            client,
            admin_auth_headers,
            project_id,
            b"photo",
            route=route,
            metadata={"photo_session_id": str(uuid4())},
        )
        assert response.status_code == 422, response.text
        assert "stamped by the server" in response.text

    notes = client.get(f"/notes?project_id={project_id}", headers=admin_auth_headers)
    assert notes.json()["data"] == []


def test_a_capture_replay_is_reused_even_when_decoding_differs(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    results = iter([[DecodedCode("first", "QRCode")], []])
    decoder = _fake_decoder(client, lambda _data: next(results))
    try:
        first = _upload(
            client, admin_auth_headers, project_id, b"photo", client_capture_id="phone-1"
        )
        replay = _upload(
            client, admin_auth_headers, project_id, b"photo", client_capture_id="phone-1"
        )
    finally:
        decoder.close()

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["note_id"] == first.json()["data"]["note_id"]
    assert replay.json()["data"]["metadata"]["barcode_text"] == "first"


class _FakeBatchClient:
    provider = "fake"
    model = "fake-batch-model"

    def draft_from_batch(self, *, batch_context: dict[str, Any], user_hint: str | None = None):
        return {
            "summary": "nothing from the model",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
        }

    def close(self) -> None:
        pass
