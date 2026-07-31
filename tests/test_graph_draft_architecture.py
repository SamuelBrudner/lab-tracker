from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any

from api_helpers import repository_backed_api

from lab_tracker.api import LabTrackerAPI
from lab_tracker.api_parts.graph_drafts import GraphDraftsApiMixin
from lab_tracker.patching import NOT_PROVIDED
from lab_tracker.services import (
    BatchSchedulingCoordinator,
    GraphDraftGenerationCoordinator,
    GraphDraftRecords,
    GraphDraftReviewCoordinator,
    GraphDraftService,
    GraphPatchApplier,
    TransactionalDraftCommitCoordinator,
)

OWNER_TYPES = {
    "records": GraphDraftRecords,
    "generation": GraphDraftGenerationCoordinator,
    "review": GraphDraftReviewCoordinator,
    "commit": TransactionalDraftCommitCoordinator,
    "scheduling": BatchSchedulingCoordinator,
}
OWNER_MODULES = (
    "graph_draft_records.py",
    "graph_draft_generation.py",
    "graph_draft_review.py",
    "graph_draft_commit.py",
    "graph_draft_batch_reservation.py",
    "graph_draft_scheduling.py",
)
ARCHITECTURE_MODULES = (*OWNER_MODULES, "graph_draft_service.py")
INTENDED_OWNER_EDGES = {
    "records": set(),
    "generation": {"records"},
    "review": {"records", "generation"},
    "commit": {"records"},
    "scheduling": {"records", "generation"},
}
DELEGATE_OWNERS = {
    "create_graph_draft_from_note": "generation",
    "create_analysis_graph_draft_from_note": "generation",
    "create_batch_graph_draft": "generation",
    "get_graph_change_set": "records",
    "get_graph_change_set_for_read": "records",
    "list_graph_change_sets": "records",
    "query_graph_change_sets": "records",
    "list_batch_graph_drafts": "records",
    "update_graph_change_operation": "review",
    "bulk_accept_graph_change_operations": "review",
    "submit_graph_change_set": "review",
    "review_graph_change_set": "review",
    "revise_graph_change_set": "review",
    "commit_graph_change_set": "commit",
    "build_graph_context_for_note": "generation",
    "build_batch_graph_context": "generation",
    "get_graph_draft_batch_settings": "scheduling",
    "update_graph_draft_batch_settings": "scheduling",
    "run_graph_draft_batch_for_project": "scheduling",
    "enqueue_graph_draft_batch_for_project": "scheduling",
    "process_next_graph_draft_batch_run": "scheduling",
    "execute_graph_draft_batch_run": "scheduling",
    "get_graph_draft_batch_run": "scheduling",
    "run_due_graph_draft_batches": "scheduling",
    "enqueue_due_graph_draft_batches": "scheduling",
    "list_graph_draft_batch_runs": "scheduling",
}


def _source_tree(function) -> ast.FunctionDef:  # noqa: ANN001
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _direct_delegate(function) -> tuple[str, str]:  # noqa: ANN001
    node = _source_tree(function)
    assert not any(
        parameter.kind
        in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        for parameter in inspect.signature(function).parameters.values()
    )
    assert len(node.body) == 1
    statement = node.body[0]
    assert isinstance(statement, ast.Return)
    assert isinstance(statement.value, ast.Call)
    target = statement.value.func
    assert isinstance(target, ast.Attribute)
    receiver = target.value
    assert isinstance(receiver, ast.Attribute)
    assert isinstance(receiver.value, ast.Name)
    assert receiver.value.id == "self"
    return receiver.attr, target.attr


def _composed_owners(facade: GraphDraftService) -> dict[str, object]:
    return {name: getattr(facade, name) for name in OWNER_TYPES}


def _owner_adjacency(owners: dict[str, object]) -> dict[str, set[str]]:
    names_by_identity = {id(owner): name for name, owner in owners.items()}
    return {
        name: {
            names_by_identity[id(value)]
            for value in vars(owner).values()
            if id(value) in names_by_identity
        }
        for name, owner in owners.items()
    }


def _has_cycle(adjacency: dict[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(neighbor) for neighbor in adjacency[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in adjacency)


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        return f"{owner}.{node.attr}" if owner is not None else None
    return None


def _symbol_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is not None:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    # Follow simple local aliases such as ``Context = ServiceContext`` and
    # ``apply = applier.apply_graph_operation`` without trying to execute code.
    changed = True
    while changed:
        changed = False
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target = node.target
                value = node.value
            else:
                continue
            if not isinstance(target, ast.Name):
                continue
            resolved = _resolve_name(value, aliases)
            if resolved is not None and aliases.get(target.id) != resolved:
                aliases[target.id] = resolved
                changed = True
    return aliases


def _resolve_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    qualified = _qualified_name(node)
    if qualified is None:
        return None
    head, separator, tail = qualified.partition(".")
    resolved_head = aliases.get(head, head)
    return f"{resolved_head}.{tail}" if separator else resolved_head


def _module_tree(services_dir: Path, module_name: str) -> ast.Module:
    return ast.parse((services_dir / module_name).read_text())


def _canonical_symbol_names(symbol: str) -> set[str]:
    return {
        f"lab_tracker.services.{symbol}",
        f"lab_tracker.services.{_symbol_module(symbol)}.{symbol}",
    }


def _matches_symbol(resolved: str, symbol: str) -> bool:
    return resolved in _canonical_symbol_names(symbol) or resolved == symbol


def _references_symbol(tree: ast.Module, symbol: str) -> bool:
    aliases = _symbol_aliases(tree)
    return any(
        _matches_symbol(resolved, symbol)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
        and (resolved := _resolve_name(node, aliases)) is not None
    )


def _symbol_module(symbol: str) -> str:
    return {
        "GraphPatchApplier": "graph_draft_applier",
        "ServiceContext": "base",
    }[symbol]


def _callable_alias_names(
    tree: ast.Module,
    aliases: dict[str, str],
    matches_target,
) -> set[str]:  # noqa: ANN001
    callable_aliases: set[str] = set()
    assignments = [
        (node.targets[0], node.value)
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        else (node.target, node.value)
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        )
        or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        )
    ]
    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            assert isinstance(target, ast.Name)
            resolved = _resolve_name(value, aliases)
            aliases_target = (
                resolved is not None and matches_target(resolved)
            ) or (isinstance(value, ast.Name) and value.id in callable_aliases)
            if aliases_target and target.id not in callable_aliases:
                callable_aliases.add(target.id)
                changed = True
    return callable_aliases


def _calls_symbol(tree: ast.Module, symbol: str) -> bool:
    aliases = _symbol_aliases(tree)
    callable_aliases = _callable_alias_names(
        tree,
        aliases,
        lambda resolved: _matches_symbol(resolved, symbol),
    )
    return any(
        _matches_symbol(resolved, symbol)
        or (isinstance(node.func, ast.Name) and node.func.id in callable_aliases)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (resolved := _resolve_name(node.func, aliases)) is not None
    )


def _calls_method(tree: ast.Module, method_name: str) -> bool:
    aliases = _symbol_aliases(tree)
    callable_aliases = _callable_alias_names(
        tree,
        aliases,
        lambda resolved: resolved.rsplit(".", 1)[-1] == method_name,
    )
    return any(
        resolved.rsplit(".", 1)[-1] == method_name
        or (isinstance(node.func, ast.Name) and node.func.id in callable_aliases)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (resolved := _resolve_name(node.func, aliases)) is not None
    )


def test_graph_draft_lifecycle_owners_share_the_api_service_context() -> None:
    api = repository_backed_api()
    facade = api.graph_drafts
    owners = _composed_owners(facade)
    core_services = (
        api.project_authorization,
        api.projects,
        api.questions,
        api.notes,
        api.entity_versions,
    )

    assert isinstance(facade, GraphDraftService)
    assert {
        name: type(owner) for name, owner in owners.items()
    } == OWNER_TYPES
    assert all(
        owner._context is api._service_context  # noqa: SLF001
        for owner in owners.values()
    )
    assert all(
        service._context is api._service_context  # noqa: SLF001
        for service in core_services
    )

    for owner in owners.values():
        assert all(
            not isinstance(value, (GraphDraftService, LabTrackerAPI))
            for value in vars(owner).values()
        )


def test_owner_dependency_graph_has_only_intended_edges_and_no_cycles() -> None:
    owners = _composed_owners(repository_backed_api().graph_drafts)
    adjacency = _owner_adjacency(owners)

    assert adjacency == INTENDED_OWNER_EDGES
    assert not _has_cycle(adjacency)


def test_only_commit_coordinator_references_or_owns_the_patch_applier() -> None:
    facade = repository_backed_api().graph_drafts
    owners = _composed_owners(facade)
    services_dir = Path(inspect.getfile(GraphDraftService)).parent

    applier_holders = {
        name
        for name, owner in owners.items()
        if any(isinstance(value, GraphPatchApplier) for value in vars(owner).values())
    }
    assert applier_holders == {"commit"}

    for module_name in ARCHITECTURE_MODULES:
        tree = _module_tree(services_dir, module_name)
        may_apply = module_name == "graph_draft_commit.py"
        if not may_apply:
            assert not _references_symbol(tree, "GraphPatchApplier")
        assert _calls_method(tree, "apply_graph_operation") is may_apply


def test_review_uses_the_non_persisting_generation_revision_seam() -> None:
    node = _source_tree(GraphDraftReviewCoordinator.revise_graph_change_set)
    calls = {
        (
            call.func.value.value.id,
            call.func.value.attr,
            call.func.attr,
        )
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Attribute)
        and isinstance(call.func.value.value, ast.Name)
    }

    assert ("self", "generation", "propose_note_revision") in calls


def test_compatibility_facade_is_an_explicit_one_hop_delegate() -> None:
    mixin_targets = {
        node.func.attr
        for method_name, method in inspect.getmembers(
            GraphDraftsApiMixin,
            predicate=inspect.isfunction,
        )
        for node in ast.walk(_source_tree(method))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "self"
        and node.func.value.attr == "graph_drafts"
        and method_name != "__init__"
    }

    assert mixin_targets == set(DELEGATE_OWNERS)
    for method_name, owner_name in DELEGATE_OWNERS.items():
        owner, target = _direct_delegate(getattr(GraphDraftService, method_name))
        assert (owner, target) == (owner_name, method_name)


def test_compatibility_facade_signatures_exactly_match_the_service() -> None:
    for method_name in DELEGATE_OWNERS:
        service_signature = inspect.signature(getattr(GraphDraftService, method_name))
        facade_signature = inspect.signature(getattr(GraphDraftsApiMixin, method_name))

        assert tuple(service_signature.parameters) == tuple(facade_signature.parameters)
        for name, service_parameter in service_signature.parameters.items():
            facade_parameter = facade_signature.parameters[name]
            assert facade_parameter.kind is service_parameter.kind
            assert facade_parameter.annotation == service_parameter.annotation
            if service_parameter.default is inspect.Parameter.empty:
                assert facade_parameter.default is inspect.Parameter.empty
            elif service_parameter.default is NOT_PROVIDED:
                assert facade_parameter.default is NOT_PROVIDED
            else:
                assert facade_parameter.default == service_parameter.default
        assert facade_signature.return_annotation == service_signature.return_annotation


def test_graph_draft_api_has_no_variadic_any_delegates() -> None:
    for method_name in DELEGATE_OWNERS:
        method = getattr(GraphDraftsApiMixin, method_name)
        signature = inspect.signature(method)
        assert all(
            parameter.kind
            not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
            for parameter in signature.parameters.values()
        )
        tree = _source_tree(method)
        assert tree.args.vararg is None
        assert tree.args.kwarg is None
        assert signature.return_annotation not in {Any, "Any"}


def test_coordinator_modules_do_not_import_the_facade_or_api() -> None:
    services_dir = Path(inspect.getfile(GraphDraftService)).parent

    for module_name in OWNER_MODULES:
        tree = ast.parse((services_dir / module_name).read_text())
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert "lab_tracker.api" not in imported_modules
        assert "lab_tracker.services.graph_draft_service" not in imported_modules
        assert "GraphDraftService" not in imported_names
        assert "LabTrackerAPI" not in imported_names


def test_lifecycle_modules_never_construct_their_own_service_context() -> None:
    services_dir = Path(inspect.getfile(GraphDraftService)).parent

    for module_name in ARCHITECTURE_MODULES:
        tree = _module_tree(services_dir, module_name)
        assert not _calls_symbol(tree, "ServiceContext"), (
            f"{module_name} constructs ServiceContext instead of using the "
            "composition-root instance"
        )


def test_ast_guards_follow_import_and_local_callable_aliases() -> None:
    tree = ast.parse(
        """
from lab_tracker.services.base import ServiceContext as ImportedContext
from lab_tracker.services.graph_draft_applier import GraphPatchApplier as ImportedApplier

ContextFactory = ImportedContext
SecondContextFactory = ContextFactory
ApplierType = ImportedApplier

def exercise(owner):
    apply = owner.apply_graph_operation
    apply()
    SecondContextFactory()
"""
    )

    assert _calls_symbol(tree, "ServiceContext")
    assert _references_symbol(tree, "GraphPatchApplier")
    assert _calls_method(tree, "apply_graph_operation")


def test_graph_draft_owner_sizes_and_constructor_breadth_stay_bounded() -> None:
    assert len(Path(inspect.getfile(GraphDraftService)).read_text().splitlines()) <= 450
    for owner_type in OWNER_TYPES.values():
        path = Path(inspect.getfile(owner_type))
        assert len(path.read_text().splitlines()) <= 900
        parameters = inspect.signature(owner_type).parameters.values()
        assert len(tuple(parameters)) <= 8
