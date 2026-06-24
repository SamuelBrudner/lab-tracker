"""Project-scoped graph projection helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from lab_tracker.models import (
    Analysis,
    Claim,
    ClaimEdge,
    Dataset,
    EntityType,
    ExplorationNode,
    ExternalArtifactReference,
    Goal,
    Note,
    Question,
    QuestionLink,
    Session,
    Visualization,
)
from lab_tracker.repository import LabTrackerRepository
from lab_tracker.schemas import (
    ProjectGraphEdge,
    ProjectGraphNode,
    ProjectGraphRead,
    ProjectGraphView,
)

_VIEW_VALUES = {"evidence", "questions", "full"}
_NODE_TYPE_ORDER = {
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
_QUESTION_LINK_ROLE_ORDER = {"primary": 0, "secondary": 1}
_GRAPH_LABEL_LIMIT = 180


def build_project_graph(
    repository: LabTrackerRepository,
    project_id: UUID,
    *,
    view: ProjectGraphView = "evidence",
) -> ProjectGraphRead:
    """Build a deterministic, read-only graph projection for a project."""

    view_value = _normalize_view(view)
    questions, _ = repository.query_questions(project_id=project_id, limit=None, offset=0)
    datasets: list[Dataset] = []
    analyses: list[Analysis] = []
    claims: list[Claim] = []
    claim_edges: list[ClaimEdge] = []
    exploration_nodes: list[ExplorationNode] = []
    visualizations: list[Visualization] = []
    goals: list[Goal] = []
    notes: list[Note] = []
    sessions: list[Session] = []
    if view_value in {"evidence", "full"}:
        datasets, _ = repository.query_datasets(project_id=project_id, limit=None, offset=0)
        analyses, _ = repository.query_analyses(project_id=project_id, limit=None, offset=0)
        claims, _ = repository.query_claims(project_id=project_id, limit=None, offset=0)
        visualizations, _ = repository.query_visualizations(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        goals, _ = repository.query_goals(project_id=project_id, limit=None, offset=0)
        claim_edges, _ = repository.query_claim_edges(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        exploration_nodes, _ = repository.query_exploration_nodes(
            project_id=project_id,
            limit=None,
            offset=0,
        )
    if view_value == "full":
        notes, _ = repository.query_notes(project_id=project_id, limit=None, offset=0)
        sessions, _ = repository.query_sessions(project_id=project_id, limit=None, offset=0)

    builder = _ProjectGraphBuilder(project_id=project_id, view=view_value)
    for question in _sort_entities(questions, "question_id", _question_label):
        builder.add_node(_question_node(question))

    if view_value in {"evidence", "full"}:
        for session in _sort_entities(sessions, "session_id", _session_label):
            builder.add_node(_session_node(session))
        for note in _sort_entities(notes, "note_id", _note_label):
            builder.add_node(_note_node(note))
        for dataset in _sort_entities(datasets, "dataset_id", _dataset_label):
            builder.add_node(_dataset_node(dataset))
        for analysis in _sort_entities(analyses, "analysis_id", _analysis_label):
            builder.add_node(_analysis_node(analysis))
        for claim in _sort_entities(claims, "claim_id", _claim_label):
            builder.add_node(_claim_node(claim))
            for citation in sorted(claim.external_citations, key=_external_artifact_sort_key):
                builder.add_node(_external_artifact_node(citation))
        for node in _sort_entities(exploration_nodes, "node_id", _exploration_node_label):
            builder.add_node(_exploration_node_node(node))
        for visualization in _sort_entities(visualizations, "viz_id", _visualization_label):
            builder.add_node(_visualization_node(visualization))
        for goal in _sort_entities(goals, "goal_id", _goal_label):
            builder.add_node(_goal_node(goal))

    _add_question_edges(builder, questions)
    if view_value in {"evidence", "full"}:
        _add_evidence_edges(
            builder,
            datasets,
            analyses,
            claims,
            claim_edges,
            exploration_nodes,
            visualizations,
            goals,
        )
    if view_value == "full":
        _add_full_edges(builder, notes, sessions, datasets)

    return builder.graph()


def project_graph_to_mermaid(graph: ProjectGraphRead) -> str:
    """Render a project graph as stable Mermaid flowchart text."""

    lines = ["graph LR"]
    if not graph.nodes:
        lines.append('  empty["No graph records"]')
        return "\n".join(lines) + "\n"

    node_ids = {node.id: f"n{index}" for index, node in enumerate(graph.nodes)}
    for node in graph.nodes:
        label = f"{node.entity_type}: {node.label}"
        lines.append(f'  {node_ids[node.id]}["{_escape_mermaid(label)}"]')
    for edge in graph.edges:
        source = node_ids.get(edge.source)
        target = node_ids.get(edge.target)
        if source is None or target is None:
            continue
        lines.append(f'  {source} -- "{_escape_mermaid(edge.label)}" --> {target}')
    return "\n".join(lines) + "\n"


class _ProjectGraphBuilder:
    def __init__(self, *, project_id: UUID, view: ProjectGraphView) -> None:
        self._project_id = project_id
        self._view = view
        self._nodes: dict[str, ProjectGraphNode] = {}
        self._edges: dict[str, ProjectGraphEdge] = {}

    def add_node(self, node: ProjectGraphNode) -> None:
        self._nodes[node.id] = node

    def add_edge(self, source: str, target: str, label: str, relationship: str) -> None:
        if source not in self._nodes or target not in self._nodes:
            return
        edge_id = f"{relationship}:{source}->{target}"
        self._edges[edge_id] = ProjectGraphEdge(
            id=edge_id,
            label=label,
            relationship=relationship,
            source=source,
            target=target,
        )

    def graph(self) -> ProjectGraphRead:
        nodes = sorted(
            self._nodes.values(),
            key=lambda node: (
                _NODE_TYPE_ORDER.get(node.entity_type, 99),
                node.label.casefold(),
                node.id,
            ),
        )
        edges = sorted(
            self._edges.values(),
            key=lambda edge: (edge.source, edge.target, edge.relationship, edge.label),
        )
        return ProjectGraphRead(
            project_id=self._project_id,
            view=self._view,
            nodes=nodes,
            edges=edges,
        )


def _normalize_view(view: ProjectGraphView) -> ProjectGraphView:
    view_value = str(view)
    if view_value not in _VIEW_VALUES:
        raise ValueError(f"Unknown project graph view: {view_value}")
    return view_value  # type: ignore[return-value]


def _entity_node_id(entity_type: str, entity_id: UUID | str) -> str:
    return f"{entity_type}:{entity_id}"


def _external_artifact_node_id(artifact: ExternalArtifactReference) -> str:
    return _entity_node_id("external_artifact", artifact.uri)


def _external_artifact_sort_key(artifact: ExternalArtifactReference) -> tuple[str, str, str]:
    return (artifact.source_system, artifact.uri, artifact.content_hash)


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return str(enum_value)
    return str(value)


def _short_id(value: UUID) -> str:
    return str(value).split("-", 1)[0]


def _sort_entities(items: Iterable[Any], id_attr: str, label_getter) -> list[Any]:
    return sorted(
        items,
        key=lambda item: (
            str(label_getter(item)).casefold(),
            str(getattr(item, id_attr)),
        ),
    )


def _truncate_graph_label(value: str) -> str:
    return value[:_GRAPH_LABEL_LIMIT]


def _question_label(question: Question) -> str:
    return question.text


def _dataset_label(dataset: Dataset) -> str:
    return f"Dataset {dataset.commit_hash or _short_id(dataset.dataset_id)}"


def _analysis_label(analysis: Analysis) -> str:
    return f"Analysis {analysis.code_version or _short_id(analysis.analysis_id)}"


def _claim_label(claim: Claim) -> str:
    return _truncate_graph_label(claim.statement)


def _exploration_node_label(node: ExplorationNode) -> str:
    return _truncate_graph_label(node.title)


def _visualization_label(visualization: Visualization) -> str:
    return visualization.caption or visualization.viz_type


def _goal_label(goal: Goal) -> str:
    return goal.title


def _note_label(note: Note) -> str:
    if note.transcribed_text:
        return _truncate_graph_label(note.transcribed_text)
    if note.raw_content:
        return _truncate_graph_label(note.raw_content)
    if note.raw_asset is not None:
        return _truncate_graph_label(note.raw_asset.filename)
    return f"Note {_short_id(note.note_id)}"


def _session_label(session: Session) -> str:
    return f"{_enum_value(session.session_type) or 'session'} session"


def _question_node(question: Question) -> ProjectGraphNode:
    return ProjectGraphNode(
        id=_entity_node_id("question", question.question_id),
        entity_type="question",
        entity_id=str(question.question_id),
        label=question.text,
        detail=_enum_value(question.question_type),
        status=_enum_value(question.status),
        route=f"/app/questions/{question.question_id}",
        metadata={
            "hypothesis": question.hypothesis or "",
            "question_type": _enum_value(question.question_type) or "",
        },
    )


def _dataset_node(dataset: Dataset) -> ProjectGraphNode:
    return ProjectGraphNode(
        id=_entity_node_id("dataset", dataset.dataset_id),
        entity_type="dataset",
        entity_id=str(dataset.dataset_id),
        label=_dataset_label(dataset),
        detail=dataset.commit_hash,
        status=_enum_value(dataset.status),
        route=f"/app/datasets/{dataset.dataset_id}",
    )


def _analysis_node(analysis: Analysis) -> ProjectGraphNode:
    return ProjectGraphNode(
        id=_entity_node_id("analysis", analysis.analysis_id),
        entity_type="analysis",
        entity_id=str(analysis.analysis_id),
        label=_analysis_label(analysis),
        detail=analysis.method_hash,
        status=_enum_value(analysis.status),
        metadata={"code_version": analysis.code_version},
    )


def _claim_node(claim: Claim) -> ProjectGraphNode:
    return ProjectGraphNode(
        id=_entity_node_id("claim", claim.claim_id),
        entity_type="claim",
        entity_id=str(claim.claim_id),
        label=_claim_label(claim),
        detail=f"confidence {claim.confidence:g}",
        status=_enum_value(claim.status),
    )


def _exploration_node_node(node: ExplorationNode) -> ProjectGraphNode:
    return ProjectGraphNode(
        id=_entity_node_id("exploration_node", node.node_id),
        entity_type="exploration_node",
        entity_id=str(node.node_id),
        label=_exploration_node_label(node),
        detail=node.node_type.value.replace("_", " "),
        status=_enum_value(node.status),
        metadata={
            "target_entity_type": node.target.entity_type.value,
            "target_entity_id": str(node.target.entity_id),
            "origin": node.origin.value,
        },
    )


def _external_artifact_node(artifact: ExternalArtifactReference) -> ProjectGraphNode:
    return ProjectGraphNode(
        id=_external_artifact_node_id(artifact),
        entity_type="external_artifact",
        entity_id=artifact.uri,
        label=artifact.uri,
        detail=artifact.source_system,
        metadata={
            "kind": artifact.kind.value,
            "content_hash": artifact.content_hash,
            "metadata": artifact.metadata,
        },
    )


def _visualization_node(visualization: Visualization) -> ProjectGraphNode:
    asset_metadata: dict[str, Any] = {}
    if visualization.asset is not None:
        asset_metadata = {
            "asset": {
                "filename": visualization.asset.filename,
                "content_type": visualization.asset.content_type,
                "checksum": visualization.asset.checksum,
            },
            "asset_download_path": visualization.asset_download_path,
        }
    return ProjectGraphNode(
        id=_entity_node_id("visualization", visualization.viz_id),
        entity_type="visualization",
        entity_id=str(visualization.viz_id),
        label=_visualization_label(visualization),
        detail=visualization.file_path,
        route=f"/app/visualizations/{visualization.viz_id}",
        metadata={"viz_type": visualization.viz_type, **asset_metadata},
    )


def _goal_node(goal: Goal) -> ProjectGraphNode:
    return ProjectGraphNode(
        id=_entity_node_id("goal", goal.goal_id),
        entity_type="goal",
        entity_id=str(goal.goal_id),
        label=goal.title,
        detail=_enum_value(goal.goal_type),
        status=_enum_value(goal.status),
        route=f"/app/goals/{goal.goal_id}",
        metadata={
            "target_date": goal.target_date.isoformat() if goal.target_date else "",
            "external_ref": goal.external_ref or "",
        },
    )


def _note_node(note: Note) -> ProjectGraphNode:
    return ProjectGraphNode(
        id=_entity_node_id("note", note.note_id),
        entity_type="note",
        entity_id=str(note.note_id),
        label=_note_label(note),
        status=_enum_value(note.status),
        route=f"/app/notes/{note.note_id}",
    )


def _session_node(session: Session) -> ProjectGraphNode:
    return ProjectGraphNode(
        id=_entity_node_id("session", session.session_id),
        entity_type="session",
        entity_id=str(session.session_id),
        label=_session_label(session),
        detail=session.link_code,
        status=_enum_value(session.status),
        route=f"/app/sessions/{session.session_id}",
    )


def _add_question_edges(builder: _ProjectGraphBuilder, questions: list[Question]) -> None:
    for question in _sort_entities(questions, "question_id", _question_label):
        question_id = _entity_node_id("question", question.question_id)
        for parent_id in sorted(question.parent_question_ids, key=str):
            builder.add_edge(
                _entity_node_id("question", parent_id),
                question_id,
                "parent",
                "question_parent",
            )
        if question.superseded_by_question_id is not None:
            builder.add_edge(
                question_id,
                _entity_node_id("question", question.superseded_by_question_id),
                "superseded by",
                "question_superseded_by",
            )
        if question.supersedes_question_id is not None:
            builder.add_edge(
                question_id,
                _entity_node_id("question", question.supersedes_question_id),
                "supersedes",
                "question_supersedes",
            )


def _add_evidence_edges(
    builder: _ProjectGraphBuilder,
    datasets: list[Dataset],
    analyses: list[Analysis],
    claims: list[Claim],
    claim_edges: list[ClaimEdge],
    exploration_nodes: list[ExplorationNode],
    visualizations: list[Visualization],
    goals: list[Goal],
) -> None:
    for dataset in _sort_entities(datasets, "dataset_id", _dataset_label):
        dataset_id = _entity_node_id("dataset", dataset.dataset_id)
        for link in _sorted_question_links(dataset):
            role = _enum_value(link.role) or "linked"
            builder.add_edge(
                _entity_node_id("question", link.question_id),
                dataset_id,
                f"{role} question",
                f"dataset_question_{role}",
            )
    for analysis in _sort_entities(analyses, "analysis_id", _analysis_label):
        analysis_id = _entity_node_id("analysis", analysis.analysis_id)
        for dataset_id in sorted(analysis.dataset_ids, key=str):
            builder.add_edge(
                _entity_node_id("dataset", dataset_id),
                analysis_id,
                "used by",
                "analysis_dataset",
            )
    for claim in _sort_entities(claims, "claim_id", _claim_label):
        claim_id = _entity_node_id("claim", claim.claim_id)
        for dataset_id in sorted(claim.supported_by_dataset_ids, key=str):
            builder.add_edge(
                _entity_node_id("dataset", dataset_id),
                claim_id,
                "supports",
                "claim_dataset_support",
            )
        for analysis_id in sorted(claim.supported_by_analysis_ids, key=str):
            builder.add_edge(
                _entity_node_id("analysis", analysis_id),
                claim_id,
                "supports",
                "claim_analysis_support",
            )
        for question_id in sorted(claim.answers_question_ids, key=str):
            builder.add_edge(
                claim_id,
                _entity_node_id("question", question_id),
                "answers",
                "claim_question_answers",
            )
        for citation in sorted(claim.external_citations, key=_external_artifact_sort_key):
            builder.add_edge(
                claim_id,
                _external_artifact_node_id(citation),
                "cites",
                "claim_cites",
            )
    for edge in sorted(
        claim_edges,
        key=lambda item: (str(item.claim_id), item.relation.value, str(item.target_claim_id)),
    ):
        relation = edge.relation.value
        builder.add_edge(
            _entity_node_id("claim", edge.claim_id),
            _entity_node_id("claim", edge.target_claim_id),
            relation.replace("_", " "),
            f"claim_relation_{relation}",
        )
    _add_exploration_edges(builder, exploration_nodes)
    for visualization in _sort_entities(visualizations, "viz_id", _visualization_label):
        visualization_id = _entity_node_id("visualization", visualization.viz_id)
        builder.add_edge(
            _entity_node_id("analysis", visualization.analysis_id),
            visualization_id,
            "generates",
            "visualization_analysis",
        )
        for dataset_id in sorted(visualization.dataset_ids, key=str):
            builder.add_edge(
                _entity_node_id("dataset", dataset_id),
                visualization_id,
                "grounds",
                "visualization_dataset",
            )
        for claim_id in sorted(visualization.related_claim_ids, key=str):
            builder.add_edge(
                _entity_node_id("claim", claim_id),
                visualization_id,
                "related claim",
                "visualization_claim",
            )
    for goal in _sort_entities(goals, "goal_id", _goal_label):
        goal_id = _entity_node_id("goal", goal.goal_id)
        for link in sorted(
            goal.links,
            key=lambda item: (
                _enum_value(item.target.entity_type) or "",
                str(item.target.entity_id),
                _enum_value(item.relation) or "",
                item.slot or "",
            ),
        ):
            relation = _enum_value(link.relation) or "linked"
            status = _enum_value(link.link_status) or "candidate"
            label = relation.replace("_", " ")
            if link.slot:
                label = f"{label}: {link.slot}"
            builder.add_edge(
                _entity_node_id(_entity_type_value(link.target.entity_type), link.target.entity_id),
                goal_id,
                label,
                f"goal_{relation}_{status}",
            )


def _add_full_edges(
    builder: _ProjectGraphBuilder,
    notes: list[Note],
    sessions: list[Session],
    datasets: list[Dataset],
) -> None:
    for note in _sort_entities(notes, "note_id", _note_label):
        note_id = _entity_node_id("note", note.note_id)
        targets = sorted(
            note.targets,
            key=lambda target: (_enum_value(target.entity_type) or "", str(target.entity_id)),
        )
        for target in targets:
            entity_type = _entity_type_value(target.entity_type)
            builder.add_edge(
                note_id,
                _entity_node_id(entity_type, target.entity_id),
                "targets",
                f"note_target_{entity_type}",
            )
    for session in _sort_entities(sessions, "session_id", _session_label):
        if session.primary_question_id is None:
            continue
        builder.add_edge(
            _entity_node_id("question", session.primary_question_id),
            _entity_node_id("session", session.session_id),
            "session question",
            "session_question",
        )
    for dataset in _sort_entities(datasets, "dataset_id", _dataset_label):
        source_session_id = dataset.commit_manifest.source_session_id
        if source_session_id is None:
            continue
        builder.add_edge(
            _entity_node_id("session", source_session_id),
            _entity_node_id("dataset", dataset.dataset_id),
            "source session",
            "dataset_source_session",
        )


def _add_exploration_edges(
    builder: _ProjectGraphBuilder,
    exploration_nodes: list[ExplorationNode],
) -> None:
    for node in _sort_entities(exploration_nodes, "node_id", _exploration_node_label):
        node_id = _entity_node_id("exploration_node", node.node_id)
        builder.add_edge(
            _entity_node_id(_entity_type_value(node.target.entity_type), node.target.entity_id),
            node_id,
            "concerns",
            "exploration_target",
        )
        for evidence_ref in sorted(
            node.evidence_refs,
            key=lambda ref: (_entity_type_value(ref.entity_type), str(ref.entity_id)),
        ):
            builder.add_edge(
                _entity_node_id(
                    _entity_type_value(evidence_ref.entity_type),
                    evidence_ref.entity_id,
                ),
                node_id,
                "evidence",
                "exploration_evidence",
            )
        for parent_id in sorted(node.parent_node_ids, key=str):
            builder.add_edge(
                _entity_node_id("exploration_node", parent_id),
                node_id,
                "parent",
                "exploration_parent",
            )
        for dependency_id in sorted(node.also_depends_on_node_ids, key=str):
            builder.add_edge(
                _entity_node_id("exploration_node", dependency_id),
                node_id,
                "depends on",
                "exploration_dependency",
            )
        if node.invalidates_node_id is not None:
            builder.add_edge(
                node_id,
                _entity_node_id("exploration_node", node.invalidates_node_id),
                "invalidates",
                "exploration_invalidates_node",
            )
        if node.invalidates_claim_id is not None:
            builder.add_edge(
                node_id,
                _entity_node_id("claim", node.invalidates_claim_id),
                "invalidates",
                "exploration_invalidates_claim",
            )


def _sorted_question_links(dataset: Dataset) -> list[QuestionLink]:
    links = list(dataset.question_links)
    if not links and dataset.primary_question_id is not None:
        return [
            QuestionLink(
                question_id=dataset.primary_question_id,
                role="primary",
            )
        ]
    return sorted(
        links,
        key=lambda link: (
            _QUESTION_LINK_ROLE_ORDER.get(_enum_value(link.role) or "", 99),
            str(link.question_id),
        ),
    )


def _entity_type_value(entity_type: EntityType | str) -> str:
    return _enum_value(entity_type) or str(entity_type)


def _escape_mermaid(value: str) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )
