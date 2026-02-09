"""OCR backends.

This module defines a pluggable interface for turning raw image bytes into text.
The first implementation targets Tesseract via ``pytesseract``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import io
from typing import Any, Iterable

from lab_tracker.errors import ValidationError


@dataclass(frozen=True)
class OCRBoundingBox:
    """Pixel-space bounding box (origin is the top-left of the image)."""

    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class OCRRegion:
    """A localized OCR hit (typically a word) with bounding box and confidence."""

    text: str
    confidence: float | None
    bbox: OCRBoundingBox


@dataclass(frozen=True)
class OCRResult:
    """OCR output for an image.

    Confidence is a backend-specific 0-100 score when available.
    """

    text: str
    confidence: float | None
    regions: list[OCRRegion]


class OCRBackend(ABC):
    """Extract text from an image."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Stable identifier for the backend implementation."""

    @abstractmethod
    def extract_text(self, image_bytes: bytes, content_type: str | None) -> OCRResult:
        """Extract text from raw image bytes."""


_SUPPORTED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    # HEIC/HEIF are both ISO-BMFF containers; most callers use image/heic.
    "image/heic",
    "image/heif",
}


def _sniff_content_type(image_bytes: bytes) -> str | None:
    if len(image_bytes) >= 3 and image_bytes[:3] == b"\xFF\xD8\xFF":
        return "image/jpeg"
    if len(image_bytes) >= 8 and image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    # HEIF/HEIC: ISO BMFF where bytes[4:8] == b"ftyp" and major_brand indicates HEIF.
    if len(image_bytes) >= 12 and image_bytes[4:8] == b"ftyp":
        major_brand = image_bytes[8:12]
        if major_brand in {b"heic", b"heif", b"heix", b"hevc", b"mif1", b"msf1"}:
            return "image/heic"
    return None


def _normalize_content_type(content_type: str | None, image_bytes: bytes) -> str:
    resolved = (content_type or "").split(";", 1)[0].strip().lower()
    if resolved in {"image/jpg"}:
        resolved = "image/jpeg"
    if resolved in {"image/heif"}:
        resolved = "image/heic"

    if not resolved or resolved == "application/octet-stream":
        sniffed = _sniff_content_type(image_bytes)
        if sniffed:
            resolved = sniffed

    if not resolved:
        raise ValidationError("content_type is required when image format cannot be inferred.")
    if resolved not in _SUPPORTED_CONTENT_TYPES:
        raise ValidationError(f"Unsupported image content type: {resolved}")
    return resolved


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed


def _mean(values: Iterable[float]) -> float | None:
    total = 0.0
    count = 0
    for value in values:
        total += float(value)
        count += 1
    if count == 0:
        return None
    return total / count


def _missing_dependency_message(*packages: str) -> str:
    unique = ", ".join(sorted({pkg for pkg in packages if pkg}))
    suffix = f" Missing: {unique}." if unique else ""
    return (
        "OCR dependencies are not installed. "
        "Install with `pip install -e '.[ocr]'` (or install the missing packages)."
        + suffix
    )


def _load_pil_image(image_bytes: bytes, content_type: str) -> Any:
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(_missing_dependency_message("Pillow")) from exc

    if content_type in {"image/heic", "image/heif"}:
        try:
            from pillow_heif import register_heif_opener  # type: ignore[import-not-found]

            register_heif_opener()
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(_missing_dependency_message("pillow-heif", "Pillow")) from exc

    with Image.open(io.BytesIO(image_bytes)) as image:
        image.load()
        return image.convert("RGB")


def _preprocess_image(image: Any) -> Any:
    """Lightweight preprocessing for phone photos of whiteboards/notebooks.

    Uses only Pillow primitives so the backend can remain optional.
    """

    try:
        from PIL import ImageEnhance, ImageFilter, ImageOps  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(_missing_dependency_message("Pillow")) from exc

    # Grayscale + contrast normalization.
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray, cutoff=2)

    # Slight sharpness bump helps with faint pen strokes.
    gray = gray.filter(ImageFilter.UnsharpMask(radius=1, percent=150, threshold=3))

    # Simple denoise via median filtering; keep it light to avoid wiping thin strokes.
    gray = gray.filter(ImageFilter.MedianFilter(size=3))

    # Binarize via Otsu thresholding (pure-Python, using image histogram).
    threshold = _otsu_threshold(gray.histogram())
    bw = gray.point(lambda p: 255 if p > threshold else 0)

    # Normalize brightness after binarization to reduce gray "fog".
    bw = ImageEnhance.Contrast(bw).enhance(1.2)
    return bw


def _otsu_threshold(histogram: list[int]) -> int:
    # Histogram is expected to be a 256-bin grayscale histogram.
    if len(histogram) < 256:
        return 127

    total = sum(histogram[:256])
    if total <= 0:
        return 127

    sum_total = 0.0
    for i in range(256):
        sum_total += i * histogram[i]

    sum_background = 0.0
    weight_background = 0
    best_threshold = 127
    best_variance = -1.0

    for t in range(256):
        weight_background += histogram[t]
        if weight_background == 0:
            continue
        weight_foreground = total - weight_background
        if weight_foreground == 0:
            break

        sum_background += t * histogram[t]
        mean_background = sum_background / weight_background
        mean_foreground = (sum_total - sum_background) / weight_foreground

        # Between-class variance.
        variance = weight_background * weight_foreground * (mean_background - mean_foreground) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = t

    return best_threshold


def _detect_rotation_degrees(image: Any, *, pytesseract: Any, lang: str | None) -> int:
    """Detect coarse page rotation via Tesseract OSD.

    Tesseract reports 0/90/180/270. This corrects common phone/camera rotations.
    """

    try:
        osd = pytesseract.image_to_osd(image, lang=lang)  # type: ignore[attr-defined]
    except Exception:
        return 0
    if not isinstance(osd, str):
        try:
            osd = str(osd)
        except Exception:
            return 0

    rotate: int | None = None
    for line in osd.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == "rotate":
            parsed = _parse_float(value)
            if parsed is None:
                continue
            rotate = int(parsed)
            break

    if rotate in {0, 90, 180, 270}:
        return rotate
    return 0


def _rotate_image(image: Any, rotate_degrees_clockwise: int) -> Any:
    if rotate_degrees_clockwise == 0:
        return image
    # Pillow rotates counter-clockwise for positive angles.
    return image.rotate(-rotate_degrees_clockwise, expand=True)


class TesseractOCRBackend(OCRBackend):
    backend_name = "tesseract"

    def __init__(
        self,
        *,
        tesseract_cmd: str | None = None,
        languages: str = "eng",
        enable_preprocessing: bool = True,
        enable_osd: bool = True,
        tesseract_config: str | None = None,
    ) -> None:
        self._tesseract_cmd = tesseract_cmd
        self._languages = (languages or "").strip() or "eng"
        self._enable_preprocessing = enable_preprocessing
        self._enable_osd = enable_osd
        self._tesseract_config = (tesseract_config or "").strip() or None

    def extract_text(self, image_bytes: bytes, content_type: str | None) -> OCRResult:
        resolved_type = _normalize_content_type(content_type, image_bytes)
        image = _load_pil_image(image_bytes, resolved_type)
        if self._enable_preprocessing:
            image = _preprocess_image(image)

        try:
            import pytesseract  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(_missing_dependency_message("pytesseract")) from exc

        previous_cmd = getattr(pytesseract.pytesseract, "tesseract_cmd", None)
        if self._tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self._tesseract_cmd

        try:
            if self._enable_osd:
                rotate = _detect_rotation_degrees(
                    image,
                    pytesseract=pytesseract,
                    lang=self._languages,
                )
                image = _rotate_image(image, rotate)

            data = pytesseract.image_to_data(  # type: ignore[attr-defined]
                image,
                lang=self._languages,
                config=self._tesseract_config,
                output_type=getattr(pytesseract, "Output", None).DICT,  # type: ignore[union-attr]
            )
        except AttributeError as exc:
            # Likely missing expected pytesseract attributes.
            raise RuntimeError(_missing_dependency_message("pytesseract")) from exc
        finally:
            if self._tesseract_cmd and previous_cmd is not None:
                pytesseract.pytesseract.tesseract_cmd = previous_cmd

        regions: list[OCRRegion] = []
        confidences: list[float] = []

        text_values = list(data.get("text", []))
        left_values = list(data.get("left", []))
        top_values = list(data.get("top", []))
        width_values = list(data.get("width", []))
        height_values = list(data.get("height", []))
        conf_values = list(data.get("conf", []))

        # Used to rebuild text with reasonable line breaks.
        page_values = list(data.get("page_num", []))
        block_values = list(data.get("block_num", []))
        par_values = list(data.get("par_num", []))
        line_values = list(data.get("line_num", []))

        lines: list[str] = []
        current_line_key: tuple[int, int, int, int] | None = None
        current_line_words: list[str] = []

        n = len(text_values)
        for i in range(n):
            raw_text = str(text_values[i] or "").strip()
            if not raw_text:
                continue

            conf = _parse_float(conf_values[i] if i < len(conf_values) else None)
            if conf is not None and conf >= 0:
                confidences.append(conf)
            else:
                conf = None

            left = int(left_values[i]) if i < len(left_values) else 0
            top = int(top_values[i]) if i < len(top_values) else 0
            width = int(width_values[i]) if i < len(width_values) else 0
            height = int(height_values[i]) if i < len(height_values) else 0

            regions.append(
                OCRRegion(
                    text=raw_text,
                    confidence=conf,
                    bbox=OCRBoundingBox(left=left, top=top, width=width, height=height),
                )
            )

            # Best-effort line reconstruction.
            page = int(page_values[i]) if i < len(page_values) else 0
            block = int(block_values[i]) if i < len(block_values) else 0
            par = int(par_values[i]) if i < len(par_values) else 0
            line = int(line_values[i]) if i < len(line_values) else 0
            line_key = (page, block, par, line)
            if current_line_key is None:
                current_line_key = line_key
            if line_key != current_line_key:
                if current_line_words:
                    lines.append(" ".join(current_line_words).strip())
                current_line_words = [raw_text]
                current_line_key = line_key
            else:
                current_line_words.append(raw_text)

        if current_line_words:
            lines.append(" ".join(current_line_words).strip())

        full_text = "\n".join([line for line in lines if line]).strip()
        return OCRResult(
            text=full_text,
            confidence=_mean(confidences),
            regions=regions,
        )

