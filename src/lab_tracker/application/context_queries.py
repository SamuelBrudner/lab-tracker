"""Typed application queries that assemble cross-aggregate read models."""

from __future__ import annotations

import base64
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.orm import Session as OrmSession

from lab_tracker.artifact_resolution import (
    GitStoreResolutionTarget,
    HttpStoreResolutionTarget,
    PreparedArtifactResolutionTarget,
    RcloneStoreResolutionTarget,
    ResolvedArtifact,
    ResolverRegistry,
    git_store_resolution_target,
    http_store_resolution_target,
    rclone_store_resolution_target,
    resolver_registry_for_prepared_target,
    sanitize_artifact_resolution_result,
    snapshot_artifact_resolution_identity,
    unresolved,
)
from lab_tracker.artifact_resolution_limits import (
    ArtifactContentBounds,
    ArtifactContentBoundsError,
)
from lab_tracker.auth import AuthContext
from lab_tracker.decision_context import JsonObject, build_decision_context
from lab_tracker.decision_context_query import (
    DecisionContextRepository,
    RepositoryDecisionContextReader,
)
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.git_store_locator import (
    PinnedGitPath,
    canonical_git_store_uri,
)
from lab_tracker.graph_query import GraphQueryService
from lab_tracker.local_store_locator import (
    PortableStorePath,
    canonical_store_uri,
    is_valid_store_name,
    parse_canonical_store_authority,
)
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
    Project,
    ProjectStatus,
    ProvenanceLink,
    Question,
    StoreCapability,
    StoreKind,
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
from lab_tracker.rclone_store_definition import is_rclone_store_kind
from lab_tracker.schemas import (
    AssistantDecisionContextRequest,
    GraphNeighborhoodRead,
    GraphOverviewRead,
    GraphRetrievalMode,
    GraphSearchRead,
    GraphTraversalDirection,
    PortfolioProjectGroupSummary,
    ProjectGraphRead,
    ProjectGraphView,
    RetrievedGraphRead,
    SearchResults,
)
from lab_tracker.semantic_retrieval import EmbeddingClient
from lab_tracker.store_authority_use import (
    DetachedStoreAuthorityBinding,
    StoreAuthoritySnapshotProvider,
    StoreAuthorityUseProof,
    detach_store_authority_binding,
    revalidate_store_authority_binding,
)

from .types import Page

ExternalArtifactEntityType = Literal["analysis", "claim", "dataset"]


@dataclass(frozen=True)
class PreparedExternalArtifactResolution:
    """A caller-visible handle for a database-free resolution plan.

    ``target`` is an inspectable detached view, never the dispatch authority.
    Resolution consumes a separate private record created while the authorized
    request scope is open, releases that scope, then invokes an external resolver.
    """

    entity_type: ExternalArtifactEntityType
    entity_id: UUID
    artifact_index: int
    target: PreparedArtifactResolutionTarget
    max_bytes: int
    byte_range: tuple[int, int] | None
    _authorization: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedRemoteStoreResolutionPlan:
    """Pure detached inputs that are not yet a host-I/O capability."""

    binding: DetachedStoreAuthorityBinding
    logical_reference: ExternalArtifactReference
    locator: PortableStorePath | PinnedGitPath
    required_capabilities: tuple[StoreCapability, ...]
    unavailable_result: ResolvedArtifact


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedExternalArtifactResolutionRecord:
    """Private detached authority consumed instead of caller-visible plan data."""

    entity_type: ExternalArtifactEntityType
    entity_id: UUID
    artifact_index: int
    materialized: (
        PreparedArtifactResolutionTarget | _PreparedRemoteStoreResolutionPlan
    )
    max_bytes: int
    byte_range: tuple[int, int] | None


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

    def get_project_for_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Project: ...

    def get_dataset(self, dataset_id: UUID) -> Dataset: ...

    def get_dataset_for_read(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Dataset: ...

    def get_analysis(self, analysis_id: UUID) -> Analysis: ...

    def get_analysis_for_read(
        self,
        analysis_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Analysis: ...

    def get_claim(self, claim_id: UUID) -> Claim: ...

    def get_claim_for_read(
        self,
        claim_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Claim: ...

    def get_question(self, question_id: UUID) -> Question: ...

    def get_goal(self, goal_id: UUID) -> Goal: ...

    def require_goal_read(
        self,
        goal: Goal,
        *,
        actor: AuthContext | None,
    ) -> set[UUID]: ...

    def record_usage_event(
        self,
        *,
        verb: UsageEventVerb | str,
        resource_type: UsageEventResourceType | str,
        project_id: UUID | None,
        actor: AuthContext | None,
        result_count: int | None,
        retrieval_strategy: Any = None,
        retrieval_fallback: Any = None,
        semantic_duration_ms: int | None = None,
        shadow_overlap_milli: int | None = None,
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

    def query_provenance_links(
        self,
        *,
        project_id: UUID,
        status: str | None,
        limit: int | None,
        offset: int,
    ) -> tuple[list[ProvenanceLink], int]: ...


@dataclass(frozen=True)
class ContextQueries:
    """Cross-aggregate reads hidden behind a typed application boundary."""

    api: ContextAccess
    repository: ContextRepository
    session: OrmSession
    release_read_scope: Callable[[], None]
    resolver_registry: ResolverRegistry | None = None
    store_authority_snapshot_provider: StoreAuthoritySnapshotProvider | None = None
    settings: Any | None = None
    semantic_client: EmbeddingClient | None = None
    _prepared_external_artifact_resolutions: dict[
        object, _PreparedExternalArtifactResolutionRecord
    ] = field(default_factory=dict, init=False, repr=False, compare=False)
    _prepared_external_artifact_resolution_lock: Lock = field(
        default_factory=Lock,
        init=False,
        repr=False,
        compare=False,
    )

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
        context = build_decision_context(
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
        data = context.get("data")
        if not isinstance(data, dict):
            return context
        scope = data.get("scope")
        project_scope = scope.get("project") if isinstance(scope, dict) else None
        context_project_id = (
            project_scope.get("project_id") if isinstance(project_scope, dict) else None
        )
        if not context_project_id:
            return context
        # This authorization check necessarily precedes query embedding or any
        # other semantic adapter call.
        project = self.api.get_project_for_read(UUID(str(context_project_id)), actor=actor)
        graph_service = GraphQueryService(
            self.session,
            semantic_client=self.semantic_client,
            semantic_search_mode=(
                self.settings.semantic_search_mode if self.settings is not None else "off"
            ),
        )
        graph_search = graph_service.search(
            project,
            query=payload.query,
            entity_types=None,
            statuses=None,
            limit=8,
            offset=0,
            retrieval_mode=payload.retrieval_mode,
        )
        explicit_anchors = [
            (entity_type, str(entity_id))
            for entity_type, entity_id in (
                ("question", payload.question_id),
                ("dataset", payload.dataset_id),
                ("analysis", payload.analysis_id),
                ("claim", payload.claim_id),
                ("visualization", payload.visualization_id),
            )
            if entity_id is not None
        ]
        retrieved_graph = graph_service.retrieve_multi_seed(
            project,
            explicit_anchors=explicit_anchors,
            search=graph_search,
        )
        data["retrieved_graph"] = retrieved_graph.model_dump(mode="json")
        _merge_retrieved_graph_sections(
            data,
            retrieved_graph,
            reader=reader,
            project_id=project.project_id,
            limit=payload.limit,
        )
        meta = context.setdefault("meta", {})
        if isinstance(meta, dict):
            meta["retrieval_policy"] = (
                "explicit_anchors_then_fused_graph_seeds_then_typed_graph_then_recency"
            )
            meta["effective_graph_retrieval_mode"] = (
                graph_search.retrieval.effective_mode
            )
        return context

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
        project = self.api.get_project_for_read(project_id, actor=actor)
        return build_project_graph(self.repository, project.project_id, view=view)

    def graph_overview(
        self,
        project_id: UUID,
        *,
        actor: AuthContext,
    ) -> GraphOverviewRead:
        project = self.api.get_project_for_read(project_id, actor=actor)
        return GraphQueryService(self.session).overview(project)

    def search_graph(
        self,
        project_id: UUID,
        *,
        actor: AuthContext,
        query: str,
        entity_types: list[str] | None,
        statuses: list[str] | None,
        limit: int,
        offset: int,
        retrieval_mode: GraphRetrievalMode = "auto",
    ) -> GraphSearchRead:
        project = self.api.get_project_for_read(project_id, actor=actor)
        service = GraphQueryService(
            self.session,
            semantic_client=self.semantic_client,
            semantic_search_mode=(
                self.settings.semantic_search_mode if self.settings is not None else "off"
            ),
        )
        result = service.search(
            project,
            query=query,
            entity_types=entity_types,
            statuses=statuses,
            limit=limit,
            offset=offset,
            retrieval_mode=retrieval_mode,
        )
        strategy = (
            "shadow"
            if (
                self.settings is not None
                and self.settings.semantic_search_mode == "shadow"
                and retrieval_mode != "lexical"
            )
            else result.retrieval.effective_mode
        )
        self.api.record_usage_event(
            verb=UsageEventVerb.SEARCH,
            resource_type=UsageEventResourceType.SEARCH,
            project_id=project.project_id,
            actor=actor,
            result_count=len(result.items),
            retrieval_strategy=strategy,
            retrieval_fallback=result.retrieval.fallback_reason,
            semantic_duration_ms=service.semantic_duration_ms,
            shadow_overlap_milli=service.shadow_overlap_milli,
        )
        return result

    def graph_neighborhood(
        self,
        project_id: UUID,
        *,
        actor: AuthContext,
        anchor_type: str,
        anchor_id: UUID,
        direction: GraphTraversalDirection,
        relationships: list[str] | None,
        node_types: list[str] | None,
        depth: int,
        max_nodes: int,
        max_edges: int,
        include_anchor_content: bool,
    ) -> GraphNeighborhoodRead:
        project = self.api.get_project_for_read(project_id, actor=actor)
        return GraphQueryService(self.session).neighborhood(
            project,
            anchor_type=anchor_type,
            anchor_id=anchor_id,
            direction=direction,
            relationships=relationships,
            node_types=node_types,
            depth=depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            include_anchor_content=include_anchor_content,
        )

    def dataset_provenance(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext,
        base_url: str,
    ) -> dict[str, object]:
        dataset = self.api.get_dataset_for_read(dataset_id, actor=actor)
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
        analysis = self.api.get_analysis_for_read(analysis_id, actor=actor)
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
        claim = self.api.get_claim_for_read(claim_id, actor=actor)
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
        include_set = {
            item.strip().casefold()
            for item in (include.split(",") if include else ["questions", "notes"])
            if item.strip()
        }
        project_ids: set[UUID] | None
        linked_question_ids: set[UUID] | None = None
        linked_note_ids: set[UUID] | None = None
        if goal_id is not None:
            goal = self.api.get_goal(goal_id)
            goal_project_ids = self.api.require_goal_read(goal, actor=actor)
            if project_id is not None and not _goal_matches_project(
                goal,
                project_id=project_id,
                scope_project_ids=goal_project_ids,
            ):
                # Preserve the existing project mismatch behavior without
                # revealing it to callers who cannot read the full goal.
                self.api.require_project_read(project_id, actor=actor)
                raise ValidationError("goal_id must belong to project_id.")
            project_ids = {project_id} if project_id is not None else goal_project_ids
            linked_question_ids = {
                link.target.entity_id
                for link in goal.links
                if link.target.entity_type == EntityType.QUESTION
            }
            linked_note_ids = {
                link.target.entity_id
                for link in goal.links
                if link.target.entity_type == EntityType.NOTE
            }
        elif project_id is not None:
            self.api.require_project_read(project_id, actor=actor)
            project_ids = {project_id}
        else:
            project_ids = self.api.accessible_project_ids(actor)
        questions = (
            self.repository.query_questions(
                project_id=None,
                project_ids=project_ids,
                question_ids=linked_question_ids,
                search=query,
                limit=limit,
                offset=offset,
            )[0]
            if not include_set or "questions" in include_set
            else []
        )
        notes = (
            self.repository.query_notes(
                project_id=None,
                project_ids=project_ids,
                note_ids=linked_note_ids,
                search=query,
                limit=limit,
                offset=offset,
            )[0]
            if not include_set or "notes" in include_set
            else []
        )
        self.api.record_usage_event(
            verb=UsageEventVerb.SEARCH,
            resource_type=UsageEventResourceType.SEARCH,
            project_id=_single_project_id(project_ids),
            actor=actor,
            result_count=len(questions) + len(notes),
        )
        return SearchResults(questions=questions, notes=notes)

    def prepare_external_artifact_resolution(
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
    ) -> PreparedExternalArtifactResolution:
        try:
            bounds = ArtifactContentBounds.for_request(
                max_bytes,
                byte_start,
                byte_end,
            )
        except ArtifactContentBoundsError as exc:
            raise ValidationError(str(exc)) from exc

        reference, project_id = self._locate_external_reference(
            actor=actor,
            entity_type=entity_type,
            entity_id=entity_id,
            artifact_index=artifact_index,
            content_hash=content_hash,
        )

        detached_reference = reference.model_copy(deep=True)
        materialized = self._materialize_reference(
            detached_reference,
            project_id,
            required_capabilities=_resolution_capabilities(bounds.byte_range),
        )
        public_target: PreparedArtifactResolutionTarget
        if type(materialized) is _PreparedRemoteStoreResolutionPlan:
            public_target = deepcopy(materialized.unavailable_result)
        else:
            public_target = deepcopy(
                cast(PreparedArtifactResolutionTarget, materialized)
            )
        authorization = object()
        prepared = PreparedExternalArtifactResolution(
            entity_type=entity_type,
            entity_id=entity_id,
            artifact_index=artifact_index,
            target=public_target,
            max_bytes=bounds.max_bytes,
            byte_range=bounds.byte_range,
            _authorization=authorization,
        )
        record = _PreparedExternalArtifactResolutionRecord(
            entity_type=entity_type,
            entity_id=entity_id,
            artifact_index=artifact_index,
            materialized=deepcopy(materialized),
            max_bytes=bounds.max_bytes,
            byte_range=bounds.byte_range,
        )
        with self._prepared_external_artifact_resolution_lock:
            self._prepared_external_artifact_resolutions[authorization] = record
        return prepared

    def resolve_prepared_external_artifact(
        self,
        prepared: PreparedExternalArtifactResolution,
    ) -> dict[str, Any]:
        """Release SQLAlchemy resources, then resolve a detached target only."""

        authorization = (
            prepared._authorization
            if type(prepared) is PreparedExternalArtifactResolution
            else None
        )
        with self._prepared_external_artifact_resolution_lock:
            record = (
                self._prepared_external_artifact_resolutions.pop(authorization, None)
                if type(authorization) is object
                else None
            )
            # The request scope itself is not a concurrent primitive. Serialize
            # repeated/forged plan cleanup while keeping external resolver work
            # outside this lock.
            self.release_read_scope()

        if record is None:
            bounds = ArtifactContentBounds.from_resolver(1, None)
            target: object = None
            entity_type: ExternalArtifactEntityType = "dataset"
            entity_id = UUID(int=0)
            artifact_index = 0
        else:
            try:
                bounds = ArtifactContentBounds.from_resolver(
                    record.max_bytes,
                    record.byte_range,
                )
            except ArtifactContentBoundsError as exc:
                raise ValidationError(str(exc)) from exc
            target = (
                self._authorize_remote_store_plan(record.materialized)
                if type(record.materialized) is _PreparedRemoteStoreResolutionPlan
                else record.materialized
            )
            entity_type = record.entity_type
            entity_id = record.entity_id
            artifact_index = record.artifact_index

        identity = snapshot_artifact_resolution_identity(target)
        if record is None or identity is None:
            result: object = None
        elif type(target) is ResolvedArtifact:
            # Static application denials never cross an injected resolver
            # boundary: a custom registry could otherwise perform cache, host,
            # network, credential, or subprocess work.
            result = target
        elif (
            type(target) is HttpStoreResolutionTarget
            or type(target) is RcloneStoreResolutionTarget
            or type(target) is GitStoreResolutionTarget
        ):
            registry = resolver_registry_for_prepared_target(
                target,
                configured=self.resolver_registry,
            )
            result = registry.resolve_prepared(
                target,
                max_bytes=bounds.max_bytes,
                byte_range=bounds.byte_range,
            )
        else:
            # Raw references and unknown targets are not prepared capabilities.
            # Fail closed before touching resolver configuration or adapters.
            result = None
        result = sanitize_artifact_resolution_result(
            result,
            identity=identity,
            bounds=bounds,
        )
        body = result.to_json_dict()
        body["entity_type"] = entity_type
        body["entity_id"] = str(entity_id)
        body["artifact_index"] = artifact_index
        body["content_base64"] = (
            base64.b64encode(result.content).decode("ascii")
            if result.is_verified and result.content is not None
            else None
        )
        return body

    def _authorize_remote_store_plan(
        self,
        plan: _PreparedRemoteStoreResolutionPlan,
    ) -> PreparedArtifactResolutionTarget:
        """Capture one post-release snapshot and mint a remote target or deny."""

        if any(
            capability not in plan.binding.capabilities
            for capability in plan.required_capabilities
        ):
            return plan.unavailable_result
        provider = self.store_authority_snapshot_provider
        if provider is None:
            return plan.unavailable_result
        proof = revalidate_store_authority_binding(provider(), plan.binding)
        if proof is None:
            return plan.unavailable_result
        target = _remote_store_resolution_target(plan, proof)
        return target if target is not None else plan.unavailable_result

    def _locate_external_reference(
        self,
        *,
        actor: AuthContext,
        entity_type: ExternalArtifactEntityType,
        entity_id: UUID,
        artifact_index: int,
        content_hash: str | None,
    ) -> tuple[ExternalArtifactReference, UUID]:
        artifacts, project_id = self._entity_artifacts(
            actor=actor,
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
        actor: AuthContext,
        entity_type: ExternalArtifactEntityType,
        entity_id: UUID,
    ) -> tuple[list[ExternalArtifactReference], UUID]:
        if entity_type == "analysis":
            analysis = self.api.get_analysis_for_read(entity_id, actor=actor)
            return list(analysis.external_artifacts), analysis.project_id
        if entity_type == "claim":
            claim = self.api.get_claim_for_read(entity_id, actor=actor)
            return list(claim.external_citations), claim.project_id
        dataset = self.api.get_dataset_for_read(entity_id, actor=actor)
        manifest = dataset.commit_manifest
        artifacts = list(manifest.external_artifacts) if manifest is not None else []
        return artifacts, dataset.project_id

    def _materialize_reference(
        self,
        reference: ExternalArtifactReference,
        project_id: UUID,
        *,
        required_capabilities: tuple[StoreCapability, ...],
    ) -> (
        PreparedArtifactResolutionTarget | _PreparedRemoteStoreResolutionPlan
    ):
        if reference.store_name is not None and reference.locator is not None:
            return self._resolve_store(
                reference,
                project_id,
                (reference.store_name,),
                reference.locator,
                locator_is_uri_path=False,
                required_capabilities=required_capabilities,
            )
        parsed_store = _parse_store_reference_uri(reference.uri)
        if parsed_store is None:
            if (
                reference.source_system.lower() == "store"
                or _looks_like_store_uri(reference.uri)
            ):
                return _invalid_store_reference(reference)
            # Project-authored direct references are metadata only. A project
            # role does not confer authority over the process-wide filesystem,
            # network, credential, or subprocess resolver configuration.
            return _unavailable_store_reference(reference)
        return self._resolve_store(
            reference,
            project_id,
            parsed_store.name_candidates,
            parsed_store.locator,
            locator_is_uri_path=True,
            required_capabilities=required_capabilities,
        )

    def _resolve_store(
        self,
        reference: ExternalArtifactReference,
        project_id: UUID,
        name_candidates: tuple[str, ...],
        locator: str,
        *,
        locator_is_uri_path: bool,
        required_capabilities: tuple[StoreCapability, ...],
    ) -> (
        PreparedArtifactResolutionTarget | _PreparedRemoteStoreResolutionPlan
    ):
        matches: dict[UUID, tuple[str, DataStore]] = {}
        for candidate in dict.fromkeys(name_candidates):
            candidate_store = self.repository.data_stores.get_by_name(
                project_id, candidate
            )
            if candidate_store is None or candidate_store.name != candidate:
                continue
            matches[candidate_store.store_id] = (candidate, candidate_store)
        if not matches:
            return _unavailable_store_reference(reference)
        if len(matches) != 1:
            return _invalid_store_reference(reference)
        _, store = next(iter(matches.values()))
        binding = detach_store_authority_binding(store)
        if binding is None or binding.definition.kind is StoreKind.LOCAL_FS:
            # Local I/O remains disabled until a retained grant boundary is
            # threaded into the handle-bound filesystem adapter in .63.5.
            return _unavailable_store_reference(reference)
        if any(
            capability not in binding.capabilities
            for capability in required_capabilities
        ):
            return _unavailable_store_reference(reference)

        logical_reference: ExternalArtifactReference | None = None
        parsed_locator: PortableStorePath | PinnedGitPath | None = None
        kind = binding.definition.kind
        name = binding.definition.name
        if kind is StoreKind.HTTP or is_rclone_store_kind(kind):
            portable_locator = (
                PortableStorePath.parse_uri_path(locator)
                if locator_is_uri_path
                else PortableStorePath.parse_decoded(locator)
            )
            logical_reference = _nonlocal_store_logical_reference(
                reference,
                store_name=name,
                locator=portable_locator,
                locator_is_uri_path=locator_is_uri_path,
            )
            parsed_locator = portable_locator
        elif kind is StoreKind.GIT:
            pin = (
                PinnedGitPath.parse_uri_path(locator)
                if locator_is_uri_path
                else PinnedGitPath.parse_decoded(locator)
            )
            logical_reference = _git_store_logical_reference(
                reference,
                store_name=name,
                pin=pin,
                locator_is_uri_path=locator_is_uri_path,
            )
            parsed_locator = pin
        if logical_reference is None or parsed_locator is None:
            return _invalid_store_reference(reference)
        return _PreparedRemoteStoreResolutionPlan(
            binding=binding,
            logical_reference=logical_reference,
            locator=parsed_locator,
            required_capabilities=required_capabilities,
            unavailable_result=_unavailable_store_reference(reference),
        )


def _goal_matches_project(
    goal: Goal,
    *,
    project_id: UUID,
    scope_project_ids: set[UUID],
) -> bool:
    if goal.project_id is not None:
        return goal.project_id == project_id
    return project_id in scope_project_ids


def _merge_retrieved_graph_sections(
    data: JsonObject,
    retrieved: RetrievedGraphRead,
    *,
    reader: RepositoryDecisionContextReader,
    project_id: UUID,
    limit: int,
) -> None:
    """Merge graph nodes into legacy typed sections in relevance order."""

    specs: dict[str, tuple[str, str, Callable[[str], JsonObject | None] | None]] = {
        "question": ("questions", "question_id", reader.get_question),
        "note": ("notes", "note_id", None),
        "session": ("sessions", "session_id", None),
        "dataset": ("datasets", "dataset_id", reader.get_dataset),
        "analysis": ("analyses", "analysis_id", reader.get_analysis),
        "claim": ("claims", "claim_id", reader.get_claim),
        "visualization": ("visualizations", "viz_id", reader.get_visualization),
    }
    graph_order: dict[tuple[str, str], int] = {}
    graph_priority: dict[tuple[str, str], int] = {}
    for order, retrieved_node in enumerate(retrieved.nodes):
        node = retrieved_node.node
        spec = specs.get(node.entity_type)
        if spec is None:
            continue
        section, id_key, getter = spec
        raw_items = data.setdefault(section, [])
        if not isinstance(raw_items, list):
            continue
        items = [item for item in raw_items if isinstance(item, dict)]
        item = next(
            (value for value in items if str(value.get(id_key)) == node.entity_id),
            None,
        )
        created_from_graph = item is None
        if item is None:
            item = getter(node.entity_id) if getter is not None else None
            if item is None:
                item = {
                    id_key: node.entity_id,
                    "project_id": str(project_id),
                    "status": node.status,
                    "graph_excerpt": retrieved_node.excerpt,
                    "graph_excerpt_only": True,
                }
                if node.entity_type == "note":
                    item["raw_content"] = retrieved_node.excerpt
                elif node.entity_type == "session":
                    item["link_code"] = node.detail
                    item["session_type"] = node.label.removesuffix(" session")
            item["relevance_reasons"] = []
            raw_items.append(item)
        reasons = item.setdefault("relevance_reasons", [])
        if (
            created_from_graph
            and isinstance(reasons, list)
            and retrieved_node.relevance not in reasons
        ):
            reasons.append(retrieved_node.relevance)
        node_key = (node.entity_type, node.entity_id)
        graph_order[node_key] = order
        graph_priority[node_key] = {
            "anchor": 0,
            "graph_seed": 1,
            "graph_neighbor": 2,
        }[retrieved_node.relevance]

    type_by_section = {value[0]: key for key, value in specs.items()}
    id_by_section = {value[0]: value[1] for value in specs.values()}
    for section, entity_type in type_by_section.items():
        raw_items = data.get(section)
        if not isinstance(raw_items, list):
            continue
        id_key = id_by_section[section]
        typed_items = [item for item in raw_items if isinstance(item, dict)]
        typed_items.sort(
            key=lambda item: _decision_relevance_sort_key(
                item,
                graph_order.get((entity_type, str(item.get(id_key))), 10_000),
                graph_priority.get((entity_type, str(item.get(id_key)))),
            )
        )
        data[section] = typed_items[:limit]


def _decision_relevance_sort_key(
    item: JsonObject,
    graph_order: int,
    graph_priority: int | None,
) -> tuple[int, int]:
    raw_reasons = item.get("relevance_reasons")
    reasons = set(raw_reasons) if isinstance(raw_reasons, list) else set()
    if graph_priority is not None:
        priority = graph_priority
    elif "anchor" in reasons:
        priority = 0
    elif "graph_seed" in reasons or "search_match" in reasons:
        priority = 1
    elif "graph_neighbor" in reasons:
        priority = 2
    else:
        priority = 3
    return priority, graph_order


def _single_project_id(project_ids: set[UUID] | None) -> UUID | None:
    if project_ids is None or len(project_ids) != 1:
        return None
    return next(iter(project_ids))


@dataclass(frozen=True, slots=True)
class _ParsedStoreReferenceUri:
    name_candidates: tuple[str, ...]
    locator: str


def _resolution_capabilities(
    byte_range: tuple[int, int] | None,
) -> tuple[StoreCapability, ...]:
    required = [StoreCapability.BYTES_BY_PATH]
    if byte_range is not None:
        required.append(StoreCapability.BYTE_RANGE)
    return tuple(required)


def _remote_store_resolution_target(
    plan: _PreparedRemoteStoreResolutionPlan,
    proof: StoreAuthorityUseProof,
) -> (
    HttpStoreResolutionTarget
    | RcloneStoreResolutionTarget
    | GitStoreResolutionTarget
    | None
):
    kind = proof.definition.kind
    if kind is StoreKind.HTTP and type(plan.locator) is PortableStorePath:
        return http_store_resolution_target(
            proof,
            locator=plan.locator,
            logical_reference=plan.logical_reference,
        )
    if is_rclone_store_kind(kind) and type(plan.locator) is PortableStorePath:
        return rclone_store_resolution_target(
            proof,
            locator=plan.locator,
            logical_reference=plan.logical_reference,
        )
    if kind is StoreKind.GIT and type(plan.locator) is PinnedGitPath:
        return git_store_resolution_target(
            proof,
            pin=plan.locator,
            logical_reference=plan.logical_reference,
        )
    return None


def _nonlocal_store_logical_reference(
    reference: ExternalArtifactReference,
    *,
    store_name: str,
    locator: PortableStorePath | None,
    locator_is_uri_path: bool,
) -> ExternalArtifactReference | None:
    """Canonicalize one validated nonlocal ``store://`` identity."""

    canonical_uri = (
        canonical_store_uri(store_name, locator) if locator is not None else None
    )
    if canonical_uri is None or locator is None:
        return None
    accepted_uris = {
        canonical_uri,
        f"store://{store_name}/{locator.uri_path}",
    }
    if not locator_is_uri_path:
        accepted_uris.add(f"store://{store_name}/{locator.path}")
    if reference.uri not in accepted_uris:
        return None
    return reference.model_copy(
        update={
            "source_system": "store",
            "uri": canonical_uri,
            "store_name": store_name,
            "locator": locator.path,
        },
        deep=True,
    )


def _git_store_logical_reference(
    reference: ExternalArtifactReference,
    *,
    store_name: str,
    pin: PinnedGitPath | None,
    locator_is_uri_path: bool,
) -> ExternalArtifactReference | None:
    """Canonicalize one validated immutable registered-Git identity."""

    canonical_uri = (
        canonical_git_store_uri(store_name, pin) if pin is not None else None
    )
    if canonical_uri is None or pin is None:
        return None
    accepted_uris = {
        canonical_uri,
        f"store://{store_name}/{pin.uri_path}",
    }
    if not locator_is_uri_path:
        accepted_uris.add(f"store://{store_name}/{pin.locator}")
    if reference.uri not in accepted_uris:
        return None
    return reference.model_copy(
        update={
            "source_system": "store",
            "uri": canonical_uri,
            "store_name": store_name,
            "locator": pin.locator,
        },
        deep=True,
    )


def _parse_store_reference_uri(uri: str) -> _ParsedStoreReferenceUri | None:
    """Retain legacy non-local URI parsing until kind-specific hardening lands."""

    try:
        parsed = urlsplit(uri)
    except ValueError:
        return None
    if parsed.scheme.lower() != "store" or not parsed.netloc:
        return None
    canonical_name = parse_canonical_store_authority(parsed.netloc)
    name_candidates = tuple(
        dict.fromkeys(
            candidate
            for candidate in (
                canonical_name,
                parsed.netloc if is_valid_store_name(parsed.netloc) else None,
            )
            if candidate is not None
        )
    )
    if not name_candidates:
        return None
    return _ParsedStoreReferenceUri(
        name_candidates=name_candidates,
        locator=parsed.path.lstrip("/"),
    )


def _looks_like_store_uri(uri: str) -> bool:
    """Classify malformed store-shaped input without reflecting its contents."""

    return isinstance(uri, str) and uri.lstrip().lower().startswith("store:")


def _safe_store_reference(reference: ExternalArtifactReference) -> ExternalArtifactReference:
    """Keep store failures static without reflecting a locator or registered root."""

    return ExternalArtifactReference(
        kind=reference.kind,
        source_system="store",
        uri="store://[redacted]",
        content_hash=reference.content_hash,
    )


def _invalid_store_reference(reference: ExternalArtifactReference) -> ResolvedArtifact:
    return unresolved(
        _safe_store_reference(reference),
        detail="Store artifact reference is invalid.",
    )


def _unavailable_store_reference(reference: ExternalArtifactReference) -> ResolvedArtifact:
    return unresolved(
        _safe_store_reference(reference),
        detail="Store artifact could not be resolved.",
    )
