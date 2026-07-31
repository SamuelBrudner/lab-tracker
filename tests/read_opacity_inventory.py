"""Canonical inventory for the project-scoped read-opacity contract.

This module is deliberately pure data.  The integration suites own the behavior
behind each coverage ID, while ``test_read_opacity_inventory`` certifies that the
inventory still points at the corresponding OpenAPI operation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

CORE_SUITE = "core"
EVIDENCE_ARTIFACT_SUITE = "evidence_artifact"
WORKFLOW_REGISTRY_SUITE = "workflow_registry"
ACQUISITION_SUITE = "acquisition"


@dataclass(frozen=True)
class ReadOpacityVariant:
    """One behaviorally distinct read covered by an opacity integration suite."""

    coverage_id: str
    suite: str
    method: str
    route_template: str
    variant: str
    operation_id: str

    def matches_request(
        self,
        *,
        method: str,
        request_target: str,
        variant: str,
    ) -> bool:
        """Return whether a concrete behavioral request belongs to this record."""

        request_segments = urlsplit(request_target).path.strip("/").split("/")
        template_segments = self.route_template.strip("/").split("/")
        return (
            method == self.method
            and variant == self.variant
            and len(request_segments) == len(template_segments)
            and all(
                template_segment == request_segment
                or (
                    template_segment.startswith("{")
                    and template_segment.endswith("}")
                    and bool(request_segment)
                )
                for template_segment, request_segment in zip(
                    template_segments,
                    request_segments,
                    strict=True,
                )
            )
        )


def _variant(
    suite: str,
    coverage_name: str,
    *,
    method: str,
    route_template: str,
    operation_id: str,
    variant: str = "default",
) -> ReadOpacityVariant:
    return ReadOpacityVariant(
        coverage_id=f"{suite}.{coverage_name}",
        suite=suite,
        method=method,
        route_template=route_template,
        variant=variant,
        operation_id=operation_id,
    )


CORE_READ_OPACITY_VARIANTS = (
    _variant(
        CORE_SUITE,
        "project-detail",
        method="GET",
        route_template="/projects/{project_id}",
        operation_id="get_project_projects__project_id__get",
    ),
    _variant(
        CORE_SUITE,
        "publication-readiness",
        method="GET",
        route_template="/projects/{project_id}/publication-readiness",
        operation_id=("publication_readiness_projects__project_id__publication_readiness_get"),
    ),
    _variant(
        CORE_SUITE,
        "project-graph-json",
        method="GET",
        route_template="/projects/{project_id}/graph",
        operation_id="get_project_graph_projects__project_id__graph_get",
    ),
    _variant(
        CORE_SUITE,
        "project-graph-mermaid",
        method="GET",
        route_template="/projects/{project_id}/graph/mermaid",
        operation_id=("get_project_graph_mermaid_projects__project_id__graph_mermaid_get"),
    ),
    _variant(
        CORE_SUITE,
        "question-detail",
        method="GET",
        route_template="/questions/{question_id}",
        operation_id="get_question_questions__question_id__get",
    ),
    _variant(
        CORE_SUITE,
        "question-versions",
        method="GET",
        route_template="/questions/{question_id}/versions",
        operation_id="list_question_versions_questions__question_id__versions_get",
    ),
    _variant(
        CORE_SUITE,
        "question-version-diff",
        method="GET",
        route_template="/questions/{question_id}/versions/diff",
        operation_id=("diff_question_versions_questions__question_id__versions_diff_get"),
    ),
    _variant(
        CORE_SUITE,
        "question-refactors",
        method="GET",
        route_template="/questions/{question_id}/refactors",
        operation_id="list_question_refactors_questions__question_id__refactors_get",
    ),
    _variant(
        CORE_SUITE,
        "question-ara-artifact",
        method="GET",
        route_template="/questions/{question_id}/ara-artifact",
        operation_id=("export_question_subtree_questions__question_id__ara_artifact_get"),
    ),
    _variant(
        CORE_SUITE,
        "question-ara-layer",
        method="GET",
        route_template="/questions/{question_id}/ara-artifact/{layer_name}",
        operation_id=(
            "export_question_subtree_layer_questions__question_id__ara_artifact__layer_name__get"
        ),
    ),
    _variant(
        CORE_SUITE,
        "note-detail",
        method="GET",
        route_template="/notes/{note_id}",
        operation_id="get_note_notes__note_id__get",
    ),
    _variant(
        CORE_SUITE,
        "note-raw",
        method="GET",
        route_template="/notes/{note_id}/raw",
        operation_id="download_note_raw_notes__note_id__raw_get",
    ),
    _variant(
        CORE_SUITE,
        "session-detail",
        method="GET",
        route_template="/sessions/{session_id}",
        operation_id="get_session_sessions__session_id__get",
    ),
    _variant(
        CORE_SUITE,
        "session-by-link",
        method="GET",
        route_template="/sessions/by-link/{link_code}",
        operation_id="get_session_by_link_code_sessions_by_link__link_code__get",
    ),
    _variant(
        CORE_SUITE,
        "session-outputs",
        method="GET",
        route_template="/sessions/{session_id}/outputs",
        operation_id="list_session_outputs_sessions__session_id__outputs_get",
    ),
)


EVIDENCE_ARTIFACT_READ_OPACITY_VARIANTS = (
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "dataset-detail",
        method="GET",
        route_template="/datasets/{dataset_id}",
        operation_id="get_dataset_datasets__dataset_id__get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "dataset-files",
        method="GET",
        route_template="/datasets/{dataset_id}/files",
        operation_id="list_dataset_files_datasets__dataset_id__files_get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "dataset-file-download",
        method="GET",
        route_template="/datasets/{dataset_id}/files/{file_id}/download",
        operation_id=("download_dataset_file_datasets__dataset_id__files__file_id__download_get"),
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "dataset-provenance",
        method="GET",
        route_template="/datasets/{dataset_id}/provenance",
        operation_id="get_dataset_provenance_datasets__dataset_id__provenance_get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "analysis-detail",
        method="GET",
        route_template="/analyses/{analysis_id}",
        operation_id="get_analysis_analyses__analysis_id__get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "analysis-provenance",
        method="GET",
        route_template="/analyses/{analysis_id}/provenance",
        operation_id="get_analysis_provenance_analyses__analysis_id__provenance_get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "claim-detail",
        method="GET",
        route_template="/claims/{claim_id}",
        operation_id="get_claim_claims__claim_id__get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "claim-versions",
        method="GET",
        route_template="/claims/{claim_id}/versions",
        operation_id="list_claim_versions_claims__claim_id__versions_get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "claim-version-diff",
        method="GET",
        route_template="/claims/{claim_id}/versions/diff",
        operation_id="diff_claim_versions_claims__claim_id__versions_diff_get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "claim-edges",
        method="GET",
        route_template="/claims/{claim_id}/edges",
        operation_id="list_claim_edges_claims__claim_id__edges_get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "claim-provenance",
        method="GET",
        route_template="/claims/{claim_id}/provenance",
        operation_id="get_claim_provenance_claims__claim_id__provenance_get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "visualization-detail",
        method="GET",
        route_template="/visualizations/{viz_id}",
        operation_id="get_visualization_visualizations__viz_id__get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "visualization-file-download",
        method="GET",
        route_template="/visualizations/{viz_id}/file/download",
        operation_id=("download_visualization_file_visualizations__viz_id__file_download_get"),
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "exploration-node-detail",
        method="GET",
        route_template="/exploration-nodes/{node_id}",
        operation_id="get_exploration_node_exploration_nodes__node_id__get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "provenance-link-detail",
        method="GET",
        route_template="/provenance-links/{link_id}",
        operation_id="get_provenance_link_provenance_links__link_id__get",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "resolver-dataset",
        method="POST",
        route_template="/external-artifacts/resolve",
        operation_id=("resolve_external_artifact_external_artifacts_resolve_post"),
        variant="entity_type=dataset",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "resolver-analysis",
        method="POST",
        route_template="/external-artifacts/resolve",
        operation_id=("resolve_external_artifact_external_artifacts_resolve_post"),
        variant="entity_type=analysis",
    ),
    _variant(
        EVIDENCE_ARTIFACT_SUITE,
        "resolver-claim",
        method="POST",
        route_template="/external-artifacts/resolve",
        operation_id=("resolve_external_artifact_external_artifacts_resolve_post"),
        variant="entity_type=claim",
    ),
)


WORKFLOW_REGISTRY_READ_OPACITY_VARIANTS = (
    _variant(
        WORKFLOW_REGISTRY_SUITE,
        "graph-draft-detail",
        method="GET",
        route_template="/graph-drafts/{change_set_id}",
        operation_id="get_graph_draft_graph_drafts__change_set_id__get",
    ),
    _variant(
        WORKFLOW_REGISTRY_SUITE,
        "batch-detail",
        method="GET",
        route_template="/batches/{change_set_id}",
        operation_id="get_batch_batches__change_set_id__get",
    ),
    _variant(
        WORKFLOW_REGISTRY_SUITE,
        "data-store-detail",
        method="GET",
        route_template="/data-stores/{store_id}",
        operation_id="get_data_store_data_stores__store_id__get",
    ),
    _variant(
        WORKFLOW_REGISTRY_SUITE,
        "data-store-health",
        method="GET",
        route_template="/data-stores/{store_id}/health",
        operation_id="data_store_health_data_stores__store_id__health_get",
    ),
)

ACQUISITION_READ_OPACITY_VARIANTS = (
    _variant(
        ACQUISITION_SUITE,
        "experiment-detail",
        method="GET",
        route_template="/experiments/{experiment_id}",
        operation_id="get_experiment_experiments__experiment_id__get",
    ),
    _variant(
        ACQUISITION_SUITE,
        "experiment-sessions",
        method="GET",
        route_template="/experiments/{experiment_id}/sessions",
        operation_id=(
            "list_experiment_sessions_experiments__experiment_id__sessions_get"
        ),
    ),
    _variant(
        ACQUISITION_SUITE,
        "experiment-datasets",
        method="GET",
        route_template="/experiments/{experiment_id}/datasets",
        operation_id=(
            "list_experiment_datasets_experiments__experiment_id__datasets_get"
        ),
    ),
    _variant(
        ACQUISITION_SUITE,
        "session-experiments",
        method="GET",
        route_template="/sessions/{session_id}/experiments",
        operation_id="list_session_experiments_sessions__session_id__experiments_get",
    ),
    _variant(
        ACQUISITION_SUITE,
        "dataset-experiments",
        method="GET",
        route_template="/datasets/{dataset_id}/experiments",
        operation_id="list_dataset_experiments_datasets__dataset_id__experiments_get",
    ),
    _variant(
        ACQUISITION_SUITE,
        "session-collections",
        method="GET",
        route_template="/sessions/{session_id}/collections",
        operation_id="list_session_collections_sessions__session_id__collections_get",
    ),
    _variant(
        ACQUISITION_SUITE,
        "collection-snapshots",
        method="GET",
        route_template="/collections/{collection_id}/snapshots",
        operation_id=(
            "list_collection_snapshots_collections__collection_id__snapshots_get"
        ),
    ),
    _variant(
        ACQUISITION_SUITE,
        "collection-snapshot-detail",
        method="GET",
        route_template="/collection-snapshots/{snapshot_id}",
        operation_id=(
            "get_collection_snapshot_collection_snapshots__snapshot_id__get"
        ),
    ),
    _variant(
        ACQUISITION_SUITE,
        "collection-snapshot-members",
        method="GET",
        route_template="/collection-snapshots/{snapshot_id}/members",
        operation_id=(
            "list_collection_members_collection_snapshots__snapshot_id__members_get"
        ),
    ),
    _variant(
        ACQUISITION_SUITE,
        "collection-snapshot-manifest",
        method="GET",
        route_template="/collection-snapshots/{snapshot_id}/manifest",
        operation_id=(
            "download_collection_manifest_collection_snapshots__snapshot_id__manifest_get"
        ),
    ),
)


READ_OPACITY_VARIANTS_BY_SUITE: Mapping[str, tuple[ReadOpacityVariant, ...]] = MappingProxyType(
    {
        CORE_SUITE: CORE_READ_OPACITY_VARIANTS,
        EVIDENCE_ARTIFACT_SUITE: EVIDENCE_ARTIFACT_READ_OPACITY_VARIANTS,
        WORKFLOW_REGISTRY_SUITE: WORKFLOW_REGISTRY_READ_OPACITY_VARIANTS,
        ACQUISITION_SUITE: ACQUISITION_READ_OPACITY_VARIANTS,
    }
)

READ_OPACITY_VARIANTS = tuple(
    variant
    for suite_variants in READ_OPACITY_VARIANTS_BY_SUITE.values()
    for variant in suite_variants
)

READ_OPACITY_VARIANTS_BY_ID: Mapping[str, ReadOpacityVariant] = MappingProxyType(
    {variant.coverage_id: variant for variant in READ_OPACITY_VARIANTS}
)
