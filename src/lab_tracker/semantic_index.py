"""Transactional semantic-index queue, reconciler, and lease-based worker."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, event, exists, select, union_all, update
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

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
from lab_tracker.graph_documents import GRAPH_DOCUMENT_RENDERER_VERSION, GraphNodeDocument
from lab_tracker.graph_query import GraphQueryService
from lab_tracker.semantic_retrieval import (
    EmbeddingClient,
    jobs_ready_for_claim,
    pack_embedding,
    semantic_configuration_hash,
    stable_embedding_error_code,
)

_TRACKER_INFO_KEY = "lab_tracker_semantic_changes_v1"
_DIRECT_MODELS: dict[type[Any], tuple[str, str]] = {
    QuestionModel: ("question", "question_id"),
    SessionModel: ("session", "session_id"),
    NoteModel: ("note", "note_id"),
    DatasetModel: ("dataset", "dataset_id"),
    AnalysisModel: ("analysis", "analysis_id"),
    ClaimModel: ("claim", "claim_id"),
    ExplorationNodeModel: ("exploration_node", "node_id"),
    GoalModel: ("goal", "goal_id"),
}


@dataclass(frozen=True)
class GraphIdentity:
    project_id: UUID
    entity_type: str
    entity_id: UUID


@dataclass(frozen=True)
class ClaimedSemanticJob:
    job_id: UUID
    project_id: UUID
    entity_type: str
    entity_id: UUID
    config_hash: str
    generation: int
    claim_token: UUID


def install_semantic_change_tracking(
    factory: sessionmaker[OrmSession],
    *,
    client: EmbeddingClient,
) -> Callable[[], None]:
    """Install transaction-bound enqueueing for an adapter-enabled runtime.

    The listeners only add derivative jobs to the canonical transaction.  A
    rollback therefore removes both the source write and its queued work.
    """

    config_hash = semantic_configuration_hash(client.descriptor)

    def before_flush(
        session: OrmSession,
        _flush_context: object,
        _instances: object,
    ) -> None:
        changed: list[tuple[object, bool]] = []
        for value in session.new:
            if (
                type(value) in _DIRECT_MODELS
                or isinstance(value, (VisualizationModel, GoalLinkModel))
            ):
                changed.append((value, False))
        for value in session.dirty:
            if (
                type(value) in _DIRECT_MODELS
                or isinstance(value, (VisualizationModel, GoalLinkModel))
            ):
                changed.append((value, False))
        for value in session.deleted:
            if (
                type(value) in _DIRECT_MODELS
                or isinstance(value, (VisualizationModel, GoalLinkModel))
            ):
                changed.append((value, True))
        if changed:
            session.info.setdefault(_TRACKER_INFO_KEY, []).extend(changed)

    def after_flush(session: OrmSession, _flush_context: object) -> None:
        changed = session.info.pop(_TRACKER_INFO_KEY, [])
        for value, deleted in changed:
            identity = _identity_for_model(session, value)
            if identity is None:
                continue
            if deleted:
                _purge_identity(session, identity)
            else:
                enqueue_semantic_identity(session, identity, config_hash=config_hash)

    event.listen(factory, "before_flush", before_flush)
    event.listen(factory, "after_flush_postexec", after_flush)

    def remove() -> None:
        event.remove(factory, "before_flush", before_flush)
        event.remove(factory, "after_flush_postexec", after_flush)

    return remove


def enqueue_semantic_identity(
    session: OrmSession,
    identity: GraphIdentity,
    *,
    config_hash: str,
) -> SemanticIndexJobModel:
    """Coalesce one changed node by advancing its requested generation."""

    job = session.scalar(
        select(SemanticIndexJobModel).where(
            SemanticIndexJobModel.project_id == identity.project_id,
            SemanticIndexJobModel.entity_type == identity.entity_type,
            SemanticIndexJobModel.entity_id == identity.entity_id,
            SemanticIndexJobModel.config_hash == config_hash,
        )
    )
    if job is None:
        job = SemanticIndexJobModel(
            project_id=identity.project_id,
            entity_type=identity.entity_type,
            entity_id=identity.entity_id,
            config_hash=config_hash,
            requested_generation=1,
            completed_generation=0,
            state="pending",
        )
        session.add(job)
        return job
    job.requested_generation += 1
    job.state = "pending"
    job.retry_at = None
    job.error_code = None
    return job


class SemanticIndexReconciler:
    """Bounded repair for backfill, configuration changes, and bypassed writes."""

    def __init__(self, session: OrmSession, client: EmbeddingClient) -> None:
        self._session = session
        self._config_hash = semantic_configuration_hash(client.descriptor)
        self._dimensions = client.descriptor.dimensions

    def reconcile_project(self, project_id: UUID, *, limit: int = 500) -> int:
        if limit < 1:
            return 0
        identities = list(
            self._missing_job_identities(project_id, limit=limit)
        )
        for identity in identities:
            enqueue_semantic_identity(
                self._session,
                identity,
                config_hash=self._config_hash,
            )
        remaining = limit - len(identities)
        if remaining > 0:
            stale = list(self._stale_job_identities(project_id, limit=remaining))
            for identity in stale:
                enqueue_semantic_identity(
                    self._session,
                    identity,
                    config_hash=self._config_hash,
                )
            identities.extend(stale)
            remaining -= len(stale)
        if remaining > 0:
            remaining -= self._remove_stale_config(project_id, limit=remaining)
        if remaining > 0:
            self._remove_orphans(project_id, limit=remaining)
        return len(identities)

    def _missing_job_identities(
        self,
        project_id: UUID,
        *,
        limit: int,
    ) -> Iterable[GraphIdentity]:
        branches: list[Any] = []
        for model, entity_type, id_column in _project_model_specs():
            missing = ~exists(
                select(SemanticIndexJobModel.job_id).where(
                    SemanticIndexJobModel.project_id == project_id,
                    SemanticIndexJobModel.entity_type == entity_type,
                    SemanticIndexJobModel.entity_id == id_column,
                    SemanticIndexJobModel.config_hash == self._config_hash,
                )
            )
            if model is VisualizationModel:
                branches.append(
                    select(
                        AnalysisModel.project_id.label("project_id"),
                        id_column.label("entity_id"),
                        _literal_type(entity_type),
                    )
                    .select_from(VisualizationModel)
                    .join(
                        AnalysisModel,
                        AnalysisModel.analysis_id == VisualizationModel.analysis_id,
                    )
                    .where(AnalysisModel.project_id == project_id, missing)
                )
            else:
                branches.append(
                    select(
                        model.project_id.label("project_id"),
                        id_column.label("entity_id"),
                        _literal_type(entity_type),
                    ).where(model.project_id == project_id, missing)
                )
        linked_goal_missing = ~exists(
            select(SemanticIndexJobModel.job_id).where(
                SemanticIndexJobModel.project_id == project_id,
                SemanticIndexJobModel.entity_type == "goal",
                SemanticIndexJobModel.entity_id == GoalLinkModel.goal_id,
                SemanticIndexJobModel.config_hash == self._config_hash,
            )
        )
        branches.append(
            select(
                GoalLinkModel.entity_id.label("project_id"),
                GoalLinkModel.goal_id.label("entity_id"),
                _literal_type("goal"),
            ).where(
                GoalLinkModel.entity_type == "project",
                GoalLinkModel.entity_id == project_id,
                linked_goal_missing,
            )
        )
        statement = (
            union_all(*branches)
            .order_by("entity_type", "entity_id")
            .limit(limit)
        )
        for row in self._session.execute(statement):
            yield GraphIdentity(
                project_id=UUID(str(row.project_id)),
                entity_type=str(row.entity_type),
                entity_id=UUID(str(row.entity_id)),
            )

    def _stale_job_identities(
        self,
        project_id: UUID,
        *,
        limit: int,
    ) -> Iterable[GraphIdentity]:
        jobs = list(
            self._session.scalars(
                select(SemanticIndexJobModel)
                .where(
                    SemanticIndexJobModel.project_id == project_id,
                    SemanticIndexJobModel.config_hash == self._config_hash,
                    SemanticIndexJobModel.state == "completed",
                    SemanticIndexJobModel.completed_generation
                    >= SemanticIndexJobModel.requested_generation,
                )
                .order_by(
                    SemanticIndexJobModel.updated_at,
                    SemanticIndexJobModel.entity_type,
                    SemanticIndexJobModel.entity_id,
                )
                .limit(limit)
            )
        )
        graph = GraphQueryService(self._session)
        for job in jobs:
            identity = GraphIdentity(job.project_id, job.entity_type, job.entity_id)
            document = graph.render_document(
                project_id,
                entity_type=job.entity_type,
                entity_id=job.entity_id,
            )
            if document is None:
                _purge_identity(self._session, identity)
                continue
            entries = list(
                self._session.scalars(
                    select(SemanticIndexEntryModel).where(
                        SemanticIndexEntryModel.project_id == project_id,
                        SemanticIndexEntryModel.entity_type == job.entity_type,
                        SemanticIndexEntryModel.entity_id == job.entity_id,
                        SemanticIndexEntryModel.config_hash == self._config_hash,
                    )
                )
            )
            expects_entries = bool(document.chunks())
            entries_current = bool(entries) == expects_entries and all(
                entry.document_hash == document.document_hash
                and entry.renderer_version == GRAPH_DOCUMENT_RENDERER_VERSION
                and entry.dimension == self._dimensions
                and entry.status_snapshot == document.status_snapshot
                and entry.source_updated_at == document.source_updated_at
                for entry in entries
            )
            if not entries_current:
                yield identity

    def _remove_stale_config(self, project_id: UUID, *, limit: int) -> int:
        rows = list(
            self._session.execute(
                select(
                    SemanticIndexJobModel.entity_type,
                    SemanticIndexJobModel.entity_id,
                    SemanticIndexJobModel.config_hash,
                )
                .where(
                    SemanticIndexJobModel.project_id == project_id,
                    SemanticIndexJobModel.config_hash != self._config_hash,
                )
                .order_by(
                    SemanticIndexJobModel.updated_at,
                    SemanticIndexJobModel.entity_type,
                    SemanticIndexJobModel.entity_id,
                )
                .limit(limit)
            )
        )
        for row in rows:
            conditions = (
                SemanticIndexEntryModel.project_id == project_id,
                SemanticIndexEntryModel.entity_type == row.entity_type,
                SemanticIndexEntryModel.entity_id == row.entity_id,
                SemanticIndexEntryModel.config_hash == row.config_hash,
            )
            self._session.execute(delete(SemanticIndexEntryModel).where(*conditions))
            self._session.execute(
                delete(SemanticIndexJobModel).where(
                    SemanticIndexJobModel.project_id == project_id,
                    SemanticIndexJobModel.entity_type == row.entity_type,
                    SemanticIndexJobModel.entity_id == row.entity_id,
                    SemanticIndexJobModel.config_hash == row.config_hash,
                )
            )
        return len(rows)

    def _remove_orphans(self, project_id: UUID, *, limit: int) -> int:
        rows = list(
            self._session.execute(
                select(
                    SemanticIndexEntryModel.entity_type,
                    SemanticIndexEntryModel.entity_id,
                )
                .where(SemanticIndexEntryModel.project_id == project_id)
                .distinct()
                .limit(limit)
            )
        )
        removed = 0
        graph = GraphQueryService(self._session)
        for row in rows:
            document = graph.render_document(
                project_id,
                entity_type=str(row.entity_type),
                entity_id=UUID(str(row.entity_id)),
            )
            if document is None:
                _purge_identity(
                    self._session,
                    GraphIdentity(
                        project_id=project_id,
                        entity_type=str(row.entity_type),
                        entity_id=UUID(str(row.entity_id)),
                    ),
                )
                removed += 1
        return removed


class SemanticIndexWorker:
    """Claim briefly, embed without a transaction, then generation-check replace."""

    def __init__(
        self,
        factory: sessionmaker[OrmSession],
        client: EmbeddingClient,
        *,
        lease_seconds: int = 300,
    ) -> None:
        self._factory = factory
        self._client = client
        self._config_hash = semantic_configuration_hash(client.descriptor)
        self._lease_seconds = lease_seconds

    def process_one(self) -> bool:
        claim = self._claim_one()
        if claim is None:
            return False
        try:
            document = self._render(claim)
            if document is None:
                self._complete_deleted(claim)
                return True
            vectors = self._embed_document(document)
            self._replace_if_current(claim, document, vectors)
        except Exception as exc:
            self._fail(claim, exc)
        return True

    def _claim_one(self) -> ClaimedSemanticJob | None:
        now = datetime.now(timezone.utc)
        token = uuid4()
        with self._factory.begin() as session:
            if session.bind is not None and session.bind.dialect.name == "sqlite":
                candidate_id = (
                    select(SemanticIndexJobModel.job_id)
                    .where(
                        SemanticIndexJobModel.config_hash == self._config_hash,
                        jobs_ready_for_claim(now),
                    )
                    .order_by(
                        SemanticIndexJobModel.updated_at,
                        SemanticIndexJobModel.job_id,
                    )
                    .limit(1)
                    .scalar_subquery()
                )
                claimed = session.execute(
                    update(SemanticIndexJobModel)
                    .where(
                        SemanticIndexJobModel.job_id == candidate_id,
                        SemanticIndexJobModel.config_hash == self._config_hash,
                        jobs_ready_for_claim(now),
                    )
                    .values(
                        state="claimed",
                        claim_token=token,
                        claimed_at=now,
                        lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                        attempts=SemanticIndexJobModel.attempts + 1,
                    )
                    .returning(
                        SemanticIndexJobModel.job_id,
                        SemanticIndexJobModel.project_id,
                        SemanticIndexJobModel.entity_type,
                        SemanticIndexJobModel.entity_id,
                        SemanticIndexJobModel.config_hash,
                        SemanticIndexJobModel.requested_generation,
                    )
                ).one_or_none()
                if claimed is None:
                    return None
                return ClaimedSemanticJob(
                    job_id=claimed.job_id,
                    project_id=claimed.project_id,
                    entity_type=claimed.entity_type,
                    entity_id=claimed.entity_id,
                    config_hash=claimed.config_hash,
                    generation=claimed.requested_generation,
                    claim_token=token,
                )
            statement = (
                select(SemanticIndexJobModel)
                .where(
                    SemanticIndexJobModel.config_hash == self._config_hash,
                    jobs_ready_for_claim(now),
                )
                .order_by(SemanticIndexJobModel.updated_at, SemanticIndexJobModel.job_id)
                .limit(1)
            )
            statement = statement.with_for_update(skip_locked=True)
            job = session.scalar(statement)
            if job is None:
                return None
            job.state = "claimed"
            job.claim_token = token
            job.claimed_at = now
            job.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            job.attempts += 1
            return ClaimedSemanticJob(
                job_id=job.job_id,
                project_id=job.project_id,
                entity_type=job.entity_type,
                entity_id=job.entity_id,
                config_hash=job.config_hash,
                generation=job.requested_generation,
                claim_token=token,
            )

    def _render(self, claim: ClaimedSemanticJob) -> GraphNodeDocument | None:
        with self._factory() as session:
            return GraphQueryService(session).render_document(
                claim.project_id,
                entity_type=claim.entity_type,
                entity_id=claim.entity_id,
            )

    def _embed_document(
        self,
        document: GraphNodeDocument,
    ) -> list[tuple[int, int, int, bytes]]:
        chunks = document.chunks()
        vectors: list[tuple[int, int, int, bytes]] = []
        batch_limit = self._client.descriptor.batch_limit
        for start in range(0, len(chunks), batch_limit):
            batch = chunks[start : start + batch_limit]
            embedded = self._client.embed(
                [chunk.text for chunk in batch],
                purpose="document",
            )
            if len(embedded) != len(batch):
                raise ValueError("Embedding client returned an invalid document batch size.")
            for chunk, vector in zip(batch, embedded, strict=True):
                vectors.append(
                    (
                        chunk.index,
                        chunk.start_char,
                        chunk.end_char,
                        pack_embedding(vector, self._client.descriptor.dimensions),
                    )
                )
        return vectors

    def _replace_if_current(
        self,
        claim: ClaimedSemanticJob,
        rendered: GraphNodeDocument,
        vectors: list[tuple[int, int, int, bytes]],
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._factory.begin() as session:
            job = session.get(SemanticIndexJobModel, claim.job_id)
            if not _claim_is_current(job, claim):
                return
            assert job is not None
            current = GraphQueryService(session).render_document(
                claim.project_id,
                entity_type=claim.entity_type,
                entity_id=claim.entity_id,
            )
            if current is None:
                _purge_identity(
                    session,
                    GraphIdentity(claim.project_id, claim.entity_type, claim.entity_id),
                )
                return
            if current.document_hash != rendered.document_hash:
                job.state = "pending"
                job.claim_token = None
                job.lease_expires_at = None
                return
            session.execute(
                delete(SemanticIndexEntryModel).where(
                    SemanticIndexEntryModel.project_id == claim.project_id,
                    SemanticIndexEntryModel.entity_type == claim.entity_type,
                    SemanticIndexEntryModel.entity_id == claim.entity_id,
                    SemanticIndexEntryModel.config_hash == claim.config_hash,
                )
            )
            for chunk_index, chunk_start, chunk_end, vector_blob in vectors:
                session.add(
                    SemanticIndexEntryModel(
                        project_id=claim.project_id,
                        entity_type=claim.entity_type,
                        entity_id=claim.entity_id,
                        config_hash=claim.config_hash,
                        chunk_index=chunk_index,
                        chunk_start=chunk_start,
                        chunk_end=chunk_end,
                        status_snapshot=current.summary.status,
                        source_updated_at=current.summary.updated_at,
                        document_hash=current.document_hash,
                        renderer_version=GRAPH_DOCUMENT_RENDERER_VERSION,
                        dimension=self._client.descriptor.dimensions,
                        embedding=vector_blob,
                        indexed_at=now,
                    )
                )
            job.completed_generation = claim.generation
            job.state = "completed"
            job.claim_token = None
            job.claimed_at = None
            job.lease_expires_at = None
            job.retry_at = None
            job.error_code = None

    def _complete_deleted(self, claim: ClaimedSemanticJob) -> None:
        with self._factory.begin() as session:
            job = session.get(SemanticIndexJobModel, claim.job_id)
            if not _claim_is_current(job, claim):
                return
            assert job is not None
            _purge_identity(
                session,
                GraphIdentity(claim.project_id, claim.entity_type, claim.entity_id),
            )

    def _fail(self, claim: ClaimedSemanticJob, exc: BaseException) -> None:
        with self._factory.begin() as session:
            job = session.get(SemanticIndexJobModel, claim.job_id)
            if not _claim_is_current(job, claim):
                return
            assert job is not None
            job.state = "failed"
            job.error_code = stable_embedding_error_code(exc)
            job.claim_token = None
            job.claimed_at = None
            job.lease_expires_at = None
            delay_seconds = min(3_600, 2 ** min(job.attempts, 10))
            job.retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)


def _claim_is_current(
    job: SemanticIndexJobModel | None,
    claim: ClaimedSemanticJob,
) -> bool:
    return bool(
        job is not None
        and job.claim_token == claim.claim_token
        and job.requested_generation == claim.generation
        and job.config_hash == claim.config_hash
    )


def _identity_for_model(
    session: OrmSession,
    value: object,
) -> GraphIdentity | None:
    if isinstance(value, VisualizationModel):
        entity_id = value.viz_id
        project_id = session.scalar(
            select(AnalysisModel.project_id).where(
                AnalysisModel.analysis_id == value.analysis_id
            )
        )
        if entity_id is None or project_id is None:
            return None
        return GraphIdentity(project_id, "visualization", entity_id)
    if isinstance(value, GoalLinkModel):
        if value.entity_type != "project":
            return None
        return GraphIdentity(value.entity_id, "goal", value.goal_id)
    if isinstance(value, GoalModel) and value.project_id is None:
        linked_project_id = session.scalar(
            select(GoalLinkModel.entity_id).where(
                GoalLinkModel.goal_id == value.goal_id,
                GoalLinkModel.entity_type == "project",
            )
        )
        if linked_project_id is None:
            return None
        return GraphIdentity(linked_project_id, "goal", value.goal_id)
    spec = _DIRECT_MODELS.get(type(value))
    if spec is None:
        return None
    entity_type, id_attribute = spec
    direct_entity_id = getattr(value, id_attribute, None)
    direct_project_id = getattr(value, "project_id", None)
    if direct_entity_id is None or direct_project_id is None:
        return None
    return GraphIdentity(direct_project_id, entity_type, direct_entity_id)


def _purge_identity(session: OrmSession, identity: GraphIdentity) -> None:
    conditions = (
        SemanticIndexEntryModel.project_id == identity.project_id,
        SemanticIndexEntryModel.entity_type == identity.entity_type,
        SemanticIndexEntryModel.entity_id == identity.entity_id,
    )
    session.execute(delete(SemanticIndexEntryModel).where(*conditions))
    session.execute(
        delete(SemanticIndexJobModel).where(
            SemanticIndexJobModel.project_id == identity.project_id,
            SemanticIndexJobModel.entity_type == identity.entity_type,
            SemanticIndexJobModel.entity_id == identity.entity_id,
        )
    )


def _project_model_specs() -> list[tuple[Any, str, Any]]:
    return [
        (QuestionModel, "question", QuestionModel.question_id),
        (SessionModel, "session", SessionModel.session_id),
        (NoteModel, "note", NoteModel.note_id),
        (DatasetModel, "dataset", DatasetModel.dataset_id),
        (AnalysisModel, "analysis", AnalysisModel.analysis_id),
        (ClaimModel, "claim", ClaimModel.claim_id),
        (ExplorationNodeModel, "exploration_node", ExplorationNodeModel.node_id),
        (VisualizationModel, "visualization", VisualizationModel.viz_id),
        (GoalModel, "goal", GoalModel.goal_id),
    ]


def _literal_type(entity_type: str) -> Any:
    from sqlalchemy import literal

    return literal(entity_type).label("entity_type")
