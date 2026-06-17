from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimEdge,
    ClaimRelation,
    ClaimStatus,
    Dataset,
    DatasetCommitManifest,
    DatasetFile,
    DatasetStatus,
    EntityOrigin,
    ExternalArtifactKind,
    ExternalArtifactReference,
    OutcomeStatus,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
    RecordExportRecords,
    SupervisionEdge,
    Visualization,
    VisualizationAsset,
)
from lab_tracker.provenance import (
    build_analysis_provenance_document,
    build_dataset_provenance_document,
    build_record_export_provenance_document,
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


def _node_type_includes(node: dict[str, object], node_type: str) -> bool:
    node_types = node["@type"]
    if isinstance(node_types, list):
        return node_type in node_types
    return node_types == node_type


def test_record_export_provenance_includes_terminal_reasons():
    question_id = UUID("11111111-aaaa-aaaa-aaaa-111111111111")
    dataset_id = UUID("22222222-aaaa-aaaa-aaaa-222222222222")
    analysis_id = UUID("33333333-aaaa-aaaa-aaaa-333333333333")
    claim_id = UUID("44444444-aaaa-aaaa-aaaa-444444444444")
    project_id = uuid4()

    question = Question(
        question_id=question_id,
        project_id=project_id,
        text="Dead-end hypothesis",
        question_type=QuestionType.HYPOTHESIS_DRIVEN,
        status=QuestionStatus.ABANDONED,
        terminal_reason="The control group erased the apparent effect.",
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=project_id,
        commit_hash="commit-dead-end",
        primary_question_id=question_id,
        question_links=[QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)],
        commit_manifest=DatasetCommitManifest(),
        status=DatasetStatus.ARCHIVED,
        terminal_reason="The source acquisition was corrupted.",
    )
    analysis = Analysis(
        analysis_id=analysis_id,
        project_id=project_id,
        dataset_ids=[dataset_id],
        method_hash="method-dead-end",
        code_version="v1",
        status=AnalysisStatus.ARCHIVED,
        terminal_reason="The analysis environment could not be reproduced.",
    )
    claim = Claim(
        claim_id=claim_id,
        project_id=project_id,
        statement="Rejected interpretation",
        confidence=0.1,
        status=ClaimStatus.REJECTED,
        terminal_reason="A later analysis refuted the interpretation.",
    )

    document = build_record_export_provenance_document(
        "http://example.test",
        RecordExportRecords(
            questions=[question],
            datasets=[dataset],
            analyses=[analysis],
            claims=[claim],
        ),
    )

    context = document["@context"]
    assert isinstance(context, dict)
    assert context["terminalReason"] == "lab:terminalReason"
    assert (
        _node_by_id(document, f"http://example.test/questions/{question_id}")["terminalReason"]
        == "The control group erased the apparent effect."
    )
    assert (
        _node_by_id(document, f"http://example.test/datasets/{dataset_id}")["terminalReason"]
        == "The source acquisition was corrupted."
    )
    assert (
        _node_by_id(document, f"http://example.test/analyses/{analysis_id}")["terminalReason"]
        == "The analysis environment could not be reproduced."
    )
    assert (
        _node_by_id(document, f"http://example.test/claims/{claim_id}")["terminalReason"]
        == "A later analysis refuted the interpretation."
    )


def test_claim_relations_and_external_citations_are_exported_in_provenance():
    project_id = UUID("11111111-bbbb-bbbb-bbbb-111111111111")
    source_claim_id = UUID("22222222-bbbb-bbbb-bbbb-222222222222")
    target_claim_id = UUID("33333333-bbbb-bbbb-bbbb-333333333333")
    edge_id = UUID("44444444-bbbb-bbbb-bbbb-444444444444")
    citation = ExternalArtifactReference(
        source_system="doi",
        uri="doi:10.1101/example-preprint",
        content_hash="sha256:paper",
    )
    source_claim = Claim(
        claim_id=source_claim_id,
        project_id=project_id,
        statement="Perturbation reduces activity.",
        confidence=80.0,
        status=ClaimStatus.PROPOSED,
        external_citations=[citation],
    )
    target_claim = Claim(
        claim_id=target_claim_id,
        project_id=project_id,
        statement="Perturbation does not affect activity.",
        confidence=25.0,
        status=ClaimStatus.PROPOSED,
    )
    edge = ClaimEdge(
        edge_id=edge_id,
        claim_id=source_claim_id,
        target_claim_id=target_claim_id,
        relation=ClaimRelation.REFUTES,
    )

    document = build_record_export_provenance_document(
        "http://example.test",
        RecordExportRecords(
            claims=[source_claim, target_claim],
            claim_edges=[edge],
        ),
    )

    source_node = _node_by_id(document, f"http://example.test/claims/{source_claim_id}")
    relation_iri = f"http://example.test/claim-relations/{edge_id}"
    assert source_node["cites"] == [{"@id": citation.uri}]
    assert source_node["claimRelation"] == [{"@id": relation_iri}]
    assert _node_by_id(document, citation.uri)["externalContentHash"] == "sha256:paper"
    relation_node = _node_by_id(document, relation_iri)
    assert relation_node["@type"] == "lab:ClaimRelation"
    assert relation_node["claimRelationType"] == "refutes"
    assert relation_node["claimRelationSource"] == {
        "@id": f"http://example.test/claims/{source_claim_id}"
    }
    assert relation_node["claimRelationTarget"] == {
        "@id": f"http://example.test/claims/{target_claim_id}"
    }


def test_analysis_provenance_distinguishes_ai_suggested_and_user_revised_nodes():
    change_set_id = UUID("99999999-9999-4999-8999-999999999999")
    analysis_id = UUID("55555555-aaaa-aaaa-aaaa-555555555555")
    dataset_id = UUID("66666666-aaaa-aaaa-aaaa-666666666666")
    claim_id = UUID("77777777-aaaa-aaaa-aaaa-777777777777")
    viz_id = UUID("88888888-aaaa-aaaa-aaaa-888888888888")
    creator_user_id = UUID("aaaaaaaa-9999-4999-8999-aaaaaaaaaaaa")

    analysis = Analysis(
        analysis_id=analysis_id,
        project_id=uuid4(),
        dataset_ids=[dataset_id],
        method_hash="method-ai-origin",
        code_version="v1",
        status=AnalysisStatus.COMMITTED,
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=analysis.project_id,
        commit_hash="commit-ai-origin",
        primary_question_id=uuid4(),
        question_links=[],
        commit_manifest=DatasetCommitManifest(),
        status=DatasetStatus.COMMITTED,
    )
    claim = Claim(
        claim_id=claim_id,
        project_id=analysis.project_id,
        statement="AI-suggested claim",
        confidence=0.7,
        status=ClaimStatus.SUPPORTED,
        supported_by_analysis_ids=[analysis_id],
        created_by_user_id=creator_user_id,
        origin=EntityOrigin.AI_SUGGESTED,
        change_set_id=change_set_id,
        origin_provider="openai",
        origin_model="fake-gpt",
        origin_prompt_version="multimodal-graph-draft-v1",
    )
    visualization = Visualization(
        viz_id=viz_id,
        analysis_id=analysis_id,
        viz_type="line",
        file_path="figs/ai-origin.png",
        created_by_user_id=creator_user_id,
        origin=EntityOrigin.USER_REVISED,
        change_set_id=change_set_id,
        origin_provider="openai",
        origin_model="fake-gpt",
        origin_prompt_version="multimodal-graph-draft-v1",
    )

    document = build_analysis_provenance_document(
        "http://example.test",
        analysis,
        datasets=[dataset],
        claims=[claim],
        visualizations=[visualization],
    )

    draft_iri = f"http://example.test/graph-drafts/{change_set_id}"
    agent_iri = f"{draft_iri}/software-agent"
    claim_node = _node_by_id(document, f"http://example.test/claims/{claim_id}")
    viz_node = _node_by_id(document, f"http://example.test/visualizations/{viz_id}")
    draft_node = _node_by_id(document, draft_iri)
    agent_node = _node_by_id(document, agent_iri)

    assert claim_node["origin"] == "ai_suggested"
    assert claim_node["changeSet"] == {"@id": draft_iri}
    assert claim_node["prov:wasGeneratedBy"] == {"@id": draft_iri}
    assert claim_node["prov:wasAttributedTo"] == {
        "@id": f"http://example.test/agents/{creator_user_id}"
    }
    assert viz_node["origin"] == "user_revised"
    assert viz_node["changeSet"] == {"@id": draft_iri}
    assert viz_node["prov:wasInformedBy"] == {"@id": draft_iri}
    before_iri = f"http://example.test/visualizations/{viz_id}/versions/before/{change_set_id}"
    assert viz_node["prov:wasRevisionOf"] == {"@id": before_iri}
    # The wasRevisionOf target must resolve to a node present in @graph (raises if absent).
    before_node = _node_by_id(document, before_iri)
    assert before_node["origin"] == "ai_suggested"
    assert before_node["changeSet"] == {"@id": draft_iri}
    assert draft_node["prov:wasAssociatedWith"] == {"@id": agent_iri}
    assert _node_type_includes(agent_node, "prov:SoftwareAgent")
    assert agent_node["aiProvider"] == "openai"
    assert agent_node["aiModel"] == "fake-gpt"
    assert agent_node["aiPromptVersion"] == "multimodal-graph-draft-v1"


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

    commit_id = (
        "http://example.test/datasets/11111111-1111-1111-1111-111111111111/provenance/commit"
    )
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


def test_dataset_provenance_attributes_creator_and_active_supervisor():
    creator_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-000000000001")
    supervisor_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-000000000002")
    inactive_supervisor_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-000000000003")
    dataset_id = UUID("11111111-1111-1111-1111-000000000001")
    question_id = UUID("22222222-2222-2222-2222-000000000001")
    created_at = datetime(2026, 4, 23, tzinfo=timezone.utc)

    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=uuid4(),
        commit_hash="commit-attributed",
        primary_question_id=question_id,
        question_links=[],
        commit_manifest=DatasetCommitManifest(),
        status=DatasetStatus.COMMITTED,
        created_at=created_at,
        created_by="legacy-user-string",
        created_by_user_id=creator_user_id,
    )
    active_edge = SupervisionEdge(
        edge_id=uuid4(),
        supervisor_user_id=supervisor_user_id,
        supervisee_user_id=creator_user_id,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    inactive_edge = SupervisionEdge(
        edge_id=uuid4(),
        supervisor_user_id=inactive_supervisor_user_id,
        supervisee_user_id=creator_user_id,
        started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    document = build_dataset_provenance_document(
        "http://example.test",
        dataset,
        supervision_edges=[inactive_edge, active_edge],
    )

    context = document["@context"]
    assert isinstance(context, dict)
    assert context["wasAttributedTo"] == {"@id": "prov:wasAttributedTo", "@type": "@id"}
    assert context["actedOnBehalfOf"] == {"@id": "prov:actedOnBehalfOf", "@type": "@id"}
    assert context["supervisionStartedAt"] == "lab:supervisionStartedAt"
    assert context["supervisionEndedAt"] == "lab:supervisionEndedAt"

    dataset_iri = "http://example.test/datasets/11111111-1111-1111-1111-000000000001"
    creator_iri = f"http://example.test/agents/{creator_user_id}"
    supervisor_iri = f"http://example.test/agents/{supervisor_user_id}"
    dataset_node = _node_by_id(document, dataset_iri)
    creator_node = _node_by_id(document, creator_iri)
    supervisor_node = _node_by_id(document, supervisor_iri)

    assert dataset_node["prov:wasAttributedTo"] == {"@id": creator_iri}
    assert _node_type_includes(creator_node, "prov:Agent")
    assert _node_type_includes(creator_node, "prov:Person")
    assert creator_node["userId"] == str(creator_user_id)
    assert creator_node["prov:actedOnBehalfOf"] == {
        "@id": supervisor_iri,
        "supervisionStartedAt": "2026-01-01T00:00:00+00:00",
    }
    assert _node_type_includes(supervisor_node, "prov:Person")
    assert str(inactive_supervisor_user_id) not in {
        str(node.get("userId")) for node in document["@graph"] if isinstance(node, dict)
    }


def test_dataset_provenance_uses_stable_synthetic_ids():
    dataset_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    question_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=uuid4(),
        commit_hash="commit-456",
        primary_question_id=question_id,
        question_links=[QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)],
        commit_manifest=DatasetCommitManifest(
            files=[DatasetFile(path="nested/file.bin", checksum="def456")],
            question_links=[QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)],
        ),
        status=DatasetStatus.STAGED,
    )

    document = build_dataset_provenance_document("http://example.test/", dataset)

    commit_id = (
        "http://example.test/datasets/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/provenance/commit"
    )
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
        question_links=[QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)],
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
        question_links=[QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)],
        commit_manifest=DatasetCommitManifest(
            metadata={EXTERNAL_ARTIFACTS_METADATA_KEY: encode_external_artifacts([artifact])},
            question_links=[QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)],
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


def test_malformed_legacy_external_artifact_metadata_is_ignored_in_provenance():
    dataset_id = UUID("aaaaaaaa-5555-5555-5555-aaaaaaaaaaaa")
    question_id = UUID("bbbbbbbb-6666-6666-6666-bbbbbbbbbbbb")
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=uuid4(),
        commit_hash="commit-malformed-legacy-external",
        primary_question_id=question_id,
        question_links=[QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)],
        commit_manifest=DatasetCommitManifest(
            metadata={EXTERNAL_ARTIFACTS_METADATA_KEY: "not-json"},
            question_links=[QuestionLink(question_id=question_id, role=QuestionLinkRole.PRIMARY)],
        ),
        status=DatasetStatus.COMMITTED,
    )

    document = build_dataset_provenance_document("http://example.test", dataset)
    commit_node = _node_by_id(
        document,
        "http://example.test/datasets/aaaaaaaa-5555-5555-5555-aaaaaaaaaaaa/provenance/commit",
    )

    assert "prov:used" not in commit_node


def test_analysis_provenance_exports_external_run_reference():
    analysis_id = UUID("55555555-aaaa-1111-aaaa-555555555555")
    dataset_id = UUID("66666666-bbbb-2222-bbbb-666666666666")
    run = ExternalArtifactReference(
        kind=ExternalArtifactKind.ACTIVITY,
        source_system="mlflow",
        uri="mlflow://experiments/fly/runs/run-001",
        content_hash="sha256:run001",
        metadata={"run_name": "gain-fit"},
    )
    analysis = Analysis(
        analysis_id=analysis_id,
        project_id=uuid4(),
        dataset_ids=[dataset_id],
        method_hash="method-1",
        code_version="git:abc123",
        environment_hash="conda:lock",
        external_artifacts=[run],
        status=AnalysisStatus.COMMITTED,
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=analysis.project_id,
        commit_hash="commit-run-ref",
        primary_question_id=uuid4(),
        question_links=[],
        commit_manifest=DatasetCommitManifest(),
        status=DatasetStatus.COMMITTED,
    )

    document = build_analysis_provenance_document(
        "http://example.test",
        analysis,
        datasets=[dataset],
        claims=[],
        visualizations=[],
    )

    analysis_node = _node_by_id(document, f"http://example.test/analyses/{analysis_id}")
    run_node = _node_by_id(document, run.uri)

    assert analysis_node["prov:used"] == [{"@id": f"http://example.test/datasets/{dataset_id}"}]
    assert analysis_node["prov:wasInformedBy"] == [{"@id": run.uri}]
    assert run_node["@type"] == "prov:Activity"
    assert run_node["externalSourceSystem"] == "mlflow"
    assert run_node["externalContentHash"] == "sha256:run001"
    assert run_node["externalMetadata"] == {"run_name": "gain-fit"}


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
        dataset_ids=[dataset_id],
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

    context = document["@context"]
    assert isinstance(context, dict)
    assert context["contentUrl"] == {"@id": "lab:contentUrl", "@type": "@id"}
    assert context["fileName"] == "lab:fileName"
    assert context["encodingFormat"] == "lab:encodingFormat"
    assert context["contentSize"] == "lab:contentSize"
    assert context["sha256"] == "lab:sha256"

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
    assert viz_node["groundingDataset"] == [
        {"@id": "http://example.test/datasets/66666666-6666-6666-6666-666666666666"}
    ]
    assert viz_node["prov:wasDerivedFrom"] == [
        {"@id": "http://example.test/datasets/66666666-6666-6666-6666-666666666666"}
    ]
    assert viz_node["contentUrl"] == (
        "http://example.test/visualizations/88888888-8888-8888-8888-888888888888/file/download"
    )
    assert viz_node["fileName"] == "signal.png"
    assert viz_node["encodingFormat"] == "image/png"
    assert viz_node["contentSize"] == 128
    assert viz_node["sha256"] == "abc123"
    assert "caption" not in viz_node


def test_analysis_provenance_attributes_supported_entities_to_people():
    actor_user_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-000000000001")
    supervisor_user_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-000000000002")
    analysis_id = UUID("55555555-5555-5555-5555-000000000001")
    dataset_id = UUID("66666666-6666-6666-6666-000000000001")
    claim_id = UUID("77777777-7777-7777-7777-000000000001")
    viz_id = UUID("88888888-8888-8888-8888-000000000001")

    analysis = Analysis(
        analysis_id=analysis_id,
        project_id=uuid4(),
        dataset_ids=[dataset_id],
        method_hash="method-people",
        code_version="v2",
        executed_by="legacy-runner",
        executed_by_user_id=actor_user_id,
        executed_at=datetime(2026, 4, 23, tzinfo=timezone.utc),
        status=AnalysisStatus.COMMITTED,
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=analysis.project_id,
        commit_hash="commit-people",
        primary_question_id=uuid4(),
        question_links=[],
        commit_manifest=DatasetCommitManifest(),
        status=DatasetStatus.COMMITTED,
        created_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
        created_by_user_id=actor_user_id,
    )
    claim = Claim(
        claim_id=claim_id,
        project_id=analysis.project_id,
        statement="Stable effect",
        confidence=0.8,
        status=ClaimStatus.SUPPORTED,
        supported_by_dataset_ids=[dataset_id],
        supported_by_analysis_ids=[analysis_id],
        created_at=datetime(2026, 4, 24, tzinfo=timezone.utc),
    )
    visualization = Visualization(
        viz_id=viz_id,
        analysis_id=analysis_id,
        viz_type="line",
        file_path="figs/signal.png",
        created_at=datetime(2026, 4, 25, tzinfo=timezone.utc),
    )
    supervision_edge = SupervisionEdge(
        edge_id=uuid4(),
        supervisor_user_id=supervisor_user_id,
        supervisee_user_id=actor_user_id,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    document = build_analysis_provenance_document(
        "http://example.test",
        analysis,
        datasets=[dataset],
        claims=[claim],
        visualizations=[visualization],
        supervision_edges=[supervision_edge],
    )

    actor_iri = f"http://example.test/agents/{actor_user_id}"
    supervisor_iri = f"http://example.test/agents/{supervisor_user_id}"
    analysis_node = _node_by_id(document, f"http://example.test/analyses/{analysis_id}")
    dataset_node = _node_by_id(document, f"http://example.test/datasets/{dataset_id}")
    claim_node = _node_by_id(document, f"http://example.test/claims/{claim_id}")
    viz_node = _node_by_id(document, f"http://example.test/visualizations/{viz_id}")
    actor_node = _node_by_id(document, actor_iri)

    assert analysis_node["prov:wasAssociatedWith"] == {"@id": actor_iri}
    assert dataset_node["prov:wasAttributedTo"] == {"@id": actor_iri}
    assert claim_node["prov:wasAttributedTo"] == {"@id": actor_iri}
    assert viz_node["prov:wasAttributedTo"] == {"@id": actor_iri}
    assert _node_type_includes(actor_node, "prov:Person")
    assert actor_node["prov:actedOnBehalfOf"] == {
        "@id": supervisor_iri,
        "supervisionStartedAt": "2026-01-01T00:00:00+00:00",
    }
    assert _node_type_includes(_node_by_id(document, supervisor_iri), "prov:Person")
