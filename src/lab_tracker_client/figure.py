"""Fail-soft figure capture helpers for Lab Tracker consumer scripts."""

from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from lab_tracker.instance_url import normalize_instance_base_url
from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client.client import (
    LabTracker,
    LTAPIError,
    LTRecord,
    LTValidationError,
    _bytes_sha256,
    _validate_metadata,
    build_evidence_metadata,
    capture_host_metadata,
    file_sha256,
    load_connection_profile,
)
from lab_tracker_client.repo import normalize_remote

FIGURE_CAPTURE_TIMEOUT_SECONDS = 2.5
FIGURE_CIRCUIT_COOLDOWN_SECONDS = 30.0
FIGURE_PREVIEW_MAX_BYTES = 2_000_000
FIGURE_UPLOAD_MAX_BYTES = 100 * 1024 * 1024
_DEFAULT_IMAGE_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.svg", "*.pdf", "*.tif", "*.tiff")
_RUN_CONTEXT: ContextVar[RunContext | None] = ContextVar("lab_tracker_run_context", default=None)


@dataclass
class _BreakerState:
    """Open-circuit state for one capture endpoint."""

    open_until: float
    last_error: str = ""


# Circuit breakers are keyed by endpoint identity (normalized base URL) so a
# transient outage of endpoint A never skips a healthy capture against a
# different explicit client/endpoint B. Each entry opens for a cooldown, then
# allows a single half-open probe that either closes it (success) or re-opens
# it with a fresh cooldown (failure).
_BREAKERS: dict[str, _BreakerState] = {}
_WARNED: set[str] = set()
def _first_capture_review_keys(kind: str) -> frozenset[str]:
    return frozenset({f"{kind}_no_preview", f"{kind}_preview_size_bytes"})


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
    run_id: str = ""
    git_commit: str = ""
    git_dirty: bool = False
    repo_remote_url: str = ""
    code_file: str = ""
    code_symbol: str = ""
    code_line: int = 0
    code_region_hash: str = ""
    extra: dict[str, NoteMetadataScalar] = field(default_factory=dict)

    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def to_metadata(self) -> dict[str, NoteMetadataScalar]:
        metadata: dict[str, NoteMetadataScalar] = {
            "run_captured_at": self.captured_at,
            "run_git_dirty": self.git_dirty,
        }
        if self.run_id:
            metadata["run_id"] = self.run_id
        if self.git_commit:
            metadata["run_git_commit"] = self.git_commit
        if self.repo_remote_url:
            metadata["run_repo_remote_url"] = self.repo_remote_url
        if self.code_file:
            metadata["run_code_file"] = self.code_file
        if self.code_symbol:
            metadata["run_code_symbol"] = self.code_symbol
        if self.code_line:
            metadata["run_code_line"] = self.code_line
        if self.code_region_hash:
            metadata["run_code_region_hash"] = self.code_region_hash
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


class CaptureContext:
    """Context manager returned by capture()/capture_figures().

    Snapshots matching files under ``root`` on enter and captures any that are
    created or modified inside the body. Format-agnostic: ``kind`` only labels
    the evidence (provider/adapter/metadata namespace); bytes are never
    interpreted, and anything over the upload cap degrades to a pointer note.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        patterns: Iterable[str] = _DEFAULT_IMAGE_PATTERNS,
        kind: str = "figure",
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
        self.kind = kind
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

    def __enter__(self) -> CaptureContext:
        self._snapshot = _snapshot_file_mtimes(self.root, self.patterns, recursive=self.recursive)
        return self

    def __exit__(self, *_: object) -> bool:
        try:
            after = _snapshot_file_mtimes(self.root, self.patterns, recursive=self.recursive)
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
                        kind=self.kind,
                    )
                )
        except Exception as exc:  # pragma: no cover - defensive guard for body exceptions.
            self.errors.append(str(exc))
            _warn_once(
                "capture-figures-scan",
                f"Lab Tracker figure capture could not scan saved figures: {exc}",
            )
        return False


# Back-compat alias: figure capture is now one kind of the generic CaptureContext.
FigureCaptureContext = CaptureContext


def run_context(
    *,
    ttl_seconds: float = 1800,
    extra: Mapping[str, NoteMetadataScalar] | None = None,
) -> _RunContextManager:
    """Return a context manager that adds run metadata to figure captures."""

    resolved_extra = dict(_validate_metadata(extra) or {})
    pointer = _run_code_pointer()
    context = RunContext(
        captured_at=datetime.now(timezone.utc).isoformat(),
        expires_at=time.monotonic() + max(0.0, float(ttl_seconds)),
        run_id=uuid.uuid4().hex,
        git_commit=_git_output("rev-parse", "HEAD"),
        git_dirty=bool(_git_output("status", "--porcelain")),
        repo_remote_url=_credential_free_repo_remote(
            _git_output("config", "--get", "remote.origin.url")
        ),
        code_file=str(pointer.get("code_file", "")),
        code_symbol=str(pointer.get("code_symbol", "")),
        code_line=int(pointer.get("code_line", 0) or 0),
        code_region_hash=str(pointer.get("code_region_hash", "")),
        extra=resolved_extra,
    )
    return _RunContextManager(context)


def _run_code_pointer() -> dict[str, NoteMetadataScalar]:
    """Best-effort repo-relative code pointer for the run's call site.

    Walks past frames inside this module so it lands on the user's code that
    opened the run context. Fail-soft: returns whatever it could resolve.
    """

    pointer: dict[str, NoteMetadataScalar] = {}
    frame: Any = None
    with suppress(Exception):
        frame = sys._getframe(1)
        while frame is not None and frame.f_code.co_filename == __file__:
            frame = frame.f_back
    if frame is None:
        return pointer
    with suppress(Exception):
        filename = frame.f_code.co_filename
        lineno = int(frame.f_lineno)
        symbol = getattr(frame.f_code, "co_qualname", None) or frame.f_code.co_name
        relative = _repo_relative_file(filename)
        if relative:
            pointer["code_file"] = relative
        if symbol:
            pointer["code_symbol"] = str(symbol)
        if lineno:
            pointer["code_line"] = lineno
        region = _code_region_hash(filename, lineno)
        if region:
            pointer["code_region_hash"] = region
    return pointer


def _repo_relative_file(filename: str) -> str:
    with suppress(Exception):
        path = Path(filename).resolve()
        toplevel = _git_output("rev-parse", "--show-toplevel")
        if toplevel:
            with suppress(ValueError):
                return path.relative_to(Path(toplevel).resolve()).as_posix()
        return path.name
    return ""


def _code_region_hash(filename: str, lineno: int, *, context_lines: int = 3) -> str:
    with suppress(Exception):
        lines = Path(filename).read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return ""
        start = max(0, lineno - 1 - context_lines)
        end = min(len(lines), lineno + context_lines)
        region = "\n".join(lines[start:end])
        if region:
            return hashlib.sha256(region.encode("utf-8")).hexdigest()
    return ""


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

    return CaptureContext(
        root,
        patterns=patterns,
        kind="figure",
        recursive=recursive,
        client=client,
        project_id=project_id,
        metadata=metadata,
        logical_prefix=logical_prefix,
        preview_max_bytes=preview_max_bytes,
        version_every_change=version_every_change,
    )


def capture(
    root: str | Path,
    *,
    patterns: Iterable[str],
    kind: str,
    recursive: bool = True,
    client: LabTracker | None = None,
    project_id: str | None = None,
    metadata: Mapping[str, NoteMetadataScalar] | None = None,
    logical_prefix: str | None = None,
    preview_max_bytes: int = FIGURE_PREVIEW_MAX_BYTES,
    version_every_change: bool = False,
) -> CaptureContext:
    """Capture any files matching ``patterns`` written inside the context body.

    Format-agnostic staged-evidence capture (csv, html, pickle, ...). ``patterns``
    and ``kind`` are required: ``patterns`` keeps the watch bounded (no implicit
    whole-tree walk) and ``kind`` labels the evidence. Files over the upload cap
    become pointer notes; bytes are never interpreted. For directory stores
    (zarr) or large data, use ``lt watch`` instead.
    """

    if not tuple(patterns):
        raise LTValidationError("capture() requires at least one file pattern.")
    if not str(kind).strip():
        raise LTValidationError("capture() requires a non-empty kind.")
    return CaptureContext(
        root,
        patterns=patterns,
        kind=str(kind).strip(),
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
    kind: str = "figure",
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
    endpoint_key = _capture_endpoint_key(client)
    try:
        file_size = resolved_path.stat().st_size
        if file_size <= 0:
            raise LTValidationError(f"Captured {kind} file must not be empty.")
        if file_size > FIGURE_UPLOAD_MAX_BYTES:
            # Never read an oversized artifact into memory: stream-hash it and
            # let _preview_payload emit a pointer (or a fig-rendered preview).
            full_payload: bytes | None = None
            content_hash = file_sha256(resolved_path)
        else:
            full_payload = resolved_path.read_bytes()
            if not full_payload:
                raise LTValidationError(f"Captured {kind} file must not be empty.")
            content_hash = _bytes_sha256(full_payload)
        resolved_path = resolved_path.resolve()
        source_uri = resolved_path.as_uri()
        client_capture_id = _client_capture_id(
            resolved_path,
            logical_id=logical_id,
            content_hash=content_hash if version_every_change else None,
            kind=kind,
        )
        base_metadata = _base_figure_metadata(
            path=resolved_path,
            source_uri=source_uri,
            source_external_id=client_capture_id,
            content_hash=content_hash,
            payload_size=file_size,
            metadata=metadata,
            kind=kind,
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
        if _breaker_blocks(endpoint_key):
            remaining = _figure_circuit_state().get(endpoint_key or "", 0.0)
            _warn_once(
                f"circuit-open:{endpoint_key}",
                f"Lab Tracker figure capture is paused for {endpoint_key} after a "
                f"connection or timeout failure (retrying in ~{remaining:.0f}s).",
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
            # Clamp the capture request without mutating the shared client's
            # timeout: pass a per-request timeout so concurrent captures cannot
            # race on client._client.timeout.
            capture_timeout = _clamped_timeout(
                getattr(getattr(resolved_client, "_client", None), "timeout", None)
            )
            preview = _preview_payload(
                fig=fig,
                path=resolved_path,
                full_payload=full_payload,
                full_size_bytes=file_size,
                max_bytes=preview_max_bytes,
                source_uri=source_uri,
                content_hash=content_hash,
            )
            upload_metadata = dict(base_metadata)
            upload_metadata.update(
                {
                    f"{kind}_no_preview": preview.no_preview,
                    f"{kind}_preview_size_bytes": preview.size_bytes,
                    f"{kind}_review_bytes_stale": False,
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
                timeout=capture_timeout,
            )
            # The endpoint answered: close any open breaker for it.
            _close_circuit(endpoint_key)
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
                    kind=kind,
                    timeout=capture_timeout,
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
            _trip_circuit(endpoint_key, str(exc))
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
    kind: str = "figure",
    timeout: Any = None,
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
        kind=kind,
    )
    try:
        note = client._patch_note_metadata(
            str(note.id), merged_metadata, status="staged", timeout=timeout
        )
    except Exception as exc:
        if _is_transport_failure(exc):
            _trip_circuit(_capture_endpoint_key(client), str(exc))
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
    kind: str = "figure",
) -> dict[str, NoteMetadataScalar]:
    merged = dict(_validate_metadata(metadata) or {})
    merged.update(capture_host_metadata())
    context = _active_run_context()
    if context is not None:
        merged.update(context.to_metadata())
    evidence = build_evidence_metadata(
        source_provider=f"local-{kind}",
        source_uri=source_uri,
        source_external_id=source_external_id,
        content_hash=content_hash,
        capture_kind=kind,
        adapter=f"lab-tracker-client-{kind}",
        title=path.name,
        metadata=merged,
    )
    evidence.update(
        {
            "content_hash_current": content_hash,
            f"{kind}_content_hash_current": content_hash,
            f"{kind}_source_uri_current": source_uri,
            f"{kind}_source_external_id_current": source_external_id,
            f"{kind}_client_capture_id": source_external_id,
            f"{kind}_full_size_bytes": payload_size,
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
    kind: str = "figure",
) -> dict[str, NoteMetadataScalar]:
    first_capture_keys = _first_capture_review_keys(kind)
    merged: dict[str, NoteMetadataScalar] = {
        str(key): value
        for key, value in existing_metadata.items()
        if isinstance(value, (str, bool, int, float))
    }
    for key, value in current_metadata.items():
        if key.startswith("evidence_") and key in merged:
            continue
        if key in first_capture_keys and key in merged:
            continue
        merged[key] = value
    merged.update(
        {
            "content_hash_current": content_hash,
            f"{kind}_content_hash_current": content_hash,
            f"{kind}_source_uri_current": source_uri,
            f"{kind}_source_external_id_current": source_external_id,
            f"{kind}_review_bytes_stale": stale_review_bytes,
        }
    )
    return merged


def _preview_payload(
    *,
    fig: Any,
    path: Path,
    full_payload: bytes | None,
    full_size_bytes: int,
    max_bytes: int,
    source_uri: str,
    content_hash: str,
) -> _PreviewPayload:
    resolved_max_bytes = max(1, min(int(max_bytes), FIGURE_UPLOAD_MAX_BYTES))
    if full_payload is not None and len(full_payload) <= resolved_max_bytes:
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
        "the file exceeds the upload cap or matplotlib/Pillow is unavailable. "
        "Uploading a pointer note.",
    )
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


def _render_downscaled_png(fig: Any, *, max_bytes: int) -> bytes | None:
    if fig is None:
        return None
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from PIL import Image
    except ImportError:
        return None
    try:
        FigureCanvasAgg(fig).draw()
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=144, bbox_inches="tight")
        payload = buffer.getvalue()
        if len(payload) <= max_bytes:
            return payload
        image = Image.open(io.BytesIO(payload))
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
    profile = load_connection_profile()
    resolved_project_id = (
        project_id
        or os.getenv("LAB_TRACKER_PROJECT_ID")
        or profile.get("default_project_id")
    )
    configured_base_url = (
        os.getenv("LAB_TRACKER_BASE_URL")
        or os.getenv("LAB_TRACKER_MCP_BASE_URL")
        or profile.get("base_url")
    )
    if not resolved_project_id or not configured_base_url:
        return None, None, False
    resolved_client = LabTracker.from_env(
        timeout_seconds=FIGURE_CAPTURE_TIMEOUT_SECONDS,
    )
    resolved_client.default_project_id = str(resolved_project_id)
    has_credentials = bool(
        resolved_client.access_token
        or (resolved_client.username and resolved_client.password)
    )
    if not has_credentials:
        resolved_client.close()
        return None, None, False
    return resolved_client, str(resolved_project_id), True


def _client_capture_id(
    path: Path,
    *,
    logical_id: str | None,
    content_hash: str | None,
    kind: str = "figure",
) -> str:
    if logical_id is None:
        logical_id = _project_relative_path(path)
    cleaned = "/".join(part.strip() for part in str(logical_id).replace("\\", "/").split("/"))
    cleaned = cleaned.strip("/")
    if not cleaned:
        cleaned = path.name
    base = f"{kind}:{cleaned}"
    if content_hash:
        base = f"{base}:{content_hash[:12]}"
    if len(base) <= 120:
        return base
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


_SNAPSHOT_SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache"}
)


def _snapshot_file_mtimes(
    root: Path,
    patterns: Iterable[str],
    *,
    recursive: bool,
) -> dict[Path, int]:
    snapshot: dict[Path, int] = {}
    for pattern in patterns:
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        for path in iterator:
            if _SNAPSHOT_SKIP_DIRS.intersection(path.parts):
                continue
            try:
                stat_result = path.stat()
                # Regular files only: skip dirs, FIFOs, sockets, devices — a
                # read on a FIFO would block the capture forever.
                if stat.S_ISREG(stat_result.st_mode):
                    snapshot[path.resolve()] = stat_result.st_mtime_ns
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


def _credential_free_repo_remote(remote: str) -> str:
    """Return a stable remote identity without URL-carried credentials."""

    without_query = remote.partition("?")[0]
    without_fragment = without_query.partition("#")[0]
    return normalize_remote(without_fragment)


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


def _capture_endpoint_key(client: LabTracker | None) -> str | None:
    """Identify the endpoint a capture would target, before building a client.

    Uses the explicit client's base URL when one is passed, else the env base
    URL the auto-client would use. Returning it lets the breaker short-circuit a
    known-bad endpoint without constructing an env client.
    """
    if client is not None:
        base_url = getattr(client, "base_url", None)
    else:
        profile = load_connection_profile()
        base_url = (
            os.getenv("LAB_TRACKER_BASE_URL")
            or os.getenv("LAB_TRACKER_MCP_BASE_URL")
            or profile.get("base_url")
        )
    if not base_url:
        return None
    with suppress(ValueError):
        return normalize_instance_base_url(str(base_url))
    return None


def _breaker_blocks(key: str | None) -> bool:
    """Return True if the endpoint's breaker is open and not yet ready to probe."""
    if key is None:
        return False
    entry = _BREAKERS.get(key)
    if entry is None:
        return False
    # Cooldown elapsed -> allow a single half-open probe (the caller closes the
    # breaker on success or re-trips it on failure).
    return time.monotonic() < entry.open_until


def _trip_circuit(key: str | None, error: str = "") -> None:
    if key is None:
        return
    _BREAKERS[key] = _BreakerState(
        open_until=time.monotonic() + FIGURE_CIRCUIT_COOLDOWN_SECONDS,
        last_error=error,
    )


def _close_circuit(key: str | None) -> None:
    if key is not None:
        _BREAKERS.pop(key, None)


def _figure_circuit_state() -> dict[str, float]:
    """Inspectable breaker state: endpoint -> seconds until the half-open probe."""
    now = time.monotonic()
    return {key: max(0.0, state.open_until - now) for key, state in _BREAKERS.items()}


def _warn_once(key: str, message: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(message, file=sys.stderr)


def _reset_figure_capture_state_for_tests() -> None:
    _BREAKERS.clear()
    _WARNED.clear()
    _RUN_CONTEXT.set(None)
