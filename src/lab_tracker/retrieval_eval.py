"""Offline metrics and rollout gate for the versioned GraphRAG eval format."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

RetrievalEvalStrategy = Literal[
    "current_decision_context",
    "ranked_lexical_seeds",
    "lexical_graph",
    "semantic_graph",
    "hybrid_rrf_graph",
]
RETRIEVAL_EVAL_STRATEGIES: tuple[RetrievalEvalStrategy, ...] = (
    "current_decision_context",
    "ranked_lexical_seeds",
    "lexical_graph",
    "semantic_graph",
    "hybrid_rrf_graph",
)


@dataclass(frozen=True)
class RetrievalObservation:
    seed_node_ids: tuple[str, ...]
    returned_node_ids: tuple[str, ...]
    path_hits: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_5: float
    recall_at_10: float
    recall_at_20: float
    mrr_at_10: float
    ndcg_at_10: float
    typed_path_recall: float
    relevant_node_precision: float
    forbidden_hit_rate: float
    case_count: int


@dataclass(frozen=True)
class SemanticRolloutGate:
    passed: bool
    typed_path_delta: float
    zero_overlap_typed_path_delta: float
    exact_top1_retention: float
    ndcg_regression: float
    new_hard_negative_regressions: int


def load_retrieval_eval_corpus(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if payload.get("schema") != "retrieval-eval/v1":
        raise ValueError("Unsupported retrieval evaluation corpus schema.")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) < 40:
        raise ValueError("retrieval-eval/v1 requires at least 40 cases.")
    required = {
        "id",
        "query",
        "graded_seed_nodes",
        "expected_paths",
        "forbidden_nodes",
        "filters",
        "tags",
        "budgets",
    }
    for case in cases:
        if not isinstance(case, dict) or not required.issubset(case):
            raise ValueError("Retrieval evaluation case is incomplete.")
    return cast(dict[str, Any], payload)


def corpus_hash(corpus: dict[str, Any]) -> str:
    encoded = json.dumps(corpus, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def evaluate_retrieval(
    corpus: dict[str, Any],
    observations: dict[str, RetrievalObservation],
    *,
    required_tag: str | None = None,
) -> RetrievalMetrics:
    cases = [
        case
        for case in corpus["cases"]
        if required_tag is None or required_tag in case["tags"]
    ]
    if not cases:
        return RetrievalMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    recalls: dict[int, list[float]] = {5: [], 10: [], 20: []}
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    path_recalls: list[float] = []
    precisions: list[float] = []
    forbidden_rates: list[float] = []
    for case in cases:
        observation = observations.get(case["id"], RetrievalObservation((), ()))
        grades = {
            item["node_id"]: int(item["grade"])
            for item in case["graded_seed_nodes"]
            if int(item["grade"]) > 0
        }
        relevant = set(grades)
        for cutoff in recalls:
            recalls[cutoff].append(
                len(relevant.intersection(observation.seed_node_ids[:cutoff]))
                / max(1, len(relevant))
            )
        first_rank = next(
            (
                rank
                for rank, node_id in enumerate(observation.seed_node_ids[:10], start=1)
                if node_id in relevant
            ),
            None,
        )
        reciprocal_ranks.append(1.0 / first_rank if first_rank is not None else 0.0)
        gains = [grades.get(node_id, 0) for node_id in observation.seed_node_ids[:10]]
        ideal = sorted(grades.values(), reverse=True)[:10]
        dcg = _dcg(gains)
        ideal_dcg = _dcg(ideal)
        ndcgs.append(dcg / ideal_dcg if ideal_dcg else 0.0)

        expected_paths = {
            (item["endpoint_node_id"], relationship)
            for item in case["expected_paths"]
            for relationship in item["relationships"]
        }
        path_recalls.append(
            len(expected_paths.intersection(observation.path_hits))
            / max(1, len(expected_paths))
        )
        node_budget = int(case["budgets"]["max_nodes"])
        returned = observation.returned_node_ids[:node_budget]
        precisions.append(
            len(relevant.intersection(returned)) / max(1, len(returned))
        )
        forbidden = set(case["forbidden_nodes"])
        forbidden_rates.append(
            len(forbidden.intersection(observation.seed_node_ids[:10]))
            / max(1, min(10, len(observation.seed_node_ids)))
        )
    return RetrievalMetrics(
        recall_at_5=_mean(recalls[5]),
        recall_at_10=_mean(recalls[10]),
        recall_at_20=_mean(recalls[20]),
        mrr_at_10=_mean(reciprocal_ranks),
        ndcg_at_10=_mean(ndcgs),
        typed_path_recall=_mean(path_recalls),
        relevant_node_precision=_mean(precisions),
        forbidden_hit_rate=_mean(forbidden_rates),
        case_count=len(cases),
    )


def evaluate_semantic_rollout_gate(
    corpus: dict[str, Any],
    *,
    lexical_graph: dict[str, RetrievalObservation],
    hybrid_graph: dict[str, RetrievalObservation],
) -> SemanticRolloutGate:
    lexical_metrics = evaluate_retrieval(corpus, lexical_graph)
    hybrid_metrics = evaluate_retrieval(corpus, hybrid_graph)
    lexical_zero = evaluate_retrieval(corpus, lexical_graph, required_tag="zero_overlap")
    hybrid_zero = evaluate_retrieval(corpus, hybrid_graph, required_tag="zero_overlap")
    exact_cases = [case for case in corpus["cases"] if "exact" in case["tags"]]
    retained = 0
    for case in exact_cases:
        relevant = {item["node_id"] for item in case["graded_seed_nodes"]}
        lexical_top = lexical_graph.get(case["id"], RetrievalObservation((), ()))
        hybrid_top = hybrid_graph.get(case["id"], RetrievalObservation((), ()))
        if (
            lexical_top.seed_node_ids
            and lexical_top.seed_node_ids[0] in relevant
            and hybrid_top.seed_node_ids
            and hybrid_top.seed_node_ids[0] == lexical_top.seed_node_ids[0]
        ):
            retained += 1
    retention = retained / len(exact_cases) if exact_cases else 1.0
    regressions = _hard_negative_regressions(corpus, lexical_graph, hybrid_graph)
    typed_path_delta = hybrid_metrics.typed_path_recall - lexical_metrics.typed_path_recall
    zero_delta = hybrid_zero.typed_path_recall - lexical_zero.typed_path_recall
    ndcg_regression = max(0.0, lexical_metrics.ndcg_at_10 - hybrid_metrics.ndcg_at_10)
    passed = bool(
        typed_path_delta >= 0.10
        and zero_delta >= 0.20
        and retention == 1.0
        and ndcg_regression <= 0.02
        and regressions == 0
    )
    return SemanticRolloutGate(
        passed=passed,
        typed_path_delta=typed_path_delta,
        zero_overlap_typed_path_delta=zero_delta,
        exact_top1_retention=retention,
        ndcg_regression=ndcg_regression,
        new_hard_negative_regressions=regressions,
    )


def evaluate_strategy_matrix(
    corpus: dict[str, Any],
    observations: dict[
        RetrievalEvalStrategy,
        dict[str, RetrievalObservation],
    ],
) -> dict[RetrievalEvalStrategy, RetrievalMetrics]:
    """Evaluate all five rollout strategies against one fixed corpus budget."""

    missing = set(RETRIEVAL_EVAL_STRATEGIES) - set(observations)
    if missing:
        raise ValueError(
            "Retrieval strategy matrix is incomplete: " + ", ".join(sorted(missing))
        )
    return {
        strategy: evaluate_retrieval(corpus, observations[strategy])
        for strategy in RETRIEVAL_EVAL_STRATEGIES
    }


def aggregate_only_report(
    corpus: dict[str, Any],
    *,
    renderer_version: str,
    chunker_version: str,
    model_descriptor: dict[str, Any],
    dimensions: int,
    metrics: dict[str, RetrievalMetrics],
) -> dict[str, Any]:
    """Build a private-replay-safe report containing no queries or result IDs."""

    return {
        "schema": "retrieval-eval-report/v1",
        "corpus_hash": corpus_hash(corpus),
        "renderer_version": renderer_version,
        "chunker_version": chunker_version,
        "model_descriptor": model_descriptor,
        "dimensions": dimensions,
        "metrics": {
            name: value.__dict__
            for name, value in sorted(metrics.items())
        },
    }


def _hard_negative_regressions(
    corpus: dict[str, Any],
    lexical_graph: dict[str, RetrievalObservation],
    hybrid_graph: dict[str, RetrievalObservation],
) -> int:
    regressions = 0
    for case in corpus["cases"]:
        forbidden = set(case["forbidden_nodes"])
        lexical = lexical_graph.get(case["id"], RetrievalObservation((), ()))
        hybrid = hybrid_graph.get(case["id"], RetrievalObservation((), ()))
        if not forbidden.intersection(lexical.seed_node_ids[:10]) and forbidden.intersection(
            hybrid.seed_node_ids[:10]
        ):
            regressions += 1
    return regressions


def _dcg(grades: list[int]) -> float:
    return float(
        sum(
            (2.0**grade - 1.0) / math.log2(rank + 1)
            for rank, grade in enumerate(grades, 1)
        )
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
