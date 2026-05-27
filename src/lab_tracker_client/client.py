"""HTTP client and idempotent upsert helpers for Lab Tracker consumers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

import httpx

from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    EntityType,
    NoteMetadataScalar,
    NoteStatus,
    ProjectStatus,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
)

JsonObject = dict[str, Any]

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_PAGE_SIZE = 200

PROJECT_STATUS_VALUES = tuple(status.value for status in ProjectStatus)
QUESTION_TYPE_VALUES = tuple(question_type.value for question_type in QuestionType)
QUESTION_STATUS_VALUES = tuple(status.value for status in QuestionStatus)
NOTE_STATUS_VALUES = tuple(status.value for status in NoteStatus)
ENTITY_TYPE_VALUES = tuple(entity_type.value for entity_type in EntityType)
SESSION_STATUS_VALUES = tuple(status.value for status in SessionStatus)
SESSION_TYPE_VALUES = tuple(session_type.value for session_type in SessionType)
DATASET_STATUS_VALUES = tuple(status.value for status in DatasetStatus)
ANALYSIS_STATUS_VALUES = tuple(status.value for status in AnalysisStatus)
CLAIM_STATUS_VALUES = tuple(status.value for status in ClaimStatus)

_ID_FIELDS = (
    "question_id",
    "note_id",
    "session_id",
    "dataset_id",
    "analysis_id",
    "claim_id",
    "viz_id",
    "visualization_id",
    "project_id",
)


class LTError(RuntimeError):
    """Base exception for Lab Tracker client failures."""


class LTAPIError(LTError):
    """Raised when the Lab Tracker API returns an error or malformed response."""


class LTValidationError(LTError):
    """Raised when client-side validation catches a bad request shape."""


class LTRecord(dict[str, Any]):
    """A dict-like API record with attribute access for consumer scripts."""

    @property
    def id(self) -> Any:
        for field_name in _ID_FIELDS:
            if field_name in self:
                return self[field_name]
        raise AttributeError("record does not include a known id field")

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_dict(self) -> dict[str, Any]:
        return dict(self)


@dataclass(frozen=True)
class EntityRef:
    """Reference to a Lab Tracker entity used as a note target."""

    entity_type: str
    entity_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "entity_type",
            _validate_enum(
                self.entity_type,
                field_name="entity_type",
                allowed_values=ENTITY_TYPE_VALUES,
            ),
        )
        object.__setattr__(self, "entity_id", str(self.entity_id).strip())
        if not self.entity_id:
            raise LTValidationError("entity_id must not be empty.")

    def to_payload(self) -> JsonObject:
        return {"entity_type": self.entity_type, "entity_id": self.entity_id}


class LabTracker:
    """Small API client for scripts that sync consumer repos with Lab Tracker."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        username: str | None = None,
        password: str | None = None,
        access_token: str | None = None,
        default_project_id: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.default_project_id = default_project_id
        self._access_token = access_token
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> "LabTracker":
        """Build a client from environment variables used by consumer repos."""

        return cls(
            base_url=os.getenv("LAB_TRACKER_BASE_URL", DEFAULT_BASE_URL),
            username=os.getenv("LAB_TRACKER_USERNAME")
            or os.getenv("LAB_TRACKER_MCP_USERNAME"),
            password=os.getenv("LAB_TRACKER_PASSWORD")
            or os.getenv("LAB_TRACKER_MCP_PASSWORD"),
            access_token=os.getenv("LAB_TRACKER_ACCESS_TOKEN"),
            default_project_id=os.getenv("LAB_TRACKER_PROJECT_ID"),
            timeout_seconds=float(
                os.getenv("LAB_TRACKER_HTTP_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))
            ),
        )

    @property
    def access_token(self) -> str | None:
        return self._access_token

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LabTracker":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def health(self) -> JsonObject:
        return self._request("GET", "/health", authenticated=False)

    def readiness(self) -> JsonObject:
        return self._request("GET", "/readiness", authenticated=False)

    def list_projects(
        self,
        *,
        status: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        resolved_status = _validate_optional_enum(
            status,
            field_name="project status",
            allowed_values=PROJECT_STATUS_VALUES,
        )
        return self._list_all(
            "/projects",
            params={"status": resolved_status},
            limit=limit,
            offset=offset,
        )

    def list_questions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        parent_question_id: str | None = None,
        ancestor_question_id: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        resolved_status = _validate_optional_enum(
            status,
            field_name="question status",
            allowed_values=QUESTION_STATUS_VALUES,
        )
        resolved_type = _validate_optional_enum(
            question_type,
            field_name="question_type",
            allowed_values=QUESTION_TYPE_VALUES,
        )
        return self._list_all(
            "/questions",
            params={
                "project_id": project_id,
                "status": resolved_status,
                "question_type": resolved_type,
                "search": search,
                "parent_question_id": parent_question_id,
                "ancestor_question_id": ancestor_question_id,
            },
            limit=limit,
            offset=offset,
        )

    def list_notes(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        resolved_status = _validate_optional_enum(
            status,
            field_name="note status",
            allowed_values=NOTE_STATUS_VALUES,
        )
        resolved_target_type = _validate_optional_enum(
            target_entity_type,
            field_name="target_entity_type",
            allowed_values=ENTITY_TYPE_VALUES,
        )
        return self._list_all(
            "/notes",
            params={
                "project_id": project_id,
                "status": resolved_status,
                "target_entity_type": resolved_target_type,
                "target_entity_id": target_entity_id,
            },
            limit=limit,
            offset=offset,
        )

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        return self._list_all(
            "/sessions",
            params={
                "project_id": project_id,
                "status": _validate_optional_enum(
                    status,
                    field_name="session status",
                    allowed_values=SESSION_STATUS_VALUES,
                ),
                "session_type": _validate_optional_enum(
                    session_type,
                    field_name="session_type",
                    allowed_values=SESSION_TYPE_VALUES,
                ),
            },
            limit=limit,
            offset=offset,
        )

    def list_datasets(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        return self._list_all(
            "/datasets",
            params={
                "project_id": project_id,
                "status": _validate_optional_enum(
                    status,
                    field_name="dataset status",
                    allowed_values=DATASET_STATUS_VALUES,
                ),
            },
            limit=limit,
            offset=offset,
        )

    def list_analyses(
        self,
        *,
        project_id: str | None = None,
        dataset_id: str | None = None,
        question_id: str | None = None,
        status: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        return self._list_all(
            "/analyses",
            params={
                "project_id": project_id,
                "dataset_id": dataset_id,
                "question_id": question_id,
                "status": _validate_optional_enum(
                    status,
                    field_name="analysis status",
                    allowed_values=ANALYSIS_STATUS_VALUES,
                ),
            },
            limit=limit,
            offset=offset,
        )

    def list_claims(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        dataset_id: str | None = None,
        analysis_id: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        return self._list_all(
            "/claims",
            params={
                "project_id": project_id,
                "status": _validate_optional_enum(
                    status,
                    field_name="claim status",
                    allowed_values=CLAIM_STATUS_VALUES,
                ),
                "dataset_id": dataset_id,
                "analysis_id": analysis_id,
            },
            limit=limit,
            offset=offset,
        )

    def list_visualizations(
        self,
        *,
        project_id: str | None = None,
        analysis_id: str | None = None,
        claim_id: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        return self._list_all(
            "/visualizations",
            params={
                "project_id": project_id,
                "analysis_id": analysis_id,
                "claim_id": claim_id,
            },
            limit=limit,
            offset=offset,
        )

    def find_project_by_name(self, name: str) -> LTRecord | None:
        cleaned = _require_non_empty(name, "name")
        for project in self.list_projects():
            if project.get("name") == cleaned:
                return project
        return None

    def upsert_project(
        self,
        *,
        name: str,
        description: str = "",
        status: str | None = None,
    ) -> LTRecord:
        resolved_status = _validate_optional_enum(
            status,
            field_name="project status",
            allowed_values=PROJECT_STATUS_VALUES,
        )
        existing = self.find_project_by_name(name)
        if existing is not None:
            return existing
        return self._data_record(
            self._request(
                "POST",
                "/projects",
                json_payload={
                    "name": _require_non_empty(name, "name"),
                    "description": description,
                    "status": resolved_status,
                },
            )
        )

    def find_question_by_text(self, project_id: str, text: str) -> LTRecord | None:
        cleaned = _require_non_empty(text, "text")
        for question in self.list_questions(project_id=str(project_id)):
            if question.get("text") == cleaned:
                return question
        return None

    def upsert_question(
        self,
        *,
        project_id: str,
        text: str,
        question_type: str = QuestionType.OTHER.value,
        status: str = QuestionStatus.ACTIVE.value,
        hypothesis: str | None = None,
        parent_question_ids: Sequence[str] | None = None,
    ) -> LTRecord:
        cleaned_text = _require_non_empty(text, "text")
        resolved_type = _validate_enum(
            question_type,
            field_name="question_type",
            allowed_values=QUESTION_TYPE_VALUES,
        )
        resolved_status = _validate_enum(
            status,
            field_name="question status",
            allowed_values=QUESTION_STATUS_VALUES,
        )
        existing = self.find_question_by_text(project_id, cleaned_text)
        if existing is not None:
            return existing
        return self._data_record(
            self._request(
                "POST",
                "/questions",
                json_payload={
                    "project_id": str(project_id),
                    "text": cleaned_text,
                    "question_type": resolved_type,
                    "hypothesis": hypothesis,
                    "status": resolved_status,
                    "parent_question_ids": list(parent_question_ids or []),
                },
            )
        )

    def find_note_by_marker(self, project_id: str, marker: str) -> LTRecord | None:
        cleaned = _require_non_empty(marker, "marker")
        for note in self.list_notes(project_id=str(project_id)):
            if first_line_marker(str(note.get("raw_content") or "")) == cleaned:
                return note
        return None

    def upsert_note(
        self,
        *,
        project_id: str,
        content: str,
        targets: Sequence[EntityRef | Mapping[str, Any] | tuple[str, str] | str] = (),
        metadata: Mapping[str, NoteMetadataScalar] | None = None,
        status: str = NoteStatus.COMMITTED.value,
        transcribed_text: str | None = None,
    ) -> LTRecord:
        marker = first_line_marker(content)
        if not marker:
            raise LTValidationError(
                "Note content has no first non-blank line; cannot derive idempotency marker."
            )
        resolved_status = _validate_enum(
            status,
            field_name="note status",
            allowed_values=NOTE_STATUS_VALUES,
        )
        resolved_targets = [_normalize_entity_ref(target) for target in targets]
        resolved_metadata = _validate_metadata(metadata)
        existing = self.find_note_by_marker(project_id, marker)
        if existing is not None:
            return existing
        return self._data_record(
            self._request(
                "POST",
                "/notes",
                json_payload={
                    "project_id": str(project_id),
                    "raw_content": content,
                    "transcribed_text": transcribed_text,
                    "targets": resolved_targets,
                    "metadata": resolved_metadata,
                    "status": resolved_status,
                },
            )
        )

    def quick_capture(
        self,
        text: str | bytes,
        *,
        project_id: str | None = None,
        filename: str = "quick-capture.txt",
        content_type: str = "text/plain",
        metadata: Mapping[str, NoteMetadataScalar] | None = None,
        source: str | None = None,
    ) -> LTRecord:
        resolved_project_id = project_id or self.default_project_id
        if not resolved_project_id:
            raise LTValidationError(
                "project_id is required for quick_capture, or set LAB_TRACKER_PROJECT_ID."
            )
        body = text if isinstance(text, bytes) else text.encode("utf-8")
        if not body:
            raise LTValidationError("quick_capture text must not be empty.")
        resolved_metadata = dict(_validate_metadata(metadata) or {})
        if source:
            resolved_metadata["source"] = source
        data: dict[str, str] = {"project_id": str(resolved_project_id)}
        if resolved_metadata:
            data["metadata"] = json.dumps(resolved_metadata)
        return self._data_record(
            self._request(
                "POST",
                "/notes/quick-capture",
                data=data,
                files={"file": (_require_non_empty(filename, "filename"), body, content_type)},
            )
        )

    def _list_all(
        self,
        path: str,
        *,
        params: JsonObject | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        page_size = _validate_limit(limit)
        current_offset = _validate_offset(offset)
        items: list[LTRecord] = []
        while True:
            payload = self._request(
                "GET",
                path,
                params={
                    **(params or {}),
                    "limit": page_size,
                    "offset": current_offset,
                },
            )
            page_items = payload.get("data")
            if not isinstance(page_items, list):
                raise LTAPIError(f"{path} response did not include a list data field.")
            items.extend(_record(item) for item in page_items)
            meta = payload.get("meta")
            if not isinstance(meta, dict):
                break
            total = int(meta.get("total") or 0)
            returned_limit = int(meta.get("limit") or page_size)
            current_offset += returned_limit
            if current_offset >= total or not page_items:
                break
        return items

    def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        params: JsonObject | None = None,
        json_payload: JsonObject | None = None,
        data: Mapping[str, str] | None = None,
        files: dict[str, Any] | None = None,
        retry_on_unauthorized: bool = True,
    ) -> JsonObject:
        headers: dict[str, str] = {}
        if authenticated:
            token = self._bearer_token(required=False)
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
        response = self._client.request(
            method,
            path,
            params=_drop_empty(params),
            json=_drop_empty(json_payload),
            data=data,
            files=files,
            headers=headers,
        )
        if response.status_code == 401 and authenticated and retry_on_unauthorized:
            self._access_token = None
            token = self._bearer_token(required=True)
            headers["Authorization"] = f"Bearer {token}"
            response = self._client.request(
                method,
                path,
                params=_drop_empty(params),
                json=_drop_empty(json_payload),
                data=data,
                files=files,
                headers=headers,
            )
        if response.status_code == 422:
            raise LTValidationError(_response_error(response))
        if response.status_code >= 400:
            raise LTAPIError(_response_error(response))
        return _response_json(response)

    def _bearer_token(self, *, required: bool) -> str | None:
        if self._access_token:
            return self._access_token
        username = (self.username or "").strip()
        password = self.password or ""
        if not username or not password:
            if required:
                raise LTAPIError(
                    "LAB_TRACKER_USERNAME and LAB_TRACKER_PASSWORD are required "
                    "when the Lab Tracker API has authentication enabled."
                )
            return None
        response = self._client.post(
            "/auth/login",
            json={"username": username, "password": password},
        )
        if response.status_code == 422:
            raise LTValidationError(_response_error(response))
        if response.status_code >= 400:
            raise LTAPIError(_response_error(response))
        payload = _response_json(response)
        try:
            token = str(payload["data"]["access_token"])
        except (KeyError, TypeError) as exc:
            raise LTAPIError("Login response did not include an access token.") from exc
        self._access_token = token
        return token

    @staticmethod
    def _data_record(payload: JsonObject) -> LTRecord:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise LTAPIError("Lab Tracker API response did not include an object data field.")
        return _record(data)


def first_line_marker(content: str) -> str:
    for line in content.splitlines():
        marker = line.strip()
        if marker:
            return marker
    return ""


def ids(path: str | Path = "lt_ids.json") -> dict[str, str]:
    resolved = Path(path)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LTError(f"{resolved} not found.") from exc
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise LTValidationError(f"{resolved} must contain a JSON object of string ids.")
    return payload


def client_from_env() -> LabTracker:
    return LabTracker.from_env()


client = client_from_env()


def health() -> JsonObject:
    return client.health()


def readiness() -> JsonObject:
    return client.readiness()


def list_projects(**kwargs: Any) -> list[LTRecord]:
    return client.list_projects(**kwargs)


def list_questions(**kwargs: Any) -> list[LTRecord]:
    return client.list_questions(**kwargs)


def list_notes(**kwargs: Any) -> list[LTRecord]:
    return client.list_notes(**kwargs)


def list_sessions(**kwargs: Any) -> list[LTRecord]:
    return client.list_sessions(**kwargs)


def list_datasets(**kwargs: Any) -> list[LTRecord]:
    return client.list_datasets(**kwargs)


def list_analyses(**kwargs: Any) -> list[LTRecord]:
    return client.list_analyses(**kwargs)


def list_claims(**kwargs: Any) -> list[LTRecord]:
    return client.list_claims(**kwargs)


def list_visualizations(**kwargs: Any) -> list[LTRecord]:
    return client.list_visualizations(**kwargs)


def find_project_by_name(name: str) -> LTRecord | None:
    return client.find_project_by_name(name)


def find_question_by_text(project_id: str, text: str) -> LTRecord | None:
    return client.find_question_by_text(project_id, text)


def find_note_by_marker(project_id: str, marker: str) -> LTRecord | None:
    return client.find_note_by_marker(project_id, marker)


def upsert_project(**kwargs: Any) -> LTRecord:
    return client.upsert_project(**kwargs)


def upsert_question(**kwargs: Any) -> LTRecord:
    return client.upsert_question(**kwargs)


def upsert_note(**kwargs: Any) -> LTRecord:
    return client.upsert_note(**kwargs)


def quick_capture(text: str | bytes, **kwargs: Any) -> LTRecord:
    return client.quick_capture(text, **kwargs)


def _record(payload: Any) -> LTRecord:
    if not isinstance(payload, dict):
        raise LTAPIError("Lab Tracker API returned a non-object record.")
    return LTRecord(payload)


def _drop_empty(payload: JsonObject | None) -> JsonObject | None:
    if payload is None:
        return None
    return {key: value for key, value in payload.items() if value is not None}


def _validate_limit(limit: int) -> int:
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise LTValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}.")
    return limit


def _validate_offset(offset: int) -> int:
    if offset < 0:
        raise LTValidationError("offset must be 0 or greater.")
    return offset


def _require_non_empty(value: str, field_name: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise LTValidationError(f"{field_name} must not be empty.")
    return cleaned


def _validate_optional_enum(
    value: str | None,
    *,
    field_name: str,
    allowed_values: tuple[str, ...],
) -> str | None:
    if value is None:
        return None
    return _validate_enum(value, field_name=field_name, allowed_values=allowed_values)


def _validate_enum(
    value: str,
    *,
    field_name: str,
    allowed_values: tuple[str, ...],
) -> str:
    cleaned = str(value).strip().lower()
    if cleaned not in allowed_values:
        allowed_text = ", ".join(allowed_values)
        raise LTValidationError(
            f"Invalid {field_name} {value!r}. Allowed values: {allowed_text}."
        )
    return cleaned


def _validate_metadata(
    metadata: Mapping[str, NoteMetadataScalar] | None,
) -> dict[str, NoteMetadataScalar] | None:
    if metadata is None:
        return None
    validated: dict[str, NoteMetadataScalar] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not key.strip():
            raise LTValidationError("metadata keys must be non-empty strings.")
        if not isinstance(value, (str, bool, int, float)):
            raise LTValidationError(
                "metadata values must be strings, numbers, or booleans."
            )
        validated[key] = value
    return validated


def _normalize_entity_ref(ref: EntityRef | Mapping[str, Any] | tuple[str, str] | str) -> JsonObject:
    if isinstance(ref, EntityRef):
        return ref.to_payload()
    if isinstance(ref, str):
        entity_type, separator, entity_id = ref.partition(":")
        if not separator:
            raise LTValidationError(f"Bad entity ref string {ref!r}; expected 'kind:uuid'.")
        return EntityRef(entity_type, entity_id).to_payload()
    if isinstance(ref, tuple):
        if len(ref) != 2:
            raise LTValidationError("Entity ref tuples must be (entity_type, entity_id).")
        return EntityRef(str(ref[0]), str(ref[1])).to_payload()
    if isinstance(ref, Mapping):
        try:
            entity_type = ref["entity_type"]
            entity_id = ref["entity_id"]
        except KeyError as exc:
            raise LTValidationError(
                "Entity ref mappings must include entity_type and entity_id."
            ) from exc
        return EntityRef(str(entity_type), str(entity_id)).to_payload()
    raise LTValidationError(f"Unsupported entity ref: {ref!r}")


def _response_json(response: httpx.Response) -> JsonObject:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LTAPIError("Lab Tracker API returned non-JSON content.") from exc
    if not isinstance(payload, dict):
        raise LTAPIError("Lab Tracker API returned a non-object JSON payload.")
    return payload


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Lab Tracker API returned HTTP {response.status_code}: {response.text}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        detail = payload.get("detail")
        if detail:
            return str(detail)
    return f"Lab Tracker API returned HTTP {response.status_code}: {payload}"
