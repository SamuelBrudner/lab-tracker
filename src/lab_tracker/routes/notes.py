"""Note routes."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, UploadFile
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import Response

from lab_tracker.api import LabTrackerAPI
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
from lab_tracker.patching import provided_fields
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    NoteArchiveRequest,
    NoteCreate,
    NoteRawDownloadRead,
    NoteTranscriptRequest,
    NoteUpdate,
)
from lab_tracker.upload_security import (
    enforce_request_content_length_limit,
    validate_upload_content_type,
)

from .shared import (
    CreatedByFilter,
    accessible_project_ids_from_request,
    actor_from_request,
    api_from_request,
    content_disposition_header,
    ensure_project_contributor,
    ensure_project_read,
    list_response,
    note_default_status,
    parse_entity_refs_form,
    parse_metadata_form,
    record_usage_view,
    repository_from_request,
    validate_pagination,
)


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
            metadata=payload.metadata,
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
        file: Annotated[UploadFile, File()],
        project_id: Annotated[UUID, Form()],
        transcribed_text: Annotated[str | None, Form()] = None,
        targets: Annotated[str | None, Form()] = None,
        metadata: Annotated[str | None, Form()] = None,
        client_capture_id: Annotated[str | None, Form()] = None,
        status: Annotated[NoteStatus | None, Form()] = None,
    ):
        actor = actor_from_request(request)
        ensure_project_contributor(request, project_id)
        request_api = api_from_request(request, api)
        filename = (file.filename or "").strip()
        if not filename:
            raise ValidationError("filename must not be empty.")
        enforce_request_content_length_limit(
            request,
            max_bytes=request.app.state.settings.max_upload_bytes,
        )
        content_type = validate_upload_content_type(file.content_type)
        parsed_targets = parse_entity_refs_form(targets)
        parsed_metadata = parse_metadata_form(metadata)
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
        return Envelope(data=result.entity)

    @router.post(
        "/notes/quick-capture",
        response_model=Envelope[Note],
        status_code=http_status.HTTP_201_CREATED,
    )
    def quick_capture_note(
        request: Request,
        response: Response,
        file: Annotated[UploadFile, File()],
        project_id: Annotated[UUID, Form()],
        metadata: Annotated[str | None, Form()] = None,
        client_capture_id: Annotated[str | None, Form()] = None,
    ):
        actor = actor_from_request(request)
        request_api = api_from_request(request, api)
        filename = (file.filename or "").strip()
        if not filename:
            raise ValidationError("filename must not be empty.")
        enforce_request_content_length_limit(
            request,
            max_bytes=request.app.state.settings.max_upload_bytes,
        )
        content_type = validate_upload_content_type(file.content_type)
        parsed_metadata = parse_metadata_form(metadata)
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
        return Envelope(data=result.entity)

    @router.get("/notes", response_model=ListEnvelope[Note])
    def list_notes(
        request: Request,
        project_id: UUID | None = None,
        status: NoteStatus | None = None,
        created_by: CreatedByFilter = None,
        since: datetime | None = None,
        until: datetime | None = None,
        target_entity_type: EntityType | None = None,
        target_entity_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
            project_ids = None
        else:
            project_ids = accessible_project_ids_from_request(request)
        notes, total = repository_from_request(request).query_notes(
            project_id=project_id,
            project_ids=project_ids,
            status=status.value if status is not None else None,
            created_by=created_by,
            since=since,
            until=until,
            target_entity_type=target_entity_type.value if target_entity_type is not None else None,
            target_entity_id=target_entity_id,
            limit=limit,
            offset=offset,
        )
        return list_response(notes, limit=limit, offset=offset, total=total)

    @router.get("/notes/{note_id:uuid}", response_model=Envelope[Note])
    def get_note(note_id: UUID, request: Request):
        note = api_from_request(request, api).get_note(note_id)
        ensure_project_read(request, note.project_id)
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.NOTE,
            resource_id=note.note_id,
            project_id=note.project_id,
        )
        return Envelope(data=note)

    @router.get("/notes/{note_id:uuid}/raw")
    def download_note_raw(note_id: UUID, request: Request):
        note = api_from_request(request, api).get_note(note_id)
        ensure_project_read(request, note.project_id)
        raw_asset, content = api_from_request(request, api).download_note_raw(note_id)
        accept = (request.headers.get("accept") or "").lower()
        if "application/json" not in accept:
            headers = {
                "Content-Disposition": content_disposition_header("attachment", raw_asset.filename),
                "Content-Length": str(raw_asset.size_bytes),
            }
            return Response(content=content, media_type=raw_asset.content_type, headers=headers)
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


def _transcription_client_from_request(request: Request):
    settings = getattr(request.app.state, "settings", None) or get_settings()
    factory = getattr(request.app.state, "graph_draft_client_factory", None)
    if callable(factory):
        return factory(settings)
    return make_graph_draft_client(settings)
