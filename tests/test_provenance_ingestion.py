from __future__ import annotations

from uuid import UUID, uuid4

from lab_tracker.models import (
    Dataset,
    DatasetCommitManifest,
    DatasetStatus,
    ExternalArtifactKind,
    QuestionLink,
    QuestionLinkRole,
)
from lab_tracker.provenance import build_dataset_provenance_document
from lab_tracker.provenance_ingestion import (
    dataset_manifest_from_datalad_manifest,
    dataset_manifest_from_dvc_manifest,
)


def _node_by_id(document: dict[str, object], node_id: str) -> dict[str, object]:
    graph = document["@graph"]
    assert isinstance(graph, list)
    for node in graph:
        assert isinstance(node, dict)
        if node.get("@id") == node_id:
            return node
    raise AssertionError(f"Node not found: {node_id}")


def test_dvc_manifest_imports_files_and_external_artifact():
    manifest = """
outs:
- md5: abc123
  size: 2048
  path: sub-001/behavior.nwb
- md5: def456.dir
  nfiles: 4
  size: 4096
  path: derivatives/plume-navigation
"""

    manifest_input = dataset_manifest_from_dvc_manifest(
        manifest,
        manifest_uri="dvc://lab/plume-navigation/raw.dvc?rev=abc123",
        metadata={"imported_by": "lab-tracker"},
        nwb_metadata={"Identifier": "nwb-001"},
        bids_metadata={"TaskName": "plume-navigation"},
    )

    assert [(file.path, file.checksum, file.size_bytes) for file in manifest_input.files] == [
        ("sub-001/behavior.nwb", "md5:abc123", 2048),
        ("derivatives/plume-navigation", "md5:def456.dir", 4096),
    ]
    assert manifest_input.metadata == {"imported_by": "lab-tracker"}
    assert manifest_input.nwb_metadata == {"Identifier": "nwb-001"}
    assert manifest_input.bids_metadata == {"TaskName": "plume-navigation"}
    artifact = manifest_input.external_artifacts[0]
    assert artifact.kind == ExternalArtifactKind.ENTITY
    assert artifact.source_system == "dvc"
    assert artifact.uri == "dvc://lab/plume-navigation/raw.dvc?rev=abc123"
    assert artifact.content_hash.startswith("sha256:")
    assert artifact.metadata["out_count"] == 2
    assert artifact.metadata["paths"] == [
        "sub-001/behavior.nwb",
        "derivatives/plume-navigation",
    ]


def test_datalad_manifest_import_exports_provenance_and_question_edge():
    dataset_id = UUID("aaaaaaaa-5555-5555-5555-aaaaaaaaaaaa")
    question_id = UUID("bbbbbbbb-6666-6666-6666-bbbbbbbbbbbb")
    manifest_input = dataset_manifest_from_datalad_manifest(
        {
            "dataset": "lab/plume-navigation",
            "commit": "abc123",
            "files": [
                {
                    "path": "sub-001/behavior.nwb",
                    "checksum": "sha256:file456",
                    "bytesize": 2048,
                }
            ],
            "nwb_metadata": {"Identifier": "nwb-001"},
            "bids_metadata": {"TaskName": "plume-navigation"},
        }
    )
    artifact = manifest_input.external_artifacts[0]
    question_link = QuestionLink(
        question_id=question_id,
        role=QuestionLinkRole.PRIMARY,
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=uuid4(),
        commit_hash="commit-imported-datalad",
        primary_question_id=question_id,
        question_links=[question_link],
        commit_manifest=DatasetCommitManifest(
            files=manifest_input.files,
            external_artifacts=manifest_input.external_artifacts,
            metadata=manifest_input.metadata,
            nwb_metadata=manifest_input.nwb_metadata,
            bids_metadata=manifest_input.bids_metadata,
            question_links=[question_link],
        ),
        status=DatasetStatus.COMMITTED,
    )

    document = build_dataset_provenance_document("http://example.test", dataset)

    commit_id = (
        "http://example.test/datasets/aaaaaaaa-5555-5555-5555-aaaaaaaaaaaa/provenance/commit"
    )
    file_id = (
        "http://example.test/datasets/aaaaaaaa-5555-5555-5555-aaaaaaaaaaaa/"
        "provenance/files/sub-001%2Fbehavior.nwb"
    )
    question_link_id = (
        "http://example.test/datasets/aaaaaaaa-5555-5555-5555-aaaaaaaaaaaa/"
        "provenance/question-links/bbbbbbbb-6666-6666-6666-bbbbbbbbbbbb"
    )
    commit_node = _node_by_id(document, commit_id)
    artifact_node = _node_by_id(document, artifact.uri)
    question_link_node = _node_by_id(document, question_link_id)

    assert artifact.uri == "datalad://lab/plume-navigation?commit=abc123"
    assert artifact.content_hash.startswith("sha256:")
    assert artifact.metadata["commit"] == "abc123"
    assert artifact.metadata["paths"] == ["sub-001/behavior.nwb"]
    assert {"@id": file_id} in commit_node["prov:used"]
    assert {"@id": artifact.uri} in commit_node["prov:used"]
    assert commit_node["questionLink"] == [{"@id": question_link_node["@id"]}]
    assert artifact_node["@type"] == "prov:Entity"
    assert artifact_node["externalSourceSystem"] == "datalad"
