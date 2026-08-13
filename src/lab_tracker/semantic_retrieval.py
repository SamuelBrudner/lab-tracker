"""Provider-neutral semantic indexing and exact portable vector retrieval.

This module deliberately contains no network client, credentials, provider SDK,
model download, or vector database integration.  Production adapters are
injected behind :class:`EmbeddingClient` only after they pass the offline gate.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import struct
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import and_, case, exists, func, literal, or_, select, union
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.db_models import (
    AnalysisModel,
    ClaimModel,
    DatasetModel,
    ExplorationNodeModel,
    GoalLinkModel,
    GoalModel,
    NoteModel,
    QuestionModel,
    SemanticIndexEntryModel,
    SemanticIndexJobModel,
    SessionModel,
    VisualizationModel,
)
from lab_tracker.graph_documents import (
    GRAPH_DOCUMENT_CHUNK_OVERLAP,
    GRAPH_DOCUMENT_CHUNK_SIZE,
    GRAPH_DOCUMENT_MAX_CHUNKS,
    GRAPH_DOCUMENT_RENDERER_VERSION,
)

EmbeddingPurpose = Literal["document", "query"]
SemanticSearchMode = Literal["off", "shadow", "hybrid"]

SEMANTIC_QUERY_DEADLINE_SECONDS = 2.0
SEMANTIC_READY_COVERAGE = 0.99


@dataclass(frozen=True)
class EmbeddingDescriptor:
    """Stable identity and vector contract for one injected adapter."""

    adapter: str
    model: str
    revision: str
    dimensions: int
    normalization: Literal["unit_l2"]
    batch_limit: int

    def __post_init__(self) -> None:
        if not self.adapter.strip() or not self.model.strip() or not self.revision.strip():
            raise ValueError("Embedding descriptor identity fields must be non-empty.")
        if self.dimensions < 1:
            raise ValueError("Embedding dimensions must be positive.")
        if self.batch_limit < 1:
            raise ValueError("Embedding batch_limit must be positive.")


@runtime_checkable
class EmbeddingClient(Protocol):
    """Synchronous provider-neutral interface used by workers and queries."""

    @property
    def descriptor(self) -> EmbeddingDescriptor: ...

    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose,
    ) -> Sequence[Sequence[float]]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class SemanticCandidate:
    entity_type: str
    entity_id: UUID
    rank: int
    chunk_index: int
    document_hash: str

    @property
    def key(self) -> tuple[str, str]:
        return self.entity_type, str(self.entity_id)


@dataclass(frozen=True)
class SemanticSearchResult:
    candidates: tuple[SemanticCandidate, ...]
    scanned_chunks: int
    corrupt_chunks: int
    timed_out: bool


@dataclass(frozen=True)
class SemanticCoverage:
    coverage: float
    total_jobs: int
    current_jobs: int
    queue_depth: int
    failed_jobs: int
    oldest_lag_seconds: float | None

    @property
    def ready(self) -> bool:
        return self.total_jobs > 0 and self.coverage >= SEMANTIC_READY_COVERAGE


class FrozenVectorEmbeddingClient:
    """Deterministic, offline adapter backed by a checked-in vector mapping."""

    def __init__(
        self,
        descriptor: EmbeddingDescriptor,
        vectors: Mapping[tuple[EmbeddingPurpose, str], Sequence[float]],
    ) -> None:
        self._descriptor = descriptor
        self._vectors = {
            key: tuple(_validated_unit_vector(value, descriptor.dimensions))
            for key, value in vectors.items()
        }

    @property
    def descriptor(self) -> EmbeddingDescriptor:
        return self._descriptor

    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose,
    ) -> Sequence[Sequence[float]]:
        if len(texts) > self._descriptor.batch_limit:
            raise ValueError("Embedding batch exceeds descriptor batch_limit.")
        try:
            return [self._vectors[(purpose, text)] for text in texts]
        except KeyError as exc:
            raise ValueError("Frozen vector artifact does not contain requested text.") from exc

    def close(self) -> None:
        return None


class DeterministicFakeEmbeddingClient:
    """Small deterministic fake for lifecycle and failure-path tests."""

    def __init__(self, *, dimensions: int = 16) -> None:
        self._descriptor = EmbeddingDescriptor(
            adapter="deterministic-fake",
            model="sha256-projection",
            revision="v1",
            dimensions=dimensions,
            normalization="unit_l2",
            batch_limit=128,
        )

    @property
    def descriptor(self) -> EmbeddingDescriptor:
        return self._descriptor

    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose,
    ) -> Sequence[Sequence[float]]:
        if len(texts) > self._descriptor.batch_limit:
            raise ValueError("Embedding batch exceeds descriptor batch_limit.")
        return [_fake_vector(text, purpose, self._descriptor.dimensions) for text in texts]

    def close(self) -> None:
        return None


def semantic_configuration_hash(descriptor: EmbeddingDescriptor) -> str:
    """Hash every input that can change the meaning or shape of stored vectors."""

    payload = {
        "adapter": descriptor.adapter,
        "batch_limit": descriptor.batch_limit,
        "chunk_overlap": GRAPH_DOCUMENT_CHUNK_OVERLAP,
        "chunk_size": GRAPH_DOCUMENT_CHUNK_SIZE,
        "dimensions": descriptor.dimensions,
        "max_chunks": GRAPH_DOCUMENT_MAX_CHUNKS,
        "model": descriptor.model,
        "normalization": descriptor.normalization,
        "renderer_version": GRAPH_DOCUMENT_RENDERER_VERSION,
        "revision": descriptor.revision,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def candidate_limit_for_page(offset: int, limit: int) -> int:
    return min(500, max(100, 5 * (offset + limit + 1)))


def pack_embedding(vector: Sequence[float], dimensions: int) -> bytes:
    values = _validated_unit_vector(vector, dimensions)
    return struct.pack(f"<{dimensions}f", *values)


def unpack_embedding(blob: bytes, dimensions: int) -> tuple[float, ...]:
    if dimensions < 1 or len(blob) != dimensions * 4:
        raise ValueError("Embedding BLOB has an invalid dimension or byte length.")
    values = struct.unpack(f"<{dimensions}f", blob)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Embedding BLOB contains a non-finite value.")
    norm = math.sqrt(sum(value * value for value in values))
    if not 0.999 <= norm <= 1.001:
        raise ValueError("Embedding BLOB does not satisfy the unit-L2 contract.")
    return values


class ExactSemanticRetriever:
    """Project-scoped exact retrieval over portable little-endian float32 rows."""

    def __init__(self, session: OrmSession, client: EmbeddingClient) -> None:
        self._session = session
        self._client = client
        self.descriptor = client.descriptor
        self.config_hash = semantic_configuration_hash(self.descriptor)

    def coverage(self, project_id: UUID) -> SemanticCoverage:
        return semantic_coverage(
            self._session,
            config_hash=self.config_hash,
            project_id=project_id,
        )

    def search(
        self,
        project_id: UUID,
        query: str,
        *,
        entity_types: Sequence[str] | None,
        statuses: Sequence[str] | None,
        candidate_limit: int,
        deadline_seconds: float = SEMANTIC_QUERY_DEADLINE_SECONDS,
    ) -> SemanticSearchResult:
        started = time.monotonic()
        embedded = self._client.embed([query], purpose="query")
        if len(embedded) != 1:
            raise ValueError("Embedding client returned an invalid query batch size.")
        query_vector = _validated_unit_vector(embedded[0], self.descriptor.dimensions)
        statement = select(
            SemanticIndexEntryModel.entity_type,
            SemanticIndexEntryModel.entity_id,
            SemanticIndexEntryModel.chunk_index,
            SemanticIndexEntryModel.document_hash,
            SemanticIndexEntryModel.dimension,
            SemanticIndexEntryModel.embedding,
        ).where(
            SemanticIndexEntryModel.project_id == project_id,
            SemanticIndexEntryModel.config_hash == self.config_hash,
        )
        if entity_types is not None:
            statement = statement.where(
                SemanticIndexEntryModel.entity_type.in_(tuple(entity_types))
            )
        if statuses is not None:
            statement = statement.where(
                SemanticIndexEntryModel.status_snapshot.in_(tuple(statuses))
            )

        chunk_heap: list[tuple[float, str, str, int, str]] = []
        scanned = 0
        corrupt = 0
        timed_out = False
        chunk_capacity = min(4_000, max(candidate_limit * GRAPH_DOCUMENT_MAX_CHUNKS, 100))
        for row in self._session.execute(statement.execution_options(yield_per=500)):
            if time.monotonic() - started >= deadline_seconds:
                timed_out = True
                break
            scanned += 1
            try:
                if row.dimension != self.descriptor.dimensions:
                    raise ValueError("Stored embedding dimension is stale.")
                vector = unpack_embedding(row.embedding, row.dimension)
            except (ValueError, struct.error):
                corrupt += 1
                continue
            score = sum(left * right for left, right in zip(query_vector, vector, strict=True))
            item = (
                score,
                str(row.entity_type),
                str(row.entity_id),
                int(row.chunk_index),
                str(row.document_hash),
            )
            if len(chunk_heap) < chunk_capacity:
                heapq.heappush(chunk_heap, item)
            elif item > chunk_heap[0]:
                heapq.heapreplace(chunk_heap, item)

        best_by_node: dict[tuple[str, str], tuple[float, int, str]] = {}
        for score, entity_type, entity_id, chunk_index, document_hash in chunk_heap:
            key = entity_type, entity_id
            previous = best_by_node.get(key)
            candidate = score, -chunk_index, document_hash
            if previous is None or candidate > previous:
                best_by_node[key] = candidate
        ordered = sorted(
            best_by_node.items(),
            key=lambda item: (
                -item[1][0],
                item[0][0],
                item[0][1],
                -item[1][1],
            ),
        )[:candidate_limit]
        candidates = tuple(
            SemanticCandidate(
                entity_type=key[0],
                entity_id=UUID(key[1]),
                rank=rank,
                chunk_index=-value[1],
                document_hash=value[2],
            )
            for rank, (key, value) in enumerate(ordered, start=1)
        )
        return SemanticSearchResult(
            candidates=candidates,
            scanned_chunks=scanned,
            corrupt_chunks=corrupt,
            timed_out=timed_out,
        )


def jobs_ready_for_claim(now: datetime) -> Any:
    """Shared portable predicate for pending, retryable, and stale leased work."""

    return and_(
        SemanticIndexJobModel.completed_generation
        < SemanticIndexJobModel.requested_generation,
        or_(
            SemanticIndexJobModel.state == "pending",
            and_(
                SemanticIndexJobModel.state == "failed",
                or_(
                    SemanticIndexJobModel.retry_at.is_(None),
                    SemanticIndexJobModel.retry_at <= now,
                ),
            ),
            and_(
                SemanticIndexJobModel.state == "claimed",
                SemanticIndexJobModel.lease_expires_at < now,
            ),
        ),
    )


def semantic_coverage(
    session: OrmSession,
    *,
    config_hash: str,
    project_id: UUID | None = None,
) -> SemanticCoverage:
    """Compute project or global queue health without exposing indexed content."""

    if project_id is not None:
        return _project_semantic_coverage(
            session,
            config_hash=config_hash,
            project_id=project_id,
        )
    conditions = [SemanticIndexJobModel.config_hash == config_hash]
    rows = session.execute(
        select(
            func.count(SemanticIndexJobModel.job_id),
            func.sum(
                case(
                    (
                        and_(
                            SemanticIndexJobModel.completed_generation
                            >= SemanticIndexJobModel.requested_generation,
                            SemanticIndexJobModel.state == "completed",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (SemanticIndexJobModel.state == "failed", 1),
                    else_=0,
                )
            ),
            func.min(SemanticIndexJobModel.updated_at),
        ).where(*conditions)
    ).one()
    total = int(rows[0] or 0)
    current = int(rows[1] or 0)
    failed = int(rows[2] or 0)
    oldest = rows[3]
    queue_depth = total - current
    oldest_lag = None
    if queue_depth and oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        oldest_lag = max(0.0, (datetime.now(timezone.utc) - oldest).total_seconds())
    return SemanticCoverage(
        coverage=(current / total) if total else 0.0,
        total_jobs=total,
        current_jobs=current,
        queue_depth=queue_depth,
        failed_jobs=failed,
        oldest_lag_seconds=oldest_lag,
    )


def _project_semantic_coverage(
    session: OrmSession,
    *,
    config_hash: str,
    project_id: UUID,
) -> SemanticCoverage:
    """Measure coverage against canonical graph nodes, including unqueued imports."""

    identities = _project_graph_identities(project_id).subquery(
        "semantic_coverage_identities"
    )
    join_condition = and_(
        SemanticIndexJobModel.project_id == project_id,
        SemanticIndexJobModel.entity_type == identities.c.entity_type,
        SemanticIndexJobModel.entity_id == identities.c.entity_id,
        SemanticIndexJobModel.config_hash == config_hash,
    )
    current_condition = and_(
        SemanticIndexJobModel.completed_generation
        >= SemanticIndexJobModel.requested_generation,
        SemanticIndexJobModel.state == "completed",
    )
    rows = session.execute(
        select(
            func.count(),
            func.sum(case((current_condition, 1), else_=0)),
            func.sum(
                case((SemanticIndexJobModel.state == "failed", 1), else_=0)
            ),
            func.min(
                case(
                    (or_(SemanticIndexJobModel.job_id.is_(None), ~current_condition),
                     SemanticIndexJobModel.updated_at),
                    else_=None,
                )
            ),
        )
        .select_from(identities)
        .outerjoin(SemanticIndexJobModel, join_condition)
    ).one()
    total = int(rows[0] or 0)
    current = int(rows[1] or 0)
    failed = int(rows[2] or 0)
    oldest = rows[3]
    queue_depth = total - current
    oldest_lag = None
    if queue_depth and oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        oldest_lag = max(0.0, (datetime.now(timezone.utc) - oldest).total_seconds())
    return SemanticCoverage(
        coverage=(current / total) if total else 0.0,
        total_jobs=total,
        current_jobs=current,
        queue_depth=queue_depth,
        failed_jobs=failed,
        oldest_lag_seconds=oldest_lag,
    )


def _project_graph_identities(project_id: UUID) -> Any:
    direct_specs = (
        (QuestionModel, "question", QuestionModel.question_id),
        (SessionModel, "session", SessionModel.session_id),
        (NoteModel, "note", NoteModel.note_id),
        (DatasetModel, "dataset", DatasetModel.dataset_id),
        (AnalysisModel, "analysis", AnalysisModel.analysis_id),
        (ClaimModel, "claim", ClaimModel.claim_id),
        (ExplorationNodeModel, "exploration_node", ExplorationNodeModel.node_id),
    )
    branches = [
        select(
            literal(entity_type).label("entity_type"),
            id_column.label("entity_id"),
        ).where(model.project_id == project_id)
        for model, entity_type, id_column in direct_specs
    ]
    branches.append(
        select(
            literal("visualization").label("entity_type"),
            VisualizationModel.viz_id.label("entity_id"),
        )
        .select_from(VisualizationModel)
        .join(AnalysisModel, AnalysisModel.analysis_id == VisualizationModel.analysis_id)
        .where(AnalysisModel.project_id == project_id)
    )
    branches.append(
        select(
            literal("goal").label("entity_type"),
            GoalModel.goal_id.label("entity_id"),
        ).where(
            or_(
                GoalModel.project_id == project_id,
                exists(
                    select(GoalLinkModel.link_id).where(
                        GoalLinkModel.goal_id == GoalModel.goal_id,
                        GoalLinkModel.entity_type == "project",
                        GoalLinkModel.entity_id == project_id,
                    )
                ),
            )
        )
    )
    return union(*branches)


def stable_embedding_error_code(exc: BaseException) -> str:
    """Return a constrained code without persisting provider or record payloads."""

    if isinstance(exc, TimeoutError):
        return "provider_timeout"
    if isinstance(exc, (ValueError, struct.error)):
        return "invalid_vector"
    return "provider_failure"


def _validated_unit_vector(vector: Sequence[float], dimensions: int) -> tuple[float, ...]:
    if len(vector) != dimensions:
        raise ValueError("Embedding vector dimension does not match its descriptor.")
    values = tuple(float(value) for value in vector)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Embedding vector contains a non-finite value.")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0.0:
        raise ValueError("Embedding vector must have a positive L2 norm.")
    return tuple(value / norm for value in values)


def _fake_vector(text: str, purpose: EmbeddingPurpose, dimensions: int) -> tuple[float, ...]:
    values: list[float] = []
    counter = 0
    while len(values) < dimensions:
        digest = hashlib.sha256(f"{purpose}\0{text}\0{counter}".encode()).digest()
        values.extend((byte - 127.5) / 127.5 for byte in digest)
        counter += 1
    return _validated_unit_vector(values[:dimensions], dimensions)
