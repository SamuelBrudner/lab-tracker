"""Raw-body voice capture for hands-free phone shortcuts.

An iOS Shortcut, Tasker task, or HTTP Shortcuts action records audio and POSTs
it as the *whole* request body, with every other field in the query string, so
no multipart form or JSON-in-a-form-field has to be assembled on the phone.
The result is the same staged voice note the phone capture page makes, under
the same auth: a paired-device token, a person's session, or a personal
access token that may stage evidence. Nothing here commits or links anything
the caller did not ask for; ``session_id=latest`` is the caller's own standing
declaration ("my most recently started active session in this project"), and
the resolution is recorded on the note for review.
"""

from __future__ import annotations

import mimetypes
import tempfile
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import IO, Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Query
from starlette import status as http_status
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext
from lab_tracker.errors import PayloadTooLargeError, ValidationError
from lab_tracker.models import (
    EntityOrigin,
    EntityRef,
    EntityType,
    Note,
    NoteMetadataScalar,
    NoteStatus,
)
from lab_tracker.schemas import Envelope
from lab_tracker.upload_security import (
    DEFAULT_CONTENT_TYPE,
    enforce_request_content_length_limit,
    normalize_upload_content_type,
    validate_upload_content_type,
)

from .notes import (
    _ensure_capture_project_writable,
    _maybe_schedule_auto_transcription,
    device_capture_metadata,
    source_file_metadata,
)
from .shared import (
    actor_from_request,
    api_from_request,
    ensure_scope_allows_note_status,
    handlers_from_request,
    origin_stamp,
    stamp_kwargs,
)

VOICE_CAPTURE_PATH = "/notes/voice-capture"
LATEST_SESSION = "latest"
CAPTURE_CHANNEL_SHORTCUT = "shortcut"
# How the note's session target was chosen, so a reviewer can tell a declared
# session from one resolved as "latest".
SESSION_RESOLUTION_EXPLICIT = "explicit"
SESSION_RESOLUTION_LATEST = "latest_active"
SESSION_RESOLUTION_NONE = "none_active"
HINT_MAX_CHARS = 500
FILENAME_MAX_CHARS = 120
# Active sessions scanned to resolve "latest"; a person rarely has more open.
_LATEST_SESSION_SCAN_LIMIT = 200
# Small recordings stay in memory; longer ones spill to a temporary file.
_SPOOL_MEMORY_BYTES = 1024 * 1024
# Recorder apps often send application/octet-stream; the file extension names
# the audio format instead. Only these extensions are trusted for that.
_AUDIO_EXTENSION_TYPES = {
    ".3gp": "audio/3gpp",
    ".aac": "audio/aac",
    ".amr": "audio/amr",
    ".caf": "audio/x-caf",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".weba": "audio/webm",
    ".webm": "audio/webm",
}
_DEFAULT_EXTENSION_FOR_TYPE = {
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/3gpp": ".3gp",
    "audio/amr": ".amr",
    "audio/flac": ".flac",
}


def build_voice_capture_router(api: LabTrackerAPI) -> APIRouter:
    """Routes for raw-body voice capture (hands-free phone shortcuts)."""

    router = APIRouter()

    @router.post(
        VOICE_CAPTURE_PATH,
        response_model=Envelope[Note],
        status_code=http_status.HTTP_201_CREATED,
    )
    async def capture_voice_memo(
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
        project_id: UUID,
        session_id: Annotated[str | None, Query(max_length=64)] = None,
        hint: Annotated[str | None, Query(max_length=HINT_MAX_CHARS)] = None,
        filename: Annotated[str | None, Query(max_length=FILENAME_MAX_CHARS * 2)] = None,
        client_capture_id: Annotated[str | None, Query(max_length=200)] = None,
        captured_at: Annotated[str | None, Query(max_length=64)] = None,
    ) -> Envelope[Note]:
        """Stage a voice memo sent as the raw request body.

        Send the recording as the body with an ``audio/*`` Content-Type (or
        ``application/octet-stream`` plus a ``filename`` with an audio
        extension). ``session_id`` is a session UUID or ``latest``: the
        caller's most recently started active session in the project, or no
        session when none is active. ``captured_at`` is the phone's ISO-8601
        clock at recording time (optional; the note's creation time stands in
        when it is absent). The body is bounded by the server's upload limit
        and is only read after the caller is authorized.
        """

        actor = actor_from_request(request)
        content_type = voice_content_type(request.headers.get("content-type"), filename)
        client_captured_at = normalize_captured_at(captured_at)
        resolved_filename = voice_filename(filename, content_type)
        max_bytes = int(request.app.state.settings.max_upload_bytes)
        enforce_request_content_length_limit(request, max_bytes=max_bytes)
        request_api = api_from_request(request, api)
        await run_in_threadpool(_authorize_voice_capture, request, request_api, project_id, actor)
        target_session_id, resolution = await run_in_threadpool(
            _resolve_session_target, request, actor, project_id, session_id
        )
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MEMORY_BYTES) as spool:
            await _spool_bounded_body(request, spool, max_bytes=max_bytes)
            result = await run_in_threadpool(
                _store_voice_capture,
                request_api,
                spool,
                project_id=project_id,
                actor=actor,
                filename=resolved_filename,
                content_type=content_type,
                target_session_id=target_session_id,
                session_resolution=resolution,
                hint=hint,
                captured_at=client_captured_at,
                client_capture_id=client_capture_id,
            )
        if result.reused:
            response.status_code = http_status.HTTP_200_OK
        else:
            _maybe_schedule_auto_transcription(
                background_tasks,
                request.app,
                note=result.entity,
                actor=actor,
            )
        return Envelope(data=result.entity)

    return router


def voice_content_type(header_value: str | None, filename: str | None) -> str:
    """Return the audio content type for a raw voice body, or refuse the body.

    An ``audio/*`` Content-Type is used as sent. A missing or generic
    ``application/octet-stream`` type is accepted only when ``filename`` has a
    known audio extension, which then names the type.
    """

    normalized = normalize_upload_content_type(header_value)
    if normalized.startswith("audio/"):
        return validate_upload_content_type(normalized)
    if normalized == DEFAULT_CONTENT_TYPE and filename:
        suffix = PurePosixPath(_basename(filename)).suffix.lower()
        guessed = _AUDIO_EXTENSION_TYPES.get(suffix)
        if guessed:
            return guessed
    raise ValidationError(
        "Voice capture takes an audio/* request body; send the recording with an "
        "audio Content-Type, or add a filename ending in an audio extension such as .m4a."
    )


def voice_filename(filename: str | None, content_type: str) -> str:
    """A safe display filename for the stored recording."""

    name = _basename(filename or "").strip()[:FILENAME_MAX_CHARS].strip()
    if name:
        return name
    extension = _DEFAULT_EXTENSION_FOR_TYPE.get(content_type) or (
        mimetypes.guess_extension(content_type) or ".audio"
    )
    return f"voice-memo{extension}"


def _basename(value: str) -> str:
    # Clients may send a full device path; keep only the last component of
    # either path flavour so nothing path-like reaches storage.
    return PureWindowsPath(PurePosixPath(value).name).name


def _authorize_voice_capture(
    request: Request,
    request_api: LabTrackerAPI,
    project_id: UUID,
    actor: AuthContext,
) -> None:
    _ensure_capture_project_writable(request, request_api, project_id)
    ensure_scope_allows_note_status(actor, NoteStatus.STAGED)


def _resolve_session_target(
    request: Request,
    actor: AuthContext,
    project_id: UUID,
    session_ref: str | None,
) -> tuple[UUID | None, str]:
    reference = (session_ref or "").strip()
    if not reference:
        return None, ""
    if reference.lower() == LATEST_SESSION:
        page = handlers_from_request(request).catalogs.list_sessions(
            actor=actor,
            project_id=project_id,
            status="active",
            session_type=None,
            limit=_LATEST_SESSION_SCAN_LIMIT,
            offset=0,
        )
        own = [
            session
            for session in page.items
            if session.created_by_user_id is not None
            and str(session.created_by_user_id) == str(actor.user_id)
        ]
        if not own:
            return None, SESSION_RESOLUTION_NONE
        latest = max(own, key=lambda session: session.started_at)
        return latest.session_id, SESSION_RESOLUTION_LATEST
    try:
        return UUID(reference), SESSION_RESOLUTION_EXPLICIT
    except ValueError as exc:
        raise ValidationError("session_id must be a session UUID or 'latest'.") from exc


async def _spool_bounded_body(request: Request, spool: IO[bytes], *, max_bytes: int) -> None:
    """Copy the request body into ``spool``, refusing it once it passes ``max_bytes``.

    The body is read chunk by chunk, so an oversized upload without a
    Content-Length is cut off at the limit instead of being received whole.
    """

    received = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        received += len(chunk)
        if received > max_bytes:
            raise PayloadTooLargeError(f"Upload exceeds the configured limit of {max_bytes} bytes.")
        if getattr(spool, "_rolled", False):
            # Past the in-memory threshold the write goes to disk.
            await run_in_threadpool(spool.write, chunk)
        else:
            spool.write(chunk)
    if received == 0:
        raise ValidationError("The request body is empty; send the recorded audio as the body.")
    spool.seek(0)


def _store_voice_capture(
    request_api: LabTrackerAPI,
    spool: IO[bytes],
    *,
    project_id: UUID,
    actor: AuthContext,
    filename: str,
    content_type: str,
    target_session_id: UUID | None,
    session_resolution: str,
    hint: str | None,
    captured_at: str,
    client_capture_id: str | None,
) -> Any:
    metadata = device_capture_metadata(
        actor,
        voice_capture_metadata(
            hint=hint,
            session_resolution=session_resolution,
            captured_at=captured_at,
        ),
    )
    targets = (
        [EntityRef(entity_type=EntityType.SESSION, entity_id=target_session_id)]
        if target_session_id is not None
        else []
    )
    asset = request_api.store_note_raw_asset(
        spool,
        filename=filename,
        content_type=content_type,
    )
    stamp = origin_stamp(actor, EntityOrigin.USER)
    return request_api.upload_note_raw_result(
        project_id=project_id,
        raw_asset=asset,
        owns_raw_asset=True,
        targets=targets,
        metadata=source_file_metadata(asset, metadata),
        client_capture_id=client_capture_id,
        status=NoteStatus.STAGED,
        actor=actor,
        **stamp_kwargs(stamp),
    )


def normalize_captured_at(value: str | None) -> str:
    """Return an ISO-8601 capture clock in UTC, ``""`` when absent, or refuse it."""

    text = (value or "").strip()
    if not text:
        return ""
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        # A clock at the edge of the calendar (0001-01-01T00:00:00+14:00)
        # parses but has no UTC equivalent.
        return parsed.astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError) as exc:
        raise ValidationError(
            "captured_at must be an ISO 8601 date and time, e.g. 2026-09-28T10:00:00Z."
        ) from exc


def voice_capture_metadata(
    *,
    hint: str | None,
    session_resolution: str,
    captured_at: str = "",
) -> dict[str, NoteMetadataScalar]:
    """The capture metadata a shortcut voice memo carries.

    It mirrors the phone capture page's voice metadata so the memo shows up
    in the same pending-review list and transcription flow. No server clock
    is stamped as ``captured_at``: that key is the capturing device's clock,
    and a server value would turn an exact replay into a conflict.
    """

    metadata: dict[str, NoteMetadataScalar] = {
        "capture_source": "mobile_capture",
        "capture_mode": "voice",
        "capture_kind": "voice",
        "capture_channel": CAPTURE_CHANNEL_SHORTCUT,
        "capture_review_status": "pending_review",
        "voice_note_type": "Observation",
        "transcript_status": "pending",
    }
    if captured_at:
        metadata["captured_at"] = captured_at
    cleaned_hint = (hint or "").strip()
    if cleaned_hint:
        metadata["capture_hint"] = cleaned_hint
    if session_resolution:
        metadata["capture_session_resolution"] = session_resolution
    return metadata
