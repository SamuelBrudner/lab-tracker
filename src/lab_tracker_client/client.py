"""HTTP client and idempotent upsert helpers for Lab Tracker consumers."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import platform
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from lab_tracker.assistant_next_questions import (
    OPEN_GOAL_STATUSES,
    OPEN_QUESTION_STATUSES,
    build_next_questions_payload,
)
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    EntityType,
    GoalStatus,
    NoteMetadataScalar,
    NoteStatus,
    ProjectStatus,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
)

JsonObject = dict[str, Any]
EvidenceNoteKey = tuple[str, str, str]

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
GOAL_STATUS_VALUES = tuple(status.value for status in GoalStatus)

_ID_FIELDS = (
    "question_id",
    "note_id",
    "session_id",
    "dataset_id",
    "viz_id",
    "visualization_id",
    "analysis_id",
    "claim_id",
    "change_set_id",
    "output_id",
    "project_id",
    "goal_id",
)

EVIDENCE_METADATA_KEYS = (
    "evidence_source_provider",
    "evidence_source_uri",
    "evidence_source_external_id",
    "evidence_source_observed_at",
    "evidence_capture_kind",
    "evidence_content_hash",
    "evidence_adapter",
    "evidence_title",
)

CAPTURE_HOST_METADATA_KEYS = (
    "capture_host_label",
    "capture_install_id",
    "capture_platform",
)


def _install_id_path() -> Path:
    base = os.getenv("LAB_TRACKER_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".lab-tracker"
    return root / "install-id"


def connection_profile_path() -> Path:
    base = os.getenv("LAB_TRACKER_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".lab-tracker"
    return root / "config.json"


def load_connection_profile() -> dict[str, str]:
    """Read the persisted connection profile; fail-soft to an empty mapping.

    Environment variables always win in ``from_env``; the profile only fills
    gaps so hook-, scheduler-, and GUI-launched commands work without
    per-shell env exports. A missing or malformed file must never break a
    consumer script, so every failure path returns ``{}``.
    """

    with suppress(Exception):
        payload = json.loads(connection_profile_path().read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return {
                str(key): value.strip()
                for key, value in payload.items()
                if isinstance(value, str) and value.strip()
            }
    return {}


def _resolve_install_id() -> str:
    """Read or lazily create a stable per-install id; fail-soft to ""."""
    path = _install_id_path()
    with suppress(OSError):
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    new_id = uuid.uuid4().hex
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id + "\n", encoding="utf-8")
    return new_id


def capture_host_metadata() -> dict[str, NoteMetadataScalar]:
    """Stable, fail-soft identity for the machine performing a capture or push.

    A configurable ``LAB_TRACKER_CAPTURE_HOST`` label (hostname fallback) plus a
    persisted per-install id and the OS family. Used to disambiguate the
    cross-machine content-hash join and to record which computer pushed evidence.
    """

    metadata: dict[str, NoteMetadataScalar] = {}
    label = os.getenv("LAB_TRACKER_CAPTURE_HOST", "").strip()
    if not label:
        with suppress(Exception):
            label = platform.node().strip()
    if label:
        metadata["capture_host_label"] = label
    install_id = _resolve_install_id()
    if install_id:
        metadata["capture_install_id"] = install_id
    with suppress(Exception):
        system = platform.system().strip()
        if system:
            metadata["capture_platform"] = system
    return metadata


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


EvidenceNoteIndex = dict[EvidenceNoteKey, LTRecord]


@dataclass(frozen=True)
class EvidenceImportResult:
    """Outcome from importing one external evidence source item."""

    action: str
    path: str
    source_external_id: str
    source_uri: str
    content_hash: str
    metadata: dict[str, NoteMetadataScalar]
    note: LTRecord | None = None
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
        self._supplied_access_token = bool(access_token)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_seconds,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> LabTracker:
        """Build a client from environment variables used by consumer repos.

        Values absent from the environment fall back to the persisted
        connection profile (``~/.lab-tracker/config.json``, written only by
        the consent-gated ``lt setup connect``); env vars always win. The
        profile's access token is used only when the environment supplies no
        credentials of its own (a token or a username) and does not point at
        a different server than the one the token was saved for.
        """

        profile = load_connection_profile()
        env_base_url = os.getenv("LAB_TRACKER_BASE_URL") or os.getenv("LAB_TRACKER_MCP_BASE_URL")
        env_username = os.getenv("LAB_TRACKER_USERNAME") or os.getenv("LAB_TRACKER_MCP_USERNAME")
        profile_base_url = profile.get("base_url") or DEFAULT_BASE_URL
        profile_token = profile.get("access_token")
        if env_username or (
            env_base_url and env_base_url.rstrip("/") != profile_base_url.rstrip("/")
        ):
            profile_token = None
        return cls(
            base_url=env_base_url or profile.get("base_url") or DEFAULT_BASE_URL,
            username=env_username,
            password=os.getenv("LAB_TRACKER_PASSWORD") or os.getenv("LAB_TRACKER_MCP_PASSWORD"),
            access_token=os.getenv("LAB_TRACKER_ACCESS_TOKEN") or profile_token,
            default_project_id=os.getenv("LAB_TRACKER_PROJECT_ID")
            or profile.get("default_project_id"),
            timeout_seconds=float(
                os.getenv("LAB_TRACKER_HTTP_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))
            ),
        )

    @property
    def access_token(self) -> str | None:
        return self._access_token

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LabTracker:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def health(self) -> JsonObject:
        return self._request("GET", "/health", authenticated=False)

    def readiness(self) -> JsonObject:
        return self._request("GET", "/readiness")

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
        created_by: str | None = None,
        since: str | None = None,
        until: str | None = None,
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
                "created_by": created_by,
                "since": since,
                "until": until,
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

    def register_acquisition_output(
        self,
        session_id: str,
        *,
        file_path: str,
        checksum: str,
        size_bytes: int | None = None,
    ) -> LTRecord:
        payload: JsonObject = {
            "file_path": _require_non_empty(file_path, "file_path"),
            "checksum": _require_non_empty(checksum, "checksum"),
        }
        if size_bytes is not None:
            if size_bytes < 0:
                raise LTValidationError("size_bytes must be 0 or greater.")
            payload["size_bytes"] = size_bytes
        return self._data_record(
            self._request(
                "POST",
                f"/sessions/{_require_non_empty(str(session_id), 'session_id')}/outputs",
                json_payload=payload,
            )
        )

    def list_datasets(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: str | None = None,
        until: str | None = None,
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
                "created_by": created_by,
                "since": since,
                "until": until,
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
        created_by: str | None = None,
        since: str | None = None,
        until: str | None = None,
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
                "created_by": created_by,
                "since": since,
                "until": until,
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
        created_by: str | None = None,
        since: str | None = None,
        until: str | None = None,
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
                "created_by": created_by,
                "since": since,
                "until": until,
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
        since: str | None = None,
        until: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        return self._list_all(
            "/visualizations",
            params={
                "project_id": project_id,
                "analysis_id": analysis_id,
                "claim_id": claim_id,
                "since": since,
                "until": until,
            },
            limit=limit,
            offset=offset,
        )

    def provenance(self, entity_type: str, entity_id: str) -> JsonObject:
        """Fetch the PROV-O/JSON-LD provenance document for one record.

        ``entity_type`` is the plural route segment: ``datasets``, ``analyses``,
        or ``claims``. The returned document is self-contained JSON-LD, readable
        without a running Lab Tracker instance once written to disk.
        """

        allowed = {"datasets", "analyses", "claims"}
        if entity_type not in allowed:
            raise LTValidationError(
                f"provenance entity_type must be one of {sorted(allowed)}; got {entity_type!r}."
            )
        return self._request("GET", f"/{entity_type}/{entity_id}/provenance")

    def list_goals(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> list[LTRecord]:
        path = "/goals" if project_id is None else f"/projects/{project_id}/goals"
        return self._list_all(
            path,
            params={
                "status": _validate_optional_enum(
                    status,
                    field_name="goal status",
                    allowed_values=GOAL_STATUS_VALUES,
                ),
            },
            limit=limit,
            offset=offset,
        )

    def next_questions(
        self,
        *,
        project_id: str | None = None,
        limit: int = 5,
    ) -> JsonObject:
        goals: list[JsonObject] = []
        for status in OPEN_GOAL_STATUSES:
            goals.extend(self.list_goals(project_id=project_id, status=status))

        project_ids = _project_ids_for_next_question_lookup(goals, project_id)
        questions: list[JsonObject] = []
        claims: list[JsonObject] = []
        for lookup_project_id in project_ids:
            for status in OPEN_QUESTION_STATUSES:
                questions.extend(self.list_questions(project_id=lookup_project_id, status=status))
            claims.extend(self.list_claims(project_id=lookup_project_id))

        return build_next_questions_payload(goals, questions, claims, limit=limit)

    def find_project_by_name(self, name: str) -> LTRecord | None:
        cleaned = _require_non_empty(name, "name")
        for project in self._iter_all("/projects"):
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
        for question in self._iter_all("/questions", params={"project_id": str(project_id)}):
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
        for note in self._iter_all("/notes", params={"project_id": str(project_id)}):
            if first_line_marker(str(note.get("raw_content") or "")) == cleaned:
                return note
        return None

    def find_evidence_note(
        self,
        *,
        project_id: str,
        source_provider: str,
        source_external_id: str,
        content_hash: str,
    ) -> LTRecord | None:
        provider = _require_non_empty(source_provider, "source_provider")
        external_id = _require_non_empty_preserved(
            source_external_id,
            "source_external_id",
        )
        digest = _require_non_empty(content_hash, "content_hash")
        evidence_key = (provider, external_id, digest)
        for note in self._iter_all("/notes", params={"project_id": str(project_id)}):
            if _evidence_note_key(note) == evidence_key:
                return note
        return None

    def build_evidence_note_index(self, *, project_id: str) -> EvidenceNoteIndex:
        index: EvidenceNoteIndex = {}
        for note in self._iter_all("/notes", params={"project_id": str(project_id)}):
            key = _evidence_note_key(note)
            if key is not None:
                index.setdefault(key, note)
        return index

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
            existing_content = str(existing.get("raw_content") or "")
            if _canonical_note_content(existing_content) != _canonical_note_content(content):
                raise LTValidationError(
                    "Existing note with the same first-line marker has different "
                    "content. Use a unique marker or update the existing note "
                    f"explicitly. Marker: {marker!r}."
                )
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
        client_capture_id: str | None = None,
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
        if client_capture_id:
            data["client_capture_id"] = _require_non_empty(
                client_capture_id,
                "client_capture_id",
            )
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

    def upload_note_file(
        self,
        *,
        project_id: str,
        file_path: str | Path,
        metadata: Mapping[str, NoteMetadataScalar] | None = None,
        status: str = NoteStatus.STAGED.value,
        content_type: str | None = None,
        transcribed_text: str | None = None,
        client_capture_id: str | None = None,
    ) -> LTRecord:
        path = Path(file_path).expanduser()
        payload = _read_non_empty_file(
            path,
            empty_message="file_path must point to a non-empty file.",
        )
        return self._upload_note_file_payload(
            project_id=project_id,
            path=path,
            payload=payload,
            metadata=metadata,
            status=status,
            content_type=content_type,
            transcribed_text=transcribed_text,
            client_capture_id=client_capture_id,
        )

    def _upload_note_file_payload(
        self,
        *,
        project_id: str,
        path: Path,
        payload: bytes,
        metadata: Mapping[str, NoteMetadataScalar] | None = None,
        status: str = NoteStatus.STAGED.value,
        content_type: str | None = None,
        transcribed_text: str | None = None,
        client_capture_id: str | None = None,
    ) -> LTRecord:
        note, _status_code = self._upload_note_file_payload_with_status(
            project_id=project_id,
            path=path,
            payload=payload,
            metadata=metadata,
            status=status,
            content_type=content_type,
            transcribed_text=transcribed_text,
            client_capture_id=client_capture_id,
        )
        return note

    def _upload_note_file_payload_with_status(
        self,
        *,
        project_id: str,
        path: Path,
        payload: bytes,
        metadata: Mapping[str, NoteMetadataScalar] | None = None,
        status: str = NoteStatus.STAGED.value,
        content_type: str | None = None,
        transcribed_text: str | None = None,
        client_capture_id: str | None = None,
    ) -> tuple[LTRecord, int]:
        if not payload:
            raise LTValidationError("file_path must point to a non-empty file.")
        resolved_status = _validate_enum(
            status,
            field_name="note status",
            allowed_values=NOTE_STATUS_VALUES,
        )
        resolved_metadata = _validate_metadata(metadata)
        resolved_content_type = (
            content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        data: dict[str, str] = {
            "project_id": _require_non_empty(str(project_id), "project_id"),
            "status": resolved_status,
        }
        if client_capture_id:
            data["client_capture_id"] = _require_non_empty(
                client_capture_id,
                "client_capture_id",
            )
        if resolved_metadata:
            data["metadata"] = json.dumps(resolved_metadata, sort_keys=True)
        if transcribed_text:
            data["transcribed_text"] = transcribed_text
        response_payload, status_code = self._request_with_status(
            "POST",
            "/notes/upload-file",
            data=data,
            files={"file": (path.name, payload, resolved_content_type)},
        )
        return self._data_record(response_payload), status_code

    def _patch_note_metadata(
        self,
        note_id: str,
        metadata: Mapping[str, NoteMetadataScalar],
        *,
        status: str | None = None,
    ) -> LTRecord:
        payload: JsonObject = {"metadata": _validate_metadata(metadata) or {}}
        if status is not None:
            payload["status"] = _validate_enum(
                status,
                field_name="note status",
                allowed_values=NOTE_STATUS_VALUES,
            )
        return self._data_record(
            self._request(
                "PATCH",
                f"/notes/{_require_non_empty(str(note_id), 'note_id')}",
                json_payload=payload,
            )
        )

    def create_analysis_graph_draft(self, note_id: str) -> LTRecord:
        """Request a human-reviewed analysis graph draft from an evidence note."""

        return self._data_record(
            self._request(
                "POST",
                f"/notes/{_require_non_empty(str(note_id), 'note_id')}/analysis-graph-drafts",
            )
        )

    def import_evidence_file(
        self,
        *,
        project_id: str,
        file_path: str | Path,
        source_provider: str = "local-folder",
        source_external_id: str | None = None,
        source_uri: str | None = None,
        adapter: str = "lt-import-folder",
        title: str | None = None,
        metadata: Mapping[str, NoteMetadataScalar] | None = None,
        status: str = NoteStatus.STAGED.value,
        content_type: str | None = None,
        dry_run: bool = False,
        evidence_note_index: EvidenceNoteIndex | None = None,
    ) -> EvidenceImportResult:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise LTValidationError(f"Evidence path is not a file: {path}")
        payload = _read_non_empty_file(
            path,
            empty_message="Evidence file must not be empty.",
        )
        content_hash = _bytes_sha256(payload)
        resolved_source_uri = source_uri or path.as_uri()
        resolved_external_id = source_external_id or resolved_source_uri
        evidence_metadata = build_evidence_metadata(
            source_provider=source_provider,
            source_uri=resolved_source_uri,
            source_external_id=resolved_external_id,
            content_hash=content_hash,
            capture_kind="file",
            adapter=adapter,
            title=title or path.name,
            metadata=metadata,
        )
        evidence_key = (
            str(evidence_metadata["evidence_source_provider"]),
            str(evidence_metadata["evidence_source_external_id"]),
            str(evidence_metadata["evidence_content_hash"]),
        )
        if evidence_note_index is not None:
            existing = evidence_note_index.get(evidence_key)
        else:
            existing = self.find_evidence_note(
                project_id=project_id,
                source_provider=evidence_key[0],
                source_external_id=evidence_key[1],
                content_hash=evidence_key[2],
            )
        if existing is not None:
            return EvidenceImportResult(
                action="skipped",
                path=str(path),
                source_external_id=resolved_external_id,
                source_uri=resolved_source_uri,
                content_hash=content_hash,
                metadata=evidence_metadata,
                note=existing,
                reason="duplicate",
            )
        if dry_run:
            return EvidenceImportResult(
                action="skipped",
                path=str(path),
                source_external_id=resolved_external_id,
                source_uri=resolved_source_uri,
                content_hash=content_hash,
                metadata=evidence_metadata,
                reason="dry_run",
            )
        note = self._upload_note_file_payload(
            project_id=project_id,
            path=path,
            payload=payload,
            metadata=evidence_metadata,
            status=status,
            content_type=content_type,
        )
        if evidence_note_index is not None:
            evidence_note_index[evidence_key] = note
        return EvidenceImportResult(
            action="imported",
            path=str(path),
            source_external_id=resolved_external_id,
            source_uri=resolved_source_uri,
            content_hash=content_hash,
            metadata=evidence_metadata,
            note=note,
        )

    def _iter_all(
        self,
        path: str,
        *,
        params: JsonObject | None = None,
        offset: int = 0,
    ) -> Iterator[LTRecord]:
        current_offset = _validate_offset(offset)
        while True:
            payload = self._request(
                "GET",
                path,
                params={
                    **(params or {}),
                    "limit": MAX_PAGE_SIZE,
                    "offset": current_offset,
                },
            )
            page_items = payload.get("data")
            if not isinstance(page_items, list):
                raise LTAPIError(f"{path} response did not include a list data field.")
            for item in page_items:
                yield _record(item)
            meta = payload.get("meta")
            returned_count = len(page_items)
            if not isinstance(meta, dict):
                if returned_count < MAX_PAGE_SIZE:
                    break
                current_offset += returned_count
                continue
            total = int(meta.get("total") or 0)
            current_offset += returned_count
            if current_offset >= total or returned_count == 0:
                break

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
            remaining = page_size - len(items)
            if remaining <= 0:
                break
            payload = self._request(
                "GET",
                path,
                params={
                    **(params or {}),
                    "limit": min(page_size, remaining),
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
            returned_count = len(page_items)
            current_offset += returned_count
            if len(items) >= page_size or current_offset >= total or returned_count == 0:
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
        payload, _status_code = self._request_with_status(
            method,
            path,
            authenticated=authenticated,
            params=params,
            json_payload=json_payload,
            data=data,
            files=files,
            retry_on_unauthorized=retry_on_unauthorized,
        )
        return payload

    def _request_with_status(
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
    ) -> tuple[JsonObject, int]:
        headers: dict[str, str] = {"X-LabTracker-Surface": "cli"}
        supplied_token_used = bool(self._access_token and self._supplied_access_token)
        if authenticated:
            token = self._bearer_token(required=False)
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
        response = self._send(
            method,
            path,
            params=_drop_empty(params),
            json=_drop_empty(json_payload),
            data=data,
            files=files,
            headers=headers,
        )
        if response.status_code == 401 and authenticated and retry_on_unauthorized:
            if supplied_token_used and not self._has_login_credentials():
                raise LTAPIError(
                    "LAB_TRACKER_ACCESS_TOKEN was rejected by the Lab Tracker API. "
                    "Refresh the token or set LAB_TRACKER_USERNAME and "
                    "LAB_TRACKER_PASSWORD so the client can log in."
                )
            self._access_token = None
            self._supplied_access_token = False
            token = self._bearer_token(required=True)
            headers["Authorization"] = f"Bearer {token}"
            response = self._send(
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
        return _response_json(response), response.status_code

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise LTAPIError(f"Lab Tracker request {method} {path} failed: {exc}") from exc

    def _has_login_credentials(self) -> bool:
        return bool((self.username or "").strip() and self.password)

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
        response = self._send(
            "POST",
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
        self._supplied_access_token = False
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


def _canonical_note_content(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n").strip()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    resolved = Path(path).expanduser()
    try:
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise LTValidationError(f"Could not read evidence file {resolved}: {exc}") from exc
    return digest.hexdigest()


def _bytes_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_non_empty_file(path: Path, *, empty_message: str) -> bytes:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise LTValidationError(f"Could not read evidence file {path}: {exc}") from exc
    if not payload:
        raise LTValidationError(empty_message)
    return payload


def build_evidence_metadata(
    *,
    source_provider: str,
    source_uri: str,
    source_external_id: str,
    content_hash: str,
    capture_kind: str,
    adapter: str,
    title: str,
    observed_at: str | datetime | None = None,
    metadata: Mapping[str, NoteMetadataScalar] | None = None,
) -> dict[str, NoteMetadataScalar]:
    resolved_metadata = dict(_validate_metadata(metadata) or {})
    observed = observed_at
    if isinstance(observed, datetime):
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed = observed.astimezone(timezone.utc).isoformat()
    elif observed is None:
        observed = datetime.now(timezone.utc).isoformat()
    resolved_metadata.update(
        {
            "evidence_source_provider": _require_non_empty(
                source_provider,
                "source_provider",
            ),
            "evidence_source_uri": _require_non_empty(source_uri, "source_uri"),
            "evidence_source_external_id": _require_non_empty_preserved(
                source_external_id,
                "source_external_id",
            ),
            "evidence_source_observed_at": _require_non_empty(
                str(observed),
                "observed_at",
            ),
            "evidence_capture_kind": _require_non_empty(capture_kind, "capture_kind"),
            "evidence_content_hash": _require_non_empty(content_hash, "content_hash"),
            "evidence_adapter": _require_non_empty(adapter, "adapter"),
            "evidence_title": _require_non_empty(title, "title"),
        }
    )
    return resolved_metadata


def evidence_note_matches(
    note: Mapping[str, Any],
    *,
    source_provider: str,
    source_external_id: str,
    content_hash: str,
) -> bool:
    return _evidence_note_key(note) == (source_provider, source_external_id, content_hash)


def _evidence_note_key(note: Mapping[str, Any]) -> EvidenceNoteKey | None:
    metadata = note.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    provider = str(metadata.get("evidence_source_provider") or "")
    external_id = str(metadata.get("evidence_source_external_id") or "")
    content_hash = str(metadata.get("evidence_content_hash") or "")
    if not provider or not external_id or not content_hash:
        return None
    return provider, external_id, content_hash


def ids(path: str | Path = "lt_ids.json") -> dict[str, str]:
    resolved = Path(path)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LTError(f"{resolved} not found.") from exc
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise LTValidationError(f"{resolved} must contain a JSON object of string ids.")
    return payload


def client_from_env() -> LabTracker:
    return LabTracker.from_env()


_default_client_instance: LabTracker | None = None


def _default_client() -> LabTracker:
    global _default_client_instance
    if _default_client_instance is None:
        _default_client_instance = client_from_env()
    return _default_client_instance


class _LazyLabTrackerClient:
    def __getattr__(self, name: str) -> Any:
        return getattr(_default_client(), name)

    def __enter__(self) -> LabTracker:
        return _default_client().__enter__()

    def __exit__(self, *exc_info: object) -> None:
        return _default_client().__exit__(*exc_info)


client = _LazyLabTrackerClient()


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


def register_acquisition_output(session_id: str, **kwargs: Any) -> LTRecord:
    return client.register_acquisition_output(session_id, **kwargs)


def list_datasets(**kwargs: Any) -> list[LTRecord]:
    return client.list_datasets(**kwargs)


def list_analyses(**kwargs: Any) -> list[LTRecord]:
    return client.list_analyses(**kwargs)


def list_claims(**kwargs: Any) -> list[LTRecord]:
    return client.list_claims(**kwargs)


def list_visualizations(**kwargs: Any) -> list[LTRecord]:
    return client.list_visualizations(**kwargs)


def list_goals(**kwargs: Any) -> list[LTRecord]:
    return client.list_goals(**kwargs)


def next_questions(**kwargs: Any) -> JsonObject:
    return client.next_questions(**kwargs)


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


def find_evidence_note(**kwargs: Any) -> LTRecord | None:
    return client.find_evidence_note(**kwargs)


def upload_note_file(**kwargs: Any) -> LTRecord:
    return client.upload_note_file(**kwargs)


def import_evidence_file(**kwargs: Any) -> EvidenceImportResult:
    return client.import_evidence_file(**kwargs)


def create_analysis_graph_draft(note_id: str) -> LTRecord:
    return client.create_analysis_graph_draft(note_id)


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


def _project_ids_for_next_question_lookup(
    goals: list[JsonObject],
    project_id: str | None,
) -> list[str | None]:
    if project_id is not None:
        return [project_id]
    goal_project_ids = sorted(
        {str(goal["project_id"]) for goal in goals if goal.get("project_id") is not None}
    )
    return goal_project_ids or [None]


def _require_non_empty(value: str, field_name: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise LTValidationError(f"{field_name} must not be empty.")
    return cleaned


def _require_non_empty_preserved(value: str, field_name: str) -> str:
    resolved = str(value)
    if not resolved.strip():
        raise LTValidationError(f"{field_name} must not be empty.")
    return resolved


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
        raise LTValidationError(f"Invalid {field_name} {value!r}. Allowed values: {allowed_text}.")
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
            raise LTValidationError("metadata values must be strings, numbers, or booleans.")
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
