"""API-backed MCP server for Lab Tracker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from lab_tracker.models import NoteMetadataScalar, NoteStatus, QuestionStatus

JsonObject = dict[str, Any]

SERVER_NAME = "lab-tracker-mcp"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 10.0
NOTE_STATUS_VALUES = tuple(status.value for status in NoteStatus)
NOTE_STATUS_TEXT = ", ".join(NOTE_STATUS_VALUES)
QUESTION_STATUS_VALUES = tuple(status.value for status in QuestionStatus)
QUESTION_STATUS_TEXT = ", ".join(QUESTION_STATUS_VALUES)
TASK_KIND_VALUES = (
    "plot",
    "analysis",
    "slides",
    "experiment_plan",
    "summary",
    "research_writing",
)
TASK_KIND_TEXT = ", ".join(TASK_KIND_VALUES)
CONTEXT_LOOKUP_LIMIT = 500

AGENT_CONSULTATION_POLICY = """# Lab Tracker Agent Consultation Policy

Before research-facing decisions, consult the Lab Tracker MCP server. This
includes choosing variables to plot, analyses to run, figures or slides to make,
experimental controls to prioritize, summaries to write, and research writing
such as manuscripts, grants, abstracts, results, discussion text, and figure
legends.

Prefer `lab_tracker_get_decision_context` when available. Otherwise use
`lab_tracker_list_projects`, `lab_tracker_search`, `lab_tracker_list_questions`,
`lab_tracker_list_notes`, and the low-level dataset, analysis, claim, and
visualization read tools.

If Lab Tracker is unavailable or ambiguous, state that explicitly. Do not create
or mutate Lab Tracker records unless the user explicitly asks.
"""


class LabTrackerAPIError(RuntimeError):
    """Raised when the Lab Tracker API returns an unusable response."""


@dataclass(frozen=True)
class MCPSettings:
    base_url: str = DEFAULT_BASE_URL
    username: str | None = None
    password: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "MCPSettings":
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
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            "/datasets",
            params={
                "project_id": project_id,
                "status": status,
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
            return _build_decision_context(
                self,
                task_kind=task_kind,
                query=query,
                project_id=project_id,
                question_id=question_id,
                dataset_id=dataset_id,
                analysis_id=analysis_id,
                claim_id=claim_id,
                visualization_id=visualization_id,
                limit=limit,
            )
        except (LabTrackerAPIError, httpx.HTTPError) as exc:
            return _decision_error(
                "unavailable",
                "Lab Tracker decision context is unavailable.",
                detail=str(exc),
            )

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
                "metadata": resolved_metadata,
                "status": resolved_status,
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        params: JsonObject | None = None,
        json_payload: JsonObject | None = None,
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
    if status is None:
        return None
    cleaned = status.strip().lower()
    if cleaned not in NOTE_STATUS_VALUES:
        raise LabTrackerAPIError(
            f"Invalid note status {status!r}. Allowed note statuses: {NOTE_STATUS_TEXT}."
        )
    return cleaned


def _validate_context_limit(limit: int) -> int:
    try:
        resolved = int(limit)
    except (TypeError, ValueError):
        return 20
    return min(max(resolved, 1), 100)


def _decision_error(code: str, message: str, **metadata: object) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    error.update(metadata)
    return {"error": error}


def _envelope_items(payload: JsonObject) -> list[JsonObject]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [dict(item) for item in data if isinstance(item, dict)]


def _search_items(payload: JsonObject, key: str) -> list[JsonObject]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get(key)
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def _meta_total(payload: JsonObject, fallback_key: str | None = None) -> int | None:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("total")
    if value is None and fallback_key is not None:
        value = meta.get(fallback_key)
    return value if isinstance(value, int) else None


def _find_by_id(items: list[JsonObject], id_key: str, entity_id: str) -> JsonObject | None:
    return next((item for item in items if str(item.get(id_key)) == entity_id), None)


def _entity_label(entity_type: str, entity: JsonObject) -> str:
    if entity_type == "project":
        return str(entity.get("name") or entity.get("project_id") or "")
    if entity_type == "question":
        return str(entity.get("text") or entity.get("question_id") or "")
    if entity_type == "note":
        text = str(
            entity.get("transcribed_text")
            or entity.get("raw_content")
            or entity.get("note_id")
            or ""
        )
        return text[:120]
    if entity_type == "dataset":
        return str(entity.get("commit_hash") or entity.get("dataset_id") or "")
    if entity_type == "session":
        return str(entity.get("link_code") or entity.get("session_id") or "")
    if entity_type == "analysis":
        return str(entity.get("method_hash") or entity.get("analysis_id") or "")
    if entity_type == "claim":
        return str(entity.get("statement") or entity.get("claim_id") or "")
    if entity_type == "visualization":
        return str(entity.get("caption") or entity.get("file_path") or entity.get("viz_id") or "")
    return str(entity)


def _entity_ref(entity_type: str, entity: JsonObject, id_key: str) -> JsonObject:
    return {
        "entity_type": entity_type,
        "entity_id": str(entity.get(id_key) or ""),
        "label": _entity_label(entity_type, entity),
    }


def _candidate_project(project: JsonObject, reason: str) -> JsonObject:
    return {
        "project_id": str(project.get("project_id") or ""),
        "name": project.get("name"),
        "status": project.get("status"),
        "reason": reason,
    }


def _project_lookup(projects: list[JsonObject]) -> dict[str, JsonObject]:
    return {
        str(project["project_id"]): project
        for project in projects
        if project.get("project_id") is not None
    }


def _project_ids_from_search(search_payload: JsonObject) -> set[str]:
    project_ids: set[str] = set()
    for question in _search_items(search_payload, "questions"):
        if question.get("project_id") is not None:
            project_ids.add(str(question["project_id"]))
    for note in _search_items(search_payload, "notes"):
        if note.get("project_id") is not None:
            project_ids.add(str(note["project_id"]))
    return project_ids


def _merge_entities(
    id_key: str,
    *groups: tuple[list[JsonObject], str],
) -> list[JsonObject]:
    merged: dict[str, JsonObject] = {}
    order: list[str] = []
    for items, reason in groups:
        for item in items:
            entity_id = item.get(id_key)
            if entity_id is None:
                continue
            key = str(entity_id)
            if key not in merged:
                next_item = dict(item)
                next_item["relevance_reasons"] = []
                merged[key] = next_item
                order.append(key)
            reasons = merged[key]["relevance_reasons"]
            if isinstance(reasons, list) and reason not in reasons:
                reasons.append(reason)
    return [merged[key] for key in order]


def _build_evidence_map(
    datasets: list[JsonObject],
    analyses: list[JsonObject],
    claims: list[JsonObject],
    visualizations: list[JsonObject],
) -> list[JsonObject]:
    evidence: list[JsonObject] = []
    for dataset in datasets:
        question_ids = [
            str(link.get("question_id"))
            for link in dataset.get("question_links", [])
            if isinstance(link, dict) and link.get("question_id")
        ]
        if dataset.get("primary_question_id"):
            question_ids.insert(0, str(dataset["primary_question_id"]))
        evidence.append(
            {
                "entity": _entity_ref("dataset", dataset, "dataset_id"),
                "questions": sorted(set(question_ids)),
                "reason": "dataset_question_links",
            }
        )
    for analysis in analyses:
        evidence.append(
            {
                "entity": _entity_ref("analysis", analysis, "analysis_id"),
                "datasets": [str(item) for item in analysis.get("dataset_ids", [])],
                "reason": "analysis_dataset_links",
            }
        )
    for claim in claims:
        evidence.append(
            {
                "entity": _entity_ref("claim", claim, "claim_id"),
                "datasets": [
                    str(item) for item in claim.get("supported_by_dataset_ids", [])
                ],
                "analyses": [
                    str(item) for item in claim.get("supported_by_analysis_ids", [])
                ],
                "reason": "claim_support_links",
            }
        )
    for visualization in visualizations:
        evidence.append(
            {
                "entity": _entity_ref("visualization", visualization, "viz_id"),
                "analysis_id": str(visualization.get("analysis_id") or ""),
                "claims": [
                    str(item) for item in visualization.get("related_claim_ids", [])
                ],
                "reason": "visualization_links",
            }
        )
    return evidence


def _task_guidance(
    task_kind: str,
    query: str,
    questions: list[JsonObject],
    datasets: list[JsonObject],
    analyses: list[JsonObject],
    claims: list[JsonObject],
    visualizations: list[JsonObject],
) -> JsonObject:
    focus = [_entity_label("question", item) for item in questions[:3]] or [query]
    caveats: list[str] = []
    missing_evidence: list[str] = []
    if not any(item.get("status") == "committed" for item in datasets):
        missing_evidence.append("No committed datasets were returned in this context.")
    if not analyses:
        missing_evidence.append("No prior analyses were returned in this context.")
    if not claims:
        missing_evidence.append("No claims were returned in this context.")
    if any(item.get("status") == "proposed" for item in claims):
        caveats.append("Some returned claims are still proposed, not supported.")
    if any(item.get("status") == "staged" for item in datasets + analyses):
        caveats.append("Some returned datasets or analyses are staged rather than committed.")

    if task_kind == "plot":
        candidate_outputs = [
            _entity_ref("dataset", item, "dataset_id") for item in datasets[:5]
        ] + [_entity_ref("visualization", item, "viz_id") for item in visualizations[:5]]
    elif task_kind == "analysis":
        candidate_outputs = [
            _entity_ref("analysis", item, "analysis_id") for item in analyses[:5]
        ] + [_entity_ref("dataset", item, "dataset_id") for item in datasets[:5]]
    elif task_kind == "slides":
        candidate_outputs = [
            _entity_ref("visualization", item, "viz_id") for item in visualizations[:5]
        ] + [_entity_ref("claim", item, "claim_id") for item in claims[:5]]
    elif task_kind == "experiment_plan":
        candidate_outputs = [
            _entity_ref("question", item, "question_id") for item in questions[:5]
        ]
    elif task_kind == "research_writing":
        candidate_outputs = [
            _entity_ref("claim", item, "claim_id") for item in claims[:5]
        ] + [_entity_ref("visualization", item, "viz_id") for item in visualizations[:5]]
    else:
        candidate_outputs = [
            _entity_ref("question", item, "question_id") for item in questions[:5]
        ]

    return {
        "recommended_focus": focus,
        "candidate_outputs": candidate_outputs,
        "caveats": caveats,
        "missing_evidence": missing_evidence,
    }


def _truncation(sections: list[tuple[str, JsonObject, str | None]]) -> JsonObject:
    truncated_sections: list[JsonObject] = []
    for name, payload, fallback_key in sections:
        returned = len(_envelope_items(payload))
        total = _meta_total(payload, fallback_key)
        if total is not None and total > returned:
            truncated_sections.append(
                {"section": name, "returned": returned, "total": total}
            )
    return {
        "was_truncated": bool(truncated_sections),
        "sections": truncated_sections,
    }


def _build_decision_context(
    client: LabTrackerAPIClient,
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
    cleaned_task_kind = (task_kind or "").strip()
    if cleaned_task_kind not in TASK_KIND_VALUES:
        return _decision_error(
            "invalid_task_kind",
            f"Invalid task kind {task_kind!r}. Allowed task kinds: {TASK_KIND_TEXT}.",
            allowed_task_kinds=list(TASK_KIND_VALUES),
        )
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        return _decision_error("invalid_query", "Decision-context query must not be empty.")

    resolved_limit = _validate_context_limit(limit)
    projects_payload = client.list_projects(limit=CONTEXT_LOOKUP_LIMIT)
    projects = _envelope_items(projects_payload)
    projects_by_id = _project_lookup(projects)

    resolved_project_id = str(project_id) if project_id else None
    if resolved_project_id and resolved_project_id not in projects_by_id:
        return _decision_error(
            "anchor_not_found",
            f"Project {resolved_project_id!r} was not found.",
            anchor={"entity_type": "project", "entity_id": resolved_project_id},
        )

    anchor_entities: dict[str, list[JsonObject]] = {
        "questions": [],
        "datasets": [],
        "analyses": [],
        "claims": [],
        "visualizations": [],
    }
    anchor_project_ids: set[str] = set()

    if question_id:
        questions = _envelope_items(
            client.list_questions(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        question = _find_by_id(questions, "question_id", str(question_id))
        if question is None:
            return _decision_error(
                "anchor_not_found",
                f"Question {question_id!r} was not found.",
                anchor={"entity_type": "question", "entity_id": str(question_id)},
            )
        anchor_entities["questions"].append(question)
        anchor_project_ids.add(str(question["project_id"]))

    if dataset_id:
        datasets = _envelope_items(
            client.list_datasets(project_id=resolved_project_id, limit=CONTEXT_LOOKUP_LIMIT)
        )
        dataset = _find_by_id(datasets, "dataset_id", str(dataset_id))
        if dataset is None:
            return _decision_error(
                "anchor_not_found",
                f"Dataset {dataset_id!r} was not found.",
                anchor={"entity_type": "dataset", "entity_id": str(dataset_id)},
            )
        anchor_entities["datasets"].append(dataset)
        anchor_project_ids.add(str(dataset["project_id"]))

    if analysis_id:
        analyses = _envelope_items(
            client.list_analyses(project_id=resolved_project_id, limit=CONTEXT_LOOKUP_LIMIT)
        )
        analysis = _find_by_id(analyses, "analysis_id", str(analysis_id))
        if analysis is None:
            return _decision_error(
                "anchor_not_found",
                f"Analysis {analysis_id!r} was not found.",
                anchor={"entity_type": "analysis", "entity_id": str(analysis_id)},
            )
        anchor_entities["analyses"].append(analysis)
        anchor_project_ids.add(str(analysis["project_id"]))

    if claim_id:
        claims = _envelope_items(
            client.list_claims(project_id=resolved_project_id, limit=CONTEXT_LOOKUP_LIMIT)
        )
        claim = _find_by_id(claims, "claim_id", str(claim_id))
        if claim is None:
            return _decision_error(
                "anchor_not_found",
                f"Claim {claim_id!r} was not found.",
                anchor={"entity_type": "claim", "entity_id": str(claim_id)},
            )
        anchor_entities["claims"].append(claim)
        anchor_project_ids.add(str(claim["project_id"]))

    if visualization_id:
        visualizations = _envelope_items(
            client.list_visualizations(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        visualization = _find_by_id(visualizations, "viz_id", str(visualization_id))
        if visualization is None:
            return _decision_error(
                "anchor_not_found",
                f"Visualization {visualization_id!r} was not found.",
                anchor={
                    "entity_type": "visualization",
                    "entity_id": str(visualization_id),
                },
            )
        anchor_entities["visualizations"].append(visualization)
        analyses = _envelope_items(
            client.list_analyses(project_id=resolved_project_id, limit=CONTEXT_LOOKUP_LIMIT)
        )
        analysis = _find_by_id(analyses, "analysis_id", str(visualization.get("analysis_id")))
        if analysis is not None:
            anchor_project_ids.add(str(analysis["project_id"]))

    if resolved_project_id:
        anchor_project_ids.add(resolved_project_id)
    if len(anchor_project_ids) > 1:
        return _decision_error(
            "conflicting_anchors",
            "Decision-context anchors resolve to multiple projects.",
            project_ids=sorted(anchor_project_ids),
        )
    if not resolved_project_id and anchor_project_ids:
        resolved_project_id = next(iter(anchor_project_ids))

    if not resolved_project_id:
        global_search_payload = client.search(cleaned_query, limit=resolved_limit)
        search_project_ids = _project_ids_from_search(global_search_payload)
        if len(search_project_ids) == 1:
            resolved_project_id = next(iter(search_project_ids))
        else:
            candidates = [
                _candidate_project(projects_by_id[item], "search_match")
                for item in sorted(search_project_ids)
                if item in projects_by_id
            ]
            if not candidates:
                candidates = [
                    _candidate_project(project, "active_project")
                    for project in projects
                    if project.get("status") == "active"
                ][:resolved_limit]
            return _decision_error(
                "ambiguous_project",
                "Decision context needs a project or a more specific anchor.",
                candidate_projects=candidates,
            )

    project = projects_by_id.get(resolved_project_id)
    if project is None:
        return _decision_error(
            "anchor_not_found",
            f"Project {resolved_project_id!r} was not found.",
            anchor={"entity_type": "project", "entity_id": resolved_project_id},
        )

    search_payload = client.search(
        cleaned_query,
        project_id=resolved_project_id,
        limit=resolved_limit,
    )
    questions_payload = client.list_questions(
        project_id=resolved_project_id,
        limit=resolved_limit,
    )
    notes_payload = client.list_notes(project_id=resolved_project_id, limit=resolved_limit)
    sessions_payload = client.list_sessions(
        project_id=resolved_project_id,
        limit=resolved_limit,
    )
    datasets_payload = client.list_datasets(
        project_id=resolved_project_id,
        limit=resolved_limit,
    )
    analyses_payload = client.list_analyses(
        project_id=resolved_project_id,
        limit=resolved_limit,
    )
    claims_payload = client.list_claims(project_id=resolved_project_id, limit=resolved_limit)
    visualizations_payload = client.list_visualizations(
        project_id=resolved_project_id,
        limit=resolved_limit,
    )

    questions = _merge_entities(
        "question_id",
        (anchor_entities["questions"], "anchor"),
        (_search_items(search_payload, "questions"), "search_match"),
        (_envelope_items(questions_payload), "recent_activity"),
    )
    notes = _merge_entities(
        "note_id",
        (_search_items(search_payload, "notes"), "search_match"),
        (_envelope_items(notes_payload), "recent_activity"),
    )
    sessions = _merge_entities(
        "session_id",
        (_envelope_items(sessions_payload), "recent_activity"),
    )
    datasets = _merge_entities(
        "dataset_id",
        (anchor_entities["datasets"], "anchor"),
        (_envelope_items(datasets_payload), "recent_activity"),
    )
    analyses = _merge_entities(
        "analysis_id",
        (anchor_entities["analyses"], "anchor"),
        (_envelope_items(analyses_payload), "recent_activity"),
    )
    claims = _merge_entities(
        "claim_id",
        (anchor_entities["claims"], "anchor"),
        (_envelope_items(claims_payload), "recent_activity"),
    )
    visualizations = _merge_entities(
        "viz_id",
        (anchor_entities["visualizations"], "anchor"),
        (_envelope_items(visualizations_payload), "recent_activity"),
    )

    anchors = [
        _entity_ref("question", item, "question_id")
        for item in anchor_entities["questions"]
    ] + [
        _entity_ref("dataset", item, "dataset_id")
        for item in anchor_entities["datasets"]
    ] + [
        _entity_ref("analysis", item, "analysis_id")
        for item in anchor_entities["analyses"]
    ] + [
        _entity_ref("claim", item, "claim_id") for item in anchor_entities["claims"]
    ] + [
        _entity_ref("visualization", item, "viz_id")
        for item in anchor_entities["visualizations"]
    ]

    evidence_map = _build_evidence_map(datasets, analyses, claims, visualizations)
    return {
        "data": {
            "task_kind": cleaned_task_kind,
            "query": cleaned_query,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scope": {
                "project": {
                    "project_id": str(project["project_id"]),
                    "name": project.get("name"),
                    "status": project.get("status"),
                },
                "anchors": anchors,
            },
            "context_summary": (
                f"Found {len(questions)} questions, {len(notes)} notes, "
                f"{len(datasets)} datasets, {len(analyses)} analyses, "
                f"{len(claims)} claims, and {len(visualizations)} visualizations "
                f"for {cleaned_task_kind}."
            ),
            "task_guidance": _task_guidance(
                cleaned_task_kind,
                cleaned_query,
                questions,
                datasets,
                analyses,
                claims,
                visualizations,
            ),
            "questions": questions,
            "notes": notes,
            "sessions": sessions,
            "datasets": datasets,
            "analyses": analyses,
            "claims": claims,
            "visualizations": visualizations,
            "evidence_map": evidence_map,
            "truncation": _truncation(
                [
                    ("questions", questions_payload, None),
                    ("notes", notes_payload, None),
                    ("sessions", sessions_payload, None),
                    ("datasets", datasets_payload, None),
                    ("analyses", analyses_payload, None),
                    ("claims", claims_payload, None),
                    ("visualizations", visualizations_payload, None),
                ]
            ),
        },
        "meta": {
            "retrieval_policy": "explicit_links_then_search_then_recency",
            "limit": resolved_limit,
        },
    }


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


server = FastMCP(SERVER_NAME)


@server.tool()
def lab_tracker_health() -> JsonObject:
    """Check the Lab Tracker API health endpoint."""
    client = client_from_env()
    try:
        return client.health()
    finally:
        client.close()


@server.tool()
def lab_tracker_readiness() -> JsonObject:
    """Check database and storage readiness for Lab Tracker."""
    client = client_from_env()
    try:
        return client.readiness()
    finally:
        client.close()


@server.tool()
def lab_tracker_list_projects(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker projects through the API."""
    client = client_from_env()
    try:
        return client.list_projects(status=status, limit=limit, offset=offset)
    finally:
        client.close()


@server.tool()
def lab_tracker_list_questions(
    project_id: str | None = None,
    status: str | None = None,
    question_type: str | None = None,
    search: str | None = None,
    parent_question_id: str | None = None,
    ancestor_question_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List/search questions; use parent or ancestor filters for hierarchy traversal."""
    client = client_from_env()
    try:
        return client.list_questions(
            project_id=project_id,
            status=status,
            question_type=question_type,
            search=search,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_list_notes(
    project_id: str | None = None,
    status: str | None = None,
    target_entity_type: str | None = None,
    target_entity_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker notes through the API."""
    client = client_from_env()
    try:
        return client.list_notes(
            project_id=project_id,
            status=status,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_search(
    query: str,
    project_id: str | None = None,
    include: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> JsonObject:
    """Search Lab Tracker questions and notes through the API."""
    client = client_from_env()
    try:
        return client.search(
            query,
            project_id=project_id,
            include=include,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_list_sessions(
    project_id: str | None = None,
    status: str | None = None,
    session_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker sessions through the API."""
    client = client_from_env()
    try:
        return client.list_sessions(
            project_id=project_id,
            status=status,
            session_type=session_type,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_list_datasets(
    project_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker datasets through the API."""
    client = client_from_env()
    try:
        return client.list_datasets(
            project_id=project_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_list_analyses(
    project_id: str | None = None,
    dataset_id: str | None = None,
    question_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker analyses through the API."""
    client = client_from_env()
    try:
        return client.list_analyses(
            project_id=project_id,
            dataset_id=dataset_id,
            question_id=question_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_list_claims(
    project_id: str | None = None,
    status: str | None = None,
    dataset_id: str | None = None,
    analysis_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker claims through the API."""
    client = client_from_env()
    try:
        return client.list_claims(
            project_id=project_id,
            status=status,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_list_visualizations(
    project_id: str | None = None,
    analysis_id: str | None = None,
    claim_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List Lab Tracker visualizations through the API."""
    client = client_from_env()
    try:
        return client.list_visualizations(
            project_id=project_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_get_dataset_provenance(dataset_id: str) -> JsonObject:
    """Get dataset provenance JSON-LD through the API."""
    client = client_from_env()
    try:
        return client.get_dataset_provenance(dataset_id)
    finally:
        client.close()


@server.tool()
def lab_tracker_get_analysis_provenance(analysis_id: str) -> JsonObject:
    """Get analysis provenance JSON-LD through the API."""
    client = client_from_env()
    try:
        return client.get_analysis_provenance(analysis_id)
    finally:
        client.close()


@server.tool()
def lab_tracker_get_decision_context(
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
    """Build bounded graph context before research-facing assistant decisions."""
    client = client_from_env()
    try:
        return client.get_decision_context(
            task_kind=task_kind,
            query=query,
            project_id=project_id,
            question_id=question_id,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            claim_id=claim_id,
            visualization_id=visualization_id,
            limit=limit,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_create_project(
    name: str,
    description: str | None = None,
    status: str | None = None,
) -> JsonObject:
    """Create a Lab Tracker project through the API."""
    client = client_from_env()
    try:
        return client.create_project(name=name, description=description, status=status)
    finally:
        client.close()


@server.tool()
def lab_tracker_create_question(
    project_id: str,
    text: str,
    question_type: str = "other",
    hypothesis: str | None = None,
    status: str | None = None,
    parent_question_ids: list[str] | None = None,
) -> JsonObject:
    """Create a Lab Tracker question through the API."""
    client = client_from_env()
    try:
        return client.create_question(
            project_id=project_id,
            text=text,
            question_type=question_type,
            hypothesis=hypothesis,
            status=status,
            parent_question_ids=parent_question_ids,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_refactor_question(
    question_id: str,
    replacement_text: str,
    replacement_question_type: str = "other",
    replacement_status: str = "staged",
    reason: str = "",
    replacement_hypothesis: str | None = None,
    replacement_parent_question_ids: list[str] | None = None,
    child_question_ids_to_reparent: list[str] | None = None,
    note_ids_to_retarget: list[str] | None = None,
) -> JsonObject:
    """Supersede a question with a replacement and optional child/note moves."""
    if replacement_status not in QUESTION_STATUS_VALUES:
        raise LabTrackerAPIError(
            f"Invalid replacement status {replacement_status!r}. "
            f"Allowed statuses: {QUESTION_STATUS_TEXT}."
        )
    client = client_from_env()
    try:
        return client.refactor_question(
            question_id=question_id,
            replacement_text=replacement_text,
            replacement_question_type=replacement_question_type,
            replacement_status=replacement_status,
            replacement_hypothesis=replacement_hypothesis,
            replacement_parent_question_ids=replacement_parent_question_ids,
            reason=reason,
            child_question_ids_to_reparent=child_question_ids_to_reparent,
            note_ids_to_retarget=note_ids_to_retarget,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_list_question_refactors(
    question_id: str,
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List refactor history where a question is the source or replacement."""
    client = client_from_env()
    try:
        return client.list_question_refactors(
            question_id=question_id,
            limit=limit,
            offset=offset,
        )
    finally:
        client.close()


@server.tool()
def lab_tracker_create_note(
    project_id: str,
    raw_content: str,
    transcribed_text: str | None = None,
    metadata: dict[str, NoteMetadataScalar] | None = None,
    status: str | None = None,
) -> JsonObject:
    """Create a text note; status must be staged, committed, or archived."""
    client = client_from_env()
    try:
        return client.create_note(
            project_id=project_id,
            raw_content=raw_content,
            transcribed_text=transcribed_text,
            metadata=metadata,
            status=status,
        )
    finally:
        client.close()


@server.resource(
    "lab-tracker://quickstart",
    name="Lab Tracker MCP Quickstart",
    mime_type="text/markdown",
)
def lab_tracker_quickstart() -> str:
    return (
        "# Lab Tracker MCP Quickstart\n\n"
        "Use `lab_tracker_health` and `lab_tracker_readiness` first. "
        "Read and write tools call the running Lab Tracker API, so start the app "
        "and set `LAB_TRACKER_MCP_BASE_URL` in the MCP client environment. "
        "Use `http://127.0.0.1:8000` only when the MCP process runs on the same "
        "machine as the API; remote agents should use the workstation HTTPS "
        "endpoint, for example `https://<workstation>.ts.net`. "
        "`LAB_TRACKER_MCP_USERNAME` and `LAB_TRACKER_MCP_PASSWORD` are only "
        "required when API authentication is enabled. Notes use `staged`, "
        "`committed`, or `archived` status. Note metadata values may be strings, "
        "numbers, or booleans and are stored as strings.\n"
    )


@server.resource(
    "lab-tracker://surface",
    name="Lab Tracker Retained V1 Surface",
    mime_type="text/markdown",
)
def lab_tracker_surface() -> str:
    return (
        "Lab Tracker v1 preserves projects, questions, notes, sessions, datasets, "
        "analyses, claims, and visualizations. The supported runtime surface is "
        "documented in `docs/retained-v1-surface.md`.\n"
    )


@server.resource(
    "lab-tracker://agent-consultation-policy",
    name="Lab Tracker Agent Consultation Policy",
    mime_type="text/markdown",
)
def lab_tracker_agent_consultation_policy() -> str:
    return AGENT_CONSULTATION_POLICY


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
