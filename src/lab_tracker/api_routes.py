"""HTTP API routes for lab tracker."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette import status as http_status
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, AuthService, TokenService, User
from lab_tracker.errors import (
    AuthError,
    ConflictError,
    LabTrackerError,
    NotFoundError,
    ValidationError,
)
from lab_tracker.models import (
    AnalysisStatus,
    ClaimInput,
    ClaimStatus,
    DatasetCommitManifestInput as DatasetCommitManifestInputModel,
    DatasetFile,
    DatasetStatus,
    EntityRef,
    NoteStatus,
    ProjectStatus,
    QuestionLink,
    QuestionSource,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
    TagSuggestionStatus,
    VisualizationInput,
)
from lab_tracker.schemas import (
    AcquisitionOutputCreate,
    AcquisitionOutputRead,
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthTokenRead,
    AuthUserRead,
    AnalysisCommitRequest,
    AnalysisCommitResult,
    AnalysisCreate,
    AnalysisRead,
    AnalysisUpdate,
    ClaimCommit,
    ClaimCreate,
    ClaimRead,
    ClaimUpdate,
    DatasetCommitManifestInput as DatasetCommitManifestInputSchema,
    DatasetCreate,
    DatasetRead,
    DatasetUpdate,
    EntityRefInput,
    EntityTagSuggestionRead,
    Envelope,
    ErrorEnvelope,
    ErrorInfo,
    ErrorIssue,
    ExtractedEntityInput,
    ListEnvelope,
    NoteCreate,
    NoteRawDownloadRead,
    NoteRead,
    NoteUpload,
    NoteUpdate,
    PaginationMeta,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    QuestionCreate,
    QuestionExtractionRequest,
    QuestionLinkInput,
    QuestionRead,
    QuestionUpdate,
    SessionCreate,
    SessionPromotionRequest,
    SessionRead,
    SessionUpdate,
    TagSuggestionRequest,
    TagSuggestionReviewRequest,
    VisualizationCommit,
    VisualizationCreate,
    VisualizationRead,
    VisualizationUpdate,
)

def register_routes(
    app: FastAPI,
    api: LabTrackerAPI,
    *,
    auth_service: AuthService,
    token_service: TokenService,
) -> None:
    _register_exception_handlers(app)

    @app.post(
        "/auth/register",
        response_model=Envelope[AuthTokenRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def register_auth(payload: AuthRegisterRequest):
        user = auth_service.register_user(
            username=payload.username,
            password=payload.password,
            role=payload.role,
        )
        token = token_service.issue_access_token(user)
        return Envelope(data=_auth_token_read(user, token.token, token.expires_at))

    @app.post("/auth/login", response_model=Envelope[AuthTokenRead])
    def login_auth(payload: AuthLoginRequest):
        user = auth_service.authenticate(payload.username, payload.password)
        token = token_service.issue_access_token(user)
        return Envelope(data=_auth_token_read(user, token.token, token.expires_at))

    @app.get("/auth/me", response_model=Envelope[AuthUserRead])
    def auth_me(request: Request):
        actor = _actor_from_request(request)
        user = auth_service.get_user_by_id(actor.user_id)
        if user is None:
            raise AuthError("Authentication required.")
        return Envelope(data=_auth_user_read(user))

    @app.post(
        "/projects",
        response_model=Envelope[ProjectRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_project(payload: ProjectCreate, request: Request):
        actor = _actor_from_request(request)
        project = api.create_project(
            name=payload.name,
            description=payload.description or "",
            status=payload.status or project_default_status(),
            actor=actor,
            created_by=_resolve_created_by(payload.created_by, actor),
        )
        return Envelope(data=ProjectRead.model_validate(project))

    @app.get("/projects", response_model=ListEnvelope[ProjectRead])
    def list_projects(
        status: ProjectStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _validate_pagination(limit, offset)
        projects = api.list_projects()
        if status is not None:
            projects = [project for project in projects if project.status == status]
        page, total = _paginate(projects, limit, offset)
        payload = [ProjectRead.model_validate(project) for project in page]
        return ListEnvelope(
            data=payload,
            meta=PaginationMeta(limit=limit, offset=offset, total=total),
        )

    @app.get("/projects/{project_id}", response_model=Envelope[ProjectRead])
    def get_project(project_id: UUID):
        project = api.get_project(project_id)
        return Envelope(data=ProjectRead.model_validate(project))

    @app.patch("/projects/{project_id}", response_model=Envelope[ProjectRead])
    def update_project(project_id: UUID, payload: ProjectUpdate, request: Request):
        actor = _actor_from_request(request)
        project = api.update_project(
            project_id,
            name=payload.name,
            description=payload.description,
            status=payload.status,
            actor=actor,
        )
        return Envelope(data=ProjectRead.model_validate(project))

    @app.delete("/projects/{project_id}", response_model=Envelope[ProjectRead])
    def delete_project(project_id: UUID, request: Request):
        actor = _actor_from_request(request)
        project = api.delete_project(project_id, actor=actor)
        return Envelope(data=ProjectRead.model_validate(project))

    @app.post(
        "/questions",
        response_model=Envelope[QuestionRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_question(payload: QuestionCreate, request: Request):
        actor = _actor_from_request(request)
        question = api.create_question(
            project_id=payload.project_id,
            text=payload.text,
            question_type=payload.question_type,
            hypothesis=payload.hypothesis,
            status=payload.status or question_default_status(),
            parent_question_ids=payload.parent_question_ids,
            created_from=payload.created_from or QuestionSource.MANUAL,
            actor=actor,
            created_by=_resolve_created_by(payload.created_by, actor),
        )
        return Envelope(data=QuestionRead.model_validate(question))

    @app.get("/questions", response_model=ListEnvelope[QuestionRead])
    def list_questions(
        project_id: UUID | None = None,
        status: QuestionStatus | None = None,
        question_type: QuestionType | None = None,
        created_from: QuestionSource | None = None,
        search: str | None = None,
        q: str | None = None,
        parent_question_id: UUID | None = None,
        ancestor_question_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _validate_pagination(limit, offset)
        resolved_search = search or q
        questions = api.list_questions(
            project_id=project_id,
            status=status,
            question_type=question_type,
            created_from=created_from,
            search=resolved_search,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
        )
        page, total = _paginate(questions, limit, offset)
        payload = [QuestionRead.model_validate(question) for question in page]
        return ListEnvelope(
            data=payload,
            meta=PaginationMeta(limit=limit, offset=offset, total=total),
        )

    @app.get("/questions/{question_id}", response_model=Envelope[QuestionRead])
    def get_question(question_id: UUID):
        question = api.get_question(question_id)
        return Envelope(data=QuestionRead.model_validate(question))

    @app.patch("/questions/{question_id}", response_model=Envelope[QuestionRead])
    def update_question(question_id: UUID, payload: QuestionUpdate, request: Request):
        actor = _actor_from_request(request)
        question = api.update_question(
            question_id,
            text=payload.text,
            question_type=payload.question_type,
            hypothesis=payload.hypothesis,
            status=payload.status,
            parent_question_ids=payload.parent_question_ids,
            actor=actor,
        )
        return Envelope(data=QuestionRead.model_validate(question))

    @app.delete("/questions/{question_id}", response_model=Envelope[QuestionRead])
    def delete_question(question_id: UUID, request: Request):
        actor = _actor_from_request(request)
        question = api.delete_question(question_id, actor=actor)
        return Envelope(data=QuestionRead.model_validate(question))

    @app.post(
        "/datasets",
        response_model=Envelope[DatasetRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_dataset(payload: DatasetCreate, request: Request):
        actor = _actor_from_request(request)
        dataset = api.create_dataset(
            project_id=payload.project_id,
            primary_question_id=payload.primary_question_id,
            secondary_question_ids=payload.secondary_question_ids,
            status=payload.status or dataset_default_status(),
            commit_manifest=_manifest_from_payload(payload.commit_manifest),
            commit_hash=payload.commit_hash,
            actor=actor,
            created_by=_resolve_created_by(payload.created_by, actor),
        )
        return Envelope(data=DatasetRead.model_validate(dataset))

    @app.get("/datasets", response_model=ListEnvelope[DatasetRead])
    def list_datasets(
        project_id: UUID | None = None,
        status: DatasetStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _validate_pagination(limit, offset)
        datasets = api.list_datasets(project_id=project_id)
        if status is not None:
            datasets = [dataset for dataset in datasets if dataset.status == status]
        page, total = _paginate(datasets, limit, offset)
        payload = [DatasetRead.model_validate(dataset) for dataset in page]
        return ListEnvelope(
            data=payload,
            meta=PaginationMeta(limit=limit, offset=offset, total=total),
        )

    @app.get("/datasets/{dataset_id}", response_model=Envelope[DatasetRead])
    def get_dataset(dataset_id: UUID):
        dataset = api.get_dataset(dataset_id)
        return Envelope(data=DatasetRead.model_validate(dataset))

    @app.patch("/datasets/{dataset_id}", response_model=Envelope[DatasetRead])
    def update_dataset(dataset_id: UUID, payload: DatasetUpdate, request: Request):
        actor = _actor_from_request(request)
        question_links = _links_from_payload(payload.question_links)
        dataset = api.update_dataset(
            dataset_id,
            status=payload.status,
            question_links=question_links,
            commit_manifest=_manifest_from_payload(payload.commit_manifest),
            commit_hash=payload.commit_hash,
            actor=actor,
        )
        return Envelope(data=DatasetRead.model_validate(dataset))

    @app.delete("/datasets/{dataset_id}", response_model=Envelope[DatasetRead])
    def delete_dataset(dataset_id: UUID, request: Request):
        actor = _actor_from_request(request)
        dataset = api.delete_dataset(dataset_id, actor=actor)
        return Envelope(data=DatasetRead.model_validate(dataset))

    @app.post(
        "/notes",
        response_model=Envelope[NoteRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_note(payload: NoteCreate, request: Request):
        actor = _actor_from_request(request)
        extracted_entities = _entities_from_payload(payload.extracted_entities)
        targets = _targets_from_payload(payload.targets)
        note = api.create_note(
            project_id=payload.project_id,
            raw_content=payload.raw_content,
            transcribed_text=payload.transcribed_text,
            extracted_entities=extracted_entities,
            targets=targets,
            metadata=payload.metadata,
            status=payload.status or note_default_status(),
            actor=actor,
            created_by=_resolve_created_by(payload.created_by, actor),
        )
        return Envelope(data=NoteRead.model_validate(note))

    @app.post(
        "/notes/upload",
        response_model=Envelope[NoteRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def upload_note(payload: NoteUpload, request: Request):
        actor = _actor_from_request(request)
        extracted_entities = _entities_from_payload(payload.extracted_entities)
        targets = _targets_from_payload(payload.targets)
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
            extracted_entities=extracted_entities,
            targets=targets,
            metadata=payload.metadata,
            status=payload.status or note_default_status(),
            actor=actor,
            created_by=_resolve_created_by(payload.created_by, actor),
        )
        return Envelope(data=NoteRead.model_validate(note))

    @app.get("/notes", response_model=ListEnvelope[NoteRead])
    def list_notes(
        project_id: UUID | None = None,
        status: NoteStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _validate_pagination(limit, offset)
        notes = api.list_notes(project_id=project_id)
        if status is not None:
            notes = [note for note in notes if note.status == status]
        page, total = _paginate(notes, limit, offset)
        payload = [NoteRead.model_validate(note) for note in page]
        return ListEnvelope(
            data=payload,
            meta=PaginationMeta(limit=limit, offset=offset, total=total),
        )

    @app.get("/notes/{note_id}", response_model=Envelope[NoteRead])
    def get_note(note_id: UUID):
        note = api.get_note(note_id)
        return Envelope(data=NoteRead.model_validate(note))

    @app.get("/notes/{note_id}/raw")
    def download_note_raw(note_id: UUID, request: Request):
        raw_asset, content = api.download_note_raw(note_id)
        accept = (request.headers.get("accept") or "").lower()
        if "application/json" not in accept:
            headers = {
                "Content-Disposition": f'attachment; filename=\"{raw_asset.filename}\"',
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

    @app.patch("/notes/{note_id}", response_model=Envelope[NoteRead])
    def update_note(note_id: UUID, payload: NoteUpdate, request: Request):
        actor = _actor_from_request(request)
        extracted_entities = _entities_from_payload(payload.extracted_entities)
        targets = _targets_from_payload(payload.targets)
        note = api.update_note(
            note_id,
            transcribed_text=payload.transcribed_text,
            extracted_entities=extracted_entities,
            targets=targets,
            metadata=payload.metadata,
            status=payload.status,
            actor=actor,
        )
        return Envelope(data=NoteRead.model_validate(note))

    @app.delete("/notes/{note_id}", response_model=Envelope[NoteRead])
    def delete_note(note_id: UUID, request: Request):
        actor = _actor_from_request(request)
        note = api.delete_note(note_id, actor=actor)
        return Envelope(data=NoteRead.model_validate(note))

    @app.post(
        "/notes/{note_id}/tag-suggestions",
        response_model=Envelope[list[EntityTagSuggestionRead]],
        status_code=http_status.HTTP_201_CREATED,
    )
    def suggest_tag_suggestions(
        note_id: UUID,
        request: Request,
        payload: TagSuggestionRequest | None = None,
    ):
        actor = _actor_from_request(request)
        provenance = payload.provenance if payload else None
        suggestions = api.suggest_entity_tags(
            note_id,
            provenance=provenance,
            actor=actor,
        )
        return Envelope(
            data=[EntityTagSuggestionRead.model_validate(suggestion) for suggestion in suggestions],
            meta={"count": len(suggestions)},
        )

    @app.get(
        "/notes/{note_id}/tag-suggestions",
        response_model=ListEnvelope[EntityTagSuggestionRead],
    )
    def list_tag_suggestions(
        note_id: UUID,
        status: TagSuggestionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _validate_pagination(limit, offset)
        suggestions = api.list_entity_tag_suggestions(note_id, status=status)
        page, total = _paginate(suggestions, limit, offset)
        payload = [EntityTagSuggestionRead.model_validate(suggestion) for suggestion in page]
        return ListEnvelope(
            data=payload,
            meta=PaginationMeta(limit=limit, offset=offset, total=total),
        )

    @app.patch(
        "/notes/{note_id}/tag-suggestions/{suggestion_id}",
        response_model=Envelope[EntityTagSuggestionRead],
    )
    def review_tag_suggestion(
        note_id: UUID,
        suggestion_id: UUID,
        payload: TagSuggestionReviewRequest,
        request: Request,
    ):
        actor = _actor_from_request(request)
        suggestion = api.review_entity_tag_suggestion(
            note_id,
            suggestion_id,
            status=payload.status,
            reviewed_by=payload.reviewed_by,
            actor=actor,
        )
        return Envelope(data=EntityTagSuggestionRead.model_validate(suggestion))

    @app.post(
        "/notes/{note_id}/extract-questions",
        response_model=Envelope[list[QuestionRead]],
        status_code=http_status.HTTP_201_CREATED,
    )
    def extract_questions(
        note_id: UUID,
        request: Request,
        payload: QuestionExtractionRequest | None = None,
    ):
        actor = _actor_from_request(request)
        question_type = (
            payload.question_type if payload and payload.question_type else QuestionType.OTHER
        )
        created_from = (
            payload.created_from if payload and payload.created_from else QuestionSource.API
        )
        questions = api.extract_questions_from_note(
            note_id,
            question_type=question_type,
            created_from=created_from,
            provenance=payload.provenance if payload else None,
            actor=actor,
        )
        return Envelope(
            data=[QuestionRead.model_validate(question) for question in questions],
            meta={"count": len(questions)},
        )

    @app.post(
        "/sessions",
        response_model=Envelope[SessionRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_session(payload: SessionCreate, request: Request):
        actor = _actor_from_request(request)
        session = api.create_session(
            project_id=payload.project_id,
            session_type=payload.session_type,
            primary_question_id=payload.primary_question_id,
            status=payload.status or session_default_status(),
            actor=actor,
            created_by=_resolve_created_by(payload.created_by, actor),
        )
        return Envelope(data=SessionRead.model_validate(session))

    @app.get("/sessions", response_model=ListEnvelope[SessionRead])
    def list_sessions(
        project_id: UUID | None = None,
        status: SessionStatus | None = None,
        session_type: SessionType | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _validate_pagination(limit, offset)
        sessions = api.list_sessions(project_id=project_id)
        if status is not None:
            sessions = [session for session in sessions if session.status == status]
        if session_type is not None:
            sessions = [session for session in sessions if session.session_type == session_type]
        page, total = _paginate(sessions, limit, offset)
        payload = [SessionRead.model_validate(session) for session in page]
        return ListEnvelope(
            data=payload,
            meta=PaginationMeta(limit=limit, offset=offset, total=total),
        )

    @app.get("/sessions/by-link/{link_code}", response_model=Envelope[SessionRead])
    def get_session_by_link_code(link_code: str):
        session = api.get_session_by_link_code(link_code)
        return Envelope(data=SessionRead.model_validate(session))

    @app.get("/sessions/{session_id}", response_model=Envelope[SessionRead])
    def get_session(session_id: UUID):
        session = api.get_session(session_id)
        return Envelope(data=SessionRead.model_validate(session))

    @app.patch("/sessions/{session_id}", response_model=Envelope[SessionRead])
    def update_session(session_id: UUID, payload: SessionUpdate, request: Request):
        actor = _actor_from_request(request)
        session = api.update_session(
            session_id,
            status=payload.status,
            ended_at=payload.ended_at,
            actor=actor,
        )
        return Envelope(data=SessionRead.model_validate(session))

    @app.get("/sessions/{session_id}/outputs", response_model=ListEnvelope[AcquisitionOutputRead])
    def list_session_outputs(
        session_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ):
        _validate_pagination(limit, offset)
        outputs = api.list_acquisition_outputs(session_id=session_id)
        page, total = _paginate(outputs, limit, offset)
        payload = [AcquisitionOutputRead.model_validate(output) for output in page]
        return ListEnvelope(
            data=payload,
            meta=PaginationMeta(limit=limit, offset=offset, total=total),
        )

    @app.post(
        "/sessions/{session_id}/outputs",
        response_model=Envelope[AcquisitionOutputRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_session_output(
        session_id: UUID,
        payload: AcquisitionOutputCreate,
        request: Request,
    ):
        actor = _actor_from_request(request)
        output = api.register_acquisition_output(
            session_id,
            file_path=payload.file_path,
            checksum=payload.checksum,
            size_bytes=payload.size_bytes,
            actor=actor,
        )
        return Envelope(data=AcquisitionOutputRead.model_validate(output))

    @app.delete("/sessions/{session_id}", response_model=Envelope[SessionRead])
    def delete_session(session_id: UUID, request: Request):
        actor = _actor_from_request(request)
        session = api.delete_session(session_id, actor=actor)
        return Envelope(data=SessionRead.model_validate(session))

    @app.post(
        "/sessions/{session_id}/promote",
        response_model=Envelope[DatasetRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def promote_operational_session(
        session_id: UUID,
        payload: SessionPromotionRequest,
        request: Request,
    ):
        actor = _actor_from_request(request)
        dataset = api.promote_operational_session(
            session_id,
            primary_question_id=payload.primary_question_id,
            secondary_question_ids=payload.secondary_question_ids,
            status=payload.status or DatasetStatus.COMMITTED,
            commit_manifest=_manifest_from_payload(payload.commit_manifest),
            actor=actor,
            created_by=_resolve_created_by(payload.created_by, actor),
        )
        return Envelope(data=DatasetRead.model_validate(dataset))

    @app.post(
        "/analyses",
        response_model=Envelope[AnalysisRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_analysis(payload: AnalysisCreate, request: Request):
        actor = _actor_from_request(request)
        analysis = api.create_analysis(
            project_id=payload.project_id,
            dataset_ids=payload.dataset_ids,
            method_hash=payload.method_hash,
            code_version=payload.code_version,
            environment_hash=payload.environment_hash,
            status=payload.status or analysis_default_status(),
            actor=actor,
            executed_by=payload.executed_by or _resolve_created_by(None, actor),
        )
        return Envelope(data=AnalysisRead.model_validate(analysis))

    @app.get("/analyses", response_model=ListEnvelope[AnalysisRead])
    def list_analyses(
        project_id: UUID | None = None,
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
        status: AnalysisStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _validate_pagination(limit, offset)
        analyses = api.list_analyses(
            project_id=project_id,
            dataset_id=dataset_id,
            question_id=question_id,
        )
        if status is not None:
            analyses = [analysis for analysis in analyses if analysis.status == status]
        page, total = _paginate(analyses, limit, offset)
        payload = [AnalysisRead.model_validate(analysis) for analysis in page]
        return ListEnvelope(
            data=payload,
            meta=PaginationMeta(limit=limit, offset=offset, total=total),
        )

    @app.get("/analyses/{analysis_id}", response_model=Envelope[AnalysisRead])
    def get_analysis(analysis_id: UUID):
        analysis = api.get_analysis(analysis_id)
        return Envelope(data=AnalysisRead.model_validate(analysis))

    @app.patch("/analyses/{analysis_id}", response_model=Envelope[AnalysisRead])
    def update_analysis(analysis_id: UUID, payload: AnalysisUpdate, request: Request):
        actor = _actor_from_request(request)
        analysis = api.update_analysis(
            analysis_id,
            status=payload.status,
            environment_hash=payload.environment_hash,
            actor=actor,
        )
        return Envelope(data=AnalysisRead.model_validate(analysis))

    @app.post("/analyses/{analysis_id}/commit", response_model=Envelope[AnalysisCommitResult])
    def commit_analysis(analysis_id: UUID, payload: AnalysisCommitRequest, request: Request):
        actor = _actor_from_request(request)
        analysis, claims, visualizations = api.commit_analysis(
            analysis_id,
            environment_hash=payload.environment_hash,
            claims=_claim_inputs_from_payload(payload.claims),
            visualizations=_visualization_inputs_from_payload(payload.visualizations),
            actor=actor,
        )
        result = AnalysisCommitResult(
            analysis=AnalysisRead.model_validate(analysis),
            claims=[ClaimRead.model_validate(claim) for claim in claims],
            visualizations=[
                VisualizationRead.model_validate(viz)
                for viz in visualizations
            ],
        )
        return Envelope(data=result)

    @app.delete("/analyses/{analysis_id}", response_model=Envelope[AnalysisRead])
    def delete_analysis(analysis_id: UUID, request: Request):
        actor = _actor_from_request(request)
        analysis = api.delete_analysis(analysis_id, actor=actor)
        return Envelope(data=AnalysisRead.model_validate(analysis))

    @app.post(
        "/claims",
        response_model=Envelope[ClaimRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_claim(payload: ClaimCreate, request: Request):
        actor = _actor_from_request(request)
        claim = api.create_claim(
            project_id=payload.project_id,
            statement=payload.statement,
            confidence=payload.confidence,
            status=payload.status or ClaimStatus.PROPOSED,
            supported_by_dataset_ids=payload.supported_by_dataset_ids,
            supported_by_analysis_ids=payload.supported_by_analysis_ids,
            actor=actor,
        )
        return Envelope(data=ClaimRead.model_validate(claim))

    @app.get("/claims", response_model=ListEnvelope[ClaimRead])
    def list_claims(
        project_id: UUID | None = None,
        status: ClaimStatus | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _validate_pagination(limit, offset)
        claims = api.list_claims(
            project_id=project_id,
            status=status,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
        )
        page, total = _paginate(claims, limit, offset)
        payload = [ClaimRead.model_validate(claim) for claim in page]
        return ListEnvelope(
            data=payload,
            meta=PaginationMeta(limit=limit, offset=offset, total=total),
        )

    @app.get("/claims/{claim_id}", response_model=Envelope[ClaimRead])
    def get_claim(claim_id: UUID):
        claim = api.get_claim(claim_id)
        return Envelope(data=ClaimRead.model_validate(claim))

    @app.patch("/claims/{claim_id}", response_model=Envelope[ClaimRead])
    def update_claim(claim_id: UUID, payload: ClaimUpdate, request: Request):
        actor = _actor_from_request(request)
        claim = api.update_claim(
            claim_id,
            statement=payload.statement,
            confidence=payload.confidence,
            status=payload.status,
            supported_by_dataset_ids=payload.supported_by_dataset_ids,
            supported_by_analysis_ids=payload.supported_by_analysis_ids,
            actor=actor,
        )
        return Envelope(data=ClaimRead.model_validate(claim))

    @app.delete("/claims/{claim_id}", response_model=Envelope[ClaimRead])
    def delete_claim(claim_id: UUID, request: Request):
        actor = _actor_from_request(request)
        claim = api.delete_claim(claim_id, actor=actor)
        return Envelope(data=ClaimRead.model_validate(claim))

    @app.post(
        "/visualizations",
        response_model=Envelope[VisualizationRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_visualization(payload: VisualizationCreate, request: Request):
        actor = _actor_from_request(request)
        visualization = api.create_visualization(
            analysis_id=payload.analysis_id,
            viz_type=payload.viz_type,
            file_path=payload.file_path,
            caption=payload.caption,
            related_claim_ids=payload.related_claim_ids,
            actor=actor,
        )
        return Envelope(data=VisualizationRead.model_validate(visualization))

    @app.get("/visualizations", response_model=ListEnvelope[VisualizationRead])
    def list_visualizations(
        project_id: UUID | None = None,
        analysis_id: UUID | None = None,
        claim_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        _validate_pagination(limit, offset)
        visualizations = api.list_visualizations(
            project_id=project_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
        )
        page, total = _paginate(visualizations, limit, offset)
        payload = [VisualizationRead.model_validate(viz) for viz in page]
        return ListEnvelope(
            data=payload,
            meta=PaginationMeta(limit=limit, offset=offset, total=total),
        )

    @app.get("/visualizations/{viz_id}", response_model=Envelope[VisualizationRead])
    def get_visualization(viz_id: UUID):
        visualization = api.get_visualization(viz_id)
        return Envelope(data=VisualizationRead.model_validate(visualization))

    @app.patch("/visualizations/{viz_id}", response_model=Envelope[VisualizationRead])
    def update_visualization(viz_id: UUID, payload: VisualizationUpdate, request: Request):
        actor = _actor_from_request(request)
        visualization = api.update_visualization(
            viz_id,
            viz_type=payload.viz_type,
            file_path=payload.file_path,
            caption=payload.caption,
            related_claim_ids=payload.related_claim_ids,
            actor=actor,
        )
        return Envelope(data=VisualizationRead.model_validate(visualization))

    @app.delete("/visualizations/{viz_id}", response_model=Envelope[VisualizationRead])
    def delete_visualization(viz_id: UUID, request: Request):
        actor = _actor_from_request(request)
        visualization = api.delete_visualization(viz_id, actor=actor)
        return Envelope(data=VisualizationRead.model_validate(visualization))


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ValidationError)
    def _handle_validation_error(request: Request, exc: ValidationError):
        return _error_response(
            http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            str(exc),
        )

    @app.exception_handler(NotFoundError)
    def _handle_not_found_error(request: Request, exc: NotFoundError):
        return _error_response(http_status.HTTP_404_NOT_FOUND, "not_found", str(exc))

    @app.exception_handler(AuthError)
    def _handle_auth_error(request: Request, exc: AuthError):
        return _error_response(http_status.HTTP_401_UNAUTHORIZED, "auth_error", str(exc))

    @app.exception_handler(ConflictError)
    def _handle_conflict_error(request: Request, exc: ConflictError):
        return _error_response(http_status.HTTP_409_CONFLICT, "conflict", str(exc))

    @app.exception_handler(LabTrackerError)
    def _handle_lab_tracker_error(request: Request, exc: LabTrackerError):
        return _error_response(http_status.HTTP_400_BAD_REQUEST, "lab_tracker_error", str(exc))

    @app.exception_handler(RequestValidationError)
    def _handle_request_validation_error(request: Request, exc: RequestValidationError):
        return _error_response(
            http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            "request_validation_error",
            "Request validation failed.",
            issues=_issues_from_validation_errors(exc.errors()),
        )


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    issues: list[ErrorIssue] | None = None,
) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorInfo(code=code, message=message, issues=issues))
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def _issues_from_validation_errors(errors: list[dict[str, Any]]) -> list[ErrorIssue]:
    issues: list[ErrorIssue] = []
    for error in errors:
        loc_parts = [str(part) for part in error.get("loc", []) if part != "body"]
        field = ".".join(loc_parts) if loc_parts else None
        issues.append(ErrorIssue(field=field, message=error.get("msg", "Invalid value")))
    return issues


def _actor_from_request(request: Request | None) -> AuthContext:
    if request is None:
        raise AuthError("Authentication required.")
    actor = getattr(request.state, "auth_context", None)
    if actor is None:
        raise AuthError("Authentication required.")
    return actor


def _auth_user_read(user: User) -> AuthUserRead:
    return AuthUserRead(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        created_at=user.created_at,
    )


def _auth_token_read(user: User, token: str, expires_at: datetime) -> AuthTokenRead:
    return AuthTokenRead(
        access_token=token,
        expires_at=expires_at,
        user=_auth_user_read(user),
    )


def _resolve_created_by(created_by: str | None, actor: AuthContext | None) -> str | None:
    if created_by:
        return created_by
    if actor is None:
        return None
    return str(actor.user_id)


def _validate_pagination(limit: int, offset: int) -> None:
    if limit < 1 or limit > 200:
        raise ValidationError("limit must be between 1 and 200.")
    if offset < 0:
        raise ValidationError("offset must be 0 or greater.")


def _paginate(items: list[Any], limit: int, offset: int) -> tuple[list[Any], int]:
    total = len(items)
    if offset >= total:
        return [], total
    return items[offset : offset + limit], total


def _entities_from_payload(
    payload: list[ExtractedEntityInput] | None,
) -> list[tuple[str, float, str]] | None:
    if payload is None:
        return None
    return [(entity.label, entity.confidence, entity.provenance) for entity in payload]


def _targets_from_payload(payload: list[EntityRefInput] | None) -> list[EntityRef] | None:
    if payload is None:
        return None
    return [EntityRef(entity_type=item.entity_type, entity_id=item.entity_id) for item in payload]


def _claim_inputs_from_payload(payload: list[ClaimCommit] | None) -> list[ClaimInput]:
    if payload is None:
        return []
    return [
        ClaimInput(
            statement=item.statement,
            confidence=item.confidence,
            status=item.status or ClaimStatus.PROPOSED,
            supported_by_dataset_ids=item.supported_by_dataset_ids or [],
            supported_by_analysis_ids=item.supported_by_analysis_ids or [],
        )
        for item in payload
    ]


def _visualization_inputs_from_payload(
    payload: list[VisualizationCommit] | None,
) -> list[VisualizationInput]:
    if payload is None:
        return []
    return [
        VisualizationInput(
            viz_type=item.viz_type,
            file_path=item.file_path,
            caption=item.caption,
            related_claim_ids=item.related_claim_ids or [],
        )
        for item in payload
    ]


def _links_from_payload(
    payload: list[QuestionLinkInput] | None,
) -> list[QuestionLink] | None:
    if payload is None:
        return None
    return [
        QuestionLink(
            question_id=item.question_id,
            role=item.role,
            outcome_status=item.outcome_status,
        )
        for item in payload
    ]


def _manifest_from_payload(
    payload: DatasetCommitManifestInputSchema | None,
) -> DatasetCommitManifestInputModel | None:
    if payload is None:
        return None
    files = [
        DatasetFile(path=file_item.path, checksum=file_item.checksum)
        for file_item in payload.files
    ]
    return DatasetCommitManifestInputModel(
        files=files,
        metadata=payload.metadata,
        nwb_metadata=payload.nwb_metadata,
        bids_metadata=payload.bids_metadata,
        note_ids=payload.note_ids,
        extraction_provenance=payload.extraction_provenance,
        source_session_id=payload.source_session_id,
    )


def project_default_status() -> ProjectStatus:
    return ProjectStatus.ACTIVE


def question_default_status() -> QuestionStatus:
    return QuestionStatus.STAGED


def dataset_default_status() -> DatasetStatus:
    return DatasetStatus.STAGED


def note_default_status() -> NoteStatus:
    return NoteStatus.STAGED


def session_default_status() -> SessionStatus:
    return SessionStatus.ACTIVE


def analysis_default_status() -> AnalysisStatus:
    return AnalysisStatus.STAGED
