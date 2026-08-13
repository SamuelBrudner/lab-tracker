from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

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
    EntityRef,
    EntityType,
    EntityVersion,
    ExplorationNode,
    ExplorationNodeStatus,
    ExplorationNodeType,
    ExternalArtifactKind,
    ExternalArtifactReference,
    Goal,
    GoalLink,
    GoalLinkStatus,
    GoalRelation,
    GoalStatus,
    GoalType,
    Note,
    NoteStatus,
    OutcomeStatus,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
    RecordExportRecords,
    Session,
    SessionStatus,
    SessionType,
    SupervisionEdge,
    Visualization,
    VisualizationAsset,
)
from lab_tracker.provenance import (
    AraArtifactRecords,
    build_analysis_provenance_document,
    build_ara_artifact_document,
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


def _classification_ids(node: dict[str, object]) -> set[str]:
    classifications = node["classifiedAs"]
    assert isinstance(classifications, list)
    return {
        str(classification["@id"])
        for classification in classifications
        if isinstance(classification, dict)
    }


class _OnboardingDraftClient:
    provider = "fake"
    model = "fake-member-alignment"
    timeout_seconds = 1

    def __init__(self, patch: dict[str, Any]) -> None:
        self.patch = patch

    def draft_from_note(self, **_: Any) -> dict[str, Any]:
        return self.patch

    def close(self) -> None:
        return None


def _onboarding_question_patch(project_id: str, checkpoint_id: str) -> dict[str, Any]:
    return {
        "summary": "Align the member's live question.",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "live_question_0",
                "op": "create",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "text": "Does the assay preserve response fidelity?",
                        "question_type": "other",
                        "status": "staged",
                    }
                ),
                "rationale": "The member named this as live.",
                "confidence": 0.8,
                "source_refs": [{"source_note_ids": [checkpoint_id]}],
            }
        ],
    }


def test_record_export_uses_minimal_profile_classes_and_controlled_concepts():
    base_url = "http://example.test"
    project_id = uuid4()
    question_id = uuid4()
    dataset_id = uuid4()
    session_id = uuid4()
    analysis_id = uuid4()
    claim_id = uuid4()
    note_id = uuid4()
    exploration_node_id = uuid4()
    visualization_id = uuid4()

    question = Question(
        question_id=question_id,
        project_id=project_id,
        text="Does the profile remain minimal?",
        question_type=QuestionType.HYPOTHESIS_DRIVEN,
        status=QuestionStatus.ACTIVE,
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=project_id,
        commit_hash="profile-classes",
        primary_question_id=question_id,
        question_links=[
            QuestionLink(
                question_id=question_id,
                role=QuestionLinkRole.PRIMARY,
                outcome_status=OutcomeStatus.SUPPORTS,
            )
        ],
        commit_manifest=DatasetCommitManifest(
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
    session = Session(
        session_id=session_id,
        project_id=project_id,
        session_type=SessionType.SCIENTIFIC,
        status=SessionStatus.CLOSED,
        primary_question_id=question_id,
    )
    analysis = Analysis(
        analysis_id=analysis_id,
        project_id=project_id,
        dataset_ids=[dataset_id],
        method_hash="profile-method",
        code_version="profile-v1",
        status=AnalysisStatus.COMMITTED,
    )
    claim = Claim(
        claim_id=claim_id,
        project_id=project_id,
        statement="The semantic profile is stable.",
        confidence=90,
        status=ClaimStatus.SUPPORTED,
        supported_by_analysis_ids=[analysis_id],
    )
    note = Note(
        note_id=note_id,
        project_id=project_id,
        raw_content="Profile review note",
        status=NoteStatus.COMMITTED,
    )
    exploration_node = ExplorationNode(
        node_id=exploration_node_id,
        project_id=project_id,
        node_type=ExplorationNodeType.DECISION,
        title="Adopt the minimal profile",
        target=EntityRef(entity_type=EntityType.CLAIM, entity_id=claim_id),
        status=ExplorationNodeStatus.COMMITTED,
        choice="Use concepts for refinements.",
    )
    visualization = Visualization(
        viz_id=visualization_id,
        analysis_id=analysis_id,
        dataset_ids=[dataset_id],
        viz_type="line",
        file_path="figures/profile.png",
    )

    document = build_record_export_provenance_document(
        base_url,
        RecordExportRecords(
            questions=[question],
            datasets=[dataset],
            sessions=[session],
            analyses=[analysis],
            claims=[claim],
            exploration_nodes=[exploration_node],
            notes=[note],
            visualizations=[visualization],
        ),
    )

    expected_types = {
        f"{base_url}/questions/{question_id}": "lab:ResearchQuestion",
        f"{base_url}/datasets/{dataset_id}": "lab:Dataset",
        f"{base_url}/sessions/{session_id}": "lab:AcquisitionSession",
        f"{base_url}/analyses/{analysis_id}": "lab:Analysis",
        f"{base_url}/claims/{claim_id}": "lab:Claim",
        f"{base_url}/notes/{note_id}": "lab:Note",
        f"{base_url}/exploration-nodes/{exploration_node_id}": "lab:ExplorationNode",
        f"{base_url}/visualizations/{visualization_id}": "lab:Visualization",
    }
    for node_id, expected_type in expected_types.items():
        assert _node_by_id(document, node_id)["@type"] == expected_type

    question_node = _node_by_id(document, f"{base_url}/questions/{question_id}")
    assert {
        "lab:questionType/hypothesis_driven",
        "lab:questionStatus/active",
        "lab:entityOrigin/user",
    } <= _classification_ids(question_node)
    dataset_node = _node_by_id(document, f"{base_url}/datasets/{dataset_id}")
    assert {
        "lab:datasetStatus/committed",
        "lab:entityOrigin/user",
    } <= _classification_ids(dataset_node)
    session_node = _node_by_id(document, f"{base_url}/sessions/{session_id}")
    assert {
        "lab:sessionType/scientific",
        "lab:sessionStatus/closed",
        "lab:entityOrigin/user",
    } <= _classification_ids(session_node)
    analysis_node = _node_by_id(document, f"{base_url}/analyses/{analysis_id}")
    assert {
        "lab:analysisStatus/committed",
        "lab:entityOrigin/user",
    } <= _classification_ids(analysis_node)
    claim_node = _node_by_id(document, f"{base_url}/claims/{claim_id}")
    assert {
        "lab:claimStatus/supported",
        "lab:entityOrigin/user",
    } <= _classification_ids(claim_node)
    note_node = _node_by_id(document, f"{base_url}/notes/{note_id}")
    assert {
        "lab:noteStatus/committed",
        "lab:entityOrigin/user",
    } <= _classification_ids(note_node)
    exploration_node_jsonld = _node_by_id(
        document,
        f"{base_url}/exploration-nodes/{exploration_node_id}",
    )
    assert {
        "lab:explorationNodeType/decision",
        "lab:explorationNodeStatus/committed",
        "lab:entityOrigin/user",
    } <= _classification_ids(exploration_node_jsonld)
    visualization_node = _node_by_id(
        document,
        f"{base_url}/visualizations/{visualization_id}",
    )
    assert {"lab:entityOrigin/user"} <= _classification_ids(visualization_node)
    question_link_node = _node_by_id(
        document,
        f"{base_url}/datasets/{dataset_id}/provenance/question-links/{question_id}",
    )
    assert question_link_node["@type"] == "lab:QuestionLink"
    assert _classification_ids(question_link_node) == {
        "lab:questionLinkRole/primary",
        "lab:outcomeStatus/supports",
    }


def test_record_export_preserves_real_ai_member_checkpoint_audit(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_response = client.post(
        "/projects",
        json={"name": f"Ongoing provenance assay {uuid4().hex[:8]}"},
        headers=admin_auth_headers,
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["data"]["project_id"]

    checkpoint_response = client.put(
        f"/projects/{project_id}/member-onboarding/checkpoint",
        json={
            "current_output_or_decision": "We selected the low-light assay.",
            "live_questions": ["Does the assay preserve response fidelity?"],
            "strongest_recent_context": "Pilot 4 was stable across two runs.",
            "next_move": "Repeat with the blinded batch.",
            "source_text": "Full historical handoff text.",
        },
        headers=admin_auth_headers,
    )
    assert checkpoint_response.status_code == 201, checkpoint_response.text
    checkpoint = checkpoint_response.json()["data"]["checkpoint"]
    checkpoint_id = checkpoint["note_id"]
    checkpoint_creator_id = checkpoint["created_by_user_id"]
    assert checkpoint_creator_id

    fake = _OnboardingDraftClient(
        _onboarding_question_patch(project_id, checkpoint_id)
    )
    client.app.state.graph_draft_client_factory = lambda _settings: fake
    alignment_response = client.post(
        f"/projects/{project_id}/member-onboarding/ai-alignment",
        json={"external_provider_acknowledged": True},
        headers=admin_auth_headers,
    )
    assert alignment_response.status_code == 200, alignment_response.text
    draft = alignment_response.json()["data"]["alignment"]["draft"]
    change_set_id = draft["change_set_id"]
    operation_id = draft["operations"][0]["operation_id"]

    acceptance_response = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{operation_id}",
        json={"status": "accepted"},
        headers=admin_auth_headers,
    )
    assert acceptance_response.status_code == 200, acceptance_response.text
    accepted_operation = acceptance_response.json()["data"]["operations"][0]
    accepted_at = accepted_operation["accepted_at"]
    assert accepted_operation["acceptance_mode"] == "human_selected"
    assert accepted_operation["accepted_by_user_id"] == checkpoint_creator_id
    assert accepted_at

    submitted_response = client.post(
        f"/graph-drafts/{change_set_id}/submit",
        headers=admin_auth_headers,
    )
    assert submitted_response.status_code == 200, submitted_response.text
    commit_response = client.post(
        f"/graph-drafts/{change_set_id}/commit",
        json={"message": "Owner committed the reviewed member alignment."},
        headers=admin_auth_headers,
    )
    assert commit_response.status_code == 200, commit_response.text
    committed_operation = commit_response.json()["data"]["operations"][0]
    result_question_id = committed_operation["result_entity_id"]
    assert result_question_id

    current_response = client.get(
        f"/projects/{project_id}/member-onboarding",
        headers=admin_auth_headers,
    )
    assert current_response.status_code == 200, current_response.text
    checkpoint_metadata = current_response.json()["data"]["checkpoint"]["metadata"]
    alignment_resolved_at = checkpoint_metadata[
        "member_onboarding_alignment_resolved_at"
    ]

    export_response = client.post(
        f"/record-exports/users/{checkpoint_creator_id}",
        headers=admin_auth_headers,
    )
    assert export_response.status_code == 200, export_response.text
    export_payload = export_response.json()["data"]
    assert checkpoint_id in {
        note["note_id"] for note in export_payload["records"]["notes"]
    }
    document = export_payload["provenance"]
    base_url = str(document["@context"]["lab"]).removesuffix("/terms#")
    checkpoint_iri = f"{base_url}/notes/{checkpoint_id}"
    graph_ids = {
        node["@id"] for node in document["@graph"] if isinstance(node, dict)
    }
    assert checkpoint_iri in graph_ids, graph_ids
    checkpoint_node = _node_by_id(document, checkpoint_iri)
    change_set_iri = f"{base_url}/graph-drafts/{change_set_id}"

    assert checkpoint_node["memberOnboardingRole"] == "checkpoint"
    assert checkpoint_node["historicalCoverage"] == "selective"
    assert checkpoint_node["checkpointPayloadSha256"]
    assert checkpoint_node["checkpointSourceTextSha256"]
    assert checkpoint_node["checkpointAlignmentMode"] == "ai"
    assert checkpoint_node["checkpointAlignmentResolvedAt"] == alignment_resolved_at
    assert checkpoint_node["checkpointAlignmentResolution"] == "submitted"
    assert checkpoint_node["checkpointAlignmentChangeSet"] == {
        "@id": change_set_iri
    }
    assert checkpoint_node["wasInformedBy"] == {"@id": change_set_iri}
    assert set(checkpoint_metadata).isdisjoint(checkpoint_node)

    question_node = _node_by_id(
        document,
        f"{base_url}/questions/{result_question_id}",
    )
    assert question_node["origin"] == "ai_suggested"
    assert question_node["changeSet"] == {"@id": change_set_iri}
    assert question_node["wasGeneratedBy"] == {"@id": change_set_iri}
    change_set_activity = _node_by_id(document, change_set_iri)
    assert change_set_activity["@type"] == "prov:Activity"
    assert change_set_activity["entityType"] == "graph_change_set"
    assert change_set_activity["entityId"] == change_set_id

    # The JSON-LD relation is dereferenceable to the immutable review audit,
    # where operation identity, source relation, acceptor, and decision time live.
    audit_response = client.get(
        f"/graph-drafts/{change_set_id}",
        headers=admin_auth_headers,
    )
    assert audit_response.status_code == 200, audit_response.text
    audit = audit_response.json()["data"]
    assert audit["status"] == "committed"
    assert audit["purpose"] == "member_checkpoint_alignment"
    assert audit["source_note_id"] == checkpoint_id
    assert audit["committed_by"] == checkpoint_creator_id
    [audited_operation] = audit["operations"]
    assert audited_operation["operation_id"] == operation_id
    assert audited_operation["status"] == "applied"
    assert audited_operation["source_refs"] == [
        {
            "source_note_ids": [checkpoint_id],
            "source_note_ids_resolution": "explicit",
        }
    ]
    assert audited_operation["acceptance_mode"] == "human_selected"
    assert audited_operation["accepted_by_user_id"] == checkpoint_creator_id
    assert audited_operation["accepted_at"] == accepted_at


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


def test_record_export_provenance_includes_exploration_nodes_and_falsification_fields():
    project_id = uuid4()
    question_id = UUID("11111111-c0de-c0de-c0de-111111111111")
    claim_id = UUID("22222222-c0de-c0de-c0de-222222222222")
    node_id = UUID("33333333-c0de-c0de-c0de-333333333333")
    question = Question(
        question_id=question_id,
        project_id=project_id,
        text="Which path failed?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
    )
    claim = Claim(
        claim_id=claim_id,
        project_id=project_id,
        statement="The bootstrap path was underpowered.",
        confidence=55,
        status=ClaimStatus.TESTING,
        falsification_criteria="A larger bootstrap sample separates the groups.",
        verification_plan="Repeat with the preregistered mixed model and bootstrap.",
        refuting_outcome="Bootstrap intervals separate cleanly.",
        answers_question_ids=[question_id],
    )
    exploration_node = ExplorationNode(
        node_id=node_id,
        project_id=project_id,
        node_type=ExplorationNodeType.DEAD_END,
        title="Bootstrap dead end",
        target=EntityRef(entity_type=EntityType.CLAIM, entity_id=claim_id),
        status=ExplorationNodeStatus.COMMITTED,
        evidence_refs=[EntityRef(entity_type=EntityType.QUESTION, entity_id=question_id)],
        hypothesis="Bootstrap would make the effect obvious.",
        failure_mode="Intervals stayed wide.",
        lesson="Model first, bootstrap for presentation later.",
    )

    document = build_record_export_provenance_document(
        "http://example.test",
        RecordExportRecords(
            questions=[question],
            claims=[claim],
            exploration_nodes=[exploration_node],
        ),
    )

    claim_node = _node_by_id(document, f"http://example.test/claims/{claim_id}")
    exploration_jsonld = _node_by_id(
        document,
        f"http://example.test/exploration-nodes/{node_id}",
    )
    assert claim_node["falsificationCriteria"] == claim.falsification_criteria
    assert claim_node["verificationPlan"] == claim.verification_plan
    assert claim_node["refutingOutcome"] == claim.refuting_outcome
    assert _node_type_includes(exploration_jsonld, "lab:ExplorationNode")
    assert exploration_jsonld["explorationNodeType"] == "dead_end"
    assert exploration_jsonld["target"] == {
        "@id": f"http://example.test/claims/{claim_id}",
        "entityType": "claim",
        "entityId": str(claim_id),
    }
    assert exploration_jsonld["evidence"] == [
        {"@id": f"http://example.test/questions/{question_id}"}
    ]
    assert exploration_jsonld["failureMode"] == "Intervals stayed wide."
    assert exploration_jsonld["lesson"] == "Model first, bootstrap for presentation later."


def test_record_export_provenance_includes_exploration_edges_and_invalidation_links():
    project_id = uuid4()
    question_id = UUID("11111111-eeee-eeee-eeee-111111111111")
    claim_id = UUID("22222222-eeee-eeee-eeee-222222222222")
    parent_node_id = UUID("33333333-eeee-eeee-eeee-333333333333")
    dependency_node_id = UUID("44444444-eeee-eeee-eeee-444444444444")
    pivot_node_id = UUID("55555555-eeee-eeee-eeee-555555555555")
    question = Question(
        question_id=question_id,
        project_id=project_id,
        text="Which path should supersede the old claim?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
    )
    claim = Claim(
        claim_id=claim_id,
        project_id=project_id,
        statement="The old bootstrap interpretation still holds.",
        confidence=35,
        status=ClaimStatus.TESTING,
    )
    parent_node = ExplorationNode(
        node_id=parent_node_id,
        project_id=project_id,
        node_type=ExplorationNodeType.DECISION,
        title="Try bootstrap first",
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question_id),
        status=ExplorationNodeStatus.COMMITTED,
        choice="Bootstrap",
        alternatives_considered=["Mixed model"],
        rationale="It was the fastest path.",
    )
    dependency_node = ExplorationNode(
        node_id=dependency_node_id,
        project_id=project_id,
        node_type=ExplorationNodeType.DEAD_END,
        title="Bootstrap stalled",
        target=EntityRef(entity_type=EntityType.CLAIM, entity_id=claim_id),
        status=ExplorationNodeStatus.COMMITTED,
        hypothesis="Bootstrap would support the claim.",
        failure_mode="Intervals stayed too wide.",
        lesson="The claim needs a stronger analysis.",
    )
    pivot_node = ExplorationNode(
        node_id=pivot_node_id,
        project_id=project_id,
        node_type=ExplorationNodeType.PIVOT,
        title="Pivot away from stale claim",
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question_id),
        status=ExplorationNodeStatus.COMMITTED,
        trigger="The dead end refuted the old bootstrap path.",
        rationale="The stale claim should no longer guide the analysis.",
        parent_node_ids=[parent_node_id],
        also_depends_on_node_ids=[dependency_node_id],
        invalidates_claim_id=claim_id,
    )

    document = build_record_export_provenance_document(
        "http://example.test",
        RecordExportRecords(
            questions=[question],
            claims=[claim],
            exploration_nodes=[parent_node, dependency_node, pivot_node],
        ),
    )

    pivot_jsonld = _node_by_id(
        document,
        f"http://example.test/exploration-nodes/{pivot_node_id}",
    )
    assert pivot_jsonld["wasDerivedFrom"] == [
        {"@id": f"http://example.test/exploration-nodes/{parent_node_id}"}
    ]
    assert pivot_jsonld["alsoDependsOn"] == [
        {"@id": f"http://example.test/exploration-nodes/{dependency_node_id}"}
    ]
    assert pivot_jsonld["invalidates"] == {
        "@id": f"http://example.test/claims/{claim_id}"
    }


def test_record_export_provenance_rejects_two_exploration_invalidation_refs():
    project_id = uuid4()
    question_id = UUID("11111111-f00d-f00d-f00d-111111111111")
    claim_id = UUID("22222222-f00d-f00d-f00d-222222222222")
    invalidated_node_id = UUID("33333333-f00d-f00d-f00d-333333333333")
    pivot_node_id = UUID("44444444-f00d-f00d-f00d-444444444444")
    pivot_node = ExplorationNode(
        node_id=pivot_node_id,
        project_id=project_id,
        node_type=ExplorationNodeType.PIVOT,
        title="Impossible double invalidation",
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=question_id),
        status=ExplorationNodeStatus.COMMITTED,
        trigger="Malformed legacy data.",
        rationale="The exporter should not silently drop either link.",
        invalidates_node_id=invalidated_node_id,
        invalidates_claim_id=claim_id,
    )

    with pytest.raises(ValueError, match="at most one"):
        build_record_export_provenance_document(
            "http://example.test",
            RecordExportRecords(exploration_nodes=[pivot_node]),
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
    assert claim_node["wasGeneratedBy"] == {"@id": draft_iri}
    assert claim_node["wasAttributedTo"] == {
        "@id": f"http://example.test/agents/{creator_user_id}"
    }
    assert viz_node["origin"] == "user_revised"
    assert viz_node["changeSet"] == {"@id": draft_iri}
    assert viz_node["wasInformedBy"] == {"@id": draft_iri}
    before_iri = f"http://example.test/visualizations/{viz_id}/versions/before/{change_set_id}"
    assert viz_node["wasRevisionOf"] == {"@id": before_iri}
    # The wasRevisionOf target must resolve to a node present in @graph (raises if absent).
    before_node = _node_by_id(document, before_iri)
    assert before_node["origin"] == "ai_suggested"
    assert before_node["changeSet"] == {"@id": draft_iri}
    assert draft_node["wasAssociatedWith"] == {"@id": agent_iri}
    assert _node_type_includes(agent_node, "prov:SoftwareAgent")
    assert agent_node["aiProvider"] == "openai"
    assert agent_node["aiModel"] == "fake-gpt"
    assert agent_node["aiPromptVersion"] == "multimodal-graph-draft-v1"


def test_goal_ara_logic_layer_materializes_origin_nodes():
    goal_id = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    change_set_id = UUID("99999999-8888-4777-8666-555555555555")
    goal_link_id = UUID("11111111-2222-4333-8444-555555555555")
    goal = Goal(
        goal_id=goal_id,
        project_id=uuid4(),
        goal_type=GoalType.PAPER,
        title="AI-revised goal",
        status=GoalStatus.IN_PROGRESS,
        origin=EntityOrigin.USER_REVISED,
        change_set_id=change_set_id,
        origin_provider="openai",
        origin_model="fake-gpt",
        origin_prompt_version="goal-draft-v1",
    )
    goal_link = GoalLink(
        link_id=goal_link_id,
        goal_id=goal_id,
        target=EntityRef(entity_type=EntityType.QUESTION, entity_id=uuid4()),
        relation=GoalRelation.METHODS,
        link_status=GoalLinkStatus.COMMITTED,
        slot="Methods",
    )

    document = build_ara_artifact_document(
        "http://example.test",
        scope_type=EntityType.GOAL,
        scope_id=goal_id,
        records=AraArtifactRecords(
            questions=[],
            datasets=[],
            analyses=[],
            claims=[],
            claim_edges=[],
            notes=[],
            visualizations=[],
            entity_versions=[],
            goal=goal,
            goal_links=[goal_link],
        ),
        generated_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
        layer_name="logic",
    )

    goal_iri = f"http://example.test/goals/{goal_id}"
    draft_iri = f"http://example.test/graph-drafts/{change_set_id}"
    agent_iri = f"{draft_iri}/software-agent"
    before_iri = f"{goal_iri}/versions/before/{change_set_id}"
    goal_link_iri = f"{goal_iri}/links/{goal_link_id}"
    goal_node = _node_by_id(document, goal_iri)
    goal_link_node = _node_by_id(document, goal_link_iri)
    draft_node = _node_by_id(document, draft_iri)
    agent_node = _node_by_id(document, agent_iri)
    before_node = _node_by_id(document, before_iri)

    assert document["@type"] == "lab:AraLayer"
    assert goal_node["@type"] == "lab:Goal"
    assert goal_node["goalType"] == "paper"
    assert {
        "lab:goalType/paper",
        "lab:goalStatus/in_progress",
        "lab:entityOrigin/user_revised",
    } <= _classification_ids(goal_node)
    assert goal_node["origin"] == "user_revised"
    assert goal_node["wasRevisionOf"] == {"@id": before_iri}
    assert goal_node["changeSet"] == {"@id": draft_iri}
    assert before_node["origin"] == "ai_suggested"
    assert before_node["changeSet"] == {"@id": draft_iri}
    assert draft_node["wasAssociatedWith"] == {"@id": agent_iri}
    assert _node_type_includes(agent_node, "prov:SoftwareAgent")
    assert agent_node["aiProvider"] == "openai"
    assert agent_node["aiModel"] == "fake-gpt"
    assert agent_node["aiPromptVersion"] == "goal-draft-v1"
    assert goal_link_node["@type"] == "lab:GoalLink"
    assert _classification_ids(goal_link_node) == {
        "lab:goalRelation/methods",
        "lab:goalLinkStatus/committed",
    }


def test_ara_trace_uses_the_entity_version_profile_class():
    question_id = uuid4()
    version = EntityVersion(
        version_id=uuid4(),
        entity_type=EntityType.QUESTION,
        entity_id=question_id,
        version_number=2,
        snapshot={"text": "Earlier wording"},
    )

    document = build_ara_artifact_document(
        "http://example.test",
        scope_type=EntityType.QUESTION,
        scope_id=question_id,
        records=AraArtifactRecords(
            questions=[],
            datasets=[],
            analyses=[],
            claims=[],
            claim_edges=[],
            notes=[],
            visualizations=[],
            entity_versions=[version],
        ),
        generated_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
        layer_name="trace",
    )

    version_node = _node_by_id(
        document,
        f"http://example.test/questions/{question_id}/versions/2",
    )
    assert version_node["@type"] == "lab:EntityVersion"


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

    assert dataset_node["wasGeneratedBy"] == {"@id": commit_id}
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

    assert dataset_node["wasAttributedTo"] == {"@id": creator_iri}
    assert creator_node["@type"] == "prov:Person"
    assert creator_node["userId"] == str(creator_user_id)
    assert creator_node["actedOnBehalfOf"] == {
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
    assert commit_node["used"] == [{"@id": file_node["@id"]}]
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

    assert {"@id": artifact.uri} in commit_node["used"]
    assert commit_node["wasInformedBy"] == [{"@id": activity.uri}]
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

    assert {"@id": artifact.uri} in commit_node["used"]
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

    assert "used" not in commit_node


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

    assert analysis_node["used"] == [{"@id": f"http://example.test/datasets/{dataset_id}"}]
    assert analysis_node["wasInformedBy"] == [{"@id": run.uri}]
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
    assert context["contentUrl"] == {"@id": "schema:contentUrl", "@type": "@id"}
    assert context["fileName"] == "lab:fileName"
    assert context["encodingFormat"] == "schema:encodingFormat"
    assert context["contentSize"] == "schema:contentSize"
    assert context["sha256"] == "lab:checksum"

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

    assert analysis_node["@type"] == "lab:Analysis"
    assert analysis_node["executedAt"] == "2026-04-23T00:00:00+00:00"
    assert analysis_node["used"] == [
        {"@id": "http://example.test/datasets/66666666-6666-6666-6666-666666666666"}
    ]
    assert "environmentHash" not in analysis_node
    assert "wasAssociatedWith" not in analysis_node
    assert claim_node["supportsDataset"] == [
        {"@id": "http://example.test/datasets/66666666-6666-6666-6666-666666666666"}
    ]
    assert claim_node["supportsAnalysis"] == [
        {"@id": "http://example.test/analyses/55555555-5555-5555-5555-555555555555"}
    ]
    assert viz_node["wasGeneratedBy"] == {
        "@id": "http://example.test/analyses/55555555-5555-5555-5555-555555555555"
    }
    assert viz_node["relatedClaim"] == [
        {"@id": "http://example.test/claims/77777777-7777-7777-7777-777777777777"}
    ]
    assert viz_node["groundingDataset"] == [
        {"@id": "http://example.test/datasets/66666666-6666-6666-6666-666666666666"}
    ]
    assert viz_node["wasDerivedFrom"] == [
        {"@id": "http://example.test/datasets/66666666-6666-6666-6666-666666666666"}
    ]
    assert viz_node["contentUrl"] == (
        "http://example.test/visualizations/88888888-8888-8888-8888-888888888888/file/download"
    )
    assert viz_node["fileName"] == "signal.png"
    assert viz_node["encodingFormat"] == "image/png"
    assert viz_node["contentSize"] == 128
    assert viz_node["checksum"] == "abc123"
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

    assert analysis_node["wasAssociatedWith"] == {"@id": actor_iri}
    assert dataset_node["wasAttributedTo"] == {"@id": actor_iri}
    assert claim_node["wasAttributedTo"] == {"@id": actor_iri}
    assert viz_node["wasAttributedTo"] == {"@id": actor_iri}
    assert _node_type_includes(actor_node, "prov:Person")
    assert actor_node["actedOnBehalfOf"] == {
        "@id": supervisor_iri,
        "supervisionStartedAt": "2026-01-01T00:00:00+00:00",
    }
    assert _node_type_includes(_node_by_id(document, supervisor_iri), "prov:Person")
