"""Shared types and reader protocols for decision-context assembly."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

JsonObject = dict[str, Any]


class DecisionContextReader(Protocol):
    """Read-only envelope-returning graph reader for decision context."""

    def list_projects(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        ...

    def get_project(self, project_id: str) -> JsonObject | None:
        ...

    def get_question(self, question_id: str) -> JsonObject | None:
        ...

    def get_experiment(self, experiment_id: str) -> JsonObject | None:
        ...

    def get_dataset(self, dataset_id: str) -> JsonObject | None:
        ...

    def get_analysis(self, analysis_id: str) -> JsonObject | None:
        ...

    def get_claim(self, claim_id: str) -> JsonObject | None:
        ...

    def get_visualization(self, visualization_id: str) -> JsonObject | None:
        ...

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
        recent_first: bool = False,
    ) -> JsonObject:
        ...

    def list_notes(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        target_entity_type: str | None = None,
        target_entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        ...

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        include: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> JsonObject:
        ...

    def project_ids_with_search_matches(self, query: str, *, limit: int = 50) -> set[str]:
        ...

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        session_type: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        ...

    def list_experiments(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        primary_question_id: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        ...

    def list_datasets(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        ...

    def list_analyses(
        self,
        *,
        project_id: str | None = None,
        dataset_id: str | None = None,
        question_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        ...

    def list_claims(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        dataset_id: str | None = None,
        analysis_id: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        ...

    def list_visualizations(
        self,
        *,
        project_id: str | None = None,
        analysis_id: str | None = None,
        claim_id: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        ...
