"""Machine-readable graph specs for presentation diagrams.

These models let slide diagrams be represented as backend-aligned graph specs
instead of only as PowerPoint shapes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lab_tracker.models import EntityType, GraphChangeOp, GraphDraftSemanticType

PROJECT_GRAPH_RELATIONSHIP_ENDPOINTS = {
    "analysis_dataset": ("dataset", "analysis"),
    "claim_analysis_support": ("analysis", "claim"),
    "claim_dataset_support": ("dataset", "claim"),
    "claim_question_answers": ("claim", "question"),
    "dataset_question_primary": ("question", "dataset"),
    "dataset_question_secondary": ("question", "dataset"),
    "dataset_source_session": ("session", "dataset"),
    "note_target_analysis": ("note", "analysis"),
    "note_target_dataset": ("note", "dataset"),
    "note_target_note": ("note", "note"),
    "note_target_question": ("note", "question"),
    "note_target_session": ("note", "session"),
    "note_target_visualization": ("note", "visualization"),
    "question_parent": ("question", "question"),
    "question_superseded_by": ("question", "question"),
    "question_supersedes": ("question", "question"),
    "session_question": ("question", "session"),
    "visualization_analysis": ("analysis", "visualization"),
    "visualization_claim": ("claim", "visualization"),
}
PROJECT_GRAPH_RELATIONSHIPS = frozenset(PROJECT_GRAPH_RELATIONSHIP_ENDPOINTS)
PRESENTATION_ONLY_RELATIONSHIPS = frozenset({"leads_to"})
EDGE_STATES = frozenset({"context_required", "implicit", "missing", "present", "proposed"})

PresentationGraphKind = Literal["argument", "context_packet", "graph_draft", "project_graph"]


class PresentationGraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    entity_type: EntityType | None = None
    role: str | None = None
    visible: bool = True


class PresentationGraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    relationship: str = Field(min_length=1)
    label: str = Field(min_length=1)
    state: str = "present"
    rationale: str | None = None


class PresentationGraphOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic_type: GraphDraftSemanticType
    op: GraphChangeOp
    entity_type: EntityType
    target_node: str | None = None
    payload_template: dict[str, Any] = Field(default_factory=dict)
    rationale: str | None = None


class PresentationGraphSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diagram_id: str = Field(min_length=1)
    slide_number: int = Field(ge=1)
    title: str = Field(min_length=1)
    graph_kind: PresentationGraphKind
    backend_view: str | None = None
    visible_labels: list[str] = Field(default_factory=list)
    nodes: list[PresentationGraphNode] = Field(default_factory=list)
    edges: list[PresentationGraphEdge] = Field(default_factory=list)
    operations: list[PresentationGraphOperation] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _validate_graph(self) -> PresentationGraphSpec:
        node_ids = [node.id for node in self.nodes]
        duplicate_nodes = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
        if duplicate_nodes:
            raise ValueError(f"{self.diagram_id} has duplicate node ids: {duplicate_nodes}")
        nodes_by_id = {node.id: node for node in self.nodes}
        known_nodes = set(nodes_by_id)
        for edge in self.edges:
            if edge.source not in known_nodes:
                raise ValueError(f"{self.diagram_id} edge {edge.id} has unknown source")
            if edge.target not in known_nodes:
                raise ValueError(f"{self.diagram_id} edge {edge.id} has unknown target")
            allowed_relationships = (
                PRESENTATION_ONLY_RELATIONSHIPS
                if self.graph_kind == "argument"
                else PROJECT_GRAPH_RELATIONSHIPS
            )
            if edge.relationship not in allowed_relationships:
                raise ValueError(
                    f"{self.diagram_id} edge {edge.id} uses unsupported relationship "
                    f"{edge.relationship!r}"
                )
            if edge.state not in EDGE_STATES:
                raise ValueError(f"{self.diagram_id} edge {edge.id} has invalid state")
            if edge.relationship in PROJECT_GRAPH_RELATIONSHIP_ENDPOINTS:
                source_type = nodes_by_id[edge.source].entity_type
                target_type = nodes_by_id[edge.target].entity_type
                if source_type is not None and target_type is not None:
                    expected_source, expected_target = PROJECT_GRAPH_RELATIONSHIP_ENDPOINTS[
                        edge.relationship
                    ]
                    if source_type.value != expected_source or target_type.value != expected_target:
                        raise ValueError(
                            f"{self.diagram_id} edge {edge.id} uses {edge.relationship} "
                            f"with {source_type.value}->{target_type.value}; expected "
                            f"{expected_source}->{expected_target}"
                        )
        for operation in self.operations:
            if operation.target_node is not None and operation.target_node not in known_nodes:
                raise ValueError(
                    f"{self.diagram_id} operation {operation.semantic_type.value} "
                    "has unknown target_node"
                )
        return self


class PresentationGraphManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    deck_id: str = Field(min_length=1)
    pptx_path: str = Field(min_length=1)
    backend_alignment: dict[str, Any]
    graphs: list[PresentationGraphSpec]

    @model_validator(mode="after")
    def _validate_manifest(self) -> PresentationGraphManifest:
        diagram_ids = [graph.diagram_id for graph in self.graphs]
        duplicates = sorted(
            {diagram_id for diagram_id in diagram_ids if diagram_ids.count(diagram_id) > 1}
        )
        if duplicates:
            raise ValueError(f"duplicate diagram ids: {duplicates}")
        return self


def load_presentation_graph_manifest(path: str | Path) -> PresentationGraphManifest:
    """Load and validate a presentation graph manifest."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return PresentationGraphManifest.model_validate(data)
