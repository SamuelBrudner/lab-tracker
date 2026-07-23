"""Shared helpers for HTTP route modules."""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote, unquote
from uuid import UUID

from fastapi import Query
from starlette.requests import Request
from starlette.responses import JSONResponse

from lab_tracker.api import LabTrackerAPI
from lab_tracker.application import RequestHandlers
from lab_tracker.auth import AuthContext, AuthService, TokenService, User, extract_bearer_token
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    DatasetStatus,
    EntityRef,
    NoteMetadataScalar,
    NoteStatus,
    ProjectStatus,
    QuestionStatus,
    SessionStatus,
    UsageEventResourceType,
    UsageEventVerb,
)
from lab_tracker.schemas import (
    AuthTokenRead,
    AuthUserRead,
    ErrorEnvelope,
    ErrorInfo,
    ListEnvelope,
    PaginationMeta,
)

CreatedByFilter = Annotated[
    str | None,
    Query(
        description=(
            "Filter by the FK-backed attribution user UUID. Legacy string-only "
            "attribution rows are recovered only by record export and offboarding."
        ),
    ),
]


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


def accessible_project_ids_from_request(request: Request) -> set[Any] | None:
    actor = actor_from_request(request)
    return api_from_request(request).accessible_project_ids(actor)


def ensure_project_read(request: Request, project_id: Any) -> None:
    actor = actor_from_request(request)
    api_from_request(request).require_project_read(project_id, actor=actor)


def ensure_project_contributor(request: Request, project_id: Any) -> None:
    actor = actor_from_request(request)
    api_from_request(request).require_project_contributor(project_id, actor=actor)


def ensure_project_owner(request: Request, project_id: Any) -> None:
    actor = actor_from_request(request)
    api_from_request(request).require_project_owner(project_id, actor=actor)


def ensure_group_read(request: Request, group_id: Any) -> None:
    actor = actor_from_request(request)
    api_from_request(request).require_group_read(group_id, actor=actor)


def ensure_group_owner(request: Request, group_id: Any) -> None:
    actor = actor_from_request(request)
    api_from_request(request).require_group_owner(group_id, actor=actor)


def record_usage_view(
    request: Request,
    *,
    resource_type: UsageEventResourceType,
    resource_id: UUID,
    project_id: UUID | None = None,
) -> None:
    api_from_request(request).record_usage_event(
        verb=UsageEventVerb.VIEW,
        resource_type=resource_type,
        resource_id=resource_id,
        project_id=project_id,
        actor=actor_from_request(request),
    )


def filter_project_scoped_items(request: Request, items: list[Any]) -> list[Any]:
    allowed = accessible_project_ids_from_request(request)
    if allowed is None:
        return items
    return [item for item in items if getattr(item, "project_id", None) in allowed]


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


def wants_jsonld(request: Request) -> bool:
    """True when the client asked for JSON-LD via the Accept header."""
    return "application/ld+json" in request.headers.get("accept", "")


def provenance_base_url(request: Request) -> str:
    """Base URL that roots ``@id`` identifiers in provenance documents.

    Prefers the configured ``LAB_TRACKER_CANONICAL_BASE_URL`` so identifiers
    are stable names independent of the serving host; falls back to the
    request's own base URL when unset.
    """
    settings = getattr(request.app.state, "settings", None)
    configured = str(getattr(settings, "canonical_base_url", "") or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def safe_attachment_filename(filename: str) -> str:
    cleaned = _clean_attachment_filename(filename)
    return _ascii_attachment_fallback(cleaned)


def content_disposition_header(disposition: str, filename: str) -> str:
    cleaned = _clean_attachment_filename(filename)
    fallback = _ascii_attachment_fallback(cleaned)
    header = f'{disposition}; filename="{fallback}"'
    if cleaned != fallback:
        header = f"{header}; filename*=UTF-8''{quote(cleaned, safe='')}"
    return header


def _clean_attachment_filename(filename: str) -> str:
    cleaned = unquote((filename or "").strip())
    if not cleaned:
        return "download"
    cleaned = cleaned.replace("\r", "_").replace("\n", "_")
    cleaned = cleaned.replace("\\", "/").split("/")[-1]
    cleaned = cleaned.replace('"', "'")
    if not cleaned:
        return "download"
    return cleaned


def _ascii_attachment_fallback(filename: str) -> str:
    normalized = unicodedata.normalize("NFKD", filename)
    fallback = "".join(ch for ch in normalized if 32 <= ord(ch) < 127)
    fallback = fallback.replace("\\", "_").replace("/", "_").strip()
    if fallback and not fallback.startswith("."):
        return fallback
    suffix = ""
    if "." in fallback:
        suffix = fallback[fallback.rfind(".") :]
    elif "." in filename:
        raw_suffix = filename[filename.rfind(".") :]
        suffix = "".join(ch for ch in raw_suffix if ch.isascii() and ch.isprintable())
    return f"download{suffix}"


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


def _is_metadata_scalar(value: object) -> bool:
    return isinstance(value, (str, bool, int, float))


def parse_metadata_form(raw_value: str | None) -> dict[str, NoteMetadataScalar] | None:
    parsed = parse_json_form_field(raw_value, "metadata")
    if parsed is None:
        return None
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and _is_metadata_scalar(value) for key, value in parsed.items()
    ):
        raise ValidationError(
            "metadata must decode to an object of string, number, or boolean values."
        )
    return parsed


def handlers_from_request(request: Request) -> RequestHandlers:
    handlers = getattr(request.state, "lab_tracker_handlers", None)
    if handlers is None:
        raise RuntimeError("Application handlers are not available on request state.")
    return handlers


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
