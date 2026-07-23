"""Architecture guards for the HTTP-to-application seam."""

from __future__ import annotations

import ast
from pathlib import Path

ROUTES_ROOT = Path("src/lab_tracker/routes")
FORBIDDEN_IMPORT_PREFIXES = (
    "sqlalchemy",
    "lab_tracker.db_models",
    "lab_tracker.repository",
    "lab_tracker.sqlalchemy_repository",
)
FORBIDDEN_REQUEST_STATE_ATTRIBUTES = {
    "db_session",
    "lab_tracker_repository",
}
MIGRATED_HANDLER_ROUTES = {
    "analyses.py",
    "assistant.py",
    "claims.py",
    "dataset_files.py",
    "datasets.py",
    "exploration.py",
    "external_artifacts.py",
    "notes.py",
    "portfolio.py",
    "project_graph.py",
    "projects.py",
    "provenance.py",
    "provenance_links.py",
    "questions.py",
    "search.py",
    "sessions.py",
    "visualizations.py",
}


def _route_trees() -> dict[Path, ast.Module]:
    return {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(ROUTES_ROOT.glob("*.py"))
    }


def test_routes_do_not_import_persistence_implementations() -> None:
    violations: list[str] = []
    for path, tree in _route_trees().items():
        for node in ast.walk(tree):
            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules = [node.module]
            for module in imported_modules:
                if module.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{path}:{node.lineno}: {module}")
    assert violations == []


def test_routes_cannot_recover_raw_request_persistence_dependencies() -> None:
    violations: list[str] = []
    forbidden_helpers = {"db_session_from_request", "repository_from_request"}
    for path, tree in _route_trees().items():
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in forbidden_helpers
            ):
                violations.append(f"{path}:{node.lineno}: {node.name}")
            if (
                isinstance(node, ast.Attribute)
                and node.attr in FORBIDDEN_REQUEST_STATE_ATTRIBUTES
            ):
                violations.append(f"{path}:{node.lineno}: {node.attr}")
    assert violations == []


def test_persistence_leaking_routes_now_enter_through_typed_handlers() -> None:
    missing_boundary = []
    trees = _route_trees()
    for path, tree in trees.items():
        if path.name not in MIGRATED_HANDLER_ROUTES:
            continue
        if not any(
            isinstance(node, ast.Name) and node.id == "handlers_from_request"
            for node in ast.walk(tree)
        ):
            missing_boundary.append(path.name)
    assert missing_boundary == []
