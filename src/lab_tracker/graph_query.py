"""Bounded, SQL-backed graph reads for agent-oriented navigation."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal
from typing import cast as type_cast
from uuid import UUID

from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    func,
    literal,
    null,
    or_,
    select,
    true,
    union_all,
)
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimEdgeModel,
    ClaimModel,
    ClaimQuestionModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    ExplorationNodeEdgeModel,
    ExplorationNodeModel,
    GoalLinkModel,
    GoalModel,
    NoteModel,
    NoteTargetModel,
    ProvenanceLinkModel,
    QuestionModel,
    QuestionParentModel,
    SessionModel,
    VisualizationClaimModel,
    VisualizationModel,
)
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.graph_documents import GraphNodeDocument, GraphNodeDocumentRenderer
from lab_tracker.models import (
    EntityType,
    ExternalArtifactReference,
    GoalStatus,
    Project,
    ProvenanceLinkRelation,
    ProvenanceLinkStatus,
    QuestionStatus,
    decode_session_link_code,
    encode_session_link_code,
)
from lab_tracker.project_graph import (
    DirectRelationshipSemanticMapping,
    QualifiedRelationshipSemanticMapping,
    project_graph_relationship_semantics,
)
from lab_tracker.schemas import (
    GraphEntityCount,
    GraphNeighborhoodEdge,
    GraphNeighborhoodRead,
    GraphNodeSummary,
    GraphOverviewRead,
    GraphProjectSummary,
    GraphRelationshipSemantics,
    GraphRetrievalMetadata,
    GraphRetrievalMode,
    GraphRetrievalPath,
    GraphSearchHit,
    GraphSearchRead,
    GraphTraversalDirection,
    GraphTraversalTruncation,
    PersistedGraphEntityType,
    RetrievedGraphNode,
    RetrievedGraphRead,
)
from lab_tracker.semantic_retrieval import (
    EmbeddingClient,
    ExactSemanticRetriever,
    SemanticSearchMode,
    candidate_limit_for_page,
)
from lab_tracker.sqlalchemy_repository_parts.common import substring_pattern

_PERSISTED_NODE_TYPES: tuple[PersistedGraphEntityType, ...] = (
    "question",
    "session",
    "note",
    "dataset",
    "analysis",
    "claim",
    "exploration_node",
    "visualization",
    "goal",
)
_NODE_TYPE_ORDER: dict[str, int] = {
    "question": 0,
    "session": 1,
    "note": 2,
    "dataset": 3,
    "analysis": 4,
    "claim": 5,
    "exploration_node": 6,
    "external_artifact": 7,
    "visualization": 8,
    "goal": 9,
}
_GRAPH_LABEL_LIMIT = 180
_SEARCH_SNIPPET_LIMIT = 280
_ANCHOR_CONTENT_LIMIT = 8_000
_OPEN_QUESTION_STATUSES = (QuestionStatus.STAGED.value, QuestionStatus.ACTIVE.value)
_OPEN_GOAL_STATUSES = (GoalStatus.PLANNED.value, GoalStatus.IN_PROGRESS.value)
_EDGE_FETCH_BUFFER = 201
_GRAPH_DOCUMENT_RENDERER = GraphNodeDocumentRenderer()

NodeKey = tuple[str, str]
DirectionValue = Literal["incoming", "outgoing", "both"]


@dataclass(frozen=True)
class _HydratedNode:
    summary: GraphNodeSummary
    content: str
    document: GraphNodeDocument | None


@dataclass(frozen=True)
class _SearchRow:
    key: NodeKey
    updated_at: Any
    match_rank: int
    match_reason: str
    matched_field: str
    matched_text: str


@dataclass(frozen=True)
class _EdgeCandidate:
    source: NodeKey
    target: NodeKey
    label: str
    relationship: str
    identity: str | None = None

    @property
    def edge_id(self) -> str:
        value = f"{self.relationship}:{_node_id(self.source)}->{_node_id(self.target)}"
        if self.identity:
            return f"{value}#{self.identity}"
        return value


class GraphQueryService:
    """Execute bounded graph reads against one request-scoped SQL session."""

    def __init__(
        self,
        session: OrmSession,
        *,
        semantic_client: EmbeddingClient | None = None,
        semantic_search_mode: SemanticSearchMode = "off",
    ) -> None:
        self._session = session
        self._semantic_client = semantic_client
        self._semantic_search_mode = semantic_search_mode
        self.semantic_duration_ms: int | None = None
        self.shadow_overlap_milli: int | None = None

    def overview(self, project: Project) -> GraphOverviewRead:
        self._session.flush()
        project_id = project.project_id
        counts = self._counts(project_id)
        open_question_keys = [
            ("question", str(value))
            for value in self._session.scalars(
                select(QuestionModel.question_id)
                .where(
                    QuestionModel.project_id == str(project_id),
                    QuestionModel.status.in_(_OPEN_QUESTION_STATUSES),
                )
                .order_by(QuestionModel.updated_at.desc(), QuestionModel.question_id)
                .limit(5)
            )
        ]
        open_goal_keys = [
            ("goal", str(value))
            for value in self._session.scalars(
                select(GoalModel.goal_id)
                .where(
                    _goal_project_scope(project_id),
                    GoalModel.status.in_(_OPEN_GOAL_STATUSES),
                )
                .order_by(GoalModel.updated_at.desc(), GoalModel.goal_id)
                .limit(5)
            )
        ]
        recent_keys = self._recent_keys(project_id)
        hydrated = self._hydrate_nodes(
            project_id,
            [*open_goal_keys, *open_question_keys, *recent_keys],
        )
        return GraphOverviewRead(
            project=GraphProjectSummary(
                project_id=project.project_id,
                name=project.name,
                description=project.description,
                status=project.status,
                updated_at=project.updated_at,
            ),
            counts=counts,
            open_goals=_summaries_in_order(open_goal_keys, hydrated),
            open_questions=_summaries_in_order(open_question_keys, hydrated),
            recent_nodes=_summaries_in_order(recent_keys, hydrated),
        )

    def render_document(
        self,
        project_id: UUID,
        *,
        entity_type: str,
        entity_id: UUID,
    ) -> GraphNodeDocument | None:
        """Render one authorized project-scoped node with the shared renderer."""

        key = (entity_type, str(entity_id))
        hydrated = self._hydrate_nodes(project_id, [key]).get(key)
        return hydrated.document if hydrated is not None else None

    def search(
        self,
        project: Project,
        *,
        query: str,
        entity_types: Sequence[str] | None,
        statuses: Sequence[str] | None,
        limit: int,
        offset: int,
        retrieval_mode: GraphRetrievalMode = "auto",
    ) -> GraphSearchRead:
        self._session.flush()
        normalized_query = query.strip()
        if not 2 <= len(normalized_query) <= 256:
            raise ValidationError("Graph search query must be 2 to 256 characters.")
        selected_types = tuple(entity_types) if entity_types is not None else _PERSISTED_NODE_TYPES
        selected_statuses = tuple(statuses) if statuses is not None else None
        candidate_limit = candidate_limit_for_page(offset, limit)
        metadata = GraphRetrievalMetadata(
            requested_mode=retrieval_mode,
            server_mode=self._semantic_search_mode,
            candidate_limit=candidate_limit,
        )
        semantic_allowed = retrieval_mode != "lexical" and self._semantic_search_mode != "off"
        lexical_window = candidate_limit if semantic_allowed else offset + limit + 1
        lexical_rows, lexical_truncated = self._lexical_search_rows(
            project.project_id,
            query=normalized_query,
            entity_types=selected_types,
            statuses=selected_statuses,
            limit=lexical_window,
        )
        metadata.lexical_candidate_count = min(len(lexical_rows), candidate_limit)

        semantic_candidates: tuple[Any, ...] = ()
        coverage = None
        semantic_timed_out = False
        semantic_failure = False
        if semantic_allowed and self._semantic_client is not None:
            semantic_started = time.perf_counter()
            try:
                retriever = ExactSemanticRetriever(self._session, self._semantic_client)
                coverage = retriever.coverage(project.project_id)
                semantic_result = retriever.search(
                    project.project_id,
                    normalized_query,
                    entity_types=selected_types,
                    statuses=selected_statuses,
                    candidate_limit=candidate_limit,
                )
                semantic_candidates = semantic_result.candidates
                semantic_timed_out = semantic_result.timed_out
                metadata.semantic_candidate_count = len(semantic_candidates)
                metadata.coverage = coverage.coverage
                if semantic_result.corrupt_chunks:
                    semantic_failure = True
            except TimeoutError:
                semantic_timed_out = True
            except Exception:
                # Semantic degradation must never fail graph search.
                semantic_failure = True
            finally:
                self.semantic_duration_ms = max(
                    0,
                    int((time.perf_counter() - semantic_started) * 1000),
                )
        if semantic_candidates:
            semantic_nodes = self._hydrate_nodes(
                project.project_id,
                [candidate.key for candidate in semantic_candidates],
            )
            current_semantic_candidates_list: list[Any] = []
            for candidate in semantic_candidates:
                semantic_node = semantic_nodes.get(candidate.key)
                if (
                    semantic_node is not None
                    and semantic_node.document is not None
                    and semantic_node.document.document_hash == candidate.document_hash
                ):
                    current_semantic_candidates_list.append(candidate)
            current_semantic_candidates = tuple(current_semantic_candidates_list)
            if len(current_semantic_candidates) != len(semantic_candidates):
                # A stale derivative invalidates the semantic leg for this
                # request; preserve the complete deterministic lexical result.
                semantic_failure = True
            semantic_candidates = current_semantic_candidates
            metadata.semantic_candidate_count = len(semantic_candidates)
            lexical_keys = {row.key for row in lexical_rows[:candidate_limit]}
            semantic_keys = {candidate.key for candidate in semantic_candidates}
            denominator = max(1, min(len(lexical_keys), len(semantic_keys)))
            self.shadow_overlap_milli = round(
                1_000 * len(lexical_keys & semantic_keys) / denominator
            )

        serve_hybrid = bool(
            self._semantic_search_mode == "hybrid"
            and retrieval_mode != "lexical"
            and self._semantic_client is not None
            and coverage is not None
            and coverage.ready
            and not semantic_timed_out
            and not semantic_failure
        )
        if retrieval_mode == "lexical":
            metadata.semantic_state = (
                "disabled" if self._semantic_search_mode == "off" else "unavailable"
            )
        elif self._semantic_search_mode == "off":
            metadata.semantic_state = "disabled"
            metadata.fallback_reason = "server_mode_off"
        elif self._semantic_client is None:
            metadata.semantic_state = "unavailable"
            metadata.fallback_reason = "adapter_unavailable"
        elif semantic_failure:
            metadata.semantic_state = "stale"
            metadata.fallback_reason = "semantic_index_invalid"
        elif semantic_timed_out:
            metadata.semantic_state = "stale"
            metadata.fallback_reason = "semantic_timeout"
        elif coverage is None or not coverage.ready:
            metadata.semantic_state = "partial"
            metadata.fallback_reason = "coverage_below_threshold"
        else:
            metadata.semantic_state = "ready"

        if self._semantic_search_mode == "shadow" and retrieval_mode != "lexical":
            metadata.fallback_reason = "shadow_policy"
        metadata.effective_mode = "hybrid" if serve_hybrid else "lexical"

        if not serve_hybrid:
            selected_rows = lexical_rows[offset : offset + limit]
            has_more = len(lexical_rows) > offset + limit or lexical_truncated
            hydrated = self._hydrate_nodes(
                project.project_id,
                [row.key for row in selected_rows],
            )
            lexical_items = [
                GraphSearchHit(
                    node=hydrated[row.key].summary,
                    snippet=_bounded_snippet(row.matched_text, normalized_query),
                    match_reasons=[row.match_reason, f"field:{row.matched_field}"],
                    lexical_rank=rank,
                )
                for rank, row in enumerate(selected_rows, start=offset + 1)
                if row.key in hydrated
            ]
            metadata.pool_truncated = lexical_truncated or semantic_timed_out
            return GraphSearchRead(
                project_id=project.project_id,
                query=normalized_query,
                items=lexical_items,
                limit=limit,
                offset=offset,
                has_more=has_more,
                next_offset=offset + limit if has_more else None,
                retrieval=metadata,
            )

        lexical_by_key = {
            row.key: (rank, row)
            for rank, row in enumerate(lexical_rows[:candidate_limit], start=1)
        }
        semantic_by_key: dict[NodeKey, Any] = {
            candidate.key: candidate for candidate in semantic_candidates
        }
        candidate_keys = set(lexical_by_key) | set(semantic_by_key)
        hydrated = self._hydrate_nodes(project.project_id, candidate_keys)
        current_candidate_keys: set[NodeKey] = set()
        for key in candidate_keys:
            node = hydrated.get(key)
            if node is None:
                continue
            semantic_candidate = semantic_by_key.get(key)
            if semantic_candidate is not None and (
                node.document is None
                or node.document.document_hash != semantic_candidate.document_hash
            ):
                continue
            current_candidate_keys.add(key)
        candidate_keys = current_candidate_keys
        ordered_keys = sorted(
            candidate_keys,
            key=lambda key: _fusion_sort_key(
                key,
                lexical=lexical_by_key.get(key),
                semantic=semantic_by_key.get(key),
                hydrated=hydrated[key],
            ),
        )
        metadata.semantic_candidate_count = sum(
            key in semantic_by_key for key in candidate_keys
        )
        metadata.pool_truncated = bool(
            lexical_truncated
            or semantic_timed_out
            or len(semantic_candidates) >= candidate_limit
        )
        selected_keys = ordered_keys[offset : offset + limit]
        fused_items: list[GraphSearchHit] = []
        for fused_rank, key in enumerate(selected_keys, start=offset + 1):
            lexical = lexical_by_key.get(key)
            semantic = semantic_by_key.get(key)
            node = hydrated[key]
            if lexical is not None:
                lexical_rank, lexical_row = lexical
                snippet = _bounded_snippet(lexical_row.matched_text, normalized_query)
                reasons = [
                    lexical_row.match_reason,
                    f"field:{lexical_row.matched_field}",
                ]
            else:
                lexical_rank = None
                assert semantic is not None
                if node.document is None:  # pragma: no cover - persisted candidates only
                    continue
                snippet = _semantic_chunk_snippet(node.document, semantic.chunk_index)
                reasons = ["semantic"]
            fused_items.append(
                GraphSearchHit(
                    node=node.summary,
                    snippet=snippet,
                    match_reasons=reasons,
                    lexical_rank=lexical_rank,
                    semantic_rank=semantic.rank if semantic is not None else None,
                    fused_rank=fused_rank,
                    matched_semantic_chunk_index=(
                        semantic.chunk_index if semantic is not None else None
                    ),
                )
            )
        has_more = len(ordered_keys) > offset + limit or metadata.pool_truncated
        return GraphSearchRead(
            project_id=project.project_id,
            query=normalized_query,
            items=fused_items,
            limit=limit,
            offset=offset,
            has_more=has_more,
            next_offset=offset + limit if has_more else None,
            retrieval=metadata,
        )

    def _lexical_search_rows(
        self,
        project_id: UUID,
        *,
        query: str,
        entity_types: Sequence[str],
        statuses: Sequence[str] | None,
        limit: int,
    ) -> tuple[list[_SearchRow], bool]:
        branches = self._search_branches(
            project_id,
            query=query,
            entity_types=entity_types,
            statuses=statuses,
        )
        if not branches:
            return [], False
        candidates = union_all(*branches).subquery("graph_search_candidates")
        type_order = case(
            _NODE_TYPE_ORDER,
            value=candidates.c.entity_type,
            else_=99,
        )
        rows = list(
            self._session.execute(
                select(candidates)
                .order_by(
                    candidates.c.match_rank,
                    candidates.c.updated_at.desc(),
                    type_order,
                    candidates.c.entity_id,
                )
                .limit(limit + 1)
            ).mappings()
        )
        return (
            [
                _SearchRow(
                    key=(str(row["entity_type"]), str(row["entity_id"])),
                    updated_at=row["updated_at"],
                    match_rank=int(row["match_rank"]),
                    match_reason=str(row["match_reason"]),
                    matched_field=str(row["matched_field"]),
                    matched_text=str(row["matched_text"] or ""),
                )
                for row in rows[:limit]
            ],
            len(rows) > limit,
        )

    def neighborhood(
        self,
        project: Project,
        *,
        anchor_type: str,
        anchor_id: UUID,
        direction: GraphTraversalDirection,
        relationships: Sequence[str] | None,
        node_types: Sequence[str] | None,
        depth: int,
        max_nodes: int,
        max_edges: int,
        include_anchor_content: bool,
    ) -> GraphNeighborhoodRead:
        self._session.flush()
        anchor_key = (anchor_type, str(anchor_id))
        anchor_hydrated = self._hydrate_nodes(project.project_id, [anchor_key])
        if anchor_key not in anchor_hydrated:
            # Match the opaque project-graph boundary: an inaccessible project,
            # missing anchor, and cross-project anchor are indistinguishable.
            raise NotFoundError("Project does not exist.")

        selected_relationships = set(relationships) if relationships else None
        for relationship in selected_relationships or set():
            try:
                project_graph_relationship_semantics(relationship)
            except ValueError as exc:
                raise ValidationError(f"Unsupported graph relationship: {relationship}") from exc
        selected_node_types = set(node_types) if node_types else None
        seen_nodes: set[NodeKey] = {anchor_key}
        frontier: list[NodeKey] = [anchor_key]
        edges: dict[str, _EdgeCandidate] = {}
        external_nodes: dict[NodeKey, _HydratedNode] = {}
        node_limit_reached = False
        edge_limit_reached = False
        reached_depth = 0

        for current_depth in range(1, depth + 1):
            if not frontier:
                break
            reached_depth = current_depth
            fetch_limit = min(
                701,
                max_edges - len(edges) + max_nodes + _EDGE_FETCH_BUFFER,
            )
            candidates = self._edge_candidates(
                project.project_id,
                frontier=frontier,
                direction=direction,
                relationships=selected_relationships,
                node_types=selected_node_types,
                limit=max(1, fetch_limit),
            )
            citation_candidates, citation_nodes = self._citation_candidates(
                project.project_id,
                frontier=frontier,
                direction=direction,
                relationships=selected_relationships,
                node_types=selected_node_types,
            )
            external_nodes.update(citation_nodes)
            ordered_candidates = sorted(
                {*candidates, *citation_candidates},
                key=_edge_sort_key,
            )
            next_frontier: set[NodeKey] = set()
            for candidate in ordered_candidates:
                if candidate.edge_id in edges:
                    continue
                adjacent = _adjacent_nodes(
                    candidate,
                    frontier=set(frontier),
                    direction=direction,
                )
                if not adjacent:
                    continue
                missing = [key for key in adjacent if key not in seen_nodes]
                if missing and len(seen_nodes) + len(missing) > max_nodes:
                    node_limit_reached = True
                    continue
                if len(edges) >= max_edges:
                    edge_limit_reached = True
                    break
                edges[candidate.edge_id] = candidate
                for key in missing:
                    seen_nodes.add(key)
                    if key[0] != "external_artifact":
                        next_frontier.add(key)
            if len(edges) >= max_edges and len(ordered_candidates) > len(edges):
                edge_limit_reached = True
            if edge_limit_reached:
                break
            frontier = sorted(next_frontier, key=_node_sort_key)

        neighbor_keys = sorted(seen_nodes - {anchor_key}, key=_node_sort_key)
        hydrated = self._hydrate_nodes(
            project.project_id,
            [key for key in neighbor_keys if key[0] != "external_artifact"],
        )
        hydrated.update(external_nodes)
        valid_keys = {anchor_key, *hydrated}
        response_edges = [
            _edge_read(candidate)
            for candidate in sorted(edges.values(), key=_edge_sort_key)
            if candidate.source in valid_keys and candidate.target in valid_keys
        ]
        if len(response_edges) < len(edges):
            node_limit_reached = True
        anchor_content: str | None = None
        anchor_content_truncated = False
        if include_anchor_content:
            raw_content = anchor_hydrated[anchor_key].content
            anchor_content_truncated = len(raw_content) > _ANCHOR_CONTENT_LIMIT
            anchor_content = raw_content[:_ANCHOR_CONTENT_LIMIT]
        expansion_stopped = (node_limit_reached or edge_limit_reached) and reached_depth < depth
        return GraphNeighborhoodRead(
            project_id=project.project_id,
            direction=direction,
            requested_depth=depth,
            reached_depth=reached_depth,
            anchor=anchor_hydrated[anchor_key].summary,
            anchor_content=anchor_content,
            anchor_content_truncated=anchor_content_truncated,
            nodes=_summaries_in_order(neighbor_keys, hydrated),
            edges=response_edges,
            truncation=GraphTraversalTruncation(
                truncated=node_limit_reached or edge_limit_reached,
                node_limit_reached=node_limit_reached,
                edge_limit_reached=edge_limit_reached,
                expansion_stopped=expansion_stopped,
            ),
        )

    def retrieve_multi_seed(
        self,
        project: Project,
        *,
        explicit_anchors: Sequence[NodeKey],
        search: GraphSearchRead,
        depth: int = 2,
        max_nodes: int = 50,
        max_edges: int = 100,
    ) -> RetrievedGraphRead:
        """Expand explicit anchors and up to eight fused seeds in one traversal."""

        self._session.flush()
        ordered_anchors = list(dict.fromkeys(explicit_anchors))
        query_keys: list[NodeKey] = [
            (hit.node.entity_type, hit.node.entity_id)
            for hit in search.items[:8]
            if (hit.node.entity_type, hit.node.entity_id) not in ordered_anchors
        ]
        requested_seeds = [*ordered_anchors, *query_keys]
        hydrated = self._hydrate_nodes(project.project_id, requested_seeds)
        seed_keys = [key for key in requested_seeds if key in hydrated]
        anchor_keys = {key for key in ordered_anchors if key in hydrated}
        query_rank = {key: rank for rank, key in enumerate(query_keys, start=1)}
        seed_priority = {key: rank for rank, key in enumerate(seed_keys)}

        seen_nodes: set[NodeKey] = set(seed_keys[:max_nodes])
        discovery_order: list[NodeKey] = list(seed_keys[:max_nodes])
        frontier: list[NodeKey] = list(discovery_order)
        origins: dict[NodeKey, NodeKey] = {key: key for key in frontier}
        node_paths: dict[NodeKey, list[NodeKey]] = {key: [key] for key in frontier}
        edge_paths: dict[NodeKey, list[str]] = {key: [] for key in frontier}
        inherited_priority: dict[NodeKey, int] = {
            key: seed_priority[key] for key in frontier
        }
        edges: dict[str, _EdgeCandidate] = {}
        external_nodes: dict[NodeKey, _HydratedNode] = {}
        node_limit_reached = len(seed_keys) > max_nodes
        edge_limit_reached = False
        reached_depth = 0

        for current_depth in range(1, depth + 1):
            if not frontier:
                break
            reached_depth = current_depth
            candidates = self._edge_candidates(
                project.project_id,
                frontier=frontier,
                direction="both",
                relationships=None,
                node_types=None,
                limit=min(701, max_edges + max_nodes + _EDGE_FETCH_BUFFER),
            )
            citation_candidates, citation_nodes = self._citation_candidates(
                project.project_id,
                frontier=frontier,
                direction="both",
                relationships=None,
                node_types=None,
            )
            external_nodes.update(citation_nodes)
            frontier_set = set(frontier)
            transitions: list[tuple[object, ...]] = []
            for candidate in {*candidates, *citation_candidates}:
                if candidate.source in frontier_set:
                    transitions.append(
                        (
                            inherited_priority[candidate.source],
                            _edge_sort_key(candidate),
                            _node_sort_key(candidate.target),
                            candidate.source,
                            candidate.target,
                            candidate,
                        )
                    )
                if candidate.target in frontier_set:
                    transitions.append(
                        (
                            inherited_priority[candidate.target],
                            _edge_sort_key(candidate),
                            _node_sort_key(candidate.source),
                            candidate.target,
                            candidate.source,
                            candidate,
                        )
                    )
            next_frontier: list[NodeKey] = []
            for _, _, _, parent, adjacent, raw_candidate in sorted(transitions):
                candidate = type_cast(_EdgeCandidate, raw_candidate)
                parent_key = type_cast(NodeKey, parent)
                adjacent_key = type_cast(NodeKey, adjacent)
                if candidate.edge_id not in edges:
                    if len(edges) >= max_edges:
                        edge_limit_reached = True
                        break
                    edges[candidate.edge_id] = candidate
                if adjacent_key in seen_nodes:
                    continue
                if len(seen_nodes) >= max_nodes:
                    node_limit_reached = True
                    continue
                seen_nodes.add(adjacent_key)
                discovery_order.append(adjacent_key)
                origins[adjacent_key] = origins[parent_key]
                node_paths[adjacent_key] = [*node_paths[parent_key], adjacent_key]
                edge_paths[adjacent_key] = [
                    *edge_paths[parent_key],
                    candidate.edge_id,
                ]
                inherited_priority[adjacent_key] = inherited_priority[parent_key]
                if adjacent_key[0] != "external_artifact":
                    next_frontier.append(adjacent_key)
            if edge_limit_reached:
                break
            frontier = next_frontier

        persisted_keys = [
            key
            for key in discovery_order
            if key not in hydrated and key[0] != "external_artifact"
        ]
        hydrated.update(self._hydrate_nodes(project.project_id, persisted_keys))
        hydrated.update(external_nodes)
        valid_order = [key for key in discovery_order if key in hydrated]
        valid_keys = set(valid_order)
        response_edges = [
            _edge_read(candidate)
            for candidate in sorted(edges.values(), key=_edge_sort_key)
            if candidate.source in valid_keys and candidate.target in valid_keys
        ]
        if len(response_edges) < len(edges):
            node_limit_reached = True

        response_nodes: list[RetrievedGraphNode] = []
        total_content = 0
        content_truncated = False
        for key in valid_order:
            if key in anchor_keys:
                relevance: Literal["anchor", "graph_seed", "graph_neighbor"] = "anchor"
                content_limit = 8_000
            elif key in query_rank:
                relevance = "graph_seed"
                content_limit = 2_000
            else:
                relevance = "graph_neighbor"
                content_limit = 1_200
            raw_content = hydrated[key].content
            remaining = max(0, 40_000 - total_content)
            excerpt_limit = min(content_limit, remaining)
            excerpt = raw_content[:excerpt_limit]
            truncated = len(raw_content) > excerpt_limit
            content_truncated = content_truncated or truncated
            total_content += len(excerpt)
            response_nodes.append(
                RetrievedGraphNode(
                    node=hydrated[key].summary,
                    relevance=relevance,
                    seed_rank=query_rank.get(key),
                    excerpt=excerpt,
                    content_truncated=truncated,
                )
            )

        by_key: dict[NodeKey, RetrievedGraphNode] = {
            (item.node.entity_type, item.node.entity_id): item for item in response_nodes
        }
        paths = [
            GraphRetrievalPath(
                seed_node_id=_node_id(origins[key]),
                target_node_id=_node_id(key),
                node_ids=[_node_id(value) for value in node_paths[key]],
                edge_ids=edge_paths[key],
            )
            for key in valid_order
            if key in origins
        ]
        expansion_stopped = (node_limit_reached or edge_limit_reached) and reached_depth < depth
        truncation = GraphTraversalTruncation(
            truncated=node_limit_reached or edge_limit_reached,
            node_limit_reached=node_limit_reached,
            edge_limit_reached=edge_limit_reached,
            expansion_stopped=expansion_stopped,
        )
        return RetrievedGraphRead(
            seeds=[by_key[key] for key in seed_keys if key in by_key],
            nodes=response_nodes,
            edges=response_edges,
            paths=paths,
            retrieval=search.retrieval,
            traversal_truncation=truncation,
            content_truncated=content_truncated,
            total_content_characters=total_content,
        )

    def _counts(
        self,
        project_id: UUID,
    ) -> dict[PersistedGraphEntityType, GraphEntityCount]:
        branches = [
            _count_branch(
                "question",
                QuestionModel.status,
                QuestionModel.project_id == str(project_id),
            ),
            _count_branch(
                "session",
                SessionModel.status,
                SessionModel.project_id == str(project_id),
            ),
            _count_branch(
                "note",
                NoteModel.status,
                NoteModel.project_id == str(project_id),
            ),
            _count_branch(
                "dataset",
                DatasetModel.status,
                DatasetModel.project_id == str(project_id),
            ),
            _count_branch(
                "analysis",
                AnalysisModel.status,
                AnalysisModel.project_id == str(project_id),
            ),
            _count_branch(
                "claim",
                ClaimModel.status,
                ClaimModel.project_id == str(project_id),
            ),
            _count_branch(
                "exploration_node",
                ExplorationNodeModel.status,
                ExplorationNodeModel.project_id == str(project_id),
            ),
            select(
                literal("visualization").label("entity_type"),
                cast(null(), String()).label("status"),
                func.count(VisualizationModel.viz_id).label("record_count"),
            )
            .select_from(VisualizationModel)
            .join(
                AnalysisModel,
                AnalysisModel.analysis_id == VisualizationModel.analysis_id,
            )
            .where(AnalysisModel.project_id == str(project_id)),
            _count_branch("goal", GoalModel.status, _goal_project_scope(project_id)),
        ]
        rows = self._session.execute(union_all(*branches)).mappings()
        totals: dict[str, int] = {entity_type: 0 for entity_type in _PERSISTED_NODE_TYPES}
        statuses: dict[str, dict[str, int]] = {
            entity_type: {} for entity_type in _PERSISTED_NODE_TYPES
        }
        for row in rows:
            entity_type = str(row["entity_type"])
            count_value = int(row["record_count"])
            totals[entity_type] += count_value
            if row["status"] is not None:
                statuses[entity_type][str(row["status"])] = count_value
        return {
            entity_type: GraphEntityCount(
                total=totals[entity_type],
                by_status=dict(sorted(statuses[entity_type].items())),
            )
            for entity_type in _PERSISTED_NODE_TYPES
        }

    def _recent_keys(self, project_id: UUID) -> list[NodeKey]:
        branches = _recent_branches(project_id)
        recent = union_all(*branches).subquery("recent_graph_nodes")
        type_order = case(
            _NODE_TYPE_ORDER,
            value=recent.c.entity_type,
            else_=99,
        )
        rows = self._session.execute(
            select(recent.c.entity_type, recent.c.entity_id)
            .order_by(recent.c.updated_at.desc(), type_order, recent.c.entity_id)
            .limit(10)
        )
        return [(str(row.entity_type), str(row.entity_id)) for row in rows]

    def _search_branches(
        self,
        project_id: UUID,
        *,
        query: str,
        entity_types: Sequence[str],
        statuses: Sequence[str] | None,
    ) -> list[Any]:
        selected = set(entity_types)
        branches: list[Any] = []
        if "question" in selected:
            branches.append(
                _search_branch(
                    "question",
                    QuestionModel.question_id,
                    QuestionModel.updated_at,
                    QuestionModel.status,
                    project_scope=QuestionModel.project_id == str(project_id),
                    query=query,
                    fields=(
                        ("title", QuestionModel.text, True),
                        ("hypothesis", QuestionModel.hypothesis, False),
                    ),
                    statuses=statuses,
                )
            )
        if "session" in selected:
            alternate_id: UUID | None = None
            with suppress(ValueError):
                alternate_id = decode_session_link_code(query)
            branches.append(
                _search_branch(
                    "session",
                    SessionModel.session_id,
                    SessionModel.updated_at,
                    SessionModel.status,
                    project_scope=SessionModel.project_id == str(project_id),
                    query=query,
                    fields=(("session_type", SessionModel.session_type, False),),
                    statuses=statuses,
                    alternate_exact_id=alternate_id,
                )
            )
        if "note" in selected:
            branches.append(
                _search_branch(
                    "note",
                    NoteModel.note_id,
                    NoteModel.updated_at,
                    NoteModel.status,
                    project_scope=NoteModel.project_id == str(project_id),
                    query=query,
                    fields=(
                        ("transcribed_text", NoteModel.transcribed_text, False),
                        ("raw_content", NoteModel.raw_content, False),
                        ("raw_filename", NoteModel.raw_filename, False),
                        ("metadata", cast(NoteModel.note_metadata, String()), False),
                    ),
                    statuses=statuses,
                )
            )
        if "dataset" in selected:
            branches.append(
                _search_branch(
                    "dataset",
                    DatasetModel.dataset_id,
                    DatasetModel.updated_at,
                    DatasetModel.status,
                    project_scope=DatasetModel.project_id == str(project_id),
                    query=query,
                    fields=(
                        ("commit_hash", DatasetModel.commit_hash, False),
                        ("metadata", cast(DatasetModel.manifest_metadata, String()), True),
                        ("nwb_metadata", cast(DatasetModel.manifest_nwb_metadata, String()), False),
                        (
                            "bids_metadata",
                            cast(DatasetModel.manifest_bids_metadata, String()),
                            False,
                        ),
                        (
                            "external_artifacts",
                            cast(DatasetModel.manifest_external_artifacts, String()),
                            False,
                        ),
                        ("files", cast(DatasetModel.manifest_files, String()), False),
                    ),
                    statuses=statuses,
                )
            )
        if "analysis" in selected:
            branches.append(
                _search_branch(
                    "analysis",
                    AnalysisModel.analysis_id,
                    AnalysisModel.updated_at,
                    AnalysisModel.status,
                    project_scope=AnalysisModel.project_id == str(project_id),
                    query=query,
                    fields=(
                        ("code_version", AnalysisModel.code_version, True),
                        ("method_hash", AnalysisModel.method_hash, False),
                        ("environment_hash", AnalysisModel.environment_hash, False),
                        (
                            "external_artifacts",
                            cast(AnalysisModel.external_artifacts, String()),
                            False,
                        ),
                    ),
                    statuses=statuses,
                )
            )
        if "claim" in selected:
            branches.append(
                _search_branch(
                    "claim",
                    ClaimModel.claim_id,
                    ClaimModel.updated_at,
                    ClaimModel.status,
                    project_scope=ClaimModel.project_id == str(project_id),
                    query=query,
                    fields=(
                        ("statement", ClaimModel.statement, True),
                        ("falsification_criteria", ClaimModel.falsification_criteria, False),
                        ("verification_plan", ClaimModel.verification_plan, False),
                        ("refuting_outcome", ClaimModel.refuting_outcome, False),
                        (
                            "external_citations",
                            cast(ClaimModel.external_citations, String()),
                            False,
                        ),
                    ),
                    statuses=statuses,
                )
            )
        if "exploration_node" in selected:
            branches.append(
                _search_branch(
                    "exploration_node",
                    ExplorationNodeModel.node_id,
                    ExplorationNodeModel.updated_at,
                    ExplorationNodeModel.status,
                    project_scope=ExplorationNodeModel.project_id == str(project_id),
                    query=query,
                    fields=(
                        ("title", ExplorationNodeModel.title, True),
                        ("choice", ExplorationNodeModel.choice, False),
                        (
                            "alternatives_considered",
                            cast(ExplorationNodeModel.alternatives_considered, String()),
                            False,
                        ),
                        ("rationale", ExplorationNodeModel.rationale, False),
                        ("hypothesis", ExplorationNodeModel.hypothesis, False),
                        ("failure_mode", ExplorationNodeModel.failure_mode, False),
                        ("lesson", ExplorationNodeModel.lesson, False),
                        ("tooling_context", ExplorationNodeModel.tooling_context, False),
                        ("trigger", ExplorationNodeModel.trigger, False),
                    ),
                    statuses=statuses,
                )
            )
        if "visualization" in selected:
            branches.append(
                _search_branch(
                    "visualization",
                    VisualizationModel.viz_id,
                    VisualizationModel.updated_at,
                    None,
                    project_scope=AnalysisModel.project_id == str(project_id),
                    query=query,
                    fields=(
                        ("caption", VisualizationModel.caption, True),
                        ("viz_type", VisualizationModel.viz_type, False),
                        ("file_path", VisualizationModel.file_path, False),
                        ("asset_filename", VisualizationModel.asset_filename, False),
                        ("asset_checksum", VisualizationModel.asset_checksum, False),
                    ),
                    statuses=statuses,
                    select_from=VisualizationModel,
                    joins=(
                        (
                            AnalysisModel,
                            AnalysisModel.analysis_id == VisualizationModel.analysis_id,
                        ),
                    ),
                )
            )
        if "goal" in selected:
            branches.append(
                _search_branch(
                    "goal",
                    GoalModel.goal_id,
                    GoalModel.updated_at,
                    GoalModel.status,
                    project_scope=_goal_project_scope(project_id),
                    query=query,
                    fields=(
                        ("title", GoalModel.title, True),
                        ("summary", GoalModel.summary, False),
                        ("external_ref", GoalModel.external_ref, False),
                        ("attributes", cast(GoalModel.attributes, String()), False),
                    ),
                    statuses=statuses,
                )
            )
        return branches

    def _hydrate_nodes(
        self,
        project_id: UUID,
        keys: Iterable[NodeKey],
    ) -> dict[NodeKey, _HydratedNode]:
        grouped: dict[str, set[str]] = defaultdict(set)
        for entity_type, entity_id in keys:
            grouped[entity_type].add(entity_id)
        hydrated: dict[NodeKey, _HydratedNode] = {}
        specs: dict[str, tuple[Any, Any, Any]] = {
            "question": (
                QuestionModel,
                QuestionModel.question_id,
                QuestionModel.project_id == str(project_id),
            ),
            "session": (
                SessionModel,
                SessionModel.session_id,
                SessionModel.project_id == str(project_id),
            ),
            "note": (
                NoteModel,
                NoteModel.note_id,
                NoteModel.project_id == str(project_id),
            ),
            "dataset": (
                DatasetModel,
                DatasetModel.dataset_id,
                DatasetModel.project_id == str(project_id),
            ),
            "analysis": (
                AnalysisModel,
                AnalysisModel.analysis_id,
                AnalysisModel.project_id == str(project_id),
            ),
            "claim": (
                ClaimModel,
                ClaimModel.claim_id,
                ClaimModel.project_id == str(project_id),
            ),
            "exploration_node": (
                ExplorationNodeModel,
                ExplorationNodeModel.node_id,
                ExplorationNodeModel.project_id == str(project_id),
            ),
            "goal": (GoalModel, GoalModel.goal_id, _goal_project_scope(project_id)),
        }
        for entity_type, (model, id_column, scope) in specs.items():
            ids = grouped.get(entity_type)
            if not ids:
                continue
            rows = self._session.scalars(select(model).where(scope, id_column.in_(sorted(ids))))
            for row in rows:
                key, value = _hydrate_row(entity_type, row)
                hydrated[key] = value

        visualization_ids = grouped.get("visualization")
        if visualization_ids:
            rows = self._session.scalars(
                select(VisualizationModel)
                .join(
                    AnalysisModel,
                    AnalysisModel.analysis_id == VisualizationModel.analysis_id,
                )
                .where(
                    AnalysisModel.project_id == str(project_id),
                    VisualizationModel.viz_id.in_(sorted(visualization_ids)),
                )
            )
            for row in rows:
                key, value = _hydrate_row("visualization", row)
                hydrated[key] = value
        return hydrated

    def _edge_candidates(
        self,
        project_id: UUID,
        *,
        frontier: Sequence[NodeKey],
        direction: DirectionValue,
        relationships: set[str] | None,
        node_types: set[str] | None,
        limit: int,
    ) -> list[_EdgeCandidate]:
        project_value = str(project_id)
        branches: list[Any] = []

        def add(
            source_type: str | Any,
            source_id: Any,
            target_type: str | Any,
            target_id: Any,
            label: str | Any,
            relationship: str | Any,
            *,
            select_from: Any,
            joins: Sequence[tuple[Any, Any]] = (),
            where: Sequence[Any] = (),
            identity: Any | None = None,
        ) -> None:
            branch = _edge_branch(
                source_type,
                source_id,
                target_type,
                target_id,
                label,
                relationship,
                identity=identity,
                select_from=select_from,
                joins=joins,
                where=where,
                frontier=frontier,
                direction=direction,
                node_types=node_types,
            )
            if branch is not None:
                branches.append(branch)

        add(
            "question",
            QuestionParentModel.parent_question_id,
            "question",
            QuestionParentModel.question_id,
            "parent",
            "question_parent",
            select_from=QuestionParentModel,
            joins=(
                (
                    QuestionModel,
                    QuestionModel.question_id == QuestionParentModel.question_id,
                ),
            ),
            where=(QuestionModel.project_id == project_value,),
        )
        add(
            "question",
            QuestionModel.question_id,
            "question",
            QuestionModel.superseded_by_question_id,
            "superseded by",
            "question_superseded_by",
            select_from=QuestionModel,
            where=(
                QuestionModel.project_id == project_value,
                QuestionModel.superseded_by_question_id.is_not(None),
            ),
        )
        add(
            "question",
            QuestionModel.question_id,
            "question",
            QuestionModel.supersedes_question_id,
            "supersedes",
            "question_supersedes",
            select_from=QuestionModel,
            where=(
                QuestionModel.project_id == project_value,
                QuestionModel.supersedes_question_id.is_not(None),
            ),
        )
        question_role = cast(DatasetQuestionLinkModel.role, String())
        add(
            "question",
            DatasetQuestionLinkModel.question_id,
            "dataset",
            DatasetQuestionLinkModel.dataset_id,
            question_role + literal(" question"),
            literal("dataset_question_") + question_role,
            select_from=DatasetQuestionLinkModel,
            joins=(
                (
                    DatasetModel,
                    DatasetModel.dataset_id == DatasetQuestionLinkModel.dataset_id,
                ),
            ),
            where=(DatasetModel.project_id == project_value,),
        )
        add(
            "dataset",
            AnalysisDatasetModel.dataset_id,
            "analysis",
            AnalysisDatasetModel.analysis_id,
            "used by",
            "analysis_dataset",
            select_from=AnalysisDatasetModel,
            joins=(
                (
                    AnalysisModel,
                    AnalysisModel.analysis_id == AnalysisDatasetModel.analysis_id,
                ),
            ),
            where=(AnalysisModel.project_id == project_value,),
        )
        add(
            "dataset",
            ClaimDatasetModel.dataset_id,
            "claim",
            ClaimDatasetModel.claim_id,
            "supports",
            "claim_dataset_support",
            select_from=ClaimDatasetModel,
            joins=((ClaimModel, ClaimModel.claim_id == ClaimDatasetModel.claim_id),),
            where=(ClaimModel.project_id == project_value,),
        )
        add(
            "analysis",
            ClaimAnalysisModel.analysis_id,
            "claim",
            ClaimAnalysisModel.claim_id,
            "supports",
            "claim_analysis_support",
            select_from=ClaimAnalysisModel,
            joins=((ClaimModel, ClaimModel.claim_id == ClaimAnalysisModel.claim_id),),
            where=(ClaimModel.project_id == project_value,),
        )
        add(
            "claim",
            ClaimQuestionModel.claim_id,
            "question",
            ClaimQuestionModel.question_id,
            "answers",
            "claim_question_answers",
            select_from=ClaimQuestionModel,
            joins=((ClaimModel, ClaimModel.claim_id == ClaimQuestionModel.claim_id),),
            where=(ClaimModel.project_id == project_value,),
        )
        claim_relation = cast(ClaimEdgeModel.relation, String())
        add(
            "claim",
            ClaimEdgeModel.claim_id,
            "claim",
            ClaimEdgeModel.target_claim_id,
            func.replace(claim_relation, "_", " "),
            literal("claim_relation_") + claim_relation,
            select_from=ClaimEdgeModel,
            joins=((ClaimModel, ClaimModel.claim_id == ClaimEdgeModel.claim_id),),
            where=(ClaimModel.project_id == project_value,),
        )
        add(
            cast(ExplorationNodeModel.target_entity_type, String()),
            ExplorationNodeModel.target_entity_id,
            "exploration_node",
            ExplorationNodeModel.node_id,
            "concerns",
            "exploration_target",
            select_from=ExplorationNodeModel,
            where=(ExplorationNodeModel.project_id == project_value,),
        )
        exploration_relation = case(
            (
                ExplorationNodeEdgeModel.relation == "parent",
                literal("exploration_parent"),
            ),
            else_=literal("exploration_dependency"),
        )
        exploration_label = case(
            (ExplorationNodeEdgeModel.relation == "parent", literal("parent")),
            else_=literal("depends on"),
        )
        add(
            "exploration_node",
            ExplorationNodeEdgeModel.source_node_id,
            "exploration_node",
            ExplorationNodeEdgeModel.target_node_id,
            exploration_label,
            exploration_relation,
            select_from=ExplorationNodeEdgeModel,
            joins=(
                (
                    ExplorationNodeModel,
                    ExplorationNodeModel.node_id == ExplorationNodeEdgeModel.target_node_id,
                ),
            ),
            where=(ExplorationNodeModel.project_id == project_value,),
        )
        add(
            "exploration_node",
            ExplorationNodeModel.node_id,
            "exploration_node",
            ExplorationNodeModel.invalidates_node_id,
            "invalidates",
            "exploration_invalidates_node",
            select_from=ExplorationNodeModel,
            where=(
                ExplorationNodeModel.project_id == project_value,
                ExplorationNodeModel.invalidates_node_id.is_not(None),
            ),
        )
        add(
            "exploration_node",
            ExplorationNodeModel.node_id,
            "claim",
            ExplorationNodeModel.invalidates_claim_id,
            "invalidates",
            "exploration_invalidates_claim",
            select_from=ExplorationNodeModel,
            where=(
                ExplorationNodeModel.project_id == project_value,
                ExplorationNodeModel.invalidates_claim_id.is_not(None),
            ),
        )
        add(
            "analysis",
            VisualizationModel.analysis_id,
            "visualization",
            VisualizationModel.viz_id,
            "generates",
            "visualization_analysis",
            select_from=VisualizationModel,
            joins=(
                (
                    AnalysisModel,
                    AnalysisModel.analysis_id == VisualizationModel.analysis_id,
                ),
            ),
            where=(AnalysisModel.project_id == project_value,),
        )
        add(
            "dataset",
            AnalysisDatasetModel.dataset_id,
            "visualization",
            VisualizationModel.viz_id,
            "grounds",
            "visualization_dataset",
            select_from=VisualizationModel,
            joins=(
                (
                    AnalysisModel,
                    AnalysisModel.analysis_id == VisualizationModel.analysis_id,
                ),
                (
                    AnalysisDatasetModel,
                    AnalysisDatasetModel.analysis_id == AnalysisModel.analysis_id,
                ),
            ),
            where=(AnalysisModel.project_id == project_value,),
        )
        add(
            "claim",
            VisualizationClaimModel.claim_id,
            "visualization",
            VisualizationClaimModel.viz_id,
            "related claim",
            "visualization_claim",
            select_from=VisualizationClaimModel,
            joins=(
                (
                    VisualizationModel,
                    VisualizationModel.viz_id == VisualizationClaimModel.viz_id,
                ),
                (
                    AnalysisModel,
                    AnalysisModel.analysis_id == VisualizationModel.analysis_id,
                ),
            ),
            where=(AnalysisModel.project_id == project_value,),
        )
        goal_relation = cast(GoalLinkModel.relation, String())
        goal_status = cast(GoalLinkModel.link_status, String())
        goal_label = func.replace(goal_relation, "_", " ") + case(
            (
                GoalLinkModel.slot != "",
                literal(": ") + GoalLinkModel.slot,
            ),
            else_=literal(""),
        )
        add(
            cast(GoalLinkModel.entity_type, String()),
            GoalLinkModel.entity_id,
            "goal",
            GoalLinkModel.goal_id,
            goal_label,
            literal("goal_") + goal_relation + literal("_") + goal_status,
            identity=case(
                (
                    GoalLinkModel.slot != "",
                    literal("goal-link=") + cast(GoalLinkModel.link_id, String()),
                ),
                else_=null(),
            ),
            select_from=GoalLinkModel,
            joins=((GoalModel, GoalModel.goal_id == GoalLinkModel.goal_id),),
            where=(
                _goal_project_scope(project_id),
                GoalLinkModel.entity_type != EntityType.PROJECT.value,
            ),
        )
        add(
            "note",
            NoteTargetModel.note_id,
            cast(NoteTargetModel.entity_type, String()),
            NoteTargetModel.entity_id,
            "targets",
            literal("note_target_") + cast(NoteTargetModel.entity_type, String()),
            select_from=NoteTargetModel,
            joins=((NoteModel, NoteModel.note_id == NoteTargetModel.note_id),),
            where=(
                NoteModel.project_id == project_value,
                NoteTargetModel.entity_type != EntityType.PROJECT.value,
            ),
        )
        add(
            "note",
            ProvenanceLinkModel.source_entity_id,
            "note",
            ProvenanceLinkModel.target_entity_id,
            "derived from",
            "note_was_derived_from",
            identity=literal("provenance-link=")
            + cast(ProvenanceLinkModel.link_id, String()),
            select_from=ProvenanceLinkModel,
            joins=(
                (
                    NoteModel,
                    NoteModel.note_id == ProvenanceLinkModel.source_entity_id,
                ),
            ),
            where=(
                ProvenanceLinkModel.project_id == project_value,
                NoteModel.project_id == project_value,
                ProvenanceLinkModel.status == ProvenanceLinkStatus.ACCEPTED.value,
                ProvenanceLinkModel.relation
                == ProvenanceLinkRelation.WAS_DERIVED_FROM.value,
                ProvenanceLinkModel.source_entity_type == EntityType.NOTE.value,
                ProvenanceLinkModel.target_entity_type == EntityType.NOTE.value,
            ),
        )
        add(
            "question",
            SessionModel.primary_question_id,
            "session",
            SessionModel.session_id,
            "session question",
            "session_question",
            select_from=SessionModel,
            where=(
                SessionModel.project_id == project_value,
                SessionModel.primary_question_id.is_not(None),
            ),
        )
        add(
            "session",
            DatasetModel.manifest_source_session_id,
            "dataset",
            DatasetModel.dataset_id,
            "source session",
            "dataset_source_session",
            select_from=DatasetModel,
            where=(
                DatasetModel.project_id == project_value,
                DatasetModel.manifest_source_session_id.is_not(None),
            ),
        )
        evidence_branch = self._exploration_evidence_branch(
            project_id,
            frontier=frontier,
            direction=direction,
            node_types=node_types,
        )
        if evidence_branch is not None:
            branches.append(evidence_branch)
        manifest_note_branch = self._manifest_note_branch(
            project_id,
            frontier=frontier,
            direction=direction,
            node_types=node_types,
        )
        if manifest_note_branch is not None:
            branches.append(manifest_note_branch)
        if not branches:
            return []
        candidates = union_all(*branches).subquery("graph_edge_candidates")
        statement = select(candidates)
        if relationships is not None:
            statement = statement.where(candidates.c.relationship.in_(relationships))
        source_order = case(
            _NODE_TYPE_ORDER,
            value=candidates.c.source_type,
            else_=99,
        )
        target_order = case(
            _NODE_TYPE_ORDER,
            value=candidates.c.target_type,
            else_=99,
        )
        statement = statement.order_by(
            source_order,
            candidates.c.source_id,
            target_order,
            candidates.c.target_id,
            candidates.c.relationship,
            candidates.c.label,
            candidates.c.identity,
        ).limit(limit)
        return [
            _EdgeCandidate(
                source=(str(row["source_type"]), str(row["source_id"])),
                target=(str(row["target_type"]), str(row["target_id"])),
                label=str(row["label"]),
                relationship=str(row["relationship"]),
                identity=str(row["identity"]) if row["identity"] else None,
            )
            for row in self._session.execute(statement).mappings()
        ]

    def _exploration_evidence_branch(
        self,
        project_id: UUID,
        *,
        frontier: Sequence[NodeKey],
        direction: DirectionValue,
        node_types: set[str] | None,
    ) -> Any | None:
        dialect = self._session.get_bind().dialect.name
        source_type: Any
        source_id: Any
        if dialect == "sqlite":
            references = (
                func.json_each(ExplorationNodeModel.evidence_refs)
                .table_valued("key", "value")
                .alias("exploration_evidence_ref")
            )
            source_type = func.json_extract(references.c.value, "$.entity_type")
            source_id = func.json_extract(references.c.value, "$.entity_id")
        elif dialect == "postgresql":
            references = (
                func.json_array_elements(ExplorationNodeModel.evidence_refs)
                .table_valued("value")
                .render_derived()
                .alias("exploration_evidence_ref")
            )
            source_type = references.c.value.op("->>")("entity_type")
            source_id = references.c.value.op("->>")("entity_id")
        else:
            return None
        return _edge_branch(
            source_type,
            source_id,
            "exploration_node",
            ExplorationNodeModel.node_id,
            "evidence",
            "exploration_evidence",
            select_from=ExplorationNodeModel,
            joins=((references, true()),),
            where=(ExplorationNodeModel.project_id == str(project_id),),
            frontier=frontier,
            direction=direction,
            node_types=node_types,
        )

    def _manifest_note_branch(
        self,
        project_id: UUID,
        *,
        frontier: Sequence[NodeKey],
        direction: DirectionValue,
        node_types: set[str] | None,
    ) -> Any | None:
        dialect = self._session.get_bind().dialect.name
        if dialect == "sqlite":
            references = (
                func.json_each(DatasetModel.manifest_note_ids)
                .table_valued("key", "value")
                .alias("dataset_manifest_note")
            )
            note_id = cast(references.c.value, String())
        elif dialect == "postgresql":
            references = (
                func.json_array_elements_text(DatasetModel.manifest_note_ids)
                .table_valued("value")
                .render_derived()
                .alias("dataset_manifest_note")
            )
            note_id = cast(references.c.value, String())
        else:
            return None
        project_value = str(project_id)
        return _edge_branch(
            "note",
            note_id,
            "dataset",
            DatasetModel.dataset_id,
            "commit note",
            "dataset_manifest_note",
            select_from=DatasetModel,
            joins=(
                (references, true()),
                (NoteModel, cast(NoteModel.note_id, String()) == note_id),
            ),
            where=(
                DatasetModel.project_id == project_value,
                NoteModel.project_id == project_value,
            ),
            frontier=frontier,
            direction=direction,
            node_types=node_types,
        )

    def _citation_candidates(
        self,
        project_id: UUID,
        *,
        frontier: Sequence[NodeKey],
        direction: DirectionValue,
        relationships: set[str] | None,
        node_types: set[str] | None,
    ) -> tuple[list[_EdgeCandidate], dict[NodeKey, _HydratedNode]]:
        if direction == "incoming":
            return [], {}
        if relationships is not None and "claim_cites" not in relationships:
            return [], {}
        if node_types is not None and "external_artifact" not in node_types:
            return [], {}
        claim_ids = sorted(
            entity_id for entity_type, entity_id in frontier if entity_type == "claim"
        )
        if not claim_ids:
            return [], {}
        rows = self._session.scalars(
            select(ClaimModel).where(
                ClaimModel.project_id == str(project_id),
                ClaimModel.claim_id.in_(claim_ids),
            )
        )
        candidates: list[_EdgeCandidate] = []
        nodes: dict[NodeKey, _HydratedNode] = {}
        for row in rows:
            for raw_artifact in row.external_citations or []:
                artifact = ExternalArtifactReference.model_validate(raw_artifact)
                key = ("external_artifact", artifact.uri)
                candidates.append(
                    _EdgeCandidate(
                        source=("claim", str(row.claim_id)),
                        target=key,
                        label="cites",
                        relationship="claim_cites",
                    )
                )
                content = json.dumps(
                    artifact.model_dump(mode="json"),
                    sort_keys=True,
                    ensure_ascii=False,
                )
                nodes[key] = _HydratedNode(
                    summary=GraphNodeSummary(
                        id=_node_id(key),
                        entity_type="external_artifact",
                        entity_id=artifact.uri,
                        label=_compact(artifact.uri),
                        detail=artifact.source_system,
                        metadata={
                            "kind": artifact.kind.value,
                            "content_hash": artifact.content_hash,
                            "metadata": artifact.metadata,
                        },
                    ),
                    content=content,
                    document=None,
                )
        return candidates, nodes


def _goal_project_scope(project_id: UUID) -> Any:
    project_value = str(project_id)
    linked_goal_ids = select(GoalLinkModel.goal_id).where(
        GoalLinkModel.entity_type == EntityType.PROJECT.value,
        GoalLinkModel.entity_id == project_value,
    )
    return or_(
        GoalModel.project_id == project_value,
        and_(GoalModel.project_id.is_(None), GoalModel.goal_id.in_(linked_goal_ids)),
    )


def _count_branch(entity_type: str, status_column: Any, scope: Any) -> Any:
    return (
        select(
            literal(entity_type).label("entity_type"),
            cast(status_column, String()).label("status"),
            func.count().label("record_count"),
        )
        .where(scope)
        .group_by(status_column)
    )


def _recent_branch(
    entity_type: str,
    id_column: Any,
    updated_at: Any,
    scope: Any,
    *,
    select_from: Any | None = None,
    joins: Sequence[tuple[Any, Any]] = (),
) -> Any:
    statement = select(
        literal(entity_type).label("entity_type"),
        cast(id_column, String()).label("entity_id"),
        updated_at.label("updated_at"),
    )
    if select_from is not None:
        statement = statement.select_from(select_from)
    for target, on_clause in joins:
        statement = statement.join(target, on_clause)
    return statement.where(scope)


def _recent_branches(project_id: UUID) -> list[Any]:
    project_value = str(project_id)
    return [
        _recent_branch(
            "question",
            QuestionModel.question_id,
            QuestionModel.updated_at,
            QuestionModel.project_id == project_value,
        ),
        _recent_branch(
            "session",
            SessionModel.session_id,
            SessionModel.updated_at,
            SessionModel.project_id == project_value,
        ),
        _recent_branch(
            "note",
            NoteModel.note_id,
            NoteModel.updated_at,
            NoteModel.project_id == project_value,
        ),
        _recent_branch(
            "dataset",
            DatasetModel.dataset_id,
            DatasetModel.updated_at,
            DatasetModel.project_id == project_value,
        ),
        _recent_branch(
            "analysis",
            AnalysisModel.analysis_id,
            AnalysisModel.updated_at,
            AnalysisModel.project_id == project_value,
        ),
        _recent_branch(
            "claim",
            ClaimModel.claim_id,
            ClaimModel.updated_at,
            ClaimModel.project_id == project_value,
        ),
        _recent_branch(
            "exploration_node",
            ExplorationNodeModel.node_id,
            ExplorationNodeModel.updated_at,
            ExplorationNodeModel.project_id == project_value,
        ),
        _recent_branch(
            "visualization",
            VisualizationModel.viz_id,
            VisualizationModel.updated_at,
            AnalysisModel.project_id == project_value,
            select_from=VisualizationModel,
            joins=(
                (
                    AnalysisModel,
                    AnalysisModel.analysis_id == VisualizationModel.analysis_id,
                ),
            ),
        ),
        _recent_branch(
            "goal",
            GoalModel.goal_id,
            GoalModel.updated_at,
            _goal_project_scope(project_id),
        ),
    ]


def _search_branch(
    entity_type: str,
    id_column: Any,
    updated_at: Any,
    status_column: Any | None,
    *,
    project_scope: Any,
    query: str,
    fields: Sequence[tuple[str, Any, bool]],
    statuses: Sequence[str] | None,
    alternate_exact_id: UUID | None = None,
    select_from: Any | None = None,
    joins: Sequence[tuple[Any, Any]] = (),
) -> Any:
    id_text = cast(id_column, String())
    escaped_substring = substring_pattern(query) or "%"
    escaped_prefix = escaped_substring[1:]
    exact_query = query.casefold()
    exact_id_conditions = [func.lower(id_text) == exact_query]
    if alternate_exact_id is not None:
        exact_id_conditions.append(id_text == str(alternate_exact_id))
    exact_id = or_(*exact_id_conditions)
    field_matches: list[tuple[str, Any, bool, Any, Any, Any]] = []
    for field_name, field_value, is_title in fields:
        text_value = cast(field_value, String())
        field_matches.append(
            (
                field_name,
                text_value,
                is_title,
                func.lower(text_value) == exact_query,
                text_value.ilike(escaped_prefix, escape="\\"),
                text_value.ilike(escaped_substring, escape="\\"),
            )
        )
    id_prefix = id_text.ilike(escaped_prefix, escape="\\")
    id_substring = id_text.ilike(escaped_substring, escape="\\")
    exact_title = [exact for _, _, is_title, exact, _, _ in field_matches if is_title]
    prefix_matches = [prefix for _, _, _, _, prefix, _ in field_matches]
    substring_matches = [substring for _, _, _, _, _, substring in field_matches]
    match_rank = case(
        (exact_id, 0),
        (or_(*exact_title), 0) if exact_title else (literal(False), 0),
        (or_(id_prefix, *prefix_matches), 1),
        else_=2,
    )
    match_reason = case(
        (exact_id, literal("exact_id")),
        (or_(*exact_title), literal("exact_title"))
        if exact_title
        else (literal(False), literal("exact_title")),
        (or_(id_prefix, *prefix_matches), literal("prefix")),
        else_=literal("substring"),
    )
    field_cases: list[tuple[Any, Any]] = [(exact_id, literal("id"))]
    text_cases: list[tuple[Any, Any]] = [(exact_id, id_text)]
    for field_name, text_value, _, exact, _, _ in field_matches:
        field_cases.append((exact, literal(field_name)))
        text_cases.append((exact, text_value))
    field_cases.append((id_prefix, literal("id")))
    text_cases.append((id_prefix, id_text))
    for field_name, text_value, _, _, prefix, _ in field_matches:
        field_cases.append((prefix, literal(field_name)))
        text_cases.append((prefix, text_value))
    field_cases.append((id_substring, literal("id")))
    text_cases.append((id_substring, id_text))
    for field_name, text_value, _, _, _, substring in field_matches:
        field_cases.append((substring, literal(field_name)))
        text_cases.append((substring, text_value))
    statement = select(
        literal(entity_type).label("entity_type"),
        id_text.label("entity_id"),
        updated_at.label("updated_at"),
        match_rank.label("match_rank"),
        match_reason.label("match_reason"),
        case(*field_cases, else_=literal("id")).label("matched_field"),
        case(*text_cases, else_=id_text).label("matched_text"),
    )
    if select_from is not None:
        statement = statement.select_from(select_from)
    for target, on_clause in joins:
        statement = statement.join(target, on_clause)
    conditions: list[Any] = [
        project_scope,
        or_(exact_id, id_substring, *substring_matches),
    ]
    if statuses is not None:
        if status_column is None:
            conditions.append(literal(False))
        else:
            conditions.append(cast(status_column, String()).in_(statuses))
    return statement.where(*conditions)


def _edge_branch(
    source_type: str | Any,
    source_id: Any,
    target_type: str | Any,
    target_id: Any,
    label: str | Any,
    relationship: str | Any,
    *,
    select_from: Any,
    joins: Sequence[tuple[Any, Any]],
    where: Sequence[Any],
    frontier: Sequence[NodeKey],
    direction: DirectionValue,
    node_types: set[str] | None,
    identity: Any | None = None,
) -> Any | None:
    source_type_expr = _as_text_expression(source_type)
    source_id_expr = cast(source_id, String())
    target_type_expr = _as_text_expression(target_type)
    target_id_expr = cast(target_id, String())
    candidate_condition = _candidate_condition(
        source_type_expr,
        source_id_expr,
        target_type_expr,
        target_id_expr,
        frontier=frontier,
        direction=direction,
        node_types=node_types,
    )
    if candidate_condition is None:
        return None
    statement = select(
        source_type_expr.label("source_type"),
        source_id_expr.label("source_id"),
        target_type_expr.label("target_type"),
        target_id_expr.label("target_id"),
        _as_text_expression(label).label("label"),
        _as_text_expression(relationship).label("relationship"),
        (cast(identity, String()) if identity is not None else cast(null(), String())).label(
            "identity"
        ),
    ).select_from(select_from)
    for target, on_clause in joins:
        statement = statement.join(target, on_clause)
    return statement.where(*where, candidate_condition)


def _candidate_condition(
    source_type: Any,
    source_id: Any,
    target_type: Any,
    target_id: Any,
    *,
    frontier: Sequence[NodeKey],
    direction: DirectionValue,
    node_types: set[str] | None,
) -> Any | None:
    source_in_frontier = _frontier_predicate(source_type, source_id, frontier)
    target_in_frontier = _frontier_predicate(target_type, target_id, frontier)
    source_allowed = _node_type_allowed(source_type, node_types)
    target_allowed = _node_type_allowed(target_type, node_types)
    if direction == "outgoing":
        if source_in_frontier is None:
            return None
        return and_(source_in_frontier, target_allowed)
    if direction == "incoming":
        if target_in_frontier is None:
            return None
        return and_(target_in_frontier, source_allowed)
    candidates = []
    if source_in_frontier is not None:
        candidates.append(and_(source_in_frontier, target_allowed))
    if target_in_frontier is not None:
        candidates.append(and_(target_in_frontier, source_allowed))
    return or_(*candidates) if candidates else None


def _frontier_predicate(
    entity_type: Any,
    entity_id: Any,
    frontier: Sequence[NodeKey],
) -> Any | None:
    grouped: dict[str, list[str]] = defaultdict(list)
    for node_type, node_id in frontier:
        grouped[node_type].append(node_id)
    if not grouped:
        return None
    return or_(
        *(
            and_(entity_type == node_type, entity_id.in_(sorted(node_ids)))
            for node_type, node_ids in sorted(grouped.items())
        )
    )


def _node_type_allowed(entity_type: Any, node_types: set[str] | None) -> Any:
    if node_types is None:
        return literal(True)
    return entity_type.in_(sorted(node_types))


def _as_text_expression(value: str | Any) -> Any:
    if isinstance(value, str):
        return literal(value)
    return cast(value, String())


def _hydrate_row(entity_type: str, row: Any) -> tuple[NodeKey, _HydratedNode]:
    document = _GRAPH_DOCUMENT_RENDERER.render(entity_type, row)
    return document.key, _HydratedNode(
        summary=document.summary,
        content=document.content,
        document=document,
    )

    # The legacy inline rendering below remains temporarily as a readable
    # compatibility reference while all graph consumers move to the canonical
    # renderer above. It is unreachable and will be removed after the migration
    # window.
    if entity_type == "question":
        entity_id = str(row.question_id)
        label = _compact(row.text)
        content = _join_content(row.text, row.hypothesis)
        summary = GraphNodeSummary(
            id=f"question:{entity_id}",
            entity_type="question",
            entity_id=entity_id,
            label=label,
            detail=_enum_value(row.question_type),
            status=_enum_value(row.status),
            route=f"/app/questions/{entity_id}",
            updated_at=row.updated_at,
            metadata={
                "hypothesis": row.hypothesis or "",
                "question_type": _enum_value(row.question_type) or "",
            },
        )
    elif entity_type == "session":
        entity_id = str(row.session_id)
        link_code = encode_session_link_code(UUID(entity_id))
        content = json.dumps(
            {
                "session_id": entity_id,
                "link_code": link_code,
                "session_type": _enum_value(row.session_type),
                "status": _enum_value(row.status),
                "primary_question_id": (
                    str(row.primary_question_id) if row.primary_question_id else None
                ),
                "started_at": row.started_at.isoformat(),
                "ended_at": row.ended_at.isoformat() if row.ended_at else None,
            },
            sort_keys=True,
        )
        summary = GraphNodeSummary(
            id=f"session:{entity_id}",
            entity_type="session",
            entity_id=entity_id,
            label=f"{_enum_value(row.session_type) or 'session'} session",
            detail=link_code,
            status=_enum_value(row.status),
            route=f"/app/sessions/{entity_id}",
            updated_at=row.updated_at,
        )
    elif entity_type == "note":
        entity_id = str(row.note_id)
        raw_content = row.transcribed_text or row.raw_content or row.raw_filename or ""
        content = raw_content
        summary = GraphNodeSummary(
            id=f"note:{entity_id}",
            entity_type="note",
            entity_id=entity_id,
            label=_compact(raw_content or f"Note {_short_id(entity_id)}"),
            status=_enum_value(row.status),
            route=f"/app/notes/{entity_id}",
            updated_at=row.updated_at,
        )
    elif entity_type == "dataset":
        entity_id = str(row.dataset_id)
        label = _dataset_label(row)
        content = json.dumps(
            {
                "commit_hash": row.commit_hash,
                "files": row.manifest_files or [],
                "external_artifacts": row.manifest_external_artifacts or [],
                "metadata": row.manifest_metadata or {},
                "nwb_metadata": row.manifest_nwb_metadata or {},
                "bids_metadata": row.manifest_bids_metadata or {},
                "note_ids": row.manifest_note_ids or [],
                "source_session_id": (
                    str(row.manifest_source_session_id) if row.manifest_source_session_id else None
                ),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        summary = GraphNodeSummary(
            id=f"dataset:{entity_id}",
            entity_type="dataset",
            entity_id=entity_id,
            label=label,
            detail=row.commit_hash,
            status=_enum_value(row.status),
            route=f"/app/datasets/{entity_id}",
            updated_at=row.updated_at,
        )
    elif entity_type == "analysis":
        entity_id = str(row.analysis_id)
        content = json.dumps(
            {
                "method_hash": row.method_hash,
                "code_version": row.code_version,
                "environment_hash": row.environment_hash,
                "external_artifacts": row.external_artifacts or [],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        summary = GraphNodeSummary(
            id=f"analysis:{entity_id}",
            entity_type="analysis",
            entity_id=entity_id,
            label=_compact(f"Analysis {row.code_version or _short_id(entity_id)}"),
            detail=row.method_hash,
            status=_enum_value(row.status),
            updated_at=row.updated_at,
            metadata={"code_version": row.code_version},
        )
    elif entity_type == "claim":
        entity_id = str(row.claim_id)
        content = _join_content(
            row.statement,
            row.falsification_criteria,
            row.verification_plan,
            row.refuting_outcome,
        )
        summary = GraphNodeSummary(
            id=f"claim:{entity_id}",
            entity_type="claim",
            entity_id=entity_id,
            label=_compact(row.statement),
            detail=f"confidence {row.confidence:g}",
            status=_enum_value(row.status),
            updated_at=row.updated_at,
        )
    elif entity_type == "exploration_node":
        entity_id = str(row.node_id)
        content = _join_content(
            row.title,
            row.choice,
            *(row.alternatives_considered or []),
            row.rationale,
            row.hypothesis,
            row.failure_mode,
            row.lesson,
            row.tooling_context,
            row.trigger,
        )
        summary = GraphNodeSummary(
            id=f"exploration_node:{entity_id}",
            entity_type="exploration_node",
            entity_id=entity_id,
            label=_compact(row.title),
            detail=(_enum_value(row.node_type) or "").replace("_", " "),
            status=_enum_value(row.status),
            updated_at=row.updated_at,
            metadata={
                "target_entity_type": _enum_value(row.target_entity_type) or "",
                "target_entity_id": str(row.target_entity_id),
                "origin": row.origin or "",
            },
        )
    elif entity_type == "goal":
        entity_id = str(row.goal_id)
        content = _join_content(row.title, row.summary, row.external_ref)
        summary = GraphNodeSummary(
            id=f"goal:{entity_id}",
            entity_type="goal",
            entity_id=entity_id,
            label=_compact(row.title),
            detail=_enum_value(row.goal_type),
            status=_enum_value(row.status),
            route=f"/app/goals/{entity_id}",
            updated_at=row.updated_at,
            metadata={
                "target_date": row.target_date.isoformat() if row.target_date else "",
                "external_ref": row.external_ref or "",
            },
        )
    elif entity_type == "visualization":
        entity_id = str(row.viz_id)
        content = json.dumps(
            {
                "viz_type": row.viz_type,
                "file_path": row.file_path,
                "caption": row.caption,
                "asset_filename": row.asset_filename,
                "asset_content_type": row.asset_content_type,
                "asset_checksum": row.asset_checksum,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        summary = GraphNodeSummary(
            id=f"visualization:{entity_id}",
            entity_type="visualization",
            entity_id=entity_id,
            label=_compact(row.caption or row.viz_type),
            detail=row.file_path,
            route=f"/app/visualizations/{entity_id}",
            updated_at=row.updated_at,
            metadata={"viz_type": row.viz_type},
        )
    else:  # pragma: no cover - callers constrain persisted entity types
        raise ValueError(f"Unsupported graph entity type: {entity_type}")
    key = (entity_type, entity_id)
    return key, _HydratedNode(summary=summary, content=content, document=document)


def _dataset_label(row: Any) -> str:
    metadata = row.manifest_metadata or {}
    name = str(metadata.get("dataset_name") or "").strip()
    files = row.manifest_files or []
    if not name and files:
        path = str(files[0].get("path") or "")
        name = path.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name:
        return _compact(name)
    return _compact(f"Dataset {row.commit_hash or _short_id(str(row.dataset_id))}")


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


def _short_id(value: str) -> str:
    return value.split("-", 1)[0]


def _compact(value: str) -> str:
    return str(value)[:_GRAPH_LABEL_LIMIT]


def _join_content(*values: object) -> str:
    return "\n\n".join(str(value) for value in values if value not in (None, ""))


def _bounded_snippet(value: str, query: str) -> str:
    compact_value = " ".join(str(value).split())
    if len(compact_value) <= _SEARCH_SNIPPET_LIMIT:
        return compact_value
    index = compact_value.casefold().find(query.casefold())
    if index < 0:
        return compact_value[: _SEARCH_SNIPPET_LIMIT - 1] + "…"
    half_window = (_SEARCH_SNIPPET_LIMIT - 2) // 2
    start = max(0, index - half_window)
    end = min(len(compact_value), start + _SEARCH_SNIPPET_LIMIT - 2)
    start = max(0, end - (_SEARCH_SNIPPET_LIMIT - 2))
    return (
        ("…" if start else "")
        + compact_value[start:end]
        + ("…" if end < len(compact_value) else "")
    )


def _semantic_chunk_snippet(document: GraphNodeDocument, chunk_index: int) -> str:
    chunks = document.chunks()
    if 0 <= chunk_index < len(chunks):
        return _compact(chunks[chunk_index].text)[:_SEARCH_SNIPPET_LIMIT]
    return _compact(document.semantic_text or document.content)[:_SEARCH_SNIPPET_LIMIT]


def _fusion_sort_key(
    key: NodeKey,
    *,
    lexical: tuple[int, _SearchRow] | None,
    semantic: Any | None,
    hydrated: _HydratedNode,
) -> tuple[object, ...]:
    lexical_rank = lexical[0] if lexical is not None else None
    semantic_rank = semantic.rank if semantic is not None else None
    exact = lexical is not None and lexical[1].match_rank == 0
    fused_score = sum(
        1.0 / (60 + rank)
        for rank in (lexical_rank, semantic_rank)
        if rank is not None
    )
    best_component_rank = min(
        rank for rank in (lexical_rank, semantic_rank) if rank is not None
    )
    updated_at = hydrated.summary.updated_at
    updated_timestamp = updated_at.timestamp() if updated_at is not None else 0.0
    return (
        0 if exact else 1,
        lexical_rank if exact and lexical_rank is not None else 0,
        -fused_score,
        best_component_rank,
        -updated_timestamp,
        _NODE_TYPE_ORDER.get(key[0], 99),
        key[1],
    )


def _summaries_in_order(
    keys: Sequence[NodeKey],
    hydrated: dict[NodeKey, _HydratedNode],
) -> list[GraphNodeSummary]:
    return [hydrated[key].summary for key in keys if key in hydrated]


def _node_id(key: NodeKey) -> str:
    return f"{key[0]}:{key[1]}"


def _node_sort_key(key: NodeKey) -> tuple[int, str]:
    return (_NODE_TYPE_ORDER.get(key[0], 99), key[1])


def _edge_sort_key(candidate: _EdgeCandidate) -> tuple[object, ...]:
    return (
        *_node_sort_key(candidate.source),
        *_node_sort_key(candidate.target),
        candidate.relationship,
        candidate.label,
        candidate.identity or "",
    )


def _adjacent_nodes(
    candidate: _EdgeCandidate,
    *,
    frontier: set[NodeKey],
    direction: DirectionValue,
) -> list[NodeKey]:
    adjacent: set[NodeKey] = set()
    if direction in {"outgoing", "both"} and candidate.source in frontier:
        adjacent.add(candidate.target)
    if direction in {"incoming", "both"} and candidate.target in frontier:
        adjacent.add(candidate.source)
    return sorted(adjacent, key=_node_sort_key)


def _edge_read(candidate: _EdgeCandidate) -> GraphNeighborhoodEdge:
    semantics = project_graph_relationship_semantics(candidate.relationship)
    if isinstance(semantics, DirectRelationshipSemanticMapping):
        semantic_read = GraphRelationshipSemantics(
            kind="direct",
            direction=semantics.direction,
            predicate_iri=semantics.predicate_iri,
        )
    elif isinstance(semantics, QualifiedRelationshipSemanticMapping):
        semantic_read = GraphRelationshipSemantics(
            kind="qualified",
            direction=semantics.direction,
            class_iri=semantics.class_iri,
            concept_iris=list(semantics.concept_iris),
            additional_concept_schemes=list(semantics.additional_concept_schemes),
            classification_predicate_iri=semantics.classification_predicate_iri,
        )
    else:  # pragma: no cover - exhaustive over the graph semantic mapping types
        raise TypeError("Unknown graph relationship semantic mapping.")
    return GraphNeighborhoodEdge(
        id=candidate.edge_id,
        source=_node_id(candidate.source),
        target=_node_id(candidate.target),
        label=candidate.label,
        relationship=candidate.relationship,
        semantics=semantic_read,
    )
