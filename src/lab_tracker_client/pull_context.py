"""Pull just the project context needed to choose a run's question and session offline."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ValidationError

from lab_tracker_client.client import (
    MAX_PAGE_SIZE,
    LabTracker,
    LTAPIError,
    configured_instance_url,
    connection_profile_path,
)
from lab_tracker_client.outbox import write_json_atomic
from lab_tracker_client.session_context import encode_session_link_code


class PulledQuestion(BaseModel):
    question_id: UUID
    project_id: UUID
    text: str
    status: Literal["active"]


class PulledSession(BaseModel):
    session_id: UUID
    project_id: UUID
    primary_question_id: UUID | None = None
    started_at: datetime
    status: Literal["active"]
    session_type: Literal["scientific", "operational"]

    def list_entry(self) -> dict[str, Any]:
        return {
            **self.model_dump(mode="json"),
            "link_code": encode_session_link_code(self.session_id),
        }


class ProjectContext(BaseModel):
    version: Literal[1]
    instance: str
    project_id: UUID
    pulled_at: datetime
    questions: list[PulledQuestion]
    sessions: list[PulledSession]

    def get_session(self, session_id: str) -> dict[str, Any]:
        for session in self.sessions:
            if str(session.session_id) == session_id:
                return session.list_entry()
        raise LTAPIError("Session is absent from the local pull. Run 'lt pull' when connected.")


def resolve_project_id(explicit: str | None) -> UUID:
    value = explicit or os.getenv("LAB_TRACKER_PROJECT_ID")
    try:
        return UUID(value or "")
    except ValueError as exc:
        raise LTAPIError(
            "Specify a project UUID with --project or LAB_TRACKER_PROJECT_ID."
        ) from exc


def _cache_path(instance: str, project_id: UUID) -> Path:
    instance_key = sha256(instance.encode()).hexdigest()
    return connection_profile_path().parent / "pull-cache" / f"{instance_key}-{project_id}.json"


def _pages(fetch: Callable[[int], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    while True:
        page = fetch(len(records))
        records.extend(page)
        if len(page) < MAX_PAGE_SIZE:
            return records


def _check_project(snapshot: ProjectContext, project_id: UUID) -> None:
    if (
        snapshot.project_id != project_id
        or any(question.project_id != project_id for question in snapshot.questions)
        or any(session.project_id != project_id for session in snapshot.sessions)
    ):
        raise LTAPIError("The pulled context contains records from a different project.")


def pull_project(client: LabTracker, project_id: UUID) -> dict[str, Any]:
    scope = str(project_id)
    try:
        questions = _pages(
            lambda offset: list(
                client.list_questions(
                    project_id=scope,
                    status="active",
                    offset=offset,
                )
            )
        )
        sessions = _pages(
            lambda offset: list(
                client.list_sessions(
                    project_id=scope,
                    status="active",
                    offset=offset,
                )
            )
        )
        snapshot = ProjectContext.model_validate(
            {
                "version": 1,
                "instance": client.base_url,
                "project_id": project_id,
                "pulled_at": datetime.now(timezone.utc),
                "questions": questions,
                "sessions": sessions,
            }
        )
    except LTAPIError as exc:
        raise LTAPIError(
            f"Pull failed: {exc} Offline commands can use the last successful pull."
        ) from exc
    except ValidationError as exc:
        raise LTAPIError("Lab Tracker returned invalid question or session context.") from exc
    _check_project(snapshot, project_id)
    path = _cache_path(client.base_url, project_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json_atomic(path, snapshot.model_dump(mode="json"))
    except OSError as exc:
        raise LTAPIError(f"Could not write the local pull at {path}: {exc}") from exc
    return {
        "command": "pull",
        "instance": snapshot.instance,
        "project_id": scope,
        "pulled_at": snapshot.pulled_at.isoformat(),
        "active_questions": len(snapshot.questions),
        "open_sessions": len(snapshot.sessions),
    }


def read_project(project_id: UUID) -> ProjectContext:
    instance = configured_instance_url()
    path = _cache_path(instance, project_id)
    hint = f"Run 'lt pull --project {project_id}' when connected."
    try:
        snapshot = ProjectContext.model_validate_json(path.read_bytes())
    except FileNotFoundError as exc:
        raise LTAPIError(f"No local pull for this instance and project. {hint}") from exc
    except (OSError, ValidationError) as exc:
        raise LTAPIError(f"Could not read the local pull. {hint}") from exc
    _check_project(snapshot, project_id)
    if snapshot.instance != instance:
        raise LTAPIError(f"The local pull belongs to a different instance. {hint}")
    return snapshot
