from __future__ import annotations

import re
from collections import Counter, defaultdict

from fastapi import FastAPI
from read_opacity_inventory import (
    ACQUISITION_SUITE,
    CORE_SUITE,
    EVIDENCE_ARTIFACT_SUITE,
    READ_OPACITY_VARIANTS,
    READ_OPACITY_VARIANTS_BY_SUITE,
    WORKFLOW_REGISTRY_SUITE,
)


def test_read_opacity_inventory_has_the_exact_suite_partition() -> None:
    assert len(READ_OPACITY_VARIANTS) == 50
    assert set(READ_OPACITY_VARIANTS_BY_SUITE) == {
        ACQUISITION_SUITE,
        CORE_SUITE,
        EVIDENCE_ARTIFACT_SUITE,
        WORKFLOW_REGISTRY_SUITE,
    }
    assert Counter(variant.suite for variant in READ_OPACITY_VARIANTS) == {
        ACQUISITION_SUITE: 10,
        CORE_SUITE: 18,
        EVIDENCE_ARTIFACT_SUITE: 18,
        WORKFLOW_REGISTRY_SUITE: 4,
    }
    assert {suite: len(variants) for suite, variants in READ_OPACITY_VARIANTS_BY_SUITE.items()} == {
        ACQUISITION_SUITE: 10,
        CORE_SUITE: 18,
        EVIDENCE_ARTIFACT_SUITE: 18,
        WORKFLOW_REGISTRY_SUITE: 4,
    }


def test_read_opacity_inventory_ids_and_semantic_variants_are_unique() -> None:
    coverage_ids = [variant.coverage_id for variant in READ_OPACITY_VARIANTS]
    semantic_variants = [
        (variant.method, variant.route_template, variant.variant)
        for variant in READ_OPACITY_VARIANTS
    ]

    assert len(coverage_ids) == len(set(coverage_ids)) == 50
    assert len(semantic_variants) == len(set(semantic_variants)) == 50

    for variant in READ_OPACITY_VARIANTS:
        coverage_prefix = f"{variant.suite}."
        assert variant.coverage_id.startswith(coverage_prefix)
        coverage_name = variant.coverage_id.removeprefix(coverage_prefix)
        assert variant.method in {"GET", "POST"}
        assert variant.route_template.startswith("/")
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", coverage_name)
        assert variant.variant
        assert variant.operation_id


def test_read_opacity_inventory_matches_openapi_operations(app: FastAPI) -> None:
    openapi_paths = app.openapi()["paths"]
    inventory_by_operation: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    variants_by_operation: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for variant in READ_OPACITY_VARIANTS:
        path_item = openapi_paths[variant.route_template]
        operation = path_item[variant.method.lower()]
        assert operation["operationId"] == variant.operation_id, variant.coverage_id
        inventory_by_operation[(variant.method, variant.route_template, variant.operation_id)].add(
            variant.coverage_id
        )
        variants_by_operation[(variant.method, variant.route_template, variant.operation_id)].add(
            variant.variant
        )

    assert len(inventory_by_operation) == 48
    assert len({variant.operation_id for variant in READ_OPACITY_VARIANTS}) == 48
    assert {
        operation: variants
        for operation, variants in inventory_by_operation.items()
        if len(variants) > 1
    } == {
        (
            "POST",
            "/external-artifacts/resolve",
            "resolve_external_artifact_external_artifacts_resolve_post",
        ): {
            "evidence_artifact.resolver-dataset",
            "evidence_artifact.resolver-analysis",
            "evidence_artifact.resolver-claim",
        }
    }
    assert {
        operation: variants
        for operation, variants in variants_by_operation.items()
        if len(variants) > 1
    } == {
        (
            "POST",
            "/external-artifacts/resolve",
            "resolve_external_artifact_external_artifacts_resolve_post",
        ): {
            "entity_type=dataset",
            "entity_type=analysis",
            "entity_type=claim",
        }
    }
