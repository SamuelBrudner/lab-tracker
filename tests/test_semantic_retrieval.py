from __future__ import annotations

import math
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from typing import Literal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, update

from lab_tracker.db_models import (
    QuestionModel,
    SemanticIndexEntryModel,
    SemanticIndexJobModel,
)
from lab_tracker.graph_documents import GRAPH_DOCUMENT_RENDERER_VERSION
from lab_tracker.graph_query import GraphQueryService
from lab_tracker.semantic_index import (
    SemanticIndexReconciler,
    SemanticIndexWorker,
    install_semantic_change_tracking,
)
from lab_tracker.semantic_retrieval import (
    EmbeddingDescriptor,
    ExactSemanticRetriever,
    candidate_limit_for_page,
    pack_embedding,
    semantic_configuration_hash,
    semantic_coverage,
    unpack_embedding,
)


class TopicEmbeddingClient:
    def __init__(self) -> None:
        self.query_calls = 0
        self.closed = False
        self._descriptor = EmbeddingDescriptor(
            adapter="test-topic",
            model="two-topic",
            revision="v1",
            dimensions=2,
            normalization="unit_l2",
            batch_limit=16,
        )

    @property
    def descriptor(self) -> EmbeddingDescriptor:
        return self._descriptor

    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: Literal["document", "query"],
    ) -> Sequence[Sequence[float]]:
        if purpose == "query":
            self.query_calls += 1
        return [
            (1.0, 0.0)
            if "hidden association" in text.casefold() or "zebra" in text.casefold()
            else (0.0, 1.0)
            for text in texts
        ]

    def close(self) -> None:
        self.closed = True


class TimeoutEmbeddingClient(TopicEmbeddingClient):
    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: Literal["document", "query"],
    ) -> Sequence[Sequence[float]]:
        if purpose == "query":
            raise TimeoutError("provider payload deliberately omitted")
        return super().embed(texts, purpose=purpose)


class RacingEmbeddingClient(TopicEmbeddingClient):
    def __init__(self) -> None:
        super().__init__()
        self.on_first_document: object | None = None

    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: Literal["document", "query"],
    ) -> Sequence[Sequence[float]]:
        callback = self.on_first_document
        if purpose == "document" and callable(callback):
            self.on_first_document = None
            callback()
        return super().embed(texts, purpose=purpose)


def test_portable_float32_validation_and_candidate_bound() -> None:
    blob = pack_embedding([3.0, 4.0], 2)
    assert len(blob) == 8
    unpacked = unpack_embedding(blob, 2)
    assert unpacked == pytest.approx((0.6, 0.8))
    assert math.sqrt(sum(value * value for value in unpacked)) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="byte length"):
        unpack_embedding(blob[:-1], 2)
    with pytest.raises(ValueError, match="non-finite"):
        pack_embedding([float("nan"), 1.0], 2)
    assert candidate_limit_for_page(0, 20) == 105
    assert candidate_limit_for_page(1_000, 100) == 500


def test_exact_retrieval_collapses_chunks_by_each_nodes_best_hit(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = UUID(
        client.post(
            "/projects",
            json={"name": "Semantic chunk collapse", "description": ""},
            headers=admin_auth_headers,
        ).json()["data"]["project_id"]
    )
    question_ids = [
        UUID(
            client.post(
                "/questions",
                json={
                    "project_id": str(project_id),
                    "text": text,
                    "question_type": "descriptive",
                    "status": "active",
                },
                headers=admin_auth_headers,
            ).json()["data"]["question_id"]
        )
        for text in ("Zebra first", "Zebra second")
    ]
    embedding_client = TopicEmbeddingClient()
    config_hash = semantic_configuration_hash(embedding_client.descriptor)
    factory = client.app.state.db_session_factory
    with factory.begin() as session:
        graph = GraphQueryService(session)
        documents = [
            graph.render_document(
                project_id,
                entity_type="question",
                entity_id=question_id,
            )
            for question_id in question_ids
        ]
        assert all(document is not None for document in documents)
        vectors = ((0, [0.0, 1.0]), (1, [1.0, 0.0]), (0, [0.8, 0.6]))
        for question_id, document, node_vectors in (
            (question_ids[0], documents[0], vectors[:2]),
            (question_ids[1], documents[1], vectors[2:]),
        ):
            assert document is not None
            for chunk_index, vector in node_vectors:
                session.add(
                    SemanticIndexEntryModel(
                        project_id=project_id,
                        entity_type="question",
                        entity_id=question_id,
                        config_hash=config_hash,
                        chunk_index=chunk_index,
                        chunk_start=chunk_index,
                        chunk_end=chunk_index + 1,
                        status_snapshot=document.status_snapshot,
                        source_updated_at=document.source_updated_at,
                        document_hash=document.document_hash,
                        renderer_version=GRAPH_DOCUMENT_RENDERER_VERSION,
                        dimension=2,
                        embedding=pack_embedding(vector, 2),
                        indexed_at=datetime.now(timezone.utc),
                    )
                )
    with factory() as session:
        result = ExactSemanticRetriever(session, embedding_client).search(
            project_id,
            "hidden association",
            entity_types=("question",),
            statuses=("active",),
            candidate_limit=10,
        )
    assert [candidate.entity_id for candidate in result.candidates] == question_ids
    assert result.candidates[0].chunk_index == 1


def test_semantic_tables_are_present_after_sqlite_migration(client: TestClient) -> None:
    tables = set(inspect(client.app.state.db_engine).get_table_names())
    assert {"semantic_index_entries", "semantic_index_jobs"} <= tables


def test_semantic_tables_are_present_after_postgres_migration(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_engine(migrated_postgres_database_url, future=True)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"semantic_index_entries", "semantic_index_jobs"} <= tables
    finally:
        engine.dispose()


def test_transactional_queue_worker_hybrid_policy_and_delete_purge(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = UUID(
        client.post(
            "/projects",
            json={"name": "Semantic lifecycle", "description": ""},
            headers=admin_auth_headers,
        ).json()["data"]["project_id"]
    )
    question_id = UUID(
        client.post(
            "/questions",
            json={
                "project_id": str(project_id),
                "text": "Zebra timing observation",
                "question_type": "descriptive",
                "status": "active",
            },
            headers=admin_auth_headers,
        ).json()["data"]["question_id"]
    )
    factory = client.app.state.db_session_factory
    embedding_client = TopicEmbeddingClient()
    remove_tracking = install_semantic_change_tracking(
        factory,
        client=embedding_client,
    )
    try:
        with factory.begin() as session:
            question = session.get(QuestionModel, question_id)
            assert question is not None
            question.text = "Zebra timing observation updated"
        with factory() as session:
            job = session.scalar(select(SemanticIndexJobModel))
            assert job is not None
            generation = job.requested_generation

        with factory() as session:
            question = session.get(QuestionModel, question_id)
            assert question is not None
            question.text = "Rolled back zebra edit"
            session.flush()
            session.rollback()
        with factory() as session:
            job = session.scalar(select(SemanticIndexJobModel))
            assert job is not None
            assert job.requested_generation == generation

        worker = SemanticIndexWorker(factory, embedding_client)
        assert worker.process_one() is True
        with factory() as session:
            entry = session.scalar(select(SemanticIndexEntryModel))
            job = session.scalar(select(SemanticIndexJobModel))
            assert entry is not None
            assert job is not None and job.state == "completed"
            assert entry.dimension == 2
            assert entry.document_hash

        client.app.state.semantic_embedding_client = embedding_client
        client.app.state.settings.semantic_search_mode = "hybrid"
        response = client.get(
            f"/projects/{project_id}/graph/search",
            params={"q": "hidden association", "retrieval_mode": "hybrid"},
            headers=admin_auth_headers,
        )
        assert response.status_code == 200
        result = response.json()["data"]
        assert result["retrieval"]["effective_mode"] == "hybrid"
        assert result["retrieval"]["semantic_state"] == "ready"
        assert result["items"][0]["node"]["entity_id"] == str(question_id)
        assert result["items"][0]["semantic_rank"] == 1

        before_lexical = embedding_client.query_calls
        lexical = client.get(
            f"/projects/{project_id}/graph/search",
            params={"q": "hidden association", "retrieval_mode": "lexical"},
            headers=admin_auth_headers,
        )
        assert lexical.status_code == 200
        assert embedding_client.query_calls == before_lexical

        client.app.state.settings.semantic_search_mode = "shadow"
        shadow = client.get(
            f"/projects/{project_id}/graph/search",
            params={"q": "hidden association", "retrieval_mode": "hybrid"},
            headers=admin_auth_headers,
        ).json()["data"]
        assert shadow["retrieval"]["effective_mode"] == "lexical"
        assert shadow["retrieval"]["fallback_reason"] == "shadow_policy"
        assert shadow["items"] == []

        before_missing = embedding_client.query_calls
        missing = client.get(
            f"/projects/{uuid4()}/graph/search",
            params={"q": "hidden association", "retrieval_mode": "hybrid"},
            headers=admin_auth_headers,
        )
        assert missing.status_code == 404
        assert embedding_client.query_calls == before_missing

        with factory.begin() as session:
            question = session.get(QuestionModel, question_id)
            assert question is not None
            session.delete(question)
        with factory() as session:
            assert session.scalar(select(SemanticIndexEntryModel)) is None
            assert session.scalar(select(SemanticIndexJobModel)) is None
    finally:
        remove_tracking()
        client.app.state.semantic_embedding_client = None
        client.app.state.settings.semantic_search_mode = "off"


def test_reconciler_covers_unqueued_imports_repairs_stale_rows_and_config_changes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = UUID(
        client.post(
            "/projects",
            json={"name": "Semantic reconciliation", "description": ""},
            headers=admin_auth_headers,
        ).json()["data"]["project_id"]
    )
    question_id = UUID(
        client.post(
            "/questions",
            json={
                "project_id": str(project_id),
                "text": "Zebra direct import",
                "question_type": "descriptive",
                "status": "active",
            },
            headers=admin_auth_headers,
        ).json()["data"]["question_id"]
    )
    factory = client.app.state.db_session_factory
    embedding_client = TopicEmbeddingClient()
    config_hash = semantic_configuration_hash(embedding_client.descriptor)

    with factory() as session:
        coverage = semantic_coverage(
            session,
            project_id=project_id,
            config_hash=config_hash,
        )
        assert coverage.total_jobs == 1
        assert coverage.current_jobs == 0
        assert coverage.coverage == 0.0

    with factory.begin() as session:
        assert SemanticIndexReconciler(
            session,
            embedding_client,
        ).reconcile_project(project_id) == 1
    worker = SemanticIndexWorker(factory, embedding_client)
    assert worker.process_one() is True

    # ORM-bypassing writes create no flush event. A stale semantic row must
    # degrade the whole semantic leg, preserve lexical behavior, and be found
    # by bounded reconciliation.
    with factory.begin() as session:
        session.execute(
            update(QuestionModel)
            .where(QuestionModel.question_id == question_id)
            .values(
                text="Zebra direct SQL revision",
                updated_at=datetime.now(timezone.utc),
            )
        )
    client.app.state.semantic_embedding_client = embedding_client
    client.app.state.settings.semantic_search_mode = "hybrid"
    try:
        stale = client.get(
            f"/projects/{project_id}/graph/search",
            params={"q": "hidden association", "retrieval_mode": "hybrid"},
            headers=admin_auth_headers,
        ).json()["data"]
        assert stale["retrieval"]["effective_mode"] == "lexical"
        assert stale["retrieval"]["semantic_state"] == "stale"
        assert stale["retrieval"]["fallback_reason"] == "semantic_index_invalid"

        with factory.begin() as session:
            assert SemanticIndexReconciler(
                session,
                embedding_client,
            ).reconcile_project(project_id) == 1
        assert worker.process_one() is True
        repaired = client.get(
            f"/projects/{project_id}/graph/search",
            params={"q": "hidden association", "retrieval_mode": "hybrid"},
            headers=admin_auth_headers,
        ).json()["data"]
        assert repaired["retrieval"]["effective_mode"] == "hybrid"
        assert repaired["items"][0]["node"]["entity_id"] == str(question_id)

        revised_client = TopicEmbeddingClient()
        revised_client._descriptor = EmbeddingDescriptor(
            adapter="test-topic",
            model="two-topic",
            revision="v2",
            dimensions=2,
            normalization="unit_l2",
            batch_limit=16,
        )
        revised_hash = semantic_configuration_hash(revised_client.descriptor)
        with factory.begin() as session:
            reconciler = SemanticIndexReconciler(session, revised_client)
            assert reconciler.reconcile_project(project_id, limit=10) == 1
        with factory() as session:
            hashes = set(session.scalars(select(SemanticIndexJobModel.config_hash)))
            assert hashes == {revised_hash}
    finally:
        client.app.state.semantic_embedding_client = None
        client.app.state.settings.semantic_search_mode = "off"


def test_concurrent_claim_is_exclusive_and_expired_lease_is_recoverable(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = UUID(
        client.post(
            "/projects",
            json={"name": "Semantic leases", "description": ""},
            headers=admin_auth_headers,
        ).json()["data"]["project_id"]
    )
    client.post(
        "/questions",
        json={
            "project_id": str(project_id),
            "text": "Zebra lease",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    )
    factory = client.app.state.db_session_factory
    embedding_client = TopicEmbeddingClient()
    with factory.begin() as session:
        SemanticIndexReconciler(session, embedding_client).reconcile_project(project_id)

    first_worker = SemanticIndexWorker(factory, embedding_client, lease_seconds=60)
    second_worker = SemanticIndexWorker(factory, embedding_client, lease_seconds=60)
    barrier = Barrier(2)

    def claim(worker: SemanticIndexWorker):
        barrier.wait(timeout=5)
        return worker._claim_one()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, (first_worker, second_worker)))
    successful_claims = [claim for claim in claims if claim is not None]
    assert len(successful_claims) == 1
    first_claim = successful_claims[0]
    assert first_worker._claim_one() is None

    with factory.begin() as session:
        job = session.get(SemanticIndexJobModel, first_claim.job_id)
        assert job is not None
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    recovered = second_worker._claim_one()
    assert recovered is not None
    assert recovered.job_id == first_claim.job_id
    assert recovered.claim_token != first_claim.claim_token


def test_graph_search_reports_each_non_stale_operational_fallback(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = client.post(
        "/projects",
        json={"name": "Semantic fallback matrix", "description": ""},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Lexical fallback question",
            "question_type": "descriptive",
            "status": "active",
        },
        headers=admin_auth_headers,
    )

    def fallback() -> dict[str, object]:
        response = client.get(
            f"/projects/{project_id}/graph/search",
            params={"q": "fallback", "retrieval_mode": "hybrid"},
            headers=admin_auth_headers,
        )
        assert response.status_code == 200
        return response.json()["data"]["retrieval"]

    assert fallback()["fallback_reason"] == "server_mode_off"
    client.app.state.settings.semantic_search_mode = "hybrid"
    assert fallback()["fallback_reason"] == "adapter_unavailable"
    client.app.state.semantic_embedding_client = TopicEmbeddingClient()
    assert fallback()["fallback_reason"] == "coverage_below_threshold"
    client.app.state.semantic_embedding_client = TimeoutEmbeddingClient()
    assert fallback()["fallback_reason"] == "semantic_timeout"

    client.app.state.semantic_embedding_client = None
    client.app.state.settings.semantic_search_mode = "off"


def test_generation_change_during_embedding_discards_stale_result(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = client.post(
        "/projects",
        json={"name": "Semantic generation race", "description": ""},
        headers=admin_auth_headers,
    ).json()["data"]["project_id"]
    question_id = UUID(
        client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": "Zebra generation one",
                "question_type": "descriptive",
                "status": "active",
            },
            headers=admin_auth_headers,
        ).json()["data"]["question_id"]
    )
    factory = client.app.state.db_session_factory
    embedding_client = RacingEmbeddingClient()
    remove_tracking = install_semantic_change_tracking(
        factory,
        client=embedding_client,
    )
    try:
        with factory.begin() as session:
            question = session.get(QuestionModel, question_id)
            assert question is not None
            question.text = "Zebra generation queued"

        def advance_generation() -> None:
            with factory.begin() as session:
                question = session.get(QuestionModel, question_id)
                assert question is not None
                question.text = "Zebra generation two"

        embedding_client.on_first_document = advance_generation
        worker = SemanticIndexWorker(factory, embedding_client)
        assert worker.process_one() is True
        with factory() as session:
            job = session.scalar(select(SemanticIndexJobModel))
            assert job is not None
            assert job.requested_generation == 2
            assert job.completed_generation == 0
            assert job.state == "pending"
            assert session.scalar(select(SemanticIndexEntryModel)) is None

        assert worker.process_one() is True
        with factory() as session:
            job = session.scalar(select(SemanticIndexJobModel))
            entry = session.scalar(select(SemanticIndexEntryModel))
            assert job is not None and job.completed_generation == 2
            assert entry is not None
            document = GraphQueryService(session).render_document(
                UUID(project_id),
                entity_type="question",
                entity_id=question_id,
            )
            assert document is not None
            assert entry.document_hash == document.document_hash
    finally:
        remove_tracking()
