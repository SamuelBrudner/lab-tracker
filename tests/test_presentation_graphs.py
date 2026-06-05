from __future__ import annotations

import ast
from pathlib import Path
from zipfile import ZipFile

from lab_tracker.presentation_graphs import (
    PRESENTATION_ONLY_RELATIONSHIPS,
    PROJECT_GRAPH_RELATIONSHIPS,
    PresentationGraphManifest,
    load_presentation_graph_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "docs" / "research-group-presentation-graphs.json"
PPTX_PATH = (
    REPO_ROOT
    / "outputs"
    / "019e93bf-5861-7ba2-af6a-d3677b8793cd"
    / "presentations"
    / "lab-tracker-research-group"
    / "output"
    / "lab-tracker-research-group-presentation.pptx"
)


def test_research_group_presentation_graph_manifest_validates() -> None:
    manifest = load_presentation_graph_manifest(MANIFEST_PATH)

    assert isinstance(manifest, PresentationGraphManifest)
    assert manifest.deck_id == "lab-tracker-research-group-presentation"
    assert {graph.slide_number for graph in manifest.graphs} == set(range(1, 20))
    assert {graph.diagram_id for graph in manifest.graphs} >= {
        "rg-problem-1-missing-question-edge",
        "rg-problem-2-missing-context",
        "rg-problem-3-manual-upkeep",
        "rg-resolution-assistant-context-packet",
        "rg-resolution-draft-operations",
        "rg-capture-3-proposed-operations",
    }


def test_presentation_graph_manifest_aligns_with_current_pptx_slide_count() -> None:
    manifest = load_presentation_graph_manifest(MANIFEST_PATH)

    with ZipFile(PPTX_PATH) as pptx:
        slide_count = len(
            [
                name
                for name in pptx.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ]
        )

    assert max(graph.slide_number for graph in manifest.graphs) == slide_count


def test_problem_and_capture_graphs_encode_backend_relevant_edges() -> None:
    manifest = load_presentation_graph_manifest(MANIFEST_PATH)
    graphs = {graph.diagram_id: graph for graph in manifest.graphs}

    problem_1_missing = {
        edge.relationship
        for edge in graphs["rg-problem-1-missing-question-edge"].edges
        if edge.state == "missing"
    }
    assert problem_1_missing == {
        "dataset_question_primary",
        "note_target_question",
        "session_question",
    }

    problem_3_operations = {
        operation.semantic_type.value
        for operation in graphs["rg-problem-3-manual-upkeep"].operations
    }
    assert problem_3_operations == {
        "link_note_to_question",
        "update_entity",
    }

    capture_operations = {
        operation.semantic_type.value
        for operation in graphs["rg-capture-3-proposed-operations"].operations
    }
    assert capture_operations == {
        "link_note_to_question",
        "suggest_new_dataset",
        "update_entity",
    }


def test_presentation_graph_relationship_vocabulary_matches_project_graph_builder() -> None:
    source = (REPO_ROOT / "src" / "lab_tracker" / "project_graph.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    static_relationships: set[str] = set()
    dynamic_prefixes: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_edge":
            continue
        if len(node.args) < 4:
            continue
        relationship = node.args[3]
        if isinstance(relationship, ast.Constant) and isinstance(relationship.value, str):
            static_relationships.add(relationship.value)
        elif isinstance(relationship, ast.JoinedStr):
            prefix = ""
            for part in relationship.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    prefix += part.value
                else:
                    break
            if prefix:
                dynamic_prefixes.add(prefix)

    assert "dataset_question_" in dynamic_prefixes
    assert "note_target_" in dynamic_prefixes
    for relationship in PROJECT_GRAPH_RELATIONSHIPS:
        assert relationship in static_relationships or any(
            relationship.startswith(prefix) for prefix in dynamic_prefixes
        )
    assert {"leads_to"} == PRESENTATION_ONLY_RELATIONSHIPS
