from __future__ import annotations

import json
from pathlib import Path

from lab_tracker.retrieval_eval import (
    RETRIEVAL_EVAL_STRATEGIES,
    RetrievalObservation,
    aggregate_only_report,
    corpus_hash,
    evaluate_retrieval,
    evaluate_semantic_rollout_gate,
    evaluate_strategy_matrix,
    load_retrieval_eval_corpus,
)
from lab_tracker.semantic_retrieval import (
    EmbeddingDescriptor,
    FrozenVectorEmbeddingClient,
)

ROOT = Path(__file__).parents[1]


def test_retrieval_eval_v1_has_required_coverage_and_fixed_budgets() -> None:
    corpus = load_retrieval_eval_corpus(ROOT / "retrieval-eval" / "v1" / "corpus.json")
    assert len(corpus["cases"]) == 40
    tags = {tag for case in corpus["cases"] for tag in case["tags"]}
    assert {
        "question",
        "session",
        "note",
        "dataset",
        "analysis",
        "claim",
        "exploration_node",
        "visualization",
        "goal",
        "exact",
        "synonym",
        "acronym",
        "zero_overlap",
        "contradiction",
        "near_duplicate",
        "recency_distractor",
        "graph_bridge",
        "full_chain",
    } <= tags
    assert all(
        case["budgets"]
        == {"seed_limit": 20, "max_nodes": 50, "max_edges": 100, "depth": 2}
        for case in corpus["cases"]
    )


def test_frozen_vectors_are_provider_free_and_deterministic() -> None:
    payload = json.loads(
        (ROOT / "retrieval-eval" / "v1" / "frozen-vectors.json").read_text()
    )
    descriptor = EmbeddingDescriptor(**payload["descriptor"])
    vectors = {
        (item["purpose"], item["text"]): item["value"]
        for item in payload["vectors"]
    }
    client = FrozenVectorEmbeddingClient(descriptor, vectors)
    first = client.embed(["anticipated payoff signal"], purpose="query")
    second = client.embed(["anticipated payoff signal"], purpose="query")
    assert first == second == [(1.0, 0.0, 0.0, 0.0)]
    client.close()


def test_balanced_semantic_rollout_gate_and_aggregate_only_report() -> None:
    corpus = load_retrieval_eval_corpus(ROOT / "retrieval-eval" / "v1" / "corpus.json")
    lexical: dict[str, RetrievalObservation] = {}
    hybrid: dict[str, RetrievalObservation] = {}
    for case in corpus["cases"]:
        relevant = tuple(item["node_id"] for item in case["graded_seed_nodes"])
        path_hits = tuple(
            (path["endpoint_node_id"], relationship)
            for path in case["expected_paths"]
            for relationship in path["relationships"]
        )
        lexical[case["id"]] = RetrievalObservation(relevant, relevant)
        hybrid[case["id"]] = RetrievalObservation(relevant, relevant, path_hits)
    gate = evaluate_semantic_rollout_gate(
        corpus,
        lexical_graph=lexical,
        hybrid_graph=hybrid,
    )
    assert gate.passed is True
    metrics = evaluate_retrieval(corpus, hybrid)
    report = aggregate_only_report(
        corpus,
        renderer_version="graph-document-v1",
        chunker_version="paragraph-4000-400-v1",
        model_descriptor={"adapter": "private"},
        dimensions=512,
        metrics={"hybrid": metrics},
    )
    assert report["corpus_hash"] == corpus_hash(corpus)
    assert "queries" not in report
    assert "result_ids" not in report

    matrix = evaluate_strategy_matrix(
        corpus,
        {strategy: hybrid for strategy in RETRIEVAL_EVAL_STRATEGIES},
    )
    assert tuple(matrix) == RETRIEVAL_EVAL_STRATEGIES
    assert all(value.case_count == 40 for value in matrix.values())
