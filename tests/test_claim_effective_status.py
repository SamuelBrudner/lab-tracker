"""Pure tests for the derived claim interpretation (no database)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from lab_tracker.claim_effective_status import (
    CONTESTING_RELATIONS,
    INTERPRETATION_QUERIES_PER_PROJECT,
    STATUS_AFFECTING_RELATIONS,
    ClaimInterpretation,
    ClaimInterpretationError,
    interpret_claims,
    load_claim_interpretations,
)
from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimEdge,
    ClaimEffectiveStatus,
    ClaimRelation,
    ClaimStatus,
    Dataset,
    DatasetCommitManifest,
    DatasetStatus,
    EntityRef,
    EntityType,
    ExplorationNode,
    ExplorationNodeStatus,
    ExplorationNodeType,
    QuestionLink,
    QuestionLinkRole,
)

PROJECT = UUID("99999999-9999-9999-9999-000000000001")
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _at(days: int) -> datetime:
    return T0 + timedelta(days=days)


def _claim(
    status: ClaimStatus = ClaimStatus.PROPOSED,
    *,
    created_at: datetime = T0,
    question_ids: list[UUID] | None = None,
    dataset_ids: list[UUID] | None = None,
    analysis_ids: list[UUID] | None = None,
) -> Claim:
    return Claim(
        claim_id=uuid4(),
        project_id=PROJECT,
        statement="A prediction.",
        confidence=50,
        status=status,
        terminal_reason="Refuted." if status == ClaimStatus.REJECTED else None,
        answers_question_ids=question_ids or [],
        supported_by_dataset_ids=dataset_ids or [],
        supported_by_analysis_ids=analysis_ids or [],
        created_at=created_at,
    )


def _edge(
    source: Claim,
    target: Claim,
    relation: ClaimRelation,
    *,
    created_at: datetime = T0,
) -> ClaimEdge:
    return ClaimEdge(
        edge_id=uuid4(),
        claim_id=source.claim_id,
        target_claim_id=target.claim_id,
        relation=relation,
        created_at=created_at,
    )


def _pivot(
    target: Claim,
    *,
    status: ExplorationNodeStatus = ExplorationNodeStatus.COMMITTED,
    node_type: ExplorationNodeType = ExplorationNodeType.PIVOT,
    created_at: datetime = T0,
) -> ExplorationNode:
    return ExplorationNode(
        node_id=uuid4(),
        project_id=PROJECT,
        node_type=node_type,
        title="Pivot away",
        target=EntityRef(entity_type=EntityType.CLAIM, entity_id=target.claim_id),
        status=status,
        trigger="The replication failed.",
        rationale="The effect did not hold.",
        invalidates_claim_id=target.claim_id,
        created_at=created_at,
    )


def _dataset(
    *,
    question_id: UUID,
    status: DatasetStatus = DatasetStatus.COMMITTED,
    created_at: datetime = T0,
    linked_question_ids: list[UUID] | None = None,
) -> Dataset:
    return Dataset(
        dataset_id=uuid4(),
        project_id=PROJECT,
        commit_hash="commit",
        primary_question_id=question_id,
        question_links=[
            QuestionLink(question_id=item, role=QuestionLinkRole.SECONDARY)
            for item in (linked_question_ids or [])
        ],
        commit_manifest=DatasetCommitManifest(),
        status=status,
        created_at=created_at,
    )


def _analysis(
    dataset: Dataset,
    *,
    executed_at: datetime,
    status: AnalysisStatus = AnalysisStatus.COMMITTED,
) -> Analysis:
    return Analysis(
        analysis_id=uuid4(),
        project_id=PROJECT,
        dataset_ids=[dataset.dataset_id],
        method_hash="method",
        code_version="v1",
        executed_at=executed_at,
        status=status,
        created_at=executed_at,
    )


def _interpret(
    claims: list[Claim],
    *,
    edges: list[ClaimEdge] | None = None,
    nodes: list[ExplorationNode] | None = None,
    datasets: list[Dataset] | None = None,
    analyses: list[Analysis] | None = None,
    extra_claims: list[Claim] | None = None,
) -> dict[UUID, ClaimInterpretation]:
    return interpret_claims(
        claims,
        claims_by_id={item.claim_id: item for item in [*claims, *(extra_claims or [])]},
        edges=edges or [],
        exploration_nodes=nodes or [],
        datasets=datasets or [],
        analyses=analyses or [],
    )


def test_supersedes_edge_marks_target_superseded_not_source() -> None:
    old, new = _claim(ClaimStatus.SUPPORTED, dataset_ids=[uuid4()]), _claim()
    result = _interpret([old, new], edges=[_edge(new, old, ClaimRelation.SUPERSEDES)])

    assert result[old.claim_id].effective_status == ClaimEffectiveStatus.SUPERSEDED
    assert result[old.claim_id].superseded_by_claim_id == new.claim_id
    assert result[new.claim_id].effective_status == ClaimEffectiveStatus.PROPOSED
    assert result[new.claim_id].superseded_by_claim_id is None
    assert old.status == ClaimStatus.SUPPORTED, "stored status is never mutated"


def test_refutes_and_contradicts_mark_target_contested_in_edge_order() -> None:
    target, refuter, contradictor = _claim(ClaimStatus.SUPPORTED), _claim(), _claim()
    edges = [
        _edge(contradictor, target, ClaimRelation.CONTRADICTS, created_at=_at(2)),
        _edge(refuter, target, ClaimRelation.REFUTES, created_at=_at(1)),
        _edge(refuter, target, ClaimRelation.CONTRADICTS, created_at=_at(3)),
    ]
    result = _interpret([target, refuter, contradictor], edges=edges)

    assert result[target.claim_id].effective_status == ClaimEffectiveStatus.CONTESTED
    assert result[target.claim_id].contested_by_claim_ids == (
        refuter.claim_id,
        contradictor.claim_id,
    )
    assert result[refuter.claim_id].contested_by_claim_ids == ()


@pytest.mark.parametrize("relation", sorted(STATUS_AFFECTING_RELATIONS, key=str))
def test_rejected_source_claim_does_not_contest_or_supersede(relation: ClaimRelation) -> None:
    target, rejected = _claim(ClaimStatus.SUPPORTED), _claim(ClaimStatus.REJECTED)
    result = _interpret([target, rejected], edges=[_edge(rejected, target, relation)])

    assert result[target.claim_id].effective_status == ClaimEffectiveStatus.SUPPORTED
    assert result[target.claim_id].superseded_by_claim_id is None
    assert result[target.claim_id].contested_by_claim_ids == ()


def test_rejected_claim_keeps_rejected_over_edges_and_pivots() -> None:
    rejected, other = _claim(ClaimStatus.REJECTED), _claim()
    result = _interpret(
        [rejected, other],
        edges=[_edge(other, rejected, ClaimRelation.SUPERSEDES)],
        nodes=[_pivot(rejected)],
    )

    assert result[rejected.claim_id].effective_status == ClaimEffectiveStatus.REJECTED
    assert result[rejected.claim_id].superseded_by_claim_id == other.claim_id


def test_committed_pivot_invalidates_claim_and_staged_or_archived_pivot_does_not() -> None:
    claim = _claim(ClaimStatus.SUPPORTED)
    committed = _pivot(claim)
    assert (
        _interpret([claim], nodes=[committed])[claim.claim_id].effective_status
        == ClaimEffectiveStatus.INVALIDATED
    )
    assert (
        _interpret([claim], nodes=[committed])[claim.claim_id].invalidated_by_node_id
        == committed.node_id
    )
    for status in (ExplorationNodeStatus.STAGED, ExplorationNodeStatus.ARCHIVED):
        result = _interpret([claim], nodes=[_pivot(claim, status=status)])[claim.claim_id]
        assert result.effective_status == ClaimEffectiveStatus.SUPPORTED
        assert result.invalidated_by_node_id is None
    decision = _pivot(claim, node_type=ExplorationNodeType.DECISION)
    assert _interpret([claim], nodes=[decision])[claim.claim_id].invalidated_by_node_id is None


def test_precedence_invalidated_over_superseded_over_contested() -> None:
    claim, superseder, contester = _claim(ClaimStatus.SUPPORTED), _claim(), _claim()
    edges = [
        _edge(superseder, claim, ClaimRelation.SUPERSEDES),
        _edge(contester, claim, ClaimRelation.REFUTES),
    ]
    contested_only = _interpret([claim, contester], edges=edges[1:])[claim.claim_id]
    superseded = _interpret([claim, superseder, contester], edges=edges)[claim.claim_id]
    invalidated = _interpret(
        [claim, superseder, contester], edges=edges, nodes=[_pivot(claim)]
    )[claim.claim_id]

    assert contested_only.effective_status == ClaimEffectiveStatus.CONTESTED
    assert superseded.effective_status == ClaimEffectiveStatus.SUPERSEDED
    assert superseded.contested_by_claim_ids == (contester.claim_id,)
    assert invalidated.effective_status == ClaimEffectiveStatus.INVALIDATED
    assert invalidated.superseded_by_claim_id == superseder.claim_id


def test_latest_superseder_wins_deterministically() -> None:
    claim, first, second = _claim(), _claim(), _claim()
    edges = [
        _edge(first, claim, ClaimRelation.SUPERSEDES, created_at=_at(1)),
        _edge(second, claim, ClaimRelation.SUPERSEDES, created_at=_at(2)),
    ]
    assert (
        _interpret([claim, first, second], edges=edges)[claim.claim_id].superseded_by_claim_id
        == second.claim_id
    )
    # Same timestamp: the larger edge_id string wins, whichever order edges arrive in.
    tied = [
        _edge(first, claim, ClaimRelation.SUPERSEDES),
        _edge(second, claim, ClaimRelation.SUPERSEDES),
    ]
    expected = max(tied, key=lambda edge: str(edge.edge_id)).claim_id
    for ordering in (tied, list(reversed(tied))):
        result = _interpret([claim, first, second], edges=ordering)
        assert result[claim.claim_id].superseded_by_claim_id == expected


def test_missing_edge_source_raises_claim_interpretation_error() -> None:
    claim, unknown = _claim(), _claim()
    with pytest.raises(ClaimInterpretationError, match=str(unknown.claim_id)):
        interpret_claims(
            [claim],
            claims_by_id={claim.claim_id: claim},
            edges=[_edge(unknown, claim, ClaimRelation.REFUTES)],
            exploration_nodes=[],
            datasets=[],
            analyses=[],
        )


def test_missing_source_is_only_an_error_when_it_could_change_a_status() -> None:
    claim, unknown, other = _claim(), _claim(), _claim()
    harmless = [
        _edge(unknown, claim, ClaimRelation.EXTENDS),
        _edge(unknown, claim, ClaimRelation.DEPENDS_ON),
        _edge(unknown, other, ClaimRelation.SUPERSEDES),
    ]
    result = interpret_claims(
        [claim],
        claims_by_id={claim.claim_id: claim},
        edges=harmless,
        exploration_nodes=[],
        datasets=[],
        analyses=[],
    )
    assert result[claim.claim_id].effective_status == ClaimEffectiveStatus.PROPOSED


def test_pre_registered_true_when_claim_precedes_committed_question_dataset() -> None:
    question_id = uuid4()
    claim = _claim(question_ids=[question_id], created_at=_at(0))
    primary = _dataset(question_id=question_id, created_at=_at(1))
    linked = _dataset(question_id=uuid4(), linked_question_ids=[question_id], created_at=_at(2))
    assert _interpret([claim], datasets=[primary])[claim.claim_id].pre_registered is True
    assert _interpret([claim], datasets=[linked])[claim.claim_id].pre_registered is True
    cited = _dataset(question_id=uuid4(), created_at=_at(1))
    citing = _claim(dataset_ids=[cited.dataset_id], created_at=_at(0))
    assert _interpret([citing], datasets=[cited])[citing.claim_id].pre_registered is True


def test_pre_registered_false_for_staged_dataset_or_no_evidence_or_supported_at_creation():
    question_id = uuid4()
    claim = _claim(question_ids=[question_id], created_at=_at(0))
    staged = _dataset(question_id=question_id, status=DatasetStatus.STAGED, created_at=_at(1))
    assert _interpret([claim], datasets=[staged])[claim.claim_id].pre_registered is False
    assert _interpret([claim])[claim.claim_id].pre_registered is False
    older = _dataset(question_id=question_id, created_at=_at(-5))
    supported_later = _claim(
        ClaimStatus.SUPPORTED,
        question_ids=[question_id],
        dataset_ids=[older.dataset_id],
        created_at=_at(0),
    )
    result = _interpret([supported_later], datasets=[older])
    assert result[supported_later.claim_id].pre_registered is False


def test_analysis_executed_at_counts_as_evidence_time() -> None:
    question_id = uuid4()
    dataset = _dataset(question_id=question_id, created_at=_at(5))
    earlier_analysis = _analysis(dataset, executed_at=_at(1))
    cited = _claim(analysis_ids=[earlier_analysis.analysis_id], created_at=_at(2))
    assert (
        _interpret([cited], datasets=[dataset], analyses=[earlier_analysis])[
            cited.claim_id
        ].pre_registered
        is False
    )
    predicted = _claim(question_ids=[question_id], created_at=_at(0))
    result = _interpret([predicted], datasets=[dataset], analyses=[earlier_analysis])
    assert result[predicted.claim_id].pre_registered is True
    staged_analysis = _analysis(dataset, executed_at=_at(-1), status=AnalysisStatus.STAGED)
    late_claim = _claim(analysis_ids=[staged_analysis.analysis_id], created_at=_at(0))
    assert (
        _interpret([late_claim], analyses=[staged_analysis])[late_claim.claim_id].pre_registered
        is False
    )


def _random_acyclic_edges(rng: random.Random, claims: list[Claim]) -> list[ClaimEdge]:
    """Edges only point from a later index to an earlier one, so no cycle is possible."""

    edges: list[ClaimEdge] = []
    relations = list(ClaimRelation)
    for target_index in range(len(claims)):
        for source_index in range(target_index + 1, len(claims)):
            if rng.random() < 0.4:
                edges.append(
                    _edge(
                        claims[source_index],
                        claims[target_index],
                        rng.choice(relations),
                        created_at=_at(rng.randint(0, 30)),
                    )
                )
    return edges


def test_interpretation_is_invariant_under_input_permutation() -> None:
    rng = random.Random(0)
    statuses = list(ClaimStatus)
    for _ in range(200):
        claims = [
            _claim(rng.choice(statuses), created_at=_at(rng.randint(0, 10))) for _ in range(6)
        ]
        edges = _random_acyclic_edges(rng, claims)
        pivots = [
            _pivot(
                rng.choice(claims),
                status=rng.choice(list(ExplorationNodeStatus)),
                created_at=_at(rng.randint(0, 30)),
            )
            for _ in range(rng.randint(0, 3))
        ]
        baseline = _interpret(claims, edges=edges, nodes=pivots)
        for _ in range(3):
            shuffled_claims = list(claims)
            shuffled_edges = list(edges)
            shuffled_pivots = list(pivots)
            rng.shuffle(shuffled_claims)
            rng.shuffle(shuffled_edges)
            rng.shuffle(shuffled_pivots)
            assert _interpret(shuffled_claims, edges=shuffled_edges, nodes=shuffled_pivots) == (
                baseline
            )
        by_id = {claim.claim_id: claim for claim in claims}
        for claim in claims:
            live_contest = any(
                edge.target_claim_id == claim.claim_id
                and edge.relation in CONTESTING_RELATIONS
                and by_id[edge.claim_id].status != ClaimStatus.REJECTED
                for edge in edges
            )
            if live_contest:
                assert baseline[claim.claim_id].effective_status != ClaimEffectiveStatus.SUPPORTED
            if claim.status == ClaimStatus.REJECTED:
                assert baseline[claim.claim_id].effective_status == ClaimEffectiveStatus.REJECTED


class _SpyRepository:
    def __init__(self, claims: list[Claim], edges: list[ClaimEdge]) -> None:
        self._claims = claims
        self._edges = edges
        self.calls: list[tuple[str, dict[str, object]]] = []

    def query_claims(self, **kwargs: object) -> tuple[list[Claim], int]:
        self.calls.append(("query_claims", kwargs))
        items = [item for item in self._claims if item.project_id == kwargs["project_id"]]
        return items, len(items)

    def query_claim_edges(self, **kwargs: object) -> tuple[list[ClaimEdge], int]:
        self.calls.append(("query_claim_edges", kwargs))
        return list(self._edges), len(self._edges)

    def query_exploration_nodes(self, **kwargs: object) -> tuple[list[ExplorationNode], int]:
        self.calls.append(("query_exploration_nodes", kwargs))
        return [], 0

    def query_datasets(self, **kwargs: object) -> tuple[list[Dataset], int]:
        self.calls.append(("query_datasets", kwargs))
        return [], 0

    def query_analyses(self, **kwargs: object) -> tuple[list[Analysis], int]:
        self.calls.append(("query_analyses", kwargs))
        return [], 0


def test_loader_issues_five_scoped_queries_per_project_and_interprets_every_claim() -> None:
    other_project = UUID("99999999-9999-9999-9999-000000000002")
    a, b = _claim(ClaimStatus.SUPPORTED), _claim()
    c = _claim().model_copy(update={"project_id": other_project})
    repository = _SpyRepository([a, b, c], [_edge(b, a, ClaimRelation.SUPERSEDES)])

    result = load_claim_interpretations(repository, [a, c, b])  # type: ignore[arg-type]

    assert set(result) == {a.claim_id, b.claim_id, c.claim_id}
    assert result[a.claim_id].effective_status == ClaimEffectiveStatus.SUPERSEDED
    assert len(repository.calls) == 2 * INTERPRETATION_QUERIES_PER_PROJECT
    per_project: dict[object, list[str]] = {}
    for name, kwargs in repository.calls:
        assert kwargs["limit"] is None and kwargs["offset"] == 0
        per_project.setdefault(kwargs["project_id"], []).append(name)
    assert set(per_project) == {PROJECT, other_project}
    for names in per_project.values():
        assert names == [
            "query_claims",
            "query_claim_edges",
            "query_exploration_nodes",
            "query_datasets",
            "query_analyses",
        ]
    scoped = {name: kwargs for name, kwargs in repository.calls if kwargs["project_id"] == PROJECT}
    assert scoped["query_exploration_nodes"]["node_type"] == "pivot"
    assert scoped["query_exploration_nodes"]["status"] == "committed"
    assert scoped["query_datasets"]["status"] == "committed"
    assert scoped["query_analyses"]["status"] == "committed"


def test_loader_with_no_claims_queries_nothing() -> None:
    repository = _SpyRepository([], [])
    assert load_claim_interpretations(repository, []) == {}  # type: ignore[arg-type]
    assert repository.calls == []
