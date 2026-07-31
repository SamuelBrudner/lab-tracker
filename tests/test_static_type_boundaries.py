"""Architecture guards for consumer-owned structural ports."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSUMER_MODULES = (
    "src/lab_tracker/application/catalog_queries.py",
    "src/lab_tracker/application/context_queries.py",
    "src/lab_tracker/application/file_commands.py",
    "src/lab_tracker/application/managed_deletions.py",
    "src/lab_tracker/decision_context_query.py",
    "src/lab_tracker/project_graph.py",
)
PORT_MODULES = (
    *CONSUMER_MODULES,
    "src/lab_tracker/services/graph_draft_generation.py",
    "src/lab_tracker/services/graph_draft_review.py",
    "src/lab_tracker/services/graph_draft_commit.py",
    "src/lab_tracker/services/graph_draft_batch_reservation.py",
    "src/lab_tracker/services/graph_draft_scheduling_ports.py",
)
GRAPH_COORDINATORS = (
    "src/lab_tracker/services/graph_draft_generation.py",
    "src/lab_tracker/services/graph_draft_review.py",
    "src/lab_tracker/services/graph_draft_commit.py",
    "src/lab_tracker/services/graph_draft_batch_reservation.py",
    "src/lab_tracker/services/graph_draft_scheduling.py",
)
FORBIDDEN_BOUNDARY_NAMES = {
    "LabTrackerAPI",
    "LabTrackerRepository",
    "SQLAlchemyLabTrackerRepository",
}


def _tree(relative_path: str) -> ast.Module:
    path = ROOT / relative_path
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def test_application_consumers_do_not_import_broad_or_concrete_boundaries() -> None:
    violations: list[str] = []
    for relative_path in PORT_MODULES:
        for node in ast.walk(_tree(relative_path)):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for alias in node.names:
                if alias.name in FORBIDDEN_BOUNDARY_NAMES:
                    violations.append(f"{relative_path}:{node.lineno}:{alias.name}")
    assert violations == []


def test_local_protocols_do_not_expose_a_repository_escape_hatch() -> None:
    violations: list[str] = []
    for relative_path in PORT_MODULES:
        tree = _tree(relative_path)
        for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            if not any(_base_name(base) == "Protocol" for base in class_node.bases):
                continue
            for member in class_node.body:
                name = getattr(member, "name", None)
                if name == "repository":
                    violations.append(
                        f"{relative_path}:{member.lineno}:{class_node.name}.repository"
                    )
    assert violations == []


def test_graph_coordinators_do_not_recover_the_broad_base_repository() -> None:
    violations: list[str] = []
    for relative_path in GRAPH_COORDINATORS:
        for node in ast.walk(_tree(relative_path)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr == "repository"
            ):
                violations.append(f"{relative_path}:{node.lineno}")
    assert violations == []


def test_sqlalchemy_repository_uses_structural_not_nominal_conformance() -> None:
    tree = _tree("src/lab_tracker/sqlalchemy_repository_parts/repository.py")
    repository_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "SQLAlchemyLabTrackerRepository"
    )
    assert all(_base_name(base) != "LabTrackerRepository" for base in repository_class.bases)
