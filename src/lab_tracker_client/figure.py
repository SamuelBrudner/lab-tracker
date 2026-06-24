"""Fail-soft figure capture helpers for Lab Tracker consumer scripts."""

from __future__ import annotations

import io
import mimetypes
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client.client import (
    DEFAULT_BASE_URL,
    LabTracker,
    LTAPIError,
    LTRecord,
    LTValidationError,
    _bytes_sha256,
    _env_access_token,
    _validate_metadata,
    build_evidence_metadata,
)

FIGURE_CAPTURE_TIMEOUT_SECONDS = 2.5
FIGURE_PREVIEW_MAX_BYTES = 2_000_000
FIGURE_UPLOAD_MAX_BYTES = 100 * 1024 * 1024
_DEFAULT_IMAGE_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.svg", "*.pdf", "*.tif", "*.tiff")
_RUN_CONTEXT: ContextVar[RunContext | None] = ContextVar("lab_tracker_run_context", default=None)
_CIRCUIT_OPEN = False
_WARNED: set[str] = set()
_FIRST_CAPTURE_REVIEW_METADATA_KEYS = frozenset(
    {
        "figure_no_preview",
        "figure_preview_size_bytes",
    }
)


@dataclass(frozen=True)
class FigureCaptureResult:
    """Outcome from capturing one saved figure file."""

    action: str
    path: str
    source_external_id: str
    source_uri: str
    content_hash: str
    metadata: dict[str, NoteMetadataScalar]
    client_capture_id: str
    note: LTRecord | None = None
    no_preview: bool = False
    stale_review_bytes: bool = False
    reason: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": self.action,
            "path": self.path,
            "source_external_id": self.source_external_id,
            "source_uri": self.source_uri,
            "evidence_content_hash": self.content_hash,
            "metadata": dict(self.metadata),
            "client_capture_id": self.client_capture_id,
            "no_preview": self.no_preview,
            "stale_review_bytes": self.stale_review_bytes,
        }
        if self.note is not None:
            payload["note"] = self.note.to_dict()
            with suppress(AttributeError):
                payload["note_id"] = self.note.id
        if self.reason:
            payload["reason"] = self.reason
        if self.errors:
            payload["errors"] = list(self.errors)
        return payload


@dataclass(frozen=True)
class RunContext:
    """Scalar run metadata carried by figure captures for a bounded time."""

    captured_at: str
    expires_at: float
    git_commit: str = ""
    git_dirty: bool = False
    extra: dict[str, NoteMetadataScalar] = field(default_factory=dict)

    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def to_metadata(self) -> dict[str, NoteMetadataScalar]:
        metadata: dict[str, NoteMetadataScalar] = {
            "run_captured_at": self.captured_at,
            "run_git_dirty": self.git_dirty,
        }
        if self.git_commit:
            metadata["run_git_commit"] = self.git_commit
        for key, value in self.extra.items():
            metadata[f"run_{key}"] = value
        return metadata


class _RunContextManager:
    def __init__(self, context: RunContext) -> None:
        self._context = context
        self._token: Any = None

    def __enter__(self) -> RunContext:
        self._token = _RUN_CONTEXT.set(self._context)
        return self._context

    def __exit__(self, *_: object) -> None:
        if self._token is not None:
            _RUN_CONTEXT.reset(self._token)


@dataclass(frozen=True)
class _PreviewPayload:
    payload: bytes
    path: Path
    content_type: str
    no_preview: bool

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


class FigureCaptureContext:
    """Context manager returned by capture_figures()."""

    def __init__(
        self,
        root: str | Path,
        *,
        patterns: Iterable[str] = _DEFAULT_IMAGE_PATTERNS,
        recursive: bool = True,
        client: LabTracker | None = None,
        project_id: str | None = None,
        metadata: Mapping[str, NoteMetadataScalar] | None = None,
        logical_prefix: str | None = None,
        preview_max_bytes: int = FIGURE_PREVIEW_MAX_BYTES,
        version_every_change: bool = False,
    ) -> None:
        self.root = Path(root).expanduser()
        self.patterns = tuple(patterns)
        self.recursive = recursive
        self.client = client
        self.project_id = project_id
        self.metadata = metadata
        self.logical_prefix = logical_prefix
        self.preview_max_bytes = preview_max_bytes
        self.version_every_change = version_every_change
        self.results: list[FigureCaptureResult] = []
        self.errors: list[str] = []
        self._snapshot: dict[Path, int] = {}

    def __enter__(self) -> FigureCaptureContext:
        self._snapshot = _snapshot_image_mtimes(self.root, self.patterns, recursive=self.recursive)
        return self

    def __exit__(self, *_: object) -> bool:
        try:
            after = _snapshot_image_mtimes(self.root, self.patterns, recursive=self.recursive)
            for path, mtime in sorted(after.items(), key=lambda item: item[0].as_posix()):
                if self._snapshot.get(path) == mtime:
                    continue
                logical_id = _capture_context_logical_id(
                    path,
                    root=self.root,
                    logical_prefix=self.logical_prefix,
                )
                self.results.append(
                    _capture_saved_figure(
                        fig=None,
                        path=path,
                        client=self.client,
                        project_id=self.project_id,
                        logical_id=logical_id,
                        metadata=self.metadata,
                        preview_max_bytes=self.preview_max_bytes,
                        version_every_change=self.version_every_change,
                    )
                )
        except Exception as exc:  # pragma: no cover - defensive guard for body exceptions.
            self.errors.append(str(exc))
            _warn_once(
                "capture-figures-scan",
                f"Lab Tracker figure capture could not scan saved figures: {exc}",
            )
        return False


def run_context(
    *,
    ttl_seconds: float = 1800,
    extra: Mapping[str, NoteMetadataScalar] | None = None,
) -> _RunContextManager:
    """Return a context manager that adds run metadata to figure captures."""

    resolved_extra = dict(_validate_metadata(extra) or {})
    context = RunContext(
        captured_at=datetime.now(timezone.utc).isoformat(),
        expires_at=time.monotonic() + max(0.0, float(ttl_seconds)),
        git_commit=_git_output("rev-parse", "HEAD"),
        git_dirty=bool(_git_output("status", "--porcelain")),
        extra=resolved_extra,
    )
    return _RunContextManager(context)


def savefig(
    fig: Any,
    path: str | Path,
    *,
    client: LabTracker | None = None,
    project_id: str | None = None,
    logical_id: str | None = None,
    metadata: Mapping[str, NoteMetadataScalar] | None = None,
    preview_max_bytes: int = FIGURE_PREVIEW_MAX_BYTES,
    version_every_change: bool = False,
    **savefig_kwargs: Any,
) -> FigureCaptureResult:
    """Save a figure, then fail-soft capture the saved artifact in Lab Tracker."""

    resolved_path = Path(path).expanduser()
    if fig is not None:
        fig.savefig(resolved_path, **savefig_kwargs)
    return _capture_saved_figure(
        fig=fig,
        path=resolved_path,
        client=client,
        project_id=project_id,
        logical_id=logical_id,
        metadata=metadata,
        preview_max_bytes=preview_max_bytes,
        version_every_change=version_every_change,
    )


def capture_figures(
    root: str | Path = ".",
    *,
    patterns: Iterable[str] = _DEFAULT_IMAGE_PATTERNS,
    recursive: bool = True,
    client: LabTracker | None = None,
    project_id: str | None = None,
    metadata: Mapping[str, NoteMetadataScalar] | None = None,
    logical_prefix: str | None = None,
    preview_max_bytes: int = FIGURE_PREVIEW_MAX_BYTES,
    version_every_change: bool = False,
) -> FigureCaptureContext:
    """Capture image files created or modified inside a context body."""

    return FigureCaptureContext(
        root,
        patterns=patterns,
        recursive=recursive,
        client=client,
        project_id=project_id,
        metadata=metadata,
        logical_prefix=logical_prefix,
        preview_max_bytes=preview_max_bytes,
        version_every_change=version_every_change,
    )


def _capture_saved_figure(
    *,
    fig: Any,
    path: str | Path,
    client: LabTracker | None,
    project_id: str | None,
    logical_id: str | None,
    metadata: Mapping[str, NoteMetadataScalar] | None,
    preview_max_bytes: int,
    version_every_change: bool,
) -> FigureCaptureResult:
    resolved_path = Path(path).expanduser()
    result_defaults: dict[str, Any] = {
        "action": "failed",
        "path": str(resolved_path),
        "source_external_id": "",
        "source_uri": "",
        "content_hash": "",
        "metadata": {},
        "client_capture_id": "",
    }
    try:
        full_payload = resolved_path.read_bytes()
        if not full_payload:
            raise LTValidationError("Figure file must not be empty.")
        content_hash = _bytes_sha256(full_payload)
        resolved_path = resolved_path.resolve()
        source_uri = resolved_path.as_uri()
        client_capture_id = _client_capture_id(
            resolved_path,
            logical_id=logical_id,
            content_hash=content_hash if version_every_change else None,
        )
        base_metadata = _base_figure_metadata(
            path=resolved_path,
            source_uri=source_uri,
            source_external_id=client_capture_id,
            content_hash=content_hash,
            payload_size=len(full_payload),
            metadata=metadata,
        )
        result_defaults.update(
            {
                "path": str(resolved_path),
                "source_external_id": client_capture_id,
                "source_uri": source_uri,
                "content_hash": content_hash,
                "metadata": base_metadata,
                "client_capture_id": client_capture_id,
            }
        )
        if _CIRCUIT_OPEN:
            _warn_once(
                "circuit-open",
                "Lab Tracker figure capture is paused after a connection or timeout failure.",
            )
            return FigureCaptureResult(
                **{**result_defaults, "action": "skipped", "reason": "circuit_open"}
            )
        resolved_client, resolved_project_id, close_client = _resolve_capture_client(
            client=client,
            project_id=project_id,
        )
        if resolved_client is None or resolved_project_id is None:
            _warn_once(
                "unconfigured",
                "Lab Tracker figure capture is unconfigured; set project/API settings "
                "or pass a client.",
            )
            return FigureCaptureResult(
                **{**result_defaults, "action": "skipped", "reason": "unconfigured"}
            )
        try:
            with _capture_timeout(resolved_client):
                preview = _preview_payload(
                    fig=fig,
                    path=resolved_path,
                    full_payload=full_payload,
                    max_bytes=preview_max_bytes,
                    source_uri=source_uri,
                    content_hash=content_hash,
                )
                upload_metadata = dict(base_metadata)
                upload_metadata.update(
                    {
                        "figure_no_preview": preview.no_preview,
                        "figure_preview_size_bytes": preview.size_bytes,
                        "figure_review_bytes_stale": False,
                    }
                )
                note, status_code = resolved_client._upload_note_file_payload_with_status(
                    project_id=resolved_project_id,
                    path=preview.path,
                    payload=preview.payload,
                    metadata=upload_metadata,
                    status="staged",
                    content_type=preview.content_type,
                    client_capture_id=client_capture_id,
                )
                if status_code == 200:
                    return _coalesced_result(
                        client=resolved_client,
                        note=note,
                        result_defaults=result_defaults,
                        current_metadata=upload_metadata,
                        content_hash=content_hash,
                        source_uri=source_uri,
                        source_external_id=client_capture_id,
                        no_preview=preview.no_preview,
                    )
                return FigureCaptureResult(
                    **{
                        **result_defaults,
                        "action": "imported",
                        "metadata": upload_metadata,
                        "note": note,
                        "no_preview": preview.no_preview,
                    }
                )
        finally:
            if close_client:
                resolved_client.close()
    except Exception as exc:
        if _is_transport_failure(exc):
            _trip_circuit()
        _warn_once("capture-failed", f"Lab Tracker figure capture failed: {exc}")
        return FigureCaptureResult(
            **{
                **result_defaults,
                "action": "failed",
                "reason": "capture_failed",
                "errors": [str(exc)],
            }
        )


def _coalesced_result(
    *,
    client: LabTracker,
    note: LTRecord,
    result_defaults: dict[str, Any],
    current_metadata: dict[str, NoteMetadataScalar],
    content_hash: str,
    source_uri: str,
    source_external_id: str,
    no_preview: bool,
) -> FigureCaptureResult:
    note_metadata = note.get("metadata")
    existing_metadata = dict(note_metadata) if isinstance(note_metadata, Mapping) else {}
    existing_hash = str(existing_metadata.get("evidence_content_hash") or "")
    stale_review_bytes = bool(existing_hash and existing_hash != content_hash)
    if not stale_review_bytes:
        return FigureCaptureResult(
            **{
                **result_defaults,
                "action": "coalesced",
                "metadata": current_metadata,
                "note": note,
                "no_preview": no_preview,
            }
        )
    merged_metadata = _merge_current_metadata(
        existing_metadata=existing_metadata,
        current_metadata=current_metadata,
        content_hash=content_hash,
        source_uri=source_uri,
        source_external_id=source_external_id,
        stale_review_bytes=True,
    )
    try:
        note = client._patch_note_metadata(str(note.id), merged_metadata, status="staged")
    except Exception as exc:
        if _is_transport_failure(exc):
            _trip_circuit()
        return FigureCaptureResult(
            **{
                **result_defaults,
                "action": "coalesced",
                "metadata": merged_metadata,
                "note": note,
                "no_preview": no_preview,
                "stale_review_bytes": True,
                "reason": "metadata_patch_failed",
                "errors": [str(exc)],
            }
        )
    return FigureCaptureResult(
        **{
            **result_defaults,
            "action": "coalesced",
            "metadata": merged_metadata,
            "note": note,
            "no_preview": no_preview,
            "stale_review_bytes": True,
        }
    )


def _base_figure_metadata(
    *,
    path: Path,
    source_uri: str,
    source_external_id: str,
    content_hash: str,
    payload_size: int,
    metadata: Mapping[str, NoteMetadataScalar] | None,
) -> dict[str, NoteMetadataScalar]:
    merged = dict(_validate_metadata(metadata) or {})
    context = _active_run_context()
    if context is not None:
        merged.update(context.to_metadata())
    evidence = build_evidence_metadata(
        source_provider="local-figure",
        source_uri=source_uri,
        source_external_id=source_external_id,
        content_hash=content_hash,
        capture_kind="figure",
        adapter="lab-tracker-client-figure",
        title=path.name,
        metadata=merged,
    )
    evidence.update(
        {
            "content_hash_current": content_hash,
            "figure_content_hash_current": content_hash,
            "figure_source_uri_current": source_uri,
            "figure_source_external_id_current": source_external_id,
            "figure_client_capture_id": source_external_id,
            "figure_full_size_bytes": payload_size,
        }
    )
    return evidence


def _merge_current_metadata(
    *,
    existing_metadata: Mapping[str, Any],
    current_metadata: Mapping[str, NoteMetadataScalar],
    content_hash: str,
    source_uri: str,
    source_external_id: str,
    stale_review_bytes: bool,
) -> dict[str, NoteMetadataScalar]:
    merged: dict[str, NoteMetadataScalar] = {
        str(key): value
        for key, value in existing_metadata.items()
        if isinstance(value, (str, bool, int, float))
    }
    for key, value in current_metadata.items():
        if key.startswith("evidence_") and key in merged:
            continue
        if key in _FIRST_CAPTURE_REVIEW_METADATA_KEYS and key in merged:
            continue
        merged[key] = value
    merged.update(
        {
            "content_hash_current": content_hash,
            "figure_content_hash_current": content_hash,
            "figure_source_uri_current": source_uri,
            "figure_source_external_id_current": source_external_id,
            "figure_review_bytes_stale": stale_review_bytes,
        }
    )
    return merged


def _preview_payload(
    *,
    fig: Any,
    path: Path,
    full_payload: bytes,
    max_bytes: int,
    source_uri: str,
    content_hash: str,
) -> _PreviewPayload:
    resolved_max_bytes = max(1, min(int(max_bytes), FIGURE_UPLOAD_MAX_BYTES))
    if _is_pdf_path(path):
        rendered = _render_pdf_preview_png(fig=fig, path=path, max_bytes=resolved_max_bytes)
        if rendered is not None and len(rendered) <= resolved_max_bytes:
            return _PreviewPayload(
                payload=rendered,
                path=path.with_suffix(path.suffix + ".preview.png"),
                content_type="image/png",
                no_preview=False,
            )
        _warn_once(
            "pdf-preview-unavailable",
            "Lab Tracker figure capture could not render a PDF preview; "
            "uploading a pointer note.",
        )
        return _pointer_preview_payload(
            path=path,
            source_uri=source_uri,
            content_hash=content_hash,
            full_size_bytes=len(full_payload),
        )
    if len(full_payload) <= resolved_max_bytes:
        return _PreviewPayload(
            payload=full_payload,
            path=path,
            content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            no_preview=False,
        )
    rendered = _render_downscaled_png(fig, max_bytes=resolved_max_bytes)
    if rendered is not None and len(rendered) <= resolved_max_bytes:
        return _PreviewPayload(
            payload=rendered,
            path=path.with_suffix(path.suffix + ".preview.png"),
            content_type="image/png",
            no_preview=False,
        )
    _warn_once(
        "preview-unavailable",
        "Lab Tracker figure capture could not render a bounded preview; "
        "matplotlib/Pillow may be unavailable. Uploading a pointer note.",
    )
    return _pointer_preview_payload(
        path=path,
        source_uri=source_uri,
        content_hash=content_hash,
        full_size_bytes=len(full_payload),
    )


def _pointer_preview_payload(
    *,
    path: Path,
    source_uri: str,
    content_hash: str,
    full_size_bytes: int,
) -> _PreviewPayload:
    pointer = "\n".join(
        [
            "Lab Tracker figure pointer",
            f"source_uri: {source_uri}",
            f"evidence_content_hash: {content_hash}",
            f"full_size_bytes: {full_size_bytes}",
            "",
        ]
    ).encode("utf-8")
    return _PreviewPayload(
        payload=pointer,
        path=path.with_suffix(path.suffix + ".pointer.txt"),
        content_type="text/plain",
        no_preview=True,
    )


def _is_pdf_path(path: Path) -> bool:
    return path.suffix.lower() == ".pdf"


def _render_pdf_preview_png(*, fig: Any, path: Path, max_bytes: int) -> bytes | None:
    rendered_from_figure = _render_downscaled_png(fig, max_bytes=max_bytes)
    if rendered_from_figure is not None:
        return rendered_from_figure
    rendered_from_pdf = _render_pdfium_preview_png(path, max_bytes=max_bytes)
    if rendered_from_pdf is not None:
        return rendered_from_pdf
    return _render_pymupdf_preview_png(path, max_bytes=max_bytes)


def _render_pdfium_preview_png(path: Path, *, max_bytes: int) -> bytes | None:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        pdf = pdfium.PdfDocument(str(path))
        try:
            if len(pdf) == 0:
                return None
            page = pdf[0]
            image = page.render(scale=2).to_pil()
            return _encode_bounded_png(image, max_bytes=max_bytes)
        finally:
            with suppress(Exception):
                pdf.close()
    except Exception:
        return None


def _render_pymupdf_preview_png(path: Path, *, max_bytes: int) -> bytes | None:
    try:
        import fitz
        from PIL import Image
    except ImportError:
        return None
    try:
        document = fitz.open(str(path))
        try:
            if document.page_count == 0:
                return None
            page = document.load_page(0)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            payload = pixmap.tobytes("png")
            if len(payload) <= max_bytes:
                return payload
            image = Image.open(io.BytesIO(payload))
            return _encode_bounded_png(image, max_bytes=max_bytes)
        finally:
            document.close()
    except Exception:
        return None


def _render_downscaled_png(fig: Any, *, max_bytes: int) -> bytes | None:
    if fig is None:
        return None
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
    except ImportError:
        return None
    try:
        FigureCanvasAgg(fig).draw()
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=144, bbox_inches="tight")
        payload = buffer.getvalue()
        if len(payload) <= max_bytes:
            return payload
        from PIL import Image

        image = Image.open(io.BytesIO(payload))
        return _encode_bounded_png(image, max_bytes=max_bytes)
    except Exception:
        return None
    return None


def _encode_bounded_png(image: Any, *, max_bytes: int) -> bytes | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        payload = output.getvalue()
        if len(payload) <= max_bytes:
            return payload
        resampling = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        for scale in (0.75, 0.5, 0.33, 0.25, 0.16, 0.1):
            candidate = image.copy()
            candidate.thumbnail(
                (
                    max(1, int(image.width * scale)),
                    max(1, int(image.height * scale)),
                ),
                resampling,
            )
            output = io.BytesIO()
            candidate.save(output, format="PNG", optimize=True)
            payload = output.getvalue()
            if len(payload) <= max_bytes:
                return payload
    except Exception:
        return None
    return None


@contextmanager
def _capture_timeout(client: LabTracker) -> Iterable[None]:
    http_client = getattr(client, "_client", None)
    if http_client is None or not hasattr(http_client, "timeout"):
        yield
        return
    original_timeout = http_client.timeout
    http_client.timeout = _clamped_timeout(original_timeout)
    try:
        yield
    finally:
        http_client.timeout = original_timeout


def _clamped_timeout(timeout: Any) -> httpx.Timeout:
    with suppress(AttributeError):
        return httpx.Timeout(
            connect=_clamped_timeout_value(timeout.connect),
            read=_clamped_timeout_value(timeout.read),
            write=_clamped_timeout_value(timeout.write),
            pool=_clamped_timeout_value(timeout.pool),
        )
    return httpx.Timeout(_clamped_timeout_value(timeout))


def _clamped_timeout_value(value: Any) -> float:
    if value is None:
        return FIGURE_CAPTURE_TIMEOUT_SECONDS
    with suppress(TypeError, ValueError):
        return min(float(value), FIGURE_CAPTURE_TIMEOUT_SECONDS)
    return FIGURE_CAPTURE_TIMEOUT_SECONDS


def _resolve_capture_client(
    *,
    client: LabTracker | None,
    project_id: str | None,
) -> tuple[LabTracker | None, str | None, bool]:
    if client is not None:
        resolved_project_id = project_id or client.default_project_id
        return client, str(resolved_project_id) if resolved_project_id else None, False
    resolved_project_id = project_id or os.getenv("LAB_TRACKER_PROJECT_ID")
    base_url = os.getenv("LAB_TRACKER_BASE_URL") or os.getenv("LAB_TRACKER_MCP_BASE_URL")
    username = os.getenv("LAB_TRACKER_USERNAME") or os.getenv("LAB_TRACKER_MCP_USERNAME")
    password = os.getenv("LAB_TRACKER_PASSWORD") or os.getenv("LAB_TRACKER_MCP_PASSWORD")
    access_token = _env_access_token()
    if not resolved_project_id:
        return None, None, False
    return (
        LabTracker(
            base_url=base_url or DEFAULT_BASE_URL,
            username=username,
            password=password,
            access_token=access_token,
            default_project_id=resolved_project_id,
            timeout_seconds=FIGURE_CAPTURE_TIMEOUT_SECONDS,
        ),
        str(resolved_project_id),
        True,
    )


def _client_capture_id(
    path: Path,
    *,
    logical_id: str | None,
    content_hash: str | None,
) -> str:
    if logical_id is None:
        logical_id = _project_relative_path(path)
    cleaned = "/".join(part.strip() for part in str(logical_id).replace("\\", "/").split("/"))
    cleaned = cleaned.strip("/")
    if not cleaned:
        cleaned = path.name
    base = f"figure:{cleaned}"
    if content_hash:
        base = f"{base}:{content_hash[:12]}"
    if len(base) <= 120:
        return base
    import hashlib

    suffix = hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]
    return f"{base[:104]}:{suffix}"


def _project_relative_path(path: Path) -> str:
    resolved = path.resolve()
    with suppress(ValueError, OSError):
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    return path.name


def _capture_context_logical_id(
    path: Path,
    *,
    root: Path,
    logical_prefix: str | None,
) -> str:
    with suppress(ValueError, OSError):
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        return f"{logical_prefix.strip('/')}/{relative}" if logical_prefix else relative
    return f"{logical_prefix.strip('/')}/{path.name}" if logical_prefix else path.name


def _snapshot_image_mtimes(
    root: Path,
    patterns: Iterable[str],
    *,
    recursive: bool,
) -> dict[Path, int]:
    snapshot: dict[Path, int] = {}
    for pattern in patterns:
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        for path in iterator:
            try:
                if path.is_file():
                    snapshot[path.resolve()] = path.stat().st_mtime_ns
            except OSError:
                continue
    return snapshot


def _active_run_context() -> RunContext | None:
    context = _RUN_CONTEXT.get()
    if context is None:
        return None
    if context.expired():
        _RUN_CONTEXT.set(None)
        return None
    return context


def _git_output(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_transport_failure(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, (httpx.ConnectError, httpx.TimeoutException)):
            return True
        current = current.__cause__ or current.__context__
    if isinstance(exc, LTAPIError):
        text = str(exc).lower()
        return "connect" in text or "timeout" in text or "timed out" in text
    return False


def _trip_circuit() -> None:
    global _CIRCUIT_OPEN
    _CIRCUIT_OPEN = True


def _warn_once(key: str, message: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(message, file=sys.stderr)


def _reset_figure_capture_state_for_tests() -> None:
    global _CIRCUIT_OPEN
    _CIRCUIT_OPEN = False
    _WARNED.clear()
    _RUN_CONTEXT.set(None)
