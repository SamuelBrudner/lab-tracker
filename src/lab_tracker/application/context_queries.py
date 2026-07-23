"""Typed application queries that assemble cross-aggregate read models."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.orm import Session as OrmSession

from lab_tracker.artifact_resolution import (
    DEFAULT_MAX_BYTES,
    ResolvedArtifact,
    ResolverRegistry,
    registry_from_env,
    store_relative_reference,
    unresolved,
)
from lab_tracker.auth import AuthContext
from lab_tracker.decision_context import JsonObject, build_decision_context
from lab_tracker.decision_context_query import (
    DecisionContextRepository,
    RepositoryDecisionContextReader,
)
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    Analysis,
    Claim,
    ClaimEdge,
    Dataset,
    DataStore,
    EntityType,
    ExplorationNode,
    ExternalArtifactReference,
    Goal,
    GoalLink,
    Project,
    ProjectStatus,
    Question,
    SupervisionEdge,
    UsageEventResourceType,
    UsageEventVerb,
)
from lab_tracker.portfolio_query import portfolio_summary_groups
from lab_tracker.project_graph import build_project_graph
from lab_tracker.provenance import (
    build_analysis_provenance_document,
    build_claim_provenance_document,
    build_dataset_provenance_document,
)
from lab_tracker.schemas import (
    AssistantDecisionContextRequest,
    PortfolioProjectGroupSummary,
    ProjectGraphRead,
    ProjectGraphView,
    SearchResults,
)

from .types import Page

ExternalArtifactEntityType = Literal["analysis", "claim", "dataset"]


class ContextDataStoreLookup(Protocol):
    def get_by_name(self, project_id: UUID, name: str) -> DataStore | None: ...


class ContextAccess(Protocol):
    """Domain reads and authorization needed by cross-aggregate queries."""

    def accessible_project_ids(self, actor: AuthContext | None) -> set[UUID] | None: ...

    def require_project_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...

    def get_project(self, project_id: UUID) -> Project: ...

    def get_dataset(self, dataset_id: UUID) -> Dataset: ...

    def get_analysis(self, analysis_id: UUID) -> Analysis: ...

    def get_claim(self, claim_id: UUID) -> Claim: ...

    def get_question(self, question_id: UUID) -> Question: ...

    def get_goal(self, goal_id: UUID) -> Goal: ...

    def record_usage_event(
        self,
        *,
        verb: UsageEventVerb | str,
        resource_type: UsageEventResourceType | str,
        project_id: UUID | None,
        actor: AuthContext | None,
        result_count: int | None,
    ) -> None: ...


class ContextRepository(DecisionContextRepository, Protocol):
    """Repository roles used directly or by delegated context projections."""

    @property
    def data_stores(self) -> ContextDataStoreLookup: ...

    def query_supervision_edges(
        self,
        *,
        limit: int | None,
        offset: int,
    ) -> tuple[list[SupervisionEdge], int]: ...

    def query_claim_edges(
        self,
        *,
        project_id: UUID,
        limit: int | None,
        offset: int,
    ) -> tuple[list[ClaimEdge], int]: ...

    def query_goal_links(
        self,
        *,
        goal_id: UUID,
        limit: int | None,
        offset: int,
    ) -> tuple[list[GoalLink], int]: ...

    def query_goals(
        self,
        *,
        project_id: UUID,
        limit: int | None,
        offset: int,
    ) -> tuple[list[Goal], int]: ...

    def query_exploration_nodes(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        node_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[ExplorationNode], int]: ...


@dataclass(frozen=True)
class ContextQueries:
    """Cross-aggregate reads hidden behind a typed application boundary."""

    api: ContextAccess
    repository: ContextRepository
    session: OrmSession
    resolver_registry: ResolverRegistry | None = None

    def decision_context(
        self,
        payload: AssistantDecisionContextRequest,
        *,
        actor: AuthContext,
    ) -> JsonObject:
        accessible_project_ids = self.api.accessible_project_ids(actor)
        resolved_project_id = payload.project_id
        if resolved_project_id is not None:
            self.api.require_project_read(resolved_project_id, actor=actor)
        elif accessible_project_ids is not None and len(accessible_project_ids) == 1:
            resolved_project_id = next(iter(accessible_project_ids))
        reader = RepositoryDecisionContextReader(
            self.repository,
            accessible_project_ids=accessible_project_ids,
        )
        return build_decision_context(
            reader,
            task_kind=payload.task_kind,
            query=payload.query,
            project_id=str(resolved_project_id) if resolved_project_id else None,
            question_id=str(payload.question_id) if payload.question_id else None,
            dataset_id=str(payload.dataset_id) if payload.dataset_id else None,
            analysis_id=str(payload.analysis_id) if payload.analysis_id else None,
            claim_id=str(payload.claim_id) if payload.claim_id else None,
            visualization_id=(
                str(payload.visualization_id) if payload.visualization_id else None
            ),
            created_by=str(payload.created_by) if payload.created_by else None,
            since=payload.since,
            until=payload.until,
            limit=payload.limit,
        )

    def portfolio_summary(
        self,
        *,
        actor: AuthContext,
        status: ProjectStatus | None,
        limit: int,
        offset: int,
    ) -> Page[PortfolioProjectGroupSummary]:
        groups, total = portfolio_summary_groups(
            self.session,
            accessible_project_ids=self.api.accessible_project_ids(actor),
            status=status,
            limit=limit,
            offset=offset,
        )
        return Page(items=groups, total=total)

    def project_graph(
        self,
        project_id: UUID,
        *,
        actor: AuthContext,
        view: ProjectGraphView,
    ) -> ProjectGraphRead:
        project = self.api.get_project(project_id)
        self.api.require_project_read(project.project_id, actor=actor)
        return build_project_graph(self.repository, project.project_id, view=view)

    def dataset_provenance(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext,
        base_url: str,
    ) -> dict[str, object]:
        dataset = self.api.get_dataset(dataset_id)
        self.api.require_project_read(dataset.project_id, actor=actor)
        supervision_edges, _ = self.repository.query_supervision_edges(
            limit=None,
            offset=0,
        )
        return build_dataset_provenance_document(
            base_url,
            dataset,
            supervision_edges=supervision_edges,
        )

    def analysis_provenance(
        self,
        analysis_id: UUID,
        *,
        actor: AuthContext,
        base_url: str,
    ) -> dict[str, object]:
        analysis = self.api.get_analysis(analysis_id)
        self.api.require_project_read(analysis.project_id, actor=actor)
        datasets = [
            self.api.get_dataset(dataset_id) for dataset_id in analysis.dataset_ids
        ]
        claims, _ = self.repository.query_claims(
            analysis_id=analysis_id,
            limit=None,
            offset=0,
        )
        claim_ids = {claim.claim_id for claim in claims}
        claim_edges, _ = self.repository.query_claim_edges(
            project_id=analysis.project_id,
            limit=None,
            offset=0,
        )
        claim_edges = [edge for edge in claim_edges if edge.claim_id in claim_ids]
        visualizations, _ = self.repository.query_visualizations(
            analysis_id=analysis_id,
            limit=None,
            offset=0,
        )
        supervision_edges, _ = self.repository.query_supervision_edges(
            limit=None,
            offset=0,
        )
        return build_analysis_provenance_document(
            base_url,
            analysis,
            datasets=datasets,
            claims=claims,
            visualizations=visualizations,
            claim_edges=claim_edges,
            supervision_edges=supervision_edges,
        )

    def claim_provenance(
        self,
        claim_id: UUID,
        *,
        actor: AuthContext,
        base_url: str,
    ) -> dict[str, object]:
        claim = self.api.get_claim(claim_id)
        self.api.require_project_read(claim.project_id, actor=actor)
        analyses = [
            self.api.get_analysis(analysis_id)
            for analysis_id in claim.supported_by_analysis_ids
        ]
        dataset_ids = set(claim.supported_by_dataset_ids)
        for analysis in analyses:
            dataset_ids.update(analysis.dataset_ids)
        datasets = [
            self.api.get_dataset(dataset_id) for dataset_id in sorted(dataset_ids)
        ]
        question_ids = set(claim.answers_question_ids)
        for dataset in datasets:
            question_ids.update(link.question_id for link in dataset.question_links)
        questions = [
            self.api.get_question(question_id) for question_id in sorted(question_ids)
        ]
        visualizations, _ = self.repository.query_visualizations(
            claim_id=claim_id,
            limit=None,
            offset=0,
        )
        claim_edges, _ = self.repository.query_claim_edges(
            project_id=claim.project_id,
            limit=None,
            offset=0,
        )
        claim_edges = [
            edge
            for edge in claim_edges
            if edge.claim_id == claim_id or edge.target_claim_id == claim_id
        ]
        supervision_edges, _ = self.repository.query_supervision_edges(
            limit=None,
            offset=0,
        )
        return build_claim_provenance_document(
            base_url,
            claim,
            analyses=analyses,
            datasets=datasets,
            questions=questions,
            visualizations=visualizations,
            claim_edges=claim_edges,
            supervision_edges=supervision_edges,
        )

    def search(
        self,
        *,
        actor: AuthContext,
        query: str,
        project_id: UUID | None,
        goal_id: UUID | None,
        include: str | None,
        limit: int,
        offset: int,
    ) -> SearchResults:
        allowed_project_ids = self.api.accessible_project_ids(actor)
        if project_id is not None:
            self.api.require_project_read(project_id, actor=actor)
        include_set = {
            item.strip().casefold()
            for item in (include.split(",") if include else ["questions", "notes"])
            if item.strip()
        }
        project_ids = {project_id} if project_id is not None else allowed_project_ids
        linked_question_ids: set[UUID] | None = None
        linked_note_ids: set[UUID] | None = None
        if goal_id is not None:
            goal = self.api.get_goal(goal_id)
            goal_project_id = goal.project_id
            if goal_project_id is None:
                raise ValidationError("goal_id must identify a project-scoped goal.")
            self.api.require_project_read(goal_project_id, actor=actor)
            if project_id is not None and goal_project_id != project_id:
                raise ValidationError("goal_id must belong to project_id.")
            project_ids = {goal_project_id}
            links, _ = self.repository.query_goal_links(
                goal_id=goal_id,
                limit=None,
                offset=0,
            )
            linked_question_ids = {
                link.target.entity_id
                for link in links
                if link.target.entity_type == EntityType.QUESTION
            }
            linked_note_ids = {
                link.target.entity_id
                for link in links
                if link.target.entity_type == EntityType.NOTE
            }
        questions = (
            self.repository.query_questions(
                project_id=None,
                project_ids=project_ids,
                search=query,
                limit=None if linked_question_ids is not None else limit,
                offset=0 if linked_question_ids is not None else offset,
            )[0]
            if not include_set or "questions" in include_set
            else []
        )
        notes = (
            self.repository.query_notes(
                project_id=None,
                project_ids=project_ids,
                search=query,
                limit=None if linked_note_ids is not None else limit,
                offset=0 if linked_note_ids is not None else offset,
            )[0]
            if not include_set or "notes" in include_set
            else []
        )
        if linked_question_ids is not None:
            questions = [
                item for item in questions if item.question_id in linked_question_ids
            ]
        if linked_note_ids is not None:
            notes = [item for item in notes if item.note_id in linked_note_ids]
        if linked_question_ids is not None:
            questions = questions[offset : offset + limit]
        if linked_note_ids is not None:
            notes = notes[offset : offset + limit]
        self.api.record_usage_event(
            verb=UsageEventVerb.SEARCH,
            resource_type=UsageEventResourceType.SEARCH,
            project_id=_single_project_id(project_ids),
            actor=actor,
            result_count=len(questions) + len(notes),
        )
        return SearchResults(questions=questions, notes=notes)

    def resolve_external_artifact(
        self,
        *,
        actor: AuthContext,
        entity_type: ExternalArtifactEntityType,
        entity_id: UUID,
        artifact_index: int,
        content_hash: str | None,
        max_bytes: int | None,
        byte_start: int | None,
        byte_end: int | None,
    ) -> dict[str, Any]:
        reference, project_id = self._locate_external_reference(
            entity_type=entity_type,
            entity_id=entity_id,
            artifact_index=artifact_index,
            content_hash=content_hash,
        )
        self.api.require_project_read(project_id, actor=actor)

        byte_range: tuple[int, int] | None = None
        if byte_start is not None or byte_end is not None:
            if byte_start is None or byte_end is None:
                raise ValidationError(
                    "byte_start and byte_end must be provided together."
                )
            byte_range = (byte_start, byte_end)

        materialized = self._materialize_reference(reference, project_id)
        if isinstance(materialized, ResolvedArtifact):
            result = materialized
        else:
            registry = self.resolver_registry or registry_from_env()
            result = registry.resolve(
                materialized,
                max_bytes=max_bytes or DEFAULT_MAX_BYTES,
                byte_range=byte_range,
            )
        body = result.to_json_dict()
        body["entity_type"] = entity_type
        body["entity_id"] = str(entity_id)
        body["artifact_index"] = artifact_index
        body["content_base64"] = (
            base64.b64encode(result.content).decode("ascii")
            if result.content is not None
            else None
        )
        return body

    def _locate_external_reference(
        self,
        *,
        entity_type: ExternalArtifactEntityType,
        entity_id: UUID,
        artifact_index: int,
        content_hash: str | None,
    ) -> tuple[ExternalArtifactReference, UUID]:
        artifacts, project_id = self._entity_artifacts(
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if artifact_index >= len(artifacts):
            raise NotFoundError(
                f"No external artifact at index {artifact_index} on "
                f"{entity_type} {entity_id}."
            )
        reference = artifacts[artifact_index]
        if content_hash is not None and content_hash != reference.content_hash:
            raise ValidationError(
                "content_hash does not match the artifact at the given index."
            )
        return reference, project_id

    def _entity_artifacts(
        self,
        *,
        entity_type: ExternalArtifactEntityType,
        entity_id: UUID,
    ) -> tuple[list[ExternalArtifactReference], UUID]:
        if entity_type == "analysis":
            analysis = self.api.get_analysis(entity_id)
            return list(analysis.external_artifacts), analysis.project_id
        if entity_type == "claim":
            claim = self.api.get_claim(entity_id)
            return list(claim.external_citations), claim.project_id
        dataset = self.api.get_dataset(entity_id)
        manifest = dataset.commit_manifest
        artifacts = list(manifest.external_artifacts) if manifest is not None else []
        return artifacts, dataset.project_id

    def _materialize_reference(
        self,
        reference: ExternalArtifactReference,
        project_id: UUID,
    ) -> ExternalArtifactReference | ResolvedArtifact:
        if reference.store_name is not None and reference.locator is not None:
            return self._resolve_store(
                reference,
                project_id,
                reference.store_name,
                reference.locator,
            )
        parsed = urlsplit(reference.uri)
        if parsed.scheme.lower() != "store":
            return reference
        name = parsed.netloc
        if not name:
            return unresolved(
                reference,
                detail="Store locator is missing a store name.",
            )
        return self._resolve_store(
            reference,
            project_id,
            name,
            parsed.path.lstrip("/"),
        )

    def _resolve_store(
        self,
        reference: ExternalArtifactReference,
        project_id: UUID,
        name: str,
        path: str,
    ) -> ExternalArtifactReference | ResolvedArtifact:
        store = self.repository.data_stores.get_by_name(project_id, name)
        if store is None:
            return unresolved(
                reference,
                detail=f"No data store named '{name}' in this project.",
            )
        concrete = store_relative_reference(
            store,
            path=path,
            content_hash=reference.content_hash,
        )
        if concrete is None:
            return unresolved(
                reference,
                detail=f"Store kind '{store.kind.value}' is not resolvable yet.",
            )
        return concrete


def _single_project_id(project_ids: set[UUID] | None) -> UUID | None:
    if project_ids is None or len(project_ids) != 1:
        return None
    return next(iter(project_ids))
