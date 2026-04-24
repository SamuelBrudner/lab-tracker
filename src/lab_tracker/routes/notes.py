"""Note routes."""

from __future__ import annotations

import base64
from uuid import UUID

from fastapi import APIRouter, File, Form, UploadFile
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import Response

from lab_tracker.api import LabTrackerAPI
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    EntityType,
    Note,
    NoteStatus,
)
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    NoteCreate,
    NoteRawDownloadRead,
    NoteUpdate,
)

from .shared import (
    api_from_request,
    actor_from_request,
    list_response,
    note_default_status,
    parse_entity_refs_form,
    parse_metadata_form,
    repository_from_request,
    safe_attachment_filename,
    validate_pagination,
)


def build_notes_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/notes",
        response_model=Envelope[Note],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_note(payload: NoteCreate, request: Request):
        actor = actor_from_request(request)
        note = api_from_request(request, api).create_note(
            project_id=payload.project_id,
            raw_content=payload.raw_content,
            transcribed_text=payload.transcribed_text,
            targets=payload.targets,
            metadata=payload.metadata,
            status=payload.status or note_default_status(),
            actor=actor,
        )
        return Envelope(data=note)

    @router.post(
        "/notes/upload-file",
        response_model=Envelope[Note],
        status_code=http_status.HTTP_201_CREATED,
    )
    async def upload_note_file(
        request: Request,
        file: UploadFile = File(...),
        project_id: UUID = Form(...),
        transcribed_text: str | None = Form(None),
        targets: str | None = Form(None),
        metadata: str | None = Form(None),
        status: NoteStatus | None = Form(None),
    ):
        actor = actor_from_request(request)
        request_api = api_from_request(request, api)
        filename = (file.filename or "").strip()
        if not filename:
            raise ValidationError("filename must not be empty.")
        content_type = (file.content_type or "application/octet-stream").strip()
        parsed_targets = parse_entity_refs_form(targets)
        parsed_metadata = parse_metadata_form(metadata)
        asset = request_api.store_note_raw_asset(
            file.file,
            filename=filename,
            content_type=content_type,
        )
        note = request_api.upload_note_raw(
            project_id=project_id,
            raw_asset=asset,
            owns_raw_asset=True,
            transcribed_text=transcribed_text,
            targets=parsed_targets,
            metadata=parsed_metadata,
            status=status or note_default_status(),
            actor=actor,
        )
        return Envelope(data=note)

    @router.get("/notes", response_model=ListEnvelope[Note])
    def list_notes(
        request: Request,
        project_id: UUID | None = None,
        status: NoteStatus | None = None,
        target_entity_type: EntityType | None = None,
        target_entity_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        notes, total = repository_from_request(request).query_notes(
            project_id=project_id,
            status=status.value if status is not None else None,
            target_entity_type=target_entity_type.value if target_entity_type is not None else None,
            target_entity_id=target_entity_id,
            limit=limit,
            offset=offset,
        )
        return list_response(notes, limit=limit, offset=offset, total=total)

    @router.get("/notes/{note_id:uuid}", response_model=Envelope[Note])
    def get_note(note_id: UUID, request: Request):
        note = api_from_request(request, api).get_note(note_id)
        return Envelope(data=note)

    @router.get("/notes/{note_id:uuid}/raw")
    def download_note_raw(note_id: UUID, request: Request):
        raw_asset, content = api_from_request(request, api).download_note_raw(note_id)
        accept = (request.headers.get("accept") or "").lower()
        if "application/json" not in accept:
            headers = {
                "Content-Disposition": (
                    f'attachment; filename="{safe_attachment_filename(raw_asset.filename)}"'
                ),
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
        note = api_from_request(request, api).update_note(
            note_id,
            transcribed_text=payload.transcribed_text,
            targets=payload.targets,
            metadata=payload.metadata,
            status=payload.status,
            actor=actor,
        )
        return Envelope(data=note)

    @router.delete("/notes/{note_id:uuid}", response_model=Envelope[Note])
    def delete_note(note_id: UUID, request: Request):
        actor = actor_from_request(request)
        note = api_from_request(request, api).delete_note(note_id, actor=actor)
        return Envelope(data=note)

    return router
