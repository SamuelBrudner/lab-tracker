"""Best-effort decoding of QR codes and barcodes in uploaded photos.

This is deterministic decoding of machine-readable symbols (QR, DataMatrix,
Code 128, EAN, ...) with the optional ``zxing-cpp`` package, run locally on
the instance. It is not OCR: no printed text is read, no model or external
service is called, and nothing leaves the instance. The result is only note
metadata:

* ``decoded_session_link_code`` -- an ``LT-<code>`` session link code (or a
  session capture-link URL) found in the photo, plus
  ``decoded_session_link_code_count``; ``photo_session_id`` is stamped only
  when exactly one decoded session belongs to the note's own project, and
  the exact-id provenance detector then *proposes* that session link.
* ``barcode_gs1_*`` -- GTIN, lot, expiry, serial, and catalog number parsed
  from a GS1 element string (see :mod:`lab_tracker.gs1`).
* ``barcode_text`` / ``barcode_text_format`` -- the first other code,
  bounded to :data:`MAX_BARCODE_TEXT_CHARS`.
* ``barcode_count`` -- how many distinct codes were decoded.

Decoding is never required and never blocks an upload: every limit
(bytes, pixels, a per-upload time budget, concurrent decodes) skips the
decode, and every decode failure is logged and ignored.
"""

from __future__ import annotations

import concurrent.futures
import importlib
import importlib.util
import io
import logging
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, BinaryIO, Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from lab_tracker.errors import ValidationError
from lab_tracker.gs1 import GS, GS1_METADATA_KEYS, GS1_SYMBOLOGY_IDENTIFIERS, parse_gs1_fields
from lab_tracker.models import (
    NoteMetadataScalar,
    decode_session_link_code,
    encode_session_link_code,
)

_logger = logging.getLogger(__name__)

DECODED_SESSION_LINK_CODE_KEY = "decoded_session_link_code"
DECODED_SESSION_LINK_CODE_COUNT_KEY = "decoded_session_link_code_count"
PHOTO_SESSION_ID_KEY = "photo_session_id"
BARCODE_TEXT_KEY = "barcode_text"
BARCODE_TEXT_FORMAT_KEY = "barcode_text_format"
BARCODE_COUNT_KEY = "barcode_count"
# Every key this module stamps. They are server-derived: an upload that
# supplies one is rejected, and a capture replay ignores them when it checks
# that the replay matches the original upload.
DECODED_CODE_METADATA_KEYS: frozenset[str] = frozenset(
    {
        DECODED_SESSION_LINK_CODE_KEY,
        DECODED_SESSION_LINK_CODE_COUNT_KEY,
        PHOTO_SESSION_ID_KEY,
        BARCODE_TEXT_KEY,
        BARCODE_TEXT_FORMAT_KEY,
        BARCODE_COUNT_KEY,
        *GS1_METADATA_KEYS,
    }
)

# Phone-camera and screenshot formats, and the one Pillow plugin allowed to
# open each. TIFF (microscopy stacks), HEIC (needs a plugin), and SVG (not a
# raster) are not decoded. Pillow sniffs the real format, so opening is
# restricted to the declared type's plugin: a payload in any other format
# (EPS, whose loader runs Ghostscript in a subprocess; PDF; ...) labelled as
# a photo never reaches another plugin. None of these five shells out.
_PILLOW_FORMAT_BY_CONTENT_TYPE: dict[str, str] = {
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/pjpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
    "image/bmp": "BMP",
}
# What ``image.format`` may report for each opener (the JPEG opener returns
# multi-picture JPEGs from phone cameras as MPO).
_OPENED_FORMATS: dict[str, frozenset[str]] = {
    "JPEG": frozenset({"JPEG", "MPO"}),
    "PNG": frozenset({"PNG"}),
    "WEBP": frozenset({"WEBP"}),
    "GIF": frozenset({"GIF"}),
    "BMP": frozenset({"BMP"}),
}
DECODABLE_CONTENT_TYPES: frozenset[str] = frozenset(_PILLOW_FORMAT_BY_CONTENT_TYPE)
# Uploads larger than this are not read for decoding at all.
MAX_DECODE_BYTES = 32 * 1024 * 1024
# Images whose header declares more pixels than this are skipped before any
# pixel data is decoded (a 48 MP phone photo is 48_000_000). A JPEG is decoded
# straight to a reduced scale; other formats must be decoded at full size, so
# their cap is lower (24 MP is at most ~96 MB of RGBA while converting).
MAX_DECODE_PIXELS = 50_000_000
MAX_FULL_DECODE_PIXELS = 24_000_000
# Decoding works on a grayscale copy no longer than this on its long side;
# a 12 MP phone photo (4032 x 3024) is decoded at full resolution.
MAX_DECODE_SIDE = 4096
# At most this many codes per photo are considered.
MAX_CODES_PER_PHOTO = 16
MAX_BARCODE_TEXT_CHARS = 256
# Photos decoded at the same time across the whole process; a photo that
# arrives while every slot is busy is not decoded.
MAX_CONCURRENT_DECODES = 2

LINK_CODE_PREFIX = "LT-"
_LINK_CODE_LENGTH = 26
# Same token rule as the ``lt`` client (session_context): only an explicit
# ``LT-`` prefix counts, so 26 arbitrary base32 letters never claim a session.
_LINK_CODE_TOKEN = re.compile(
    rf"(?<![A-Za-z0-9]){re.escape(LINK_CODE_PREFIX)}([A-Za-z2-7]{{{_LINK_CODE_LENGTH}}})"
    r"(?![A-Za-z0-9])"
)
# The session capture link (GET /sessions/{id}/capture-link) opens this path.
_CAPTURE_PATH_SUFFIX = "/app/capture"
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class PhotoCodeLimitError(ValueError):
    """The image is outside the decode bounds, so it is skipped."""


@dataclass(frozen=True)
class DecodedCode:
    """One symbol read from a photo."""

    text: str
    format: str
    is_gs1: bool = False


@dataclass(frozen=True)
class SessionReference:
    """A session named by a decoded code, as its canonical link code and id."""

    link_code: str
    session_id: UUID


# A reader gets the upload's bytes and its declared content type.
CodeReader = Callable[[bytes, str], Sequence[DecodedCode]]
SessionInProject = Callable[[UUID], bool]


class PhotoCodeSettings(Protocol):
    decode_photo_codes: bool
    decode_photo_codes_timeout_seconds: float


def decoder_available() -> bool:
    """True when the optional ``decode`` extra (zxing-cpp and Pillow) is installed."""

    return all(importlib.util.find_spec(name) is not None for name in ("zxingcpp", "PIL"))


def _decode_link_code(code: str) -> UUID | None:
    normalized = code.upper()
    try:
        session_id = decode_session_link_code(normalized)
    except ValueError:
        return None
    # Only the canonical form the server prints (zero pad bits) counts.
    return session_id if encode_session_link_code(session_id) == normalized else None


def _capture_link_session(text: str) -> UUID | None:
    if "://" not in text:
        return None
    try:
        parts = urlsplit(text.strip())
    except ValueError:
        return None
    if not parts.path.rstrip("/").endswith(_CAPTURE_PATH_SUFFIX):
        return None
    for value in parse_qs(parts.query).get("session_id", []):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def session_references(text: str) -> list[SessionReference]:
    """Sessions a decoded text names: ``LT-<code>`` tokens or a capture-link URL."""

    references: list[SessionReference] = []
    for match in _LINK_CODE_TOKEN.finditer(text or ""):
        session_id = _decode_link_code(match.group(1))
        if session_id is not None:
            references.append(SessionReference(encode_session_link_code(session_id), session_id))
    capture_session = _capture_link_session(text or "")
    if capture_session is not None:
        references.append(
            SessionReference(encode_session_link_code(capture_session), capture_session)
        )
    return references


def _bounded_text(text: str) -> str:
    visible = _CONTROL_CHARACTERS.sub(
        lambda match: "<GS>" if match.group(0) == GS else " ", text
    ).strip()
    if len(visible) > MAX_BARCODE_TEXT_CHARS:
        return visible[: MAX_BARCODE_TEXT_CHARS - 1] + "…"
    return visible


def photo_code_metadata(
    codes: Iterable[DecodedCode],
    *,
    session_in_project: SessionInProject,
    today: date | None = None,
) -> dict[str, NoteMetadataScalar]:
    """Note metadata for the codes decoded from one photo.

    ``session_in_project`` answers whether a session id belongs to the
    note's own project; ``photo_session_id`` is stamped only when exactly one
    distinct decoded session does. A code naming a session is never also
    reported as ``barcode_text``; GS1 fields from several symbols merge with
    the first value of each key winning.
    """

    resolved_today = today or datetime.now(timezone.utc).date()
    distinct: list[DecodedCode] = []
    seen_texts: set[str] = set()
    for code in codes:
        if code.text and code.text not in seen_texts:
            seen_texts.add(code.text)
            distinct.append(code)
        if len(distinct) >= MAX_CODES_PER_PHOTO:
            break
    if not distinct:
        return {}
    metadata: dict[str, NoteMetadataScalar] = {BARCODE_COUNT_KEY: len(distinct)}
    references: dict[UUID, SessionReference] = {}
    other: list[DecodedCode] = []
    for code in distinct:
        found = session_references(code.text)
        if found:
            for reference in found:
                references.setdefault(reference.session_id, reference)
            continue
        gs1 = parse_gs1_fields(code.text, today=resolved_today, is_gs1=code.is_gs1)
        if gs1:
            for key, value in gs1.items():
                metadata.setdefault(key, value)
            continue
        other.append(code)
    if references:
        ordered = list(references.values())
        in_project = [ref for ref in ordered if _safe_in_project(session_in_project, ref)]
        chosen = in_project[0] if len(in_project) == 1 else ordered[0]
        metadata[DECODED_SESSION_LINK_CODE_KEY] = f"{LINK_CODE_PREFIX}{chosen.link_code}"
        metadata[DECODED_SESSION_LINK_CODE_COUNT_KEY] = len(ordered)
        if len(in_project) == 1:
            metadata[PHOTO_SESSION_ID_KEY] = str(in_project[0].session_id)
    if other:
        metadata[BARCODE_TEXT_KEY] = _bounded_text(other[0].text)
        metadata[BARCODE_TEXT_FORMAT_KEY] = other[0].format
    return metadata


def _safe_in_project(session_in_project: SessionInProject, reference: SessionReference) -> bool:
    try:
        return bool(session_in_project(reference.session_id))
    except Exception:
        _logger.warning(
            "Could not resolve decoded session link code LT-%s; leaving it unresolved.",
            reference.link_code,
            exc_info=True,
        )
        return False


def read_image_codes(
    data: bytes,
    content_type: str,
    *,
    max_pixels: int = MAX_DECODE_PIXELS,
    max_full_decode_pixels: int = MAX_FULL_DECODE_PIXELS,
    max_side: int = MAX_DECODE_SIDE,
) -> list[DecodedCode]:
    """Decode every QR code and barcode zxing-cpp finds in an encoded image.

    Only the Pillow plugin for the declared ``content_type`` may open the
    bytes, and the opened format must match it; anything else raises before
    any pixel data is decoded. Raises :class:`PhotoCodeLimitError` for an
    undecodable content type or an image over ``max_pixels``
    (``max_full_decode_pixels`` for a non-JPEG, which cannot be decoded at a
    reduced scale), checked from the header before pixel data is decoded,
    and whatever Pillow or zxing-cpp raise for undecodable input; the caller
    contains both.
    """

    image_module: Any = importlib.import_module("PIL.Image")
    zxingcpp: Any = importlib.import_module("zxingcpp")
    pillow_format = _PILLOW_FORMAT_BY_CONTENT_TYPE.get(content_type.strip().lower())
    if pillow_format is None:
        raise PhotoCodeLimitError(f"{content_type!r} is not a decodable photo type")
    # Bytes that are not a readable image of the declared type (a HEIC photo
    # labelled JPEG, a truncated upload) are an expected skip, not a failure.
    unreadable = getattr(image_module, "UnidentifiedImageError", ())
    try:
        opened = image_module.open(io.BytesIO(data), formats=(pillow_format,))
    except unreadable as exc:
        raise PhotoCodeLimitError(f"upload is not a readable {pillow_format} image") from exc
    with opened as image:
        if image.format not in _OPENED_FORMATS[pillow_format]:
            raise PhotoCodeLimitError(
                f"{content_type} upload opened as {image.format}; not decoding it"
            )
        width, height = image.size
        limit = max_pixels if image.format == "JPEG" else min(max_pixels, max_full_decode_pixels)
        if width * height > limit:
            raise PhotoCodeLimitError(f"image has {width * height} pixels (limit {limit})")
        # JPEG only: decode at a reduced scale when the photo is much larger.
        image.draft("L", (max_side, max_side))
        gray = image.convert("L")
    if max(gray.size) > max_side:
        gray.thumbnail((max_side, max_side))
    gs1_type = zxingcpp.ContentType.GS1
    codes: list[DecodedCode] = []
    for barcode in zxingcpp.read_barcodes(gray, text_mode=zxingcpp.TextMode.Plain):
        is_gs1 = (
            barcode.content_type == gs1_type
            or str(barcode.symbology_identifier) in GS1_SYMBOLOGY_IDENTIFIERS
        )
        barcode_format = getattr(barcode.format, "name", None) or str(barcode.format)
        codes.append(DecodedCode(text=str(barcode.text), format=barcode_format, is_gs1=is_gs1))
        if len(codes) >= MAX_CODES_PER_PHOTO:
            break
    return codes


class PhotoCodeDecoder:
    """Run a code reader under a time budget and a process-wide concurrency cap.

    ``decode`` never raises and never waits longer than its timeout: an
    image that takes longer is abandoned (its worker finishes in the
    background, bounded by the pixel limits), and a photo that arrives while
    every slot is busy is skipped rather than queued.
    """

    def __init__(
        self,
        reader: CodeReader | None = None,
        *,
        max_concurrent: int = MAX_CONCURRENT_DECODES,
    ) -> None:
        self._reader: CodeReader = reader or read_image_codes
        self._max_concurrent = max_concurrent
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    def _pool(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self._max_concurrent,
                    thread_name_prefix="lab-tracker-photo-codes",
                )
            return self._executor

    def _run(self, data: bytes, content_type: str) -> list[DecodedCode]:
        try:
            return list(self._reader(data, content_type))
        finally:
            self._slots.release()

    def decode(
        self, data: bytes, *, content_type: str, timeout_seconds: float
    ) -> list[DecodedCode] | None:
        """The codes in ``data``, or ``None`` when decoding was skipped or failed."""

        if not self._slots.acquire(blocking=False):
            _logger.info(
                "Photo code decoding skipped: %d decodes already running.", self._max_concurrent
            )
            return None
        try:
            future = self._pool().submit(self._run, data, content_type)
        except Exception:
            self._slots.release()
            _logger.exception("Photo code decoding could not start.")
            return None
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            _logger.warning(
                "Photo code decoding exceeded its %.2fs budget; the upload proceeds without it.",
                timeout_seconds,
            )
        except PhotoCodeLimitError as exc:
            _logger.info("Photo code decoding skipped: %s.", exc)
        except Exception:
            _logger.warning(
                "Photo code decoding failed; the upload proceeds without it.", exc_info=True
            )
        return None

    def close(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)


_DEFAULT_DECODER: PhotoCodeDecoder | None = None
_DEFAULT_DECODER_LOCK = threading.Lock()


def default_photo_code_decoder() -> PhotoCodeDecoder:
    """The process-wide decoder, created on first use."""

    global _DEFAULT_DECODER
    with _DEFAULT_DECODER_LOCK:
        if _DEFAULT_DECODER is None:
            _DEFAULT_DECODER = PhotoCodeDecoder()
        return _DEFAULT_DECODER


def _refuse_decoded_code_keys(keys: Iterable[str]) -> None:
    claimed = sorted(keys)
    if claimed:
        raise ValidationError(
            "Decoded-code metadata keys are stamped by the server: " + ", ".join(claimed) + "."
        )


def ensure_no_client_decoded_code_keys(metadata: Mapping[str, Any] | None) -> None:
    """Reject a new capture whose client metadata claims a decoded-code key."""

    _refuse_decoded_code_keys(key for key in metadata or {} if key in DECODED_CODE_METADATA_KEYS)


def _stored_form(value: Any) -> str:
    # Mirrors normalize_note_metadata: metadata values are stored as strings.
    return value.strip() if isinstance(value, str) else str(value)


def ensure_no_client_decoded_code_changes(
    metadata: Mapping[str, Any] | None, *, stored: Mapping[str, Any]
) -> None:
    """Reject a metadata replacement that adds or changes a decoded-code key.

    ``PATCH /notes/{id}`` replaces the whole metadata bag and clients send
    back what they read, so a decoded key kept with its stored value, or
    dropped, is allowed; a new or different value is not.
    """

    _refuse_decoded_code_keys(
        key
        for key, value in (metadata or {}).items()
        if key in DECODED_CODE_METADATA_KEYS
        and (key not in stored or _stored_form(value) != _stored_form(stored[key]))
    )


def _read_bounded(stream: BinaryIO, max_bytes: int) -> bytes | None:
    stream.seek(0)
    data = stream.read(max_bytes + 1)
    return None if len(data) > max_bytes else data


def decoded_upload_metadata(
    stream: BinaryIO,
    *,
    content_type: str,
    size_bytes: int,
    settings: PhotoCodeSettings,
    session_in_project: SessionInProject,
    decoder: PhotoCodeDecoder | None = None,
    today: date | None = None,
) -> dict[str, NoteMetadataScalar]:
    """Decoded-code metadata for one uploaded file; ``{}`` whenever it is skipped.

    Skipped when the kill switch is off, the content type is not a decodable
    raster photo, the upload exceeds :data:`MAX_DECODE_BYTES`, or no decoder
    is available (the ``decode`` extra is not installed and no decoder was
    injected). Never raises: a failure is logged and the upload proceeds.
    """

    try:
        if not settings.decode_photo_codes:
            return {}
        if content_type.lower() not in DECODABLE_CONTENT_TYPES:
            return {}
        if size_bytes > MAX_DECODE_BYTES:
            _logger.info("Photo code decoding skipped: %d bytes is over the limit.", size_bytes)
            return {}
        if decoder is None:
            if not decoder_available():
                return {}
            decoder = default_photo_code_decoder()
        data = _read_bounded(stream, MAX_DECODE_BYTES)
        if not data:
            return {}
        codes = decoder.decode(
            data,
            content_type=content_type,
            timeout_seconds=settings.decode_photo_codes_timeout_seconds,
        )
        if not codes:
            return {}
        return photo_code_metadata(codes, session_in_project=session_in_project, today=today)
    except Exception:
        _logger.exception("Photo code decoding failed; the upload proceeds without it.")
        return {}


__all__ = [
    "BARCODE_COUNT_KEY",
    "BARCODE_TEXT_FORMAT_KEY",
    "BARCODE_TEXT_KEY",
    "DECODABLE_CONTENT_TYPES",
    "DECODED_CODE_METADATA_KEYS",
    "DECODED_SESSION_LINK_CODE_COUNT_KEY",
    "DECODED_SESSION_LINK_CODE_KEY",
    "DecodedCode",
    "MAX_BARCODE_TEXT_CHARS",
    "MAX_DECODE_BYTES",
    "MAX_DECODE_PIXELS",
    "MAX_DECODE_SIDE",
    "MAX_FULL_DECODE_PIXELS",
    "PHOTO_SESSION_ID_KEY",
    "PhotoCodeDecoder",
    "PhotoCodeLimitError",
    "SessionReference",
    "decoded_upload_metadata",
    "decoder_available",
    "default_photo_code_decoder",
    "ensure_no_client_decoded_code_changes",
    "ensure_no_client_decoded_code_keys",
    "photo_code_metadata",
    "read_image_codes",
    "session_references",
]
