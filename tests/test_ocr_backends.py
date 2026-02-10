from __future__ import annotations

import sys
import types

import pytest

from lab_tracker.errors import ValidationError
from lab_tracker.services import ocr_backends


def test_sniff_content_type_recognizes_jpeg_png_and_heic() -> None:
    assert ocr_backends._sniff_content_type(b"\xFF\xD8\xFF\x00\x00") == "image/jpeg"
    assert ocr_backends._sniff_content_type(b"\x89PNG\r\n\x1a\n\x00") == "image/png"

    # ISO-BMFF: size(4) + "ftyp"(4) + major_brand(4)
    heic_header = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"
    assert ocr_backends._sniff_content_type(heic_header) == "image/heic"


def test_normalize_content_type_sniffs_when_missing() -> None:
    png = b"\x89PNG\r\n\x1a\n\x00"
    assert ocr_backends._normalize_content_type(None, png) == "image/png"


def test_normalize_content_type_rejects_unknown_types() -> None:
    with pytest.raises(ValidationError, match="Unsupported image content type"):
        ocr_backends._normalize_content_type("image/gif", b"")


def test_tesseract_backend_formats_regions_and_confidence_without_real_deps(monkeypatch) -> None:
    # Avoid importing Pillow by stubbing the image loader.
    class _FakeImage:
        def rotate(self, *_args, **_kwargs):
            return self

    monkeypatch.setattr(ocr_backends, "_load_pil_image", lambda *_a, **_k: _FakeImage())

    fake_data = {
        "text": ["Hello", "world", "", "Next", "line"],
        "left": [0, 10, 0, 0, 10],
        "top": [0, 0, 0, 10, 10],
        "width": [5, 5, 0, 5, 5],
        "height": [5, 5, 0, 5, 5],
        "conf": ["90", "80", "-1", "70", "60"],
        "page_num": [1, 1, 1, 1, 1],
        "block_num": [1, 1, 1, 1, 1],
        "par_num": [1, 1, 1, 1, 1],
        "line_num": [1, 1, 1, 2, 2],
    }

    fake_pytesseract = types.ModuleType("pytesseract")
    fake_pytesseract.Output = types.SimpleNamespace(DICT="DICT")

    # Mimic pytesseract.pytesseract.tesseract_cmd global.
    fake_pytesseract.pytesseract = types.SimpleNamespace(tesseract_cmd="tesseract")

    def _image_to_data(_image, *, lang=None, config=None, output_type=None):
        assert lang == "eng"
        assert config is None
        assert output_type == "DICT"
        return fake_data

    fake_pytesseract.image_to_data = _image_to_data

    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)

    backend = ocr_backends.TesseractOCRBackend(enable_preprocessing=False, enable_osd=False)
    result = backend.extract_text(b"\x89PNG\r\n\x1a\n\x00", "image/png")

    assert result.text == "Hello world\nNext line"
    assert result.confidence == pytest.approx(75.0)
    assert [region.text for region in result.regions] == ["Hello", "world", "Next", "line"]


def test_detect_rotation_degrees_parses_osd_output() -> None:
    fake_pytesseract = types.SimpleNamespace(image_to_osd=lambda *_a, **_k: "Rotate: 90\n")
    assert (
        ocr_backends._detect_rotation_degrees(
            None,
            pytesseract=fake_pytesseract,
            lang=None,
        )
        == 90
    )
