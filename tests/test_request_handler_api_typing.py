"""Guards for the typed facade consumed by request-scoped handlers."""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any

from lab_tracker.api import LabTrackerAPI
from lab_tracker.application.handlers import RequestHandlerApi
from lab_tracker.services import (
    AnalysisService,
    ClaimService,
    DatasetService,
    GoalService,
    ProjectAuthorizationPolicy,
    ProjectService,
    QuestionService,
    SessionService,
    VisualizationService,
)

MIGRATED_SERVICE_METHODS = {
    "accessible_project_ids": (ProjectAuthorizationPolicy, "accessible_project_ids"),
    "require_project_read": (ProjectAuthorizationPolicy, "require_read"),
    "require_project_contributor": (
        ProjectAuthorizationPolicy,
        "require_contributor",
    ),
    "require_project_owner": (ProjectAuthorizationPolicy, "require_owner"),
    "get_project": (ProjectService, "get_project"),
    "get_project_for_read": (ProjectService, "get_project_for_read"),
    "delete_project": (ProjectService, "delete_project"),
    "get_question": (QuestionService, "get_question"),
    "get_dataset": (DatasetService, "get_dataset"),
    "delete_dataset": (DatasetService, "delete_dataset"),
    "get_session_for_read": (SessionService, "get_session_for_read"),
    "get_analysis": (AnalysisService, "get_analysis"),
    "delete_analysis": (AnalysisService, "delete_analysis"),
    "get_claim": (ClaimService, "get_claim"),
    "get_visualization": (VisualizationService, "get_visualization"),
    "delete_visualization": (VisualizationService, "delete_visualization"),
    "get_goal": (GoalService, "get_goal"),
    "require_goal_read": (GoalService, "require_goal_read"),
}
REQUEST_HANDLER_METHODS = {
    name
    for name, method in inspect.getmembers(RequestHandlerApi, predicate=inspect.isfunction)
    if not name.startswith("_")
}


def _function_node(method: object) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    return node


def _has_direct_any(annotation: object) -> bool:
    return annotation is Any or annotation == "Any"


def _surface_violations(owner: type[object], method_names: set[str]) -> list[str]:
    violations: list[str] = []
    for method_name in sorted(method_names):
        method = getattr(owner, method_name)
        signature = inspect.signature(method)
        for parameter in signature.parameters.values():
            if parameter.kind in {
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }:
                violations.append(f"{method_name}:variadic")
            if _has_direct_any(parameter.annotation):
                violations.append(f"{method_name}:{parameter.name}:Any")
        if _has_direct_any(signature.return_annotation):
            violations.append(f"{method_name}:return:Any")
    return violations


def test_every_request_handler_api_method_has_a_non_variadic_typed_facade() -> None:
    assert _surface_violations(LabTrackerAPI, REQUEST_HANDLER_METHODS) == []

    for method_name in REQUEST_HANDLER_METHODS:
        node = _function_node(getattr(LabTrackerAPI, method_name))
        assert node.args.vararg is None
        assert node.args.kwarg is None


def test_migrated_handler_delegates_exactly_match_their_concrete_services() -> None:
    assert set(MIGRATED_SERVICE_METHODS) <= REQUEST_HANDLER_METHODS
    for facade_name, (service_type, service_name) in MIGRATED_SERVICE_METHODS.items():
        facade_signature = inspect.signature(getattr(LabTrackerAPI, facade_name))
        service_signature = inspect.signature(getattr(service_type, service_name))
        assert facade_signature == service_signature, facade_name


def test_surface_guard_rejects_the_previous_variadic_any_shape() -> None:
    class LegacyVariadicFacade:
        def get_project(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("compile-only negative control")

    assert _surface_violations(LegacyVariadicFacade, {"get_project"}) == [
        "get_project:variadic",
        "get_project:args:Any",
        "get_project:variadic",
        "get_project:kwargs:Any",
        "get_project:return:Any",
    ]
