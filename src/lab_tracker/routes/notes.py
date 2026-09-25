"""Note routes."""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, FastAPI, File, Form, Query, UploadFile
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext
from lab_tracker.config import get_settings
from lab_tracker.errors import ValidationError
from lab_tracker.graph_drafting import make_graph_draft_client
from lab_tracker.models import (
    EntityType,
    Note,
    NoteMetadataScalar,
    NoteRawAsset,
    NoteStatus,
    UsageEventResourceType,
    utc_now,
)
from lab_tracker.note_text import (
    DEFAULT_NOTE_TEXT_PREVIEW_CHARS,
    MAX_NOTE_TEXT_PREVIEW_CHARS,
)
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    NoteArchiveRequest,
    NoteCreate,
    NoteRawDownloadRead,
    NoteRawTextRead,
    NoteTranscriptRequest,
    NoteUpdate,
)
from lab_tracker.upload_security import (
    enforce_request_content_length_limit,
    validate_upload_content_type,
)

from .graph_draft_clients import draft_client_from_request as _transcription_client_from_request
from .shared import (
    CreatedByFilter,
    actor_from_request,
    api_from_request,
    content_disposition_header,
    created_by_filter_value,
    ensure_project_contributor,
    handlers_from_request,
    list_response,
    note_default_status,
    parse_entity_refs_form,
    parse_metadata_form,
    record_usage_view,
    validate_pagination,
)

_logger = logging.getLogger(__name__)

# Stamped server-side on captures presented with a paired-device token, so a
# review can tell which phone a capture came from. Reserved: a client value is
# rejected rather than trusted.
CAPTURE_DEVICE_TOKEN_ID_KEY = "capture_device_token_id"
CAPTURE_DEVICE_LABEL_KEY = "capture_device_label"
_DEVICE_IDENTITY_KEYS = (CAPTURE_DEVICE_TOKEN_ID_KEY, CAPTURE_DEVICE_LABEL_KEY)


def build_notes_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/notes",
        response_model=Envelope[Note],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_note(payload: NoteCreate, request: Request, response: Response):
        actor = actor_from_request(request)
        ensure_project_contributor(request, payload.project_id)
        result = api_from_request(request, api).create_note_result(
            project_id=payload.project_id,
            raw_content=payload.raw_content,
            transcribed_text=payload.transcribed_text,
            targets=payload.targets,
            metadata=device_capture_metadata(actor, payload.metadata),
            client_capture_id=payload.client_capture_id,
            status=payload.status or note_default_status(),
            actor=actor,
        )
        if result.reused:
            response.status_code = http_status.HTTP_200_OK
        return Envelope(data=result.entity)

    @router.post(
        "/notes/upload-file",
        response_model=Envelope[Note],
        status_code=http_status.HTTP_201_CREATED,
    )
    def upload_note_file(
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
        file: Annotated[UploadFile, File()],
        project_id: Annotated[UUID, Form()],
        transcribed_text: Annotated[str | None, Form()] = None,
        targets: Annotated[str | None, Form()] = None,
        metadata: Annotated[str | None, Form()] = None,
        client_capture_id: Annotated[str | None, Form()] = None,
        status: Annotated[NoteStatus | None, Form()] = None,
    ):
        actor = actor_from_request(request)
        request_api = api_from_request(request, api)
        _ensure_capture_project_writable(request, request_api, project_id)
        filename = (file.filename or "").strip()
        if not filename:
            raise ValidationError("filename must not be empty.")
        enforce_request_content_length_limit(
            request,
            max_bytes=request.app.state.settings.max_upload_bytes,
        )
        content_type = validate_upload_content_type(file.content_type)
        parsed_targets = parse_entity_refs_form(targets)
        parsed_metadata = device_capture_metadata(actor, parse_metadata_form(metadata))
        asset = request_api.store_note_raw_asset(
            file.file,
            filename=filename,
            content_type=content_type,
        )
        enriched_metadata = source_file_metadata(asset, parsed_metadata)
        result = request_api.upload_note_raw_result(
            project_id=project_id,
            raw_asset=asset,
            owns_raw_asset=True,
            transcribed_text=transcribed_text,
            targets=parsed_targets,
            metadata=enriched_metadata,
            client_capture_id=client_capture_id,
            status=status or note_default_status(),
            actor=actor,
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

    @router.post(
        "/notes/quick-capture",
        response_model=Envelope[Note],
        status_code=http_status.HTTP_201_CREATED,
    )
    def quick_capture_note(
        request: Request,
        response: Response,
        background_tasks: BackgroundTasks,
        file: Annotated[UploadFile, File()],
        project_id: Annotated[UUID, Form()],
        metadata: Annotated[str | None, Form()] = None,
        client_capture_id: Annotated[str | None, Form()] = None,
    ):
        actor = actor_from_request(request)
        request_api = api_from_request(request, api)
        _ensure_capture_project_writable(request, request_api, project_id)
        filename = (file.filename or "").strip()
        if not filename:
            raise ValidationError("filename must not be empty.")
        enforce_request_content_length_limit(
            request,
            max_bytes=request.app.state.settings.max_upload_bytes,
        )
        content_type = validate_upload_content_type(file.content_type)
        parsed_metadata = device_capture_metadata(actor, parse_metadata_form(metadata))
        asset = request_api.store_note_raw_asset(
            file.file,
            filename=filename,
            content_type=content_type,
        )
        enriched_metadata = source_file_metadata(asset, parsed_metadata)
        result = request_api.upload_note_raw_result(
            project_id=project_id,
            raw_asset=asset,
            owns_raw_asset=True,
            metadata=enriched_metadata,
            client_capture_id=client_capture_id,
            status=NoteStatus.STAGED,
            actor=actor,
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

    @router.get("/notes", response_model=ListEnvelope[Note])
    def list_notes(
        request: Request,
        project_id: UUID | None = None,
        status: NoteStatus | None = None,
        created_by: CreatedByFilter = None,
        since: datetime | None = None,
        until: datetime | None = None,
        evidence_content_hash: str | None = None,
        target_entity_type: EntityType | None = None,
        target_entity_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        page = handlers_from_request(request).catalogs.list_notes(
            actor=actor_from_request(request),
            project_id=project_id,
            status=status.value if status is not None else None,
            created_by=created_by_filter_value(created_by),
            since=since,
            until=until,
            evidence_content_hash=evidence_content_hash,
            target_entity_type=target_entity_type.value if target_entity_type is not None else None,
            target_entity_id=target_entity_id,
            limit=limit,
            offset=offset,
        )
        return list_response(
            page.items,
            limit=limit,
            offset=offset,
            total=page.total,
        )

    @router.get("/notes/{note_id:uuid}", response_model=Envelope[Note])
    def get_note(note_id: UUID, request: Request):
        note = api_from_request(request, api).get_note_for_read(
            note_id,
            actor=actor_from_request(request),
        )
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.NOTE,
            resource_id=note.note_id,
            project_id=note.project_id,
        )
        return Envelope(data=note)

    @router.get("/notes/{note_id:uuid}/raw")
    def download_note_raw(note_id: UUID, request: Request):
        api_from_request(request, api).get_note_for_read(
            note_id,
            actor=actor_from_request(request),
        )
        accept = (request.headers.get("accept") or "").lower()
        if "application/json" not in accept:
            raw_asset, chunks = api_from_request(request, api).stream_note_raw(note_id)
            headers = {
                "Content-Disposition": content_disposition_header("attachment", raw_asset.filename),
                "Content-Length": str(raw_asset.size_bytes),
            }
            return StreamingResponse(
                chunks,
                media_type=raw_asset.content_type,
                headers=headers,
            )
        # The JSON envelope embeds the whole asset as base64, so it is
        # inherently whole-payload; binary clients get the stream above.
        raw_asset, content = api_from_request(request, api).download_note_raw(note_id)
        encoded = base64.b64encode(content).decode("ascii")
        payload = NoteRawDownloadRead(
            storage_id=raw_asset.storage_id,
            filename=raw_asset.filename,
            content_type=raw_asset.content_type,
            size_bytes=raw_asset.size_bytes,
            checksum=raw_asset.checksum,
            content_base64=encoded,
        )
        return Envelope(data=payload)

    @router.get(
        "/notes/{note_id:uuid}/raw-text",
        response_model=Envelope[NoteRawTextRead],
    )
    def read_note_raw_text(
        note_id: UUID,
        request: Request,
        max_chars: Annotated[
            int,
            Query(ge=1, le=MAX_NOTE_TEXT_PREVIEW_CHARS),
        ] = DEFAULT_NOTE_TEXT_PREVIEW_CHARS,
    ):
        request_api = api_from_request(request, api)
        request_api.get_note_for_read(
            note_id,
            actor=actor_from_request(request),
        )
        raw_asset, excerpt = request_api.read_note_raw_text(
            note_id,
            max_chars=max_chars,
        )
        return Envelope(
            data=NoteRawTextRead(
                storage_id=raw_asset.storage_id,
                filename=raw_asset.filename,
                content_type=raw_asset.content_type,
                size_bytes=raw_asset.size_bytes,
                checksum=raw_asset.checksum,
                text=excerpt.text,
                truncated=excerpt.truncated,
                included_bytes=excerpt.included_bytes,
                omitted_bytes=excerpt.omitted_bytes,
            )
        )

    @router.patch("/notes/{note_id:uuid}", response_model=Envelope[Note])
    def update_note(note_id: UUID, payload: NoteUpdate, request: Request):
        actor = actor_from_request(request)
        note = api_from_request(request, api).get_note(note_id)
        ensure_project_contributor(request, note.project_id)
        note = api_from_request(request, api).update_note(
            note_id,
            actor=actor,
            **provided_fields(payload),
        )
        return Envelope(data=note)

    @router.post("/notes/{note_id:uuid}/transcript", response_model=Envelope[Note])
    def transcribe_note(
        note_id: UUID,
        request: Request,
        payload: NoteTranscriptRequest | None = None,
    ):
        actor = actor_from_request(request)
        note = api_from_request(request, api).get_note(note_id)
        ensure_project_contributor(request, note.project_id)
        transcription_client = _transcription_client_from_request(request)
        try:
            note = api_from_request(request, api).transcribe_voice_note(
                note_id,
                transcription_client=transcription_client,
                prompt=payload.prompt if payload else None,
                actor=actor,
            )
        finally:
            close = getattr(transcription_client, "close", None)
            if callable(close):
                close()
        return Envelope(data=note)

    @router.post("/notes/{note_id:uuid}/archive", response_model=Envelope[Note])
    def archive_note(
        note_id: UUID,
        request: Request,
        payload: NoteArchiveRequest | None = None,
    ):
        actor = actor_from_request(request)
        note = api_from_request(request, api).get_note(note_id)
        ensure_project_contributor(request, note.project_id)
        archive_payload = payload or NoteArchiveRequest()
        note = api_from_request(request, api).archive_note(
            note_id,
            reason=archive_payload.reason,
            actor=actor,
        )
        return Envelope(data=note)

    @router.delete("/notes/{note_id:uuid}", response_model=Envelope[Note])
    def delete_note(note_id: UUID, request: Request):
        actor = actor_from_request(request)
        note = api_from_request(request, api).get_note(note_id)
        ensure_project_contributor(request, note.project_id)
        note = api_from_request(request, api).delete_note(note_id, actor=actor)
        return Envelope(data=note)

    return router


def source_file_metadata(
    asset: NoteRawAsset,
    client_metadata: dict[str, NoteMetadataScalar] | None = None,
) -> dict[str, NoteMetadataScalar]:
    """Merge trusted stored-file metadata with optional client file timestamps."""

    metadata: dict[str, NoteMetadataScalar] = dict(client_metadata or {})
    source_metadata: dict[str, NoteMetadataScalar] = {
        "source_file_name": asset.filename,
        "source_file_content_type": asset.content_type,
        "source_file_size_bytes": asset.size_bytes,
        "source_file_checksum": asset.checksum,
        "source_file_ingested_at": utc_now().isoformat(),
    }

    created_at = _optional_iso_datetime(metadata.get("source_file_created_at"))
    if created_at is not None:
        source_metadata["source_file_created_at"] = created_at

    last_modified_at = _optional_iso_datetime(metadata.get("source_file_last_modified_at"))
    last_modified_ms = _optional_epoch_ms(metadata.get("source_file_last_modified_ms"))
    if last_modified_ms is not None:
        source_metadata["source_file_last_modified_ms"] = last_modified_ms
        if last_modified_at is None:
            last_modified_at = datetime.fromtimestamp(
                int(last_modified_ms) / 1000,
                timezone.utc,
            ).isoformat()
    if last_modified_at is not None:
        source_metadata["source_file_last_modified_at"] = last_modified_at

    metadata.update(source_metadata)
    return metadata


def device_capture_metadata(
    actor: AuthContext,
    client_metadata: dict[str, NoteMetadataScalar] | None,
) -> dict[str, NoteMetadataScalar] | None:
    """Stamp the presenting device's identity onto a capture's metadata.

    The keys are server-owned: any client that supplies them is rejected,
    whatever principal it presents. Non-device principals get their metadata
    back unchanged (``None`` stays ``None``).
    """

    if client_metadata and any(key in client_metadata for key in _DEVICE_IDENTITY_KEYS):
        raise ValidationError("capture_device_* metadata keys are stamped by the server.")
    if not actor.is_device:
        return client_metadata
    if actor.device_token_id is None:
        raise ValueError("A device principal must carry its device_token_id.")
    metadata: dict[str, NoteMetadataScalar] = dict(client_metadata or {})
    metadata[CAPTURE_DEVICE_TOKEN_ID_KEY] = str(actor.device_token_id)
    if actor.principal_label:
        metadata[CAPTURE_DEVICE_LABEL_KEY] = actor.principal_label
    return metadata


def _optional_iso_datetime(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError("source file timestamps must be ISO 8601 datetimes.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _optional_epoch_ms(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        milliseconds = int(float(text))
    except ValueError as exc:
        raise ValidationError("source_file_last_modified_ms must be epoch milliseconds.") from exc
    if milliseconds < 0:
        raise ValidationError("source_file_last_modified_ms must be non-negative.")
    return str(milliseconds)


def _ensure_capture_project_writable(
    request: Request,
    request_api: LabTrackerAPI,
    project_id: UUID,
) -> None:
    """Authorize and resolve the capture's project before any raw bytes are stored.

    The note service re-checks both inside its transaction; this pre-check keeps
    denied or orphaned uploads from writing (and then deleting) raw assets.
    """
    ensure_project_contributor(request, project_id)
    request_api.get_project(project_id)


def _maybe_schedule_auto_transcription(
    background_tasks: BackgroundTasks,
    app: FastAPI,
    *,
    note: Note,
    actor: AuthContext,
) -> None:
    """Queue opt-in, best-effort transcription for a new audio upload."""

    settings = getattr(app.state, "settings", None) or get_settings()
    if not settings.auto_transcribe_voice_captures:
        return
    raw_asset = note.raw_asset
    if raw_asset is None or not raw_asset.content_type.lower().startswith("audio/"):
        return
    if note.transcribed_text:
        return
    background_tasks.add_task(
        _auto_transcribe_uploaded_note,
        app,
        note_id=note.note_id,
        actor=actor,
    )


def _auto_transcribe_uploaded_note(
    app: FastAPI,
    *,
    note_id: UUID,
    actor: AuthContext,
) -> None:
    """Transcribe after upload commit without making capture depend on ASR."""

    transcription_client = None
    try:
        settings = getattr(app.state, "settings", None) or get_settings()
        factory = getattr(app.state, "graph_draft_client_factory", None)
        transcription_client = (
            factory(settings) if callable(factory) else make_graph_draft_client(settings)
        )
        with app.state.db_session_factory() as session:
            background_api = app.state.session_api_factory(
                session,
                surface="http",
            )
            background_api.auto_transcribe_voice_note(
                note_id,
                transcription_client=transcription_client,
                actor=actor,
            )
    except Exception:
        _logger.exception(
            "Automatic voice transcription failed for note %s; transcript stays pending.",
            note_id,
        )
    finally:
        close = getattr(transcription_client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                _logger.exception(
                    "Closing the transcription client for note %s failed.",
                    note_id,
                )
