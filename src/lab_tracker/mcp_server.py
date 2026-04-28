"""API-backed MCP server for Lab Tracker."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

JsonObject = dict[str, Any]

SERVER_NAME = "lab-tracker-mcp"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 10.0


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

    def create_note(
        self,
        *,
        project_id: str,
        raw_content: str,
        transcribed_text: str | None = None,
        metadata: dict[str, str] | None = None,
        status: str | None = None,
    ) -> JsonObject:
        return self._request(
            "POST",
            "/notes",
            json_payload={
                "project_id": project_id,
                "raw_content": raw_content,
                "transcribed_text": transcribed_text,
                "metadata": metadata,
                "status": status,
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
        if authenticated:
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
    limit: int = 50,
    offset: int = 0,
) -> JsonObject:
    """List or search Lab Tracker questions through the API."""
    client = client_from_env()
    try:
        return client.list_questions(
            project_id=project_id,
            status=status,
            question_type=question_type,
            search=search,
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
def lab_tracker_create_note(
    project_id: str,
    raw_content: str,
    transcribed_text: str | None = None,
    metadata: dict[str, str] | None = None,
    status: str | None = None,
) -> JsonObject:
    """Create a text note through the Lab Tracker API."""
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
        "and set `LAB_TRACKER_MCP_BASE_URL`, `LAB_TRACKER_MCP_USERNAME`, and "
        "`LAB_TRACKER_MCP_PASSWORD` in the MCP client environment.\n"
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


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
