"""API-backed MCP client helpers for Lab Tracker MCP tools."""

from __future__ import annotations

import mimetypes
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from lab_tracker.artifact_resolution_limits import (
    ArtifactContentBounds,
    ArtifactContentBoundsError,
)
from lab_tracker.assistant_next_questions import (
    OPEN_GOAL_STATUSES,
    OPEN_QUESTION_STATUSES,
    build_next_questions_payload,
)
from lab_tracker.instance_url import (
    BASE_URL_ENV,
    DEFAULT_BASE_URL,
    LEGACY_MCP_BASE_URL_ENV,
    normalize_instance_base_url,
    resolve_instance_base_url,
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
from lab_tracker_client.client import load_connection_profile
from lab_tracker_client.transport import (
    MAX_UPLOAD_BYTES,
    HttpTransport,
    UploadTooLargeError,
    preflight_upload_size,
)

JsonObject = dict[str, Any]

SERVER_NAME = "lab-tracker-mcp"
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
_BEARER_SECRET_RE = re.compile(r"Bearer\s+[^\s\"'\\,}\]]+", re.IGNORECASE)
_LPAT_SECRET_RE = re.compile(r"lpat_[A-Za-z0-9_-]+")


def suppress_unverified_artifact_content(payload: JsonObject) -> JsonObject:
    """Fail closed if an artifact response carries content without verification."""

    data = payload.get("data")
    if data is None and isinstance(payload.get("error"), dict):
        return payload
    if not isinstance(data, dict):
        raise LabTrackerAPIError(
            "Lab Tracker API artifact response did not include object data."
        )
    if data.get("status") == "verified":
        return payload

    safe_data = dict(data)
    safe_data["content_base64"] = None
    safe_data["returned_bytes"] = 0
    safe_payload = dict(payload)
    safe_payload["data"] = safe_data
    return safe_payload

# Remediation guidance appended to auth failures so a rejected credential is
# self-describing at the tool boundary rather than an opaque "Invalid
# credentials" (GH #74, #79). Desktop MCP hosts only re-read env on a full
# relaunch, so a corrected config is inert until the server is restarted.
_LPAT_REMEDIATION = (
    "Re-mint an LPAT via POST /auth/tokens, verify it with `lt setup status`, then "
    "relaunch the Lab Tracker MCP server/host so it re-reads the credential (desktop "
    "apps only re-read env on a full quit-and-reopen, not a window reload)."
)
_NO_CREDENTIALS_MESSAGE = (
    "No Lab Tracker MCP credentials are configured, but the API requires "
    "authentication. Set LAB_TRACKER_MCP_API_KEY to an LPAT (mint via POST "
    "/auth/tokens) — the sanctioned auth method. LAB_TRACKER_MCP_USERNAME / "
    "LAB_TRACKER_MCP_PASSWORD login is deprecated."
)


class LabTrackerAPIError(RuntimeError):
    """Raised when the Lab Tracker API returns an unusable response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        issues: list[JsonObject] | None = None,
    ) -> None:
        super().__init__(redact_auth_secrets(message))
        self.status_code = status_code
        self.code = code
        self.issues = _redact_error_issues(issues)


class LabTrackerAPIUnavailableError(LabTrackerAPIError):
    """Raised when the Lab Tracker API cannot be reached or is unavailable."""


class LabTrackerAPIAuthError(LabTrackerAPIError):
    """Raised when Lab Tracker rejects MCP credentials or permissions."""


class LabTrackerAPIValidationError(LabTrackerAPIError):
    """Raised when a Lab Tracker API request is invalid."""


@dataclass(frozen=True)
class MCPSettings:
    base_url: str = DEFAULT_BASE_URL
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "base_url",
            normalize_instance_base_url(self.base_url),
        )

    @classmethod
    def from_env(cls) -> MCPSettings:
        """Build MCP settings with environment-first profile fallback.

        ``lt setup connect --save-token`` persists a base URL and LPAT in the
        machine connection profile. MCP hosts often launch without a shell, so
        use that profile for values absent from their environment. Never carry
        a profile token to an explicitly different server or combine it with
        explicit username/password login.
        """

        profile = load_connection_profile()
        canonical_env_url = os.getenv(BASE_URL_ENV)
        legacy_env_url = os.getenv(LEGACY_MCP_BASE_URL_ENV)
        env_base_url = None
        if canonical_env_url or legacy_env_url:
            env_base_url = resolve_instance_base_url(
                (
                    (BASE_URL_ENV, canonical_env_url),
                    (LEGACY_MCP_BASE_URL_ENV, legacy_env_url),
                )
            )
        env_username = os.getenv("LAB_TRACKER_MCP_USERNAME")
        profile_token = profile.get("access_token")
        try:
            profile_base_url = normalize_instance_base_url(
                profile.get("base_url") or DEFAULT_BASE_URL,
                setting_name="connection profile base_url",
            )
        except ValueError:
            profile_base_url = DEFAULT_BASE_URL
            profile_token = None
        if env_username or (env_base_url and env_base_url != profile_base_url):
            profile_token = None
        return cls(
            base_url=env_base_url or profile_base_url,
            username=env_username,
            password=os.getenv("LAB_TRACKER_MCP_PASSWORD"),
            api_key=os.getenv("LAB_TRACKER_MCP_API_KEY")
            or os.getenv("LAB_TRACKER_MCP_TOKEN")
            or profile_token,
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
        self._transport = HttpTransport(
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            auth=self,
            transport=transport,
        )

    @property
    def _client(self) -> httpx.Client:
        return self._transport.client

    @property
    def access_token(self) -> str | None:
        return self._access_token

    def close(self) -> None:
        self._transport.close()

    # --- TransportAuth policy (injected into the shared HttpTransport) --------
    @property
    def surface(self) -> str:
        return "mcp"

    def initial_bearer(self) -> str | None:
        static_key = self._static_api_key()
        if static_key:
            return static_key
        if self._has_credentials():
            return self._token()
        return None

    def refresh_bearer(self, response: httpx.Response) -> str:
        if self._static_api_key():
            raise _static_key_rejected_error(response, base_url=self._settings.base_url)
        self._access_token = None
        if not self._has_credentials():
            raise LabTrackerAPIAuthError(
                _NO_CREDENTIALS_MESSAGE,
                status_code=response.status_code,
                code="auth_error",
            )
        return self._token()

    def wrap_transport_error(self, method: str, path: str, exc: Exception) -> Exception:
        return LabTrackerAPIUnavailableError(
            f"Lab Tracker request {method} {path} failed: {exc}",
            code=UNAVAILABLE_CODE,
        )

    def health(self) -> JsonObject:
        return self._request("GET", "/health", authenticated=False)

    def readiness(self) -> JsonObject:
        return self._request("GET", "/readiness")

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

    def graph_overview(self, project_id: str) -> JsonObject:
        return self._request("GET", f"/projects/{project_id}/graph/overview")

    def search_graph(
        self,
        project_id: str,
        query: str,
        *,
        entity_types: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        retrieval_mode: str = "auto",
    ) -> JsonObject:
        return self._request(
            "GET",
            f"/projects/{project_id}/graph/search",
            params={
                "q": query,
                "entity_types": entity_types,
                "statuses": statuses,
                "limit": limit,
                "offset": offset,
                "retrieval_mode": retrieval_mode,
            },
        )

    def get_graph_neighborhood(
        self,
        project_id: str,
        entity_type: str,
        entity_id: str,
        *,
        direction: str = "both",
        relationships: list[str] | None = None,
        node_types: list[str] | None = None,
        depth: int = 1,
        max_nodes: int = 50,
        max_edges: int = 100,
        include_anchor_content: bool = False,
    ) -> JsonObject:
        return self._request(
            "GET",
            f"/projects/{project_id}/graph/neighborhood/{entity_type}/{entity_id}",
            params={
                "direction": direction,
                "relationships": relationships,
                "node_types": node_types,
                "depth": depth,
                "max_nodes": max_nodes,
                "max_edges": max_edges,
                "include_anchor_content": include_anchor_content,
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

    def get_visualization(self, visualization_id: str) -> JsonObject:
        return self._request("GET", f"/visualizations/{visualization_id}")

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

    def get_claim_provenance(self, claim_id: str) -> JsonObject:
        return self._request("GET", f"/claims/{claim_id}/provenance")

    def resolve_external_artifact(
        self,
        *,
        entity_type: str,
        entity_id: str,
        artifact_index: int = 0,
        content_hash: str | None = None,
        max_bytes: int | None = None,
        byte_start: int | None = None,
        byte_end: int | None = None,
    ) -> JsonObject:
        try:
            ArtifactContentBounds.for_request(max_bytes, byte_start, byte_end)
        except ArtifactContentBoundsError as exc:
            raise LabTrackerAPIValidationError(
                str(exc),
                code="validation_error",
            ) from exc

        payload: JsonObject = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "artifact_index": artifact_index,
        }
        if content_hash is not None:
            payload["content_hash"] = content_hash
        if max_bytes is not None:
            payload["max_bytes"] = max_bytes
        if byte_start is not None:
            payload["byte_start"] = byte_start
        if byte_end is not None:
            payload["byte_end"] = byte_end
        response = self._request(
            "POST",
            "/external-artifacts/resolve",
            json_payload=payload,
        )
        return suppress_unverified_artifact_content(response)

    def export_goal_artifact(
        self,
        goal_id: str,
        *,
        layer: str | None = None,
    ) -> JsonObject:
        path = f"/goals/{goal_id}/ara-artifact"
        if layer:
            path = f"{path}/{layer}"
        return self._request("GET", path)

    def export_question_subtree(
        self,
        question_id: str,
        *,
        layer: str | None = None,
    ) -> JsonObject:
        path = f"/questions/{question_id}/ara-artifact"
        if layer:
            path = f"{path}/{layer}"
        return self._request("GET", path)

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
        retrieval_mode: str = "auto",
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
                    "retrieval_mode": retrieval_mode,
                },
            )
        except LabTrackerAPIUnavailableError as exc:
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
            claims.extend(_payload_items(self.list_claims(project_id=lookup_project_id, limit=200)))

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
        falsification_criteria: str | None = None,
        verification_plan: str | None = None,
        refuting_outcome: str | None = None,
        supported_by_dataset_ids: list[str] | None = None,
        supported_by_analysis_ids: list[str] | None = None,
        answers_question_ids: list[str] | None = None,
        external_citations: list[JsonObject] | None = None,
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
                "falsification_criteria": falsification_criteria,
                "verification_plan": verification_plan,
                "refuting_outcome": refuting_outcome,
                "supported_by_dataset_ids": supported_by_dataset_ids,
                "supported_by_analysis_ids": supported_by_analysis_ids,
                "answers_question_ids": answers_question_ids,
                "external_citations": external_citations,
            },
        )

    def create_claim_edge(
        self,
        *,
        claim_id: str,
        target_claim_id: str,
        relation: str,
    ) -> JsonObject:
        return self._request(
            "POST",
            f"/claims/{claim_id}/edges",
            json_payload={
                "target_claim_id": target_claim_id,
                "relation": relation,
            },
        )

    def list_claim_edges(
        self,
        *,
        claim_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return self._request(
            "GET",
            f"/claims/{claim_id}/edges",
            params={"limit": limit, "offset": offset},
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

    def record_evidence_bundle(
        self,
        *,
        project_id: str,
        primary_question_id: str | None = None,
        dataset: JsonObject | None = None,
        analysis: JsonObject | None = None,
        claim: JsonObject | None = None,
        visualization: JsonObject | None = None,
        source_note: JsonObject | None = None,
        dry_run: bool = True,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        payload: JsonObject = {
            "project_id": project_id,
            "primary_question_id": primary_question_id,
            "dry_run": dry_run,
            "idempotency_key": idempotency_key,
        }
        payload.update(
            {
                name: component
                for name, component in (
                    ("dataset", dataset),
                    ("analysis", analysis),
                    ("claim", claim),
                    ("visualization", visualization),
                    ("source_note", source_note),
                )
                if component is not None
            }
        )
        return self._request(
            "POST",
            "/evidence-bundles",
            json_payload=payload,
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
        clear_target_date: bool = False,
        clear_external_ref: bool = False,
    ) -> JsonObject:
        if target_date is not None and clear_target_date:
            raise LabTrackerAPIValidationError(
                "target_date cannot be supplied when clear_target_date is true.",
                code="validation_error",
            )
        if external_ref is not None and clear_external_ref:
            raise LabTrackerAPIValidationError(
                "external_ref cannot be supplied when clear_external_ref is true.",
                code="validation_error",
            )
        payload: JsonObject = {
            name: value
            for name, value in (
                ("goal_type", _validate_goal_type(goal_type)),
                ("title", title),
                ("summary", summary),
                ("status", _validate_goal_status(status)),
                ("target_date", target_date),
                ("external_ref", external_ref),
                ("attributes", attributes),
            )
            if value is not None
        }
        if clear_target_date:
            payload["target_date"] = None
        if clear_external_ref:
            payload["external_ref"] = None
        return self._request(
            "PATCH",
            f"/goals/{goal_id}",
            json_payload=payload,
            preserve_json_nulls=clear_target_date or clear_external_ref,
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
        checksum_sha256: str | None = None,
        size_bytes: int | None = None,
        expected_current_storage_id: str | None = None,
    ) -> JsonObject:
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise LabTrackerAPIError(f"Visualization file does not exist: {file_path}")
        try:
            preflight_upload_size(path, max_bytes=MAX_UPLOAD_BYTES)
        except UploadTooLargeError as exc:
            raise LabTrackerAPIValidationError(str(exc), code="validation_error") from exc
        resolved_content_type = (
            content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        # Stream the file handle instead of reading it all into memory; open_file
        # re-opens per attempt so a 401 retry replays cleanly.
        response = self._transport.upload(
            "POST",
            f"/visualizations/{viz_id}/file",
            field_name="file",
            open_file=lambda: path.open("rb"),
            filename=path.name,
            content_type=resolved_content_type,
            data={
                key: str(value)
                for key, value in {
                    "checksum_sha256": checksum_sha256,
                    "size_bytes": size_bytes,
                    "expected_current_storage_id": expected_current_storage_id,
                }.items()
                if value is not None
            },
        )
        if response.status_code >= 400:
            raise _api_error_from_response(response)
        return _response_json(response)

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
        preserve_json_nulls: bool = False,
    ) -> JsonObject:
        response = self._transport.request(
            method,
            path,
            authenticated=authenticated,
            params=params,
            json=json_payload,
            files=files,
            retry_on_unauthorized=retry_on_unauthorized,
            preserve_json_nulls=preserve_json_nulls,
        )
        if response.status_code >= 400:
            raise _api_error_from_response(response)
        return _response_json(response)

    def _has_credentials(self) -> bool:
        return bool((self._settings.username or "").strip() and self._settings.password)

    def _static_api_key(self) -> str:
        return (self._settings.api_key or "").strip()

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        username = (self._settings.username or "").strip()
        password = self._settings.password or ""
        if not username or not password:
            raise LabTrackerAPIAuthError(
                _NO_CREDENTIALS_MESSAGE,
                code="auth_error",
            )
        # Proactively flag the deprecated auth path even on a successful login, so
        # the drift is visible before a stale credential turns it into a 401 outage
        # (GH #81). Emitted at login time, i.e. ~once per client.
        print(
            "WARNING: Lab Tracker MCP is authenticating with the deprecated "
            "LAB_TRACKER_MCP_USERNAME/PASSWORD login. Migrate to an LPAT: set "
            "LAB_TRACKER_MCP_API_KEY and remove the username/password env. Run "
            "`lt auth doctor` to audit every MCP config.",
            file=sys.stderr,
            flush=True,
        )
        response = self._transport.send(
            "POST",
            "/auth/login",
            json={"username": username, "password": password},
        )
        if response.status_code >= 400:
            if response.status_code in {401, 403}:
                raise _login_rejected_error(
                    response, base_url=self._settings.base_url, username=username
                )
            raise _api_error_from_response(response)
        payload = _response_json(response)
        try:
            token = str(payload["data"]["access_token"])
        except (KeyError, TypeError) as exc:
            raise LabTrackerAPIError("Login response did not include an access token.") from exc
        self._access_token = token
        return token


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
            raise LabTrackerAPIError("Note metadata values must be strings, numbers, or booleans.")
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
                "context instead of retrying indefinitely. If this looks like an "
                "unconfigured connection rather than an outage, the "
                "lab-tracker://setup-guide resource and the read-only `lt setup "
                "status` command can diagnose it."
            ),
        },
    }


def lab_tracker_api_error(operation: str, exc: LabTrackerAPIError) -> JsonObject:
    error: JsonObject = {
        "code": exc.code or "lab_tracker_api_error",
        "message": redact_auth_secrets(str(exc)),
        "operation": operation,
    }
    if exc.status_code is not None:
        error["status_code"] = exc.status_code
    if exc.issues:
        error["issues"] = _redact_error_issues(exc.issues)
    return {
        "error": error,
        "data": None,
        "next_action": {
            "action": "revise_request_or_credentials",
            "tool": None,
            "arguments": {},
            "reason": (
                "Use the structured error details to correct the request, credentials, "
                "or Lab Tracker permissions before retrying."
            ),
        },
    }


def redact_auth_secrets(value: object) -> str:
    text = str(value)
    text = _BEARER_SECRET_RE.sub("Bearer [REDACTED]", text)
    return _LPAT_SECRET_RE.sub("lpat_[REDACTED]", text)


def _redact_error_issues(issues: list[JsonObject] | None) -> list[JsonObject] | None:
    if not issues:
        return None
    redacted: list[JsonObject] = []
    for issue in issues:
        cleaned: JsonObject = {}
        for key, value in issue.items():
            cleaned[key] = redact_auth_secrets(value) if isinstance(value, str) else value
        redacted.append(cleaned)
    return redacted


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
        {str(goal["project_id"]) for goal in goals if goal.get("project_id") is not None}
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


def _static_key_rejected_error(
    response: httpx.Response, *, base_url: str
) -> LabTrackerAPIAuthError:
    """Actionable error when the static LPAT (LAB_TRACKER_MCP_API_KEY) is rejected.

    Distinguishes a static-key rejection from a username/password login failure and
    names the two likely causes (revoked/expired token vs a stale token in a
    still-running server), with remediation (GH #74).
    """
    server_message, code, issues = _response_error_parts(response)
    message = (
        f"Lab Tracker rejected the static LAB_TRACKER_MCP_API_KEY (LPAT) with HTTP "
        f"{response.status_code} at {base_url}: {server_message}. This is the static "
        "API key, not a username/password login. Likely causes: the token is expired "
        "or revoked, or the running MCP server is still holding a stale token from a "
        f"previous config and needs a relaunch. {_LPAT_REMEDIATION}"
    )
    return LabTrackerAPIAuthError(
        message,
        status_code=response.status_code,
        code=code or "auth_error",
        issues=issues,
    )


def _login_rejected_error(
    response: httpx.Response, *, base_url: str, username: str | None
) -> LabTrackerAPIAuthError:
    """Actionable error when a username/password ``POST /auth/login`` is rejected.

    Names the failing user and steers toward LPAT, which is the sanctioned auth
    method (GH #79, #81).
    """
    server_message, code, issues = _response_error_parts(response)
    who = f" for user '{username}'" if username else ""
    message = (
        f"Lab Tracker rejected the LAB_TRACKER_MCP_USERNAME/PASSWORD login{who} with "
        f"HTTP {response.status_code} at {base_url}: {server_message}. Username/password "
        "auth is deprecated — migrate to an LPAT: set LAB_TRACKER_MCP_API_KEY and remove "
        f"LAB_TRACKER_MCP_USERNAME / LAB_TRACKER_MCP_PASSWORD. {_LPAT_REMEDIATION}"
    )
    return LabTrackerAPIAuthError(
        message,
        status_code=response.status_code,
        code=code or "auth_error",
        issues=issues,
    )


def _api_error_from_response(response: httpx.Response) -> LabTrackerAPIError:
    message, code, issues = _response_error_parts(response)
    kwargs = {"status_code": response.status_code, "code": code, "issues": issues}
    if response.status_code in {401, 403}:
        return LabTrackerAPIAuthError(message, **kwargs)
    if response.status_code == 422:
        return LabTrackerAPIValidationError(message, **kwargs)
    if response.status_code >= 500:
        return LabTrackerAPIUnavailableError(message, **kwargs)
    return LabTrackerAPIError(message, **kwargs)


def _response_error(response: httpx.Response) -> str:
    return _response_error_parts(response)[0]


def _response_error_parts(
    response: httpx.Response,
) -> tuple[str, str | None, list[JsonObject] | None]:
    try:
        payload = response.json()
    except ValueError:
        return f"Lab Tracker API returned HTTP {response.status_code}: {response.text}", None, None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error["code"]) if error.get("code") else None
            issues = _coerce_error_issues(error.get("issues"))
            if error.get("message"):
                return _append_issue_text(str(error["message"]), issues), code, issues
        if payload.get("detail"):
            return str(payload["detail"]), None, None
    return f"Lab Tracker API returned HTTP {response.status_code}: {payload}", None, None


def _coerce_error_issues(value: object) -> list[JsonObject] | None:
    if not isinstance(value, list):
        return None
    issues = [issue for issue in value if isinstance(issue, dict)]
    return issues or None


def _append_issue_text(message: str, issues: list[JsonObject] | None) -> str:
    if not issues:
        return message
    issue_text = "; ".join(
        f"{issue.get('field') or 'request'}: {issue.get('message') or 'Invalid value'}"
        for issue in issues
    )
    return f"{message} Issues: {issue_text}."


def client_from_env() -> LabTrackerAPIClient:
    return LabTrackerAPIClient(MCPSettings.from_env())
