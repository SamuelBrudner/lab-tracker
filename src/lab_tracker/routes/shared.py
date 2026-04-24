"""Shared helpers for HTTP route modules."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any
from urllib.parse import unquote

from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, AuthService, TokenService, User, extract_bearer_token
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    DatasetStatus,
    EntityRef,
    NoteStatus,
    ProjectStatus,
    QuestionStatus,
    SessionStatus,
)
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.schemas import (
    AuthTokenRead,
    AuthUserRead,
    ErrorEnvelope,
    ErrorInfo,
    ListEnvelope,
    PaginationMeta,
)


def auth_user_read(user: User) -> AuthUserRead:
    return AuthUserRead(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        created_at=user.created_at,
    )


def auth_token_read(user: User, token: str, expires_at: datetime) -> AuthTokenRead:
    return AuthTokenRead(
        access_token=token,
        expires_at=expires_at,
        user=auth_user_read(user),
    )


def auth_error_response(message: str) -> JSONResponse:
    payload = ErrorEnvelope(error=ErrorInfo(code="auth_error", message=message))
    return JSONResponse(status_code=401, content=payload.model_dump())


def actor_from_request(request: Request | None) -> AuthContext:
    if request is None:
        raise AuthError("Authentication required.")
    actor = getattr(request.state, "auth_context", None)
    if actor is None:
        raise AuthError("Authentication required.")
    return actor


def api_from_request(request: Request, fallback: LabTrackerAPI | None = None) -> LabTrackerAPI:
    api = getattr(request.state, "lab_tracker_api", None)
    if api is not None:
        return api
    if fallback is not None:
        return fallback
    raise RuntimeError("Lab Tracker API is not available on request state.")


def actor_from_authorization_header(
    request: Request,
    *,
    auth_service: AuthService,
    token_service: TokenService,
) -> AuthContext:
    token = extract_bearer_token(request.headers.get("authorization"))
    claims = token_service.verify_access_token(token)
    user = auth_service.get_user_by_id(claims.user_id)
    if user is None:
        raise AuthError("Invalid token.")
    return AuthContext(user_id=user.user_id, role=user.role)


def safe_attachment_filename(filename: str) -> str:
    cleaned = unquote((filename or "").strip())
    if not cleaned:
        return "download"
    cleaned = cleaned.replace("\r", "_").replace("\n", "_")
    cleaned = cleaned.replace("\\", "/").split("/")[-1]
    cleaned = cleaned.replace('"', "'")
    if not cleaned:
        return "download"
    return cleaned


def validate_pagination(limit: int, offset: int) -> None:
    if limit < 1 or limit > 200:
        raise ValidationError("limit must be between 1 and 200.")
    if offset < 0:
        raise ValidationError("offset must be 0 or greater.")


def paginate(items: list[Any], limit: int, offset: int) -> tuple[list[Any], int]:
    total = len(items)
    if offset >= total:
        return [], total
    return items[offset : offset + limit], total


def list_response(items: list[Any], *, limit: int, offset: int, total: int) -> ListEnvelope[Any]:
    return ListEnvelope(
        data=items,
        meta=PaginationMeta(limit=limit, offset=offset, total=total),
    )


def parse_json_form_field(raw_value: str | None, field_name: str) -> Any:
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{field_name} must be valid JSON.") from exc


def parse_entity_refs_form(raw_value: str | None) -> list[EntityRef] | None:
    parsed = parse_json_form_field(raw_value, "targets")
    if parsed is None:
        return None
    if not isinstance(parsed, list):
        raise ValidationError("targets must decode to a list.")
    try:
        return [EntityRef.model_validate(item) for item in parsed]
    except Exception as exc:
        raise ValidationError("targets contains invalid entity refs.") from exc


def parse_metadata_form(raw_value: str | None) -> dict[str, str] | None:
    parsed = parse_json_form_field(raw_value, "metadata")
    if parsed is None:
        return None
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
    ):
        raise ValidationError("metadata must decode to an object of string values.")
    return parsed


def db_session_from_request(request: Request) -> Session:
    session = getattr(request.state, "db_session", None)
    if session is None:
        raise RuntimeError("Database session is not available on request state.")
    return session


def repository_from_request(request: Request) -> LabTrackerRepository:
    repository = getattr(request.state, "lab_tracker_repository", None)
    if repository is None:
        raise RuntimeError("Repository is not available on request state.")
    return repository


def file_storage_from_request(request: Request) -> Any:
    storage_backend = getattr(request.app.state, "file_storage_backend", None)
    if storage_backend is None:
        raise ValidationError("File storage backend is not configured.")
    return storage_backend


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
