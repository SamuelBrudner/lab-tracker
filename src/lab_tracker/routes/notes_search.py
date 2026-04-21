"""Note and search routes."""

from __future__ import annotations

import base64
import binascii
from uuid import UUID

from fastapi import APIRouter, File, Form, UploadFile
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import Response

from lab_tracker.api import LabTrackerAPI
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    EntityTagSuggestion,
    EntityType,
    Note,
    NoteStatus,
    QuestionExtractionCandidate,
    TagSuggestionStatus,
)
from lab_tracker.schemas import (
    Envelope,
    ListEnvelope,
    NoteCreate,
    NoteRawDownloadRead,
    NoteUpload,
    NoteUpdate,
    QuestionExtractionRequest,
    SearchResults,
    TagSuggestionRequest,
    TagSuggestionReviewRequest,
)

from .shared import (
    actor_from_request,
    list_response,
    note_default_status,
    paginate,
    parse_entity_refs_form,
    parse_metadata_form,
    repository_from_request,
    safe_attachment_filename,
    validate_pagination,
)


def build_notes_search_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.get("/search", response_model=Envelope[SearchResults])
    def search(
        q: str,
        project_id: UUID | None = None,
        include: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        include_set = {
            item.strip().casefold()
            for item in (include.split(",") if include else ["questions", "notes"])
            if item.strip()
        }
        questions = (
            api.search_questions(q, project_id=project_id, limit=limit, offset=offset)
            if not include_set or "questions" in include_set
            else []
        )
        notes = (
            api.search_notes(q, project_id=project_id, limit=limit, offset=offset)
            if not include_set or "notes" in include_set
            else []
        )
        return Envelope(
            data=SearchResults(questions=questions, notes=notes),
            meta={"questions_count": len(questions), "notes_count": len(notes)},
        )

    @router.post(
        "/notes",
        response_model=Envelope[Note],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_note(payload: NoteCreate, request: Request):
        actor = actor_from_request(request)
        note = api.create_note(
            project_id=payload.project_id,
            raw_content=payload.raw_content,
            transcribed_text=payload.transcribed_text,
            extracted_entities=payload.extracted_entities,
            targets=payload.targets,
            metadata=payload.metadata,
            status=payload.status or note_default_status(),
            actor=actor,
        )
        return Envelope(data=note)

    @router.post(
        "/notes/upload",
        response_model=Envelope[Note],
        status_code=http_status.HTTP_201_CREATED,
        deprecated=True,
    )
    def upload_note(payload: NoteUpload, request: Request):
        actor = actor_from_request(request)
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except binascii.Error as exc:
            raise ValidationError("content_base64 must be valid base64.") from exc
        note = api.upload_note_raw(
            project_id=payload.project_id,
            content=content,
            filename=payload.filename,
            content_type=payload.content_type,
            transcribed_text=payload.transcribed_text,
            extracted_entities=payload.extracted_entities,
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
        filename = (file.filename or "").strip()
        if not filename:
            raise ValidationError("filename must not be empty.")
        content_type = (file.content_type or "application/octet-stream").strip()
        asset = api.store_note_raw_asset(
            file.file,
            filename=filename,
            content_type=content_type,
        )
        note = api.upload_note_raw(
            project_id=project_id,
            raw_asset=asset,
            owns_raw_asset=True,
            transcribed_text=transcribed_text,
            targets=parse_entity_refs_form(targets),
            metadata=parse_metadata_form(metadata),
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

    @router.get("/notes/{note_id}", response_model=Envelope[Note])
    def get_note(note_id: UUID):
        note = api.get_note(note_id)
        return Envelope(data=note)

    @router.get("/notes/{note_id}/raw")
    def download_note_raw(note_id: UUID, request: Request):
        raw_asset, content = api.download_note_raw(note_id)
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

    @router.patch("/notes/{note_id}", response_model=Envelope[Note])
    def update_note(note_id: UUID, payload: NoteUpdate, request: Request):
        actor = actor_from_request(request)
        note = api.update_note(
            note_id,
            transcribed_text=payload.transcribed_text,
            extracted_entities=payload.extracted_entities,
            targets=payload.targets,
            metadata=payload.metadata,
            status=payload.status,
            actor=actor,
        )
        return Envelope(data=note)

    @router.delete("/notes/{note_id}", response_model=Envelope[Note])
    def delete_note(note_id: UUID, request: Request):
        actor = actor_from_request(request)
        note = api.delete_note(note_id, actor=actor)
        return Envelope(data=note)

    @router.post(
        "/notes/{note_id}/tag-suggestions",
        response_model=Envelope[list[EntityTagSuggestion]],
        status_code=http_status.HTTP_201_CREATED,
    )
    def suggest_tag_suggestions(
        note_id: UUID,
        request: Request,
        payload: TagSuggestionRequest | None = None,
    ):
        actor = actor_from_request(request)
        suggestions = api.suggest_entity_tags(
            note_id,
            provenance=payload.provenance if payload else None,
            actor=actor,
        )
        return Envelope(data=suggestions, meta={"count": len(suggestions)})

    @router.get(
        "/notes/{note_id}/tag-suggestions",
        response_model=ListEnvelope[EntityTagSuggestion],
    )
    def list_tag_suggestions(
        note_id: UUID,
        status: TagSuggestionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        suggestions = api.list_entity_tag_suggestions(note_id, status=status)
        page, total = paginate(suggestions, limit, offset)
        return list_response(page, limit=limit, offset=offset, total=total)

    @router.patch(
        "/notes/{note_id}/tag-suggestions/{suggestion_id}",
        response_model=Envelope[EntityTagSuggestion],
    )
    def review_tag_suggestion(
        note_id: UUID,
        suggestion_id: UUID,
        payload: TagSuggestionReviewRequest,
        request: Request,
    ):
        actor = actor_from_request(request)
        suggestion = api.review_entity_tag_suggestion(
            note_id,
            suggestion_id,
            status=payload.status,
            reviewed_by=payload.reviewed_by,
            actor=actor,
        )
        return Envelope(data=suggestion)

    @router.post(
        "/notes/{note_id}/extract-questions",
        response_model=Envelope[list[QuestionExtractionCandidate]],
    )
    def extract_questions(
        note_id: UUID,
        request: Request,
        payload: QuestionExtractionRequest | None = None,
    ):
        actor = actor_from_request(request)
        candidates = api.extract_question_candidates_from_note(
            note_id,
            default_question_type=payload.question_type if payload else None,
            provenance=payload.provenance if payload else None,
            actor=actor,
        )
        return Envelope(data=candidates, meta={"count": len(candidates)})

    return router
