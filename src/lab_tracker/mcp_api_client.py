"""API-backed MCP client helpers for Lab Tracker MCP tools."""

from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
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
    GoalLinkStatus,
    GoalStatus,
    GoalType,
    NoteMetadataScalar,
    NoteStatus,
    QuestionStatus,
)

JsonObject = dict[str, Any]

SERVER_NAME = "lab-tracker-mcp"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 10.0
UNAVAILABLE_CODE = "lab_tracker_unavailable"
UNAVAILABLE_MESSAGE = "Lab Tracker unavailable - proceeding without graph context."
NOTE_STATUS_VALUES = tuple(status.value for status in NoteStatus)
NOTE_STATUS_TEXT = ", ".join(NOTE_STATUS_VALUES)
QUESTION_STATUS_VALUES = tuple(status.value for status in QuestionStatus)
QUESTION_STATUS_TEXT = ", ".join(QUESTION_STATUS_VALUES)
DATASET_STATUS_VALUES = tuple(status.value for status in DatasetStatus)
DATASET_STATUS_TEXT = ", ".join(DATASET_STATUS_VALUES)
ANALYSIS_STATUS_VALUES = tuple(status.value for status in AnalysisStatus)
ANALYSIS_STATUS_TEXT = ", ".join(ANALYSIS_STATUS_VALUES)
CLAIM_STATUS_VALUES = tuple(status.value for status in ClaimStatus)
CLAIM_STATUS_TEXT = ", ".join(CLAIM_STATUS_VALUES)
GOAL_STATUS_VALUES = tuple(status.value for status in GoalStatus)
GOAL_STATUS_TEXT = ", ".join(GOAL_STATUS_VALUES)
GOAL_TYPE_VALUES = tuple(goal_type.value for goal_type in GoalType)
GOAL_TYPE_TEXT = ", ".join(GOAL_TYPE_VALUES)
GOAL_LINK_STATUS_VALUES = tuple(status.value for status in GoalLinkStatus)
GOAL_LINK_STATUS_TEXT = ", ".join(GOAL_LINK_STATUS_VALUES)


class LabTrackerAPIError(RuntimeError):
    """Raised when the Lab Tracker API returns an unusable response."""


@dataclass(frozen=True)
class MCPSettings:
    base_url: str = DEFAULT_BASE_URL
    username: str | None = None
    password: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> MCPSettings:
        return cls(
            base_url=os.getenv("LAB_TRACKER_MCP_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            username=os.getenv("LAB_TRACKER_MCP_USERNAME"),
            password=os.getenv("LAB_TRACKER_MCP_PASSWORD"),
            timeout_seconds=float(
                os.getenv("LAB_TRACKER_MCP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
            ),
        )


class LabTrackerAPIClient:
    """Small API client with service-login auth and one 401 retry."""

    def __init__(
        self,
        settings: MCPSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._access_token: str | None = None
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            transport=transport,
        )

    @property
    def access_token(self) -> str | None:
        return self._access_token

    def close(self) -> None:
        self._client.close()

    def health(self) -> JsonObject:
        return self._request("GET", "/health", authenticated=False)

    def readiness(self) -> JsonObject:
        return self._request("GET", "/readiness", authenticated=False)

    def describe_schema(self, *, entity_type: str | None = None) -> JsonObject:
        return self._request(
            "GET",
            "/schema/describe",
            params={"entity_type": entity_type},
        )

    def list_projects(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            "/projects",
            params={"status": status, "limit": limit, "offset": offset},
        )

    def list_questions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        parent_question_id: str | None = None,
        ancestor_question_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            "/questions",
            params={
                "project_id": project_id,
                "status": status,
                "question_type": question_type,
                "search": search,
                "created_by": created_by,
                "parent_question_id": parent_question_id,
                "ancestor_question_id": ancestor_question_id,
                "limit": limit,
                "offset": offset,
            },
        )

    def list_notes(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            "/notes",
            params={
                "project_id": project_id,
                "status": status,
                "created_by": created_by,
                "target_entity_type": target_entity_type,
                "target_entity_id": target_entity_id,
                "limit": limit,
                "offset": offset,
            },
        )

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        goal_id: str | None = None,
        include: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            "/search",
            params={
                "q": query,
                "project_id": project_id,
                "goal_id": goal_id,
                "include": include,
                "limit": limit,
                "offset": offset,
            },
        )

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            "/sessions",
            params={
                "project_id": project_id,
                "status": status,
                "session_type": session_type,
                "limit": limit,
                "offset": offset,
            },
        )

    def list_datasets(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            "/datasets",
            params={
                "project_id": project_id,
                "status": status,
                "created_by": created_by,
                "limit": limit,
                "offset": offset,
            },
        )

    def list_analyses(
        self,
        *,
        project_id: str | None = None,
        dataset_id: str | None = None,
        question_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            "/analyses",
            params={
                "project_id": project_id,
                "dataset_id": dataset_id,
                "question_id": question_id,
                "status": status,
                "created_by": created_by,
                "limit": limit,
                "offset": offset,
            },
        )

    def list_claims(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        dataset_id: str | None = None,
        analysis_id: str | None = None,
        created_by: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            "/claims",
            params={
                "project_id": project_id,
                "status": status,
                "dataset_id": dataset_id,
                "analysis_id": analysis_id,
                "created_by": created_by,
                "limit": limit,
                "offset": offset,
            },
        )

    def list_visualizations(
        self,
        *,
        project_id: str | None = None,
        analysis_id: str | None = None,
        claim_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            "/visualizations",
            params={
                "project_id": project_id,
                "analysis_id": analysis_id,
                "claim_id": claim_id,
                "limit": limit,
                "offset": offset,
            },
        )

    def list_goals(
        self,
        *,
        project_id: str | None = None,
        goal_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        path = "/goals" if project_id is None else f"/projects/{project_id}/goals"
        return self._request(
            "GET",
            path,
            params={
                "goal_type": _validate_goal_type(goal_type),
                "status": _validate_goal_status(status),
                "limit": limit,
                "offset": offset,
            },
        )

    def get_goal(self, goal_id: str) -> JsonObject:
        return self._request("GET", f"/goals/{goal_id}")

    def publication_readiness(self, project_id: str) -> JsonObject:
        return self._request("GET", f"/projects/{project_id}/publication-readiness")

    def get_dataset_provenance(self, dataset_id: str) -> JsonObject:
        return self._request("GET", f"/datasets/{dataset_id}/provenance")

    def get_analysis_provenance(self, analysis_id: str) -> JsonObject:
        return self._request("GET", f"/analyses/{analysis_id}/provenance")

    def get_decision_context(
        self,
        *,
        task_kind: str,
        query: str,
        project_id: str | None = None,
        question_id: str | None = None,
        dataset_id: str | None = None,
        analysis_id: str | None = None,
        claim_id: str | None = None,
        visualization_id: str | None = None,
        limit: int = 20,
    ) -> JsonObject:
        try:
            return self._request(
                "POST",
                "/assistant/decision-context",
                json_payload={
                    "task_kind": task_kind,
                    "query": query,
                    "project_id": project_id,
                    "question_id": question_id,
                    "dataset_id": dataset_id,
                    "analysis_id": analysis_id,
                    "claim_id": claim_id,
                    "visualization_id": visualization_id,
                    "limit": limit,
                },
            )
        except (LabTrackerAPIError, httpx.HTTPError) as exc:
            return lab_tracker_unavailable(
                "lab_tracker_get_decision_context",
                detail=str(exc),
            )

    def next_questions(
        self,
        *,
        project_id: str | None = None,
        limit: int = 5,
    ) -> JsonObject:
        goals: list[JsonObject] = []
        for status in OPEN_GOAL_STATUSES:
            payload = self.list_goals(project_id=project_id, status=status, limit=200)
            goals.extend(_payload_items(payload))

        project_ids = _project_ids_for_next_question_lookup(goals, project_id)
        questions: list[JsonObject] = []
        claims: list[JsonObject] = []
        for lookup_project_id in project_ids:
            for status in OPEN_QUESTION_STATUSES:
                payload = self.list_questions(
                    project_id=lookup_project_id,
                    status=status,
                    limit=200,
                )
                questions.extend(_payload_items(payload))
            claims.extend(
                _payload_items(self.list_claims(project_id=lookup_project_id, limit=200))
            )

        return build_next_questions_payload(goals, questions, claims, limit=limit)

    def create_project(
        self,
        *,
        name: str,
        description: str | None = None,
        status: str | None = None,
    ) -> JsonObject:
        return self._request(
            "POST",
            "/projects",
            json_payload={
                "name": name,
                "description": description,
                "status": status,
            },
        )

    def create_question(
        self,
        *,
        project_id: str,
        text: str,
        question_type: str = "other",
        hypothesis: str | None = None,
        status: str | None = None,
        parent_question_ids: list[str] | None = None,
    ) -> JsonObject:
        return self._request(
            "POST",
            "/questions",
            json_payload={
                "project_id": project_id,
                "text": text,
                "question_type": question_type,
                "hypothesis": hypothesis,
                "status": status,
                "parent_question_ids": parent_question_ids,
            },
        )

    def refactor_question(
        self,
        *,
        question_id: str,
        replacement_text: str,
        replacement_question_type: str,
        replacement_status: str,
        reason: str,
        replacement_hypothesis: str | None = None,
        replacement_parent_question_ids: list[str] | None = None,
        child_question_ids_to_reparent: list[str] | None = None,
        note_ids_to_retarget: list[str] | None = None,
    ) -> JsonObject:
        return self._request(
            "POST",
            f"/questions/{question_id}/refactor",
            json_payload={
                "replacement": {
                    "text": replacement_text,
                    "question_type": replacement_question_type,
                    "hypothesis": replacement_hypothesis,
                    "status": replacement_status,
                    "parent_question_ids": replacement_parent_question_ids,
                },
                "reason": reason,
                "child_question_ids_to_reparent": child_question_ids_to_reparent or [],
                "note_ids_to_retarget": note_ids_to_retarget or [],
            },
        )

    def list_question_refactors(
        self,
        *,
        question_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            f"/questions/{question_id}/refactors",
            params={"limit": limit, "offset": offset},
        )

    def create_note(
        self,
        *,
        project_id: str,
        raw_content: str,
        transcribed_text: str | None = None,
        targets: list[dict[str, str]] | None = None,
        metadata: dict[str, NoteMetadataScalar] | None = None,
        status: str | None = None,
    ) -> JsonObject:
        resolved_status = _validate_note_status(status)
        resolved_metadata = _validate_note_metadata(metadata)
        return self._request(
            "POST",
            "/notes",
            json_payload={
                "project_id": project_id,
                "raw_content": raw_content,
                "transcribed_text": transcribed_text,
                "targets": targets,
                "metadata": resolved_metadata,
                "status": resolved_status,
            },
        )

    def create_dataset(
        self,
        *,
        project_id: str,
        primary_question_id: str,
        secondary_question_ids: list[str] | None = None,
        commit_manifest: JsonObject | None = None,
        commit_hash: str | None = None,
        status: str | None = "staged",
    ) -> JsonObject:
        resolved_status = _validate_dataset_status(status)
        return self._request(
            "POST",
            "/datasets",
            json_payload={
                "project_id": project_id,
                "primary_question_id": primary_question_id,
                "secondary_question_ids": secondary_question_ids,
                "commit_manifest": commit_manifest,
                "commit_hash": commit_hash,
                "status": resolved_status,
            },
        )

    def create_analysis(
        self,
        *,
        project_id: str,
        dataset_ids: list[str],
        method_hash: str,
        code_version: str,
        environment_hash: str | None = None,
        status: str | None = "staged",
    ) -> JsonObject:
        resolved_status = _validate_analysis_status(status)
        return self._request(
            "POST",
            "/analyses",
            json_payload={
                "project_id": project_id,
                "dataset_ids": dataset_ids,
                "method_hash": method_hash,
                "code_version": code_version,
                "environment_hash": environment_hash,
                "status": resolved_status,
            },
        )

    def create_claim(
        self,
        *,
        project_id: str,
        statement: str,
        confidence: float,
        status: str | None = "proposed",
        supported_by_dataset_ids: list[str] | None = None,
        supported_by_analysis_ids: list[str] | None = None,
        answers_question_ids: list[str] | None = None,
    ) -> JsonObject:
        resolved_status = _validate_claim_status(status)
        return self._request(
            "POST",
            "/claims",
            json_payload={
                "project_id": project_id,
                "statement": statement,
                "confidence": confidence,
                "status": resolved_status,
                "supported_by_dataset_ids": supported_by_dataset_ids,
                "supported_by_analysis_ids": supported_by_analysis_ids,
                "answers_question_ids": answers_question_ids,
            },
        )

    def create_visualization(
        self,
        *,
        analysis_id: str,
        viz_type: str,
        file_path: str,
        caption: str | None = None,
        related_claim_ids: list[str] | None = None,
    ) -> JsonObject:
        return self._request(
            "POST",
            "/visualizations",
            json_payload={
                "analysis_id": analysis_id,
                "viz_type": viz_type,
                "file_path": file_path,
                "caption": caption,
                "related_claim_ids": related_claim_ids,
            },
        )

    def create_goal(
        self,
        *,
        project_id: str,
        goal_type: str,
        title: str,
        summary: str | None = None,
        status: str | None = "planned",
        target_date: str | None = None,
        external_ref: str | None = None,
        attributes: JsonObject | None = None,
    ) -> JsonObject:
        return self._request(
            "POST",
            f"/projects/{project_id}/goals",
            json_payload={
                "goal_type": _validate_goal_type(goal_type),
                "title": title,
                "summary": summary,
                "status": _validate_goal_status(status),
                "target_date": target_date,
                "external_ref": external_ref,
                "attributes": attributes,
            },
        )

    def update_goal(
        self,
        *,
        goal_id: str,
        goal_type: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        status: str | None = None,
        target_date: str | None = None,
        external_ref: str | None = None,
        attributes: JsonObject | None = None,
    ) -> JsonObject:
        return self._request(
            "PATCH",
            f"/goals/{goal_id}",
            json_payload={
                "goal_type": _validate_goal_type(goal_type),
                "title": title,
                "summary": summary,
                "status": _validate_goal_status(status),
                "target_date": target_date,
                "external_ref": external_ref,
                "attributes": attributes,
            },
        )

    def link_node_to_goal(
        self,
        *,
        goal_id: str,
        entity_type: str,
        entity_id: str,
        relation: str,
        link_status: str | None = "candidate",
        slot: str | None = None,
    ) -> JsonObject:
        return self._request(
            "POST",
            f"/goals/{goal_id}/links",
            json_payload={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "relation": relation,
                "link_status": _validate_goal_link_status(link_status),
                "slot": slot,
            },
        )

    def list_node_goals(
        self,
        *,
        project_id: str,
        entity_type: str,
        entity_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            f"/projects/{project_id}/nodes/{entity_type}/{entity_id}/goals",
            params={"limit": limit, "offset": offset},
        )

    def upload_visualization_file(
        self,
        *,
        viz_id: str,
        file_path: str,
        content_type: str | None = None,
    ) -> JsonObject:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise LabTrackerAPIError(f"Visualization file does not exist: {file_path}")
        resolved_content_type = (
            content_type
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )
        return self._request(
            "POST",
            f"/visualizations/{viz_id}/file",
            files={"file": (path.name, path.read_bytes(), resolved_content_type)},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        params: JsonObject | None = None,
        json_payload: JsonObject | None = None,
        files: dict[str, Any] | None = None,
        retry_on_unauthorized: bool = True,
    ) -> JsonObject:
        headers: dict[str, str] = {}
        if authenticated and self._has_credentials():
            headers["Authorization"] = f"Bearer {self._token()}"
        response = self._client.request(
            method,
            path,
            params=_drop_empty(params),
            json=_drop_empty(json_payload),
            files=files,
            headers=headers,
        )
        if response.status_code == 401 and authenticated and retry_on_unauthorized:
            self._access_token = None
            if not self._has_credentials():
                raise LabTrackerAPIError(
                    "LAB_TRACKER_MCP_USERNAME and LAB_TRACKER_MCP_PASSWORD are required "
                    "when the Lab Tracker API has authentication enabled."
                )
            headers["Authorization"] = f"Bearer {self._token()}"
            response = self._client.request(
                method,
                path,
                params=_drop_empty(params),
                json=_drop_empty(json_payload),
                files=files,
                headers=headers,
            )
        if response.status_code >= 400:
            raise LabTrackerAPIError(_response_error(response))
        return _response_json(response)

    def _has_credentials(self) -> bool:
        return bool((self._settings.username or "").strip() and self._settings.password)

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        username = (self._settings.username or "").strip()
        password = self._settings.password or ""
        if not username or not password:
            raise LabTrackerAPIError(
                "LAB_TRACKER_MCP_USERNAME and LAB_TRACKER_MCP_PASSWORD are required "
                "for authenticated Lab Tracker MCP tools."
            )
        response = self._client.post(
            "/auth/login",
            json={"username": username, "password": password},
        )
        if response.status_code >= 400:
            raise LabTrackerAPIError(_response_error(response))
        payload = _response_json(response)
        try:
            token = str(payload["data"]["access_token"])
        except (KeyError, TypeError) as exc:
            raise LabTrackerAPIError("Login response did not include an access token.") from exc
        self._access_token = token
        return token


def _drop_empty(payload: JsonObject | None) -> JsonObject | None:
    if payload is None:
        return None
    return {key: value for key, value in payload.items() if value is not None}


def _is_note_metadata_scalar(value: object) -> bool:
    return isinstance(value, (str, bool, int, float))


def _validate_note_metadata(metadata: object) -> dict[str, NoteMetadataScalar] | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise LabTrackerAPIError(
            "Note metadata must be an object with string keys and string, number, "
            "or boolean values."
        )
    validated: dict[str, NoteMetadataScalar] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not key.strip():
            raise LabTrackerAPIError("Note metadata keys must be non-empty strings.")
        if not _is_note_metadata_scalar(value):
            raise LabTrackerAPIError(
                "Note metadata values must be strings, numbers, or booleans."
            )
        validated[key] = value
    return validated


def _validate_note_status(status: str | None) -> str | None:
    return _validate_status(
        status,
        label="note status",
        allowed_values=NOTE_STATUS_VALUES,
        allowed_text=NOTE_STATUS_TEXT,
    )


def _validate_dataset_status(status: str | None) -> str | None:
    return _validate_status(
        status,
        label="dataset status",
        allowed_values=DATASET_STATUS_VALUES,
        allowed_text=DATASET_STATUS_TEXT,
    )


def _validate_analysis_status(status: str | None) -> str | None:
    return _validate_status(
        status,
        label="analysis status",
        allowed_values=ANALYSIS_STATUS_VALUES,
        allowed_text=ANALYSIS_STATUS_TEXT,
    )


def _validate_claim_status(status: str | None) -> str | None:
    return _validate_status(
        status,
        label="claim status",
        allowed_values=CLAIM_STATUS_VALUES,
        allowed_text=CLAIM_STATUS_TEXT,
    )


def _validate_goal_status(status: str | None) -> str | None:
    return _validate_status(
        status,
        label="goal status",
        allowed_values=GOAL_STATUS_VALUES,
        allowed_text=GOAL_STATUS_TEXT,
    )


def _validate_goal_type(goal_type: str | None) -> str | None:
    return _validate_status(
        goal_type,
        label="goal type",
        allowed_values=GOAL_TYPE_VALUES,
        allowed_text=GOAL_TYPE_TEXT,
    )


def _validate_goal_link_status(status: str | None) -> str | None:
    return _validate_status(
        status,
        label="goal link status",
        allowed_values=GOAL_LINK_STATUS_VALUES,
        allowed_text=GOAL_LINK_STATUS_TEXT,
    )


def _validate_status(
    status: str | None,
    *,
    label: str,
    allowed_values: tuple[str, ...],
    allowed_text: str,
) -> str | None:
    if status is None:
        return None
    cleaned = status.strip().lower()
    if cleaned not in allowed_values:
        plural_label = label.replace("status", "statuses")
        raise LabTrackerAPIError(
            f"Invalid {label} {status!r}. Allowed {plural_label}: {allowed_text}."
        )
    return cleaned


def lab_tracker_unavailable(operation: str, **metadata: object) -> JsonObject:
    error: JsonObject = {
        "code": UNAVAILABLE_CODE,
        "message": UNAVAILABLE_MESSAGE,
        "operation": operation,
    }
    error.update(metadata)
    return {
        "error": error,
        "data": None,
        "next_action": {
            "action": "proceed_without_graph_context",
            "tool": None,
            "arguments": {},
            "reason": (
                "State that Lab Tracker is unavailable, then continue without graph "
                "context instead of retrying indefinitely."
            ),
        },
    }


def _payload_items(payload: JsonObject) -> list[JsonObject]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise LabTrackerAPIError("Lab Tracker API response did not include list data.")
    return [item for item in data if isinstance(item, dict)]


def _project_ids_for_next_question_lookup(
    goals: list[JsonObject],
    project_id: str | None,
) -> list[str | None]:
    if project_id is not None:
        return [project_id]
    goal_project_ids = sorted(
        {
            str(goal["project_id"])
            for goal in goals
            if goal.get("project_id") is not None
        }
    )
    return goal_project_ids or [None]


def _response_json(response: httpx.Response) -> JsonObject:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LabTrackerAPIError("Lab Tracker API returned non-JSON content.") from exc
    if not isinstance(payload, dict):
        raise LabTrackerAPIError("Lab Tracker API returned a non-object JSON payload.")
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
        if payload.get("detail"):
            return str(payload["detail"])
    return f"Lab Tracker API returned HTTP {response.status_code}: {payload}"


def client_from_env() -> LabTrackerAPIClient:
    return LabTrackerAPIClient(MCPSettings.from_env())
