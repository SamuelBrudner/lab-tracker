from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimStatus,
    Dataset,
    DatasetCommitManifest,
    DatasetFile,
    DatasetStatus,
    ExternalArtifactKind,
    ExternalArtifactReference,
    OutcomeStatus,
    QuestionLink,
    QuestionLinkRole,
    Visualization,
    VisualizationAsset,
)
from lab_tracker.provenance import (
    build_analysis_provenance_document,
    build_dataset_provenance_document,
)
from lab_tracker.provenance_ingestion import (
    EXTERNAL_ARTIFACTS_METADATA_KEY,
    dataset_manifest_from_external_artifact,
    encode_external_artifacts,
    external_artifacts_from_metadata,
)


def _node_by_id(document: dict[str, object], node_id: str) -> dict[str, object]:
    graph = document["@graph"]
    assert isinstance(graph, list)
    for node in graph:
        assert isinstance(node, dict)
        if node.get("@id") == node_id:
            return node
    raise AssertionError(f"Node not found: {node_id}")


def test_dataset_provenance_uses_inline_context_and_json_metadata():
    dataset_id = UUID("11111111-1111-1111-1111-111111111111")
    question_id = UUID("22222222-2222-2222-2222-222222222222")
    note_id = UUID("33333333-3333-3333-3333-333333333333")
    session_id = UUID("44444444-4444-4444-4444-444444444444")

    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=uuid4(),
        commit_hash="commit-123",
        primary_question_id=question_id,
        question_links=[
            QuestionLink(
                question_id=question_id,
                role=QuestionLinkRole.PRIMARY,
                outcome_status=OutcomeStatus.SUPPORTS,
            )
        ],
        commit_manifest=DatasetCommitManifest(
            files=[DatasetFile(path="raw/data.csv", checksum="abc123")],
            metadata={"run": "7"},
            nwb_metadata={"Session Description": "baseline"},
            bids_metadata={"Name": "Example Dataset"},
            note_ids=[note_id],
            question_links=[
                QuestionLink(
                    question_id=question_id,
                    role=QuestionLinkRole.PRIMARY,
                    outcome_status=OutcomeStatus.SUPPORTS,
                )
            ],
            source_session_id=session_id,
        ),
        status=DatasetStatus.COMMITTED,
    )

    document = build_dataset_provenance_document("http://example.test", dataset)

    context = document["@context"]
    assert isinstance(context, dict)
    assert context["prov"] == "http://www.w3.org/ns/prov#"
    assert context["lab"] == "http://example.test/terms#"
    assert context["metadata"] == {"@id": "lab:metadata", "@type": "@json"}
    assert context["nwbMetadata"] == {"@id": "lab:nwbMetadata", "@type": "@json"}
    assert context["bidsMetadata"] == {"@id": "lab:bidsMetadata", "@type": "@json"}

    commit_id = "http://example.test/datasets/11111111-1111-1111-1111-111111111111/provenance/commit"
    dataset_node = _node_by_id(
        document,
        "http://example.test/datasets/11111111-1111-1111-1111-111111111111",
    )
    commit_node = _node_by_id(document, commit_id)

    assert dataset_node["prov:wasGeneratedBy"] == {"@id": commit_id}
    assert dataset_node["commitHash"] == "commit-123"
    assert commit_node["metadata"] == {"run": "7"}
    assert commit_node["nwbMetadata"] == {"Session Description": "baseline"}
    assert commit_node["bidsMetadata"] == {"Name": "Example Dataset"}
    assert commit_node["note"] == [
        {"@id": "http://example.test/notes/33333333-3333-3333-3333-333333333333"}
    ]
    assert commit_node["sourceSession"] == {
        "@id": "http://example.test/sessions/44444444-4444-4444-4444-444444444444"
    }


def test_dataset_provenance_uses_stable_synthetic_ids():
    dataset_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    question_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=uuid4(),
        commit_hash="commit-456",
        primary_question_id=question_id,
        question_links=[
            QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)
        ],
        commit_manifest=DatasetCommitManifest(
            files=[DatasetFile(path="nested/file.bin", checksum="def456")],
            question_links=[
                QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)
            ],
        ),
        status=DatasetStatus.STAGED,
    )

    document = build_dataset_provenance_document("http://example.test/", dataset)

    commit_id = "http://example.test/datasets/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/provenance/commit"
    commit_node = _node_by_id(document, commit_id)
    file_node = _node_by_id(
        document,
        "http://example.test/datasets/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/provenance/files/nested%2Ffile.bin",
    )
    question_link_node = _node_by_id(
        document,
        "http://example.test/datasets/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/provenance/question-links/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )

    assert commit_node["@type"] == "prov:Activity"
    assert commit_node["prov:used"] == [{"@id": file_node["@id"]}]
    assert commit_node["questionLink"] == [{"@id": question_link_node["@id"]}]


def test_external_dataset_artifact_round_trips_through_dataset_provenance():
    dataset_id = UUID("aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa")
    question_id = UUID("bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb")
    artifact = ExternalArtifactReference(
        source_system="datalad",
        uri="datalad://lab/plume-navigation?commit=abc123",
        content_hash="sha256:manifest123",
        metadata={
            "dataset": "plume-navigation",
            "commit": "abc123",
            "file_count": 1,
            "labels": ["nwb", "behavior"],
            "native": {"annex": True},
        },
    )
    activity = ExternalArtifactReference(
        kind=ExternalArtifactKind.ACTIVITY,
        source_system="acquisition-daemon",
        uri="urn:lab:acquisition-run:run-001",
        content_hash="sha256:activity456",
        metadata={"rig": "rig-2"},
    )
    manifest_input = dataset_manifest_from_external_artifact(
        artifact,
        files=[
            DatasetFile(
                path="sub-001/behavior.nwb",
                checksum="sha256:file456",
                size_bytes=2048,
            )
        ],
        metadata={"imported_by": "lab-tracker"},
    )

    assert manifest_input.external_artifacts == [artifact]
    assert external_artifacts_from_metadata(manifest_input.metadata) == []
    assert EXTERNAL_ARTIFACTS_METADATA_KEY not in manifest_input.metadata

    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=uuid4(),
        commit_hash="commit-external",
        primary_question_id=question_id,
        question_links=[
            QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)
        ],
        commit_manifest=DatasetCommitManifest(
            files=manifest_input.files,
            external_artifacts=[*manifest_input.external_artifacts, activity],
            metadata=manifest_input.metadata,
            question_links=[
                QuestionLink(
                    question_id=question_id,
                    role=QuestionLinkRole.PRIMARY,
                    outcome_status=OutcomeStatus.SUPPORTS,
                )
            ],
        ),
        status=DatasetStatus.COMMITTED,
    )

    document = build_dataset_provenance_document("http://example.test", dataset)

    commit_node = _node_by_id(
        document,
        "http://example.test/datasets/aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa/provenance/commit",
    )
    artifact_node = _node_by_id(document, artifact.uri)
    activity_node = _node_by_id(document, activity.uri)
    question_link_node = _node_by_id(
        document,
        "http://example.test/datasets/aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa/provenance/question-links/bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb",
    )

    assert {"@id": artifact.uri} in commit_node["prov:used"]
    assert commit_node["prov:wasInformedBy"] == [{"@id": activity.uri}]
    assert commit_node["questionLink"] == [{"@id": question_link_node["@id"]}]
    assert artifact_node["@type"] == "prov:Entity"
    assert artifact_node["externalSourceSystem"] == "datalad"
    assert artifact_node["externalContentHash"] == "sha256:manifest123"
    assert artifact_node["externalMetadata"] == {
        "commit": "abc123",
        "dataset": "plume-navigation",
        "file_count": 1,
        "labels": ["nwb", "behavior"],
        "native": {"annex": True},
    }
    assert activity_node["@type"] == "prov:Activity"
    assert activity_node["externalSourceSystem"] == "acquisition-daemon"


def test_legacy_external_artifact_metadata_still_exports_dataset_provenance():
    dataset_id = UUID("aaaaaaaa-3333-3333-3333-aaaaaaaaaaaa")
    question_id = UUID("bbbbbbbb-4444-4444-4444-bbbbbbbbbbbb")
    artifact = ExternalArtifactReference(
        source_system="dvc",
        uri="dvc://lab/run-legacy.dvc",
        content_hash="sha256:legacy-manifest",
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=uuid4(),
        commit_hash="commit-legacy-external",
        primary_question_id=question_id,
        question_links=[
            QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)
        ],
        commit_manifest=DatasetCommitManifest(
            metadata={EXTERNAL_ARTIFACTS_METADATA_KEY: encode_external_artifacts([artifact])},
            question_links=[
                QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)
            ],
        ),
        status=DatasetStatus.COMMITTED,
    )

    document = build_dataset_provenance_document("http://example.test", dataset)
    commit_node = _node_by_id(
        document,
        "http://example.test/datasets/aaaaaaaa-3333-3333-3333-aaaaaaaaaaaa/provenance/commit",
    )
    artifact_node = _node_by_id(document, artifact.uri)

    assert {"@id": artifact.uri} in commit_node["prov:used"]
    assert artifact_node["externalContentHash"] == "sha256:legacy-manifest"


def test_analysis_provenance_omits_optional_fields_and_preserves_support_links():
    analysis_id = UUID("55555555-5555-5555-5555-555555555555")
    dataset_id = UUID("66666666-6666-6666-6666-666666666666")
    claim_id = UUID("77777777-7777-7777-7777-777777777777")
    viz_id = UUID("88888888-8888-8888-8888-888888888888")

    analysis = Analysis(
        analysis_id=analysis_id,
        project_id=uuid4(),
        dataset_ids=[dataset_id],
        method_hash="method-1",
        code_version="v1",
        environment_hash=None,
        executed_by=None,
        executed_at=datetime(2026, 4, 23, tzinfo=timezone.utc),
        status=AnalysisStatus.COMMITTED,
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=analysis.project_id,
        commit_hash="commit-789",
        primary_question_id=uuid4(),
        question_links=[],
        commit_manifest=DatasetCommitManifest(),
        status=DatasetStatus.COMMITTED,
    )
    claim = Claim(
        claim_id=claim_id,
        project_id=analysis.project_id,
        statement="Stable effect",
        confidence=0.8,
        status=ClaimStatus.SUPPORTED,
        supported_by_dataset_ids=[dataset_id],
        supported_by_analysis_ids=[analysis_id],
    )
    visualization = Visualization(
        viz_id=viz_id,
        analysis_id=analysis_id,
        viz_type="line",
        file_path="figs/signal.png",
        related_claim_ids=[claim_id],
        asset=VisualizationAsset(
            storage_id=uuid4(),
            filename="signal.png",
            content_type="image/png",
            size_bytes=128,
            checksum="abc123",
        ),
    )

    document = build_analysis_provenance_document(
        "http://example.test",
        analysis,
        datasets=[dataset],
        claims=[claim],
        visualizations=[visualization],
    )

    analysis_node = _node_by_id(
        document,
        "http://example.test/analyses/55555555-5555-5555-5555-555555555555",
    )
    claim_node = _node_by_id(
        document,
        "http://example.test/claims/77777777-7777-7777-7777-777777777777",
    )
    viz_node = _node_by_id(
        document,
        "http://example.test/visualizations/88888888-8888-8888-8888-888888888888",
    )

    assert analysis_node["@type"] == "prov:Activity"
    assert analysis_node["executedAt"] == "2026-04-23T00:00:00+00:00"
    assert analysis_node["prov:used"] == [
        {"@id": "http://example.test/datasets/66666666-6666-6666-6666-666666666666"}
    ]
    assert "environmentHash" not in analysis_node
    assert "prov:wasAssociatedWith" not in analysis_node
    assert claim_node["supportsDataset"] == [
        {"@id": "http://example.test/datasets/66666666-6666-6666-6666-666666666666"}
    ]
    assert claim_node["supportsAnalysis"] == [
        {"@id": "http://example.test/analyses/55555555-5555-5555-5555-555555555555"}
    ]
    assert viz_node["prov:wasGeneratedBy"] == {
        "@id": "http://example.test/analyses/55555555-5555-5555-5555-555555555555"
    }
    assert viz_node["relatedClaim"] == [
        {"@id": "http://example.test/claims/77777777-7777-7777-7777-777777777777"}
    ]
    assert viz_node["contentUrl"] == (
        "http://example.test/visualizations/"
        "88888888-8888-8888-8888-888888888888/file/download"
    )
    assert viz_node["fileName"] == "signal.png"
    assert viz_node["encodingFormat"] == "image/png"
    assert viz_node["contentSize"] == 128
    assert viz_node["sha256"] == "abc123"
    assert "caption" not in viz_node
