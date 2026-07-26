"""Builder-wide coverage for custom IRIs in provenance JSON-LD."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

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
    Visualization,
    VisualizationAsset,
)
from lab_tracker.provenance import (
    AraArtifactRecords,
    build_analysis_provenance_document,
    build_ara_artifact_document,
    build_claim_provenance_document,
    build_dataset_provenance_document,
    build_record_export_provenance_document,
)
from lab_tracker.vocabulary import build_context, build_terms_document, terms_namespace

_BASE = "https://lab.example.org"
_WHEN = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
_LAYER_MAP_KEYS = {"logic", "src", "trace", "evidence"}


def _uuid(value: int) -> UUID:
    return UUID(int=value)


def _builder_documents() -> dict[str, dict[str, object]]:
    project_id = _uuid(1)
    question_id = _uuid(2)
    parent_question_id = _uuid(3)
    session_id = _uuid(4)
    dataset_id = _uuid(5)
    analysis_id = _uuid(6)
    claim_id = _uuid(7)
    target_claim_id = _uuid(8)
    visualization_id = _uuid(9)
    note_id = _uuid(10)
    exploration_id = _uuid(11)
    parent_exploration_id = _uuid(12)
    goal_id = _uuid(13)
    user_id = _uuid(14)
    change_set_id = _uuid(15)

    question = Question(
        question_id=question_id,
        project_id=project_id,
        text="Does the perturbation change the response?",
        question_type=QuestionType.HYPOTHESIS_DRIVEN,
        hypothesis="The perturbation increases the response.",
        status=QuestionStatus.ACTIVE,
        parent_question_ids=[parent_question_id],
        created_at=_WHEN,
        created_by_user_id=user_id,
    )
    question_link = QuestionLink(
        question_id=question_id,
        role=QuestionLinkRole.PRIMARY,
        outcome_status=OutcomeStatus.SUPPORTS,
    )
    entity_artifact = ExternalArtifactReference(
        kind=ExternalArtifactKind.ENTITY,
        source_system="archive",
        uri="urn:example:dataset-input",
        content_hash="sha256:input",
    )
    activity_artifact = ExternalArtifactReference(
        kind=ExternalArtifactKind.ACTIVITY,
        source_system="workflow",
        uri="urn:example:acquisition-run",
        content_hash="sha256:run",
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=project_id,
        commit_hash="dataset-commit",
        primary_question_id=question_id,
        question_links=[question_link],
        commit_manifest=DatasetCommitManifest(
            files=[
                DatasetFile(
                    file_id=_uuid(16),
                    path="data/response.nwb",
                    checksum="sha256:data",
                    size_bytes=128,
                )
            ],
            external_artifacts=[entity_artifact, activity_artifact],
            metadata={"instrument": "rig-a"},
            nwb_metadata={"session_description": "response"},
            bids_metadata={"task": "perturbation"},
            note_ids=[note_id],
            question_links=[question_link],
            source_session_id=session_id,
        ),
        status=DatasetStatus.COMMITTED,
        created_at=_WHEN,
        created_by_user_id=user_id,
    )
    analysis = Analysis(
        analysis_id=analysis_id,
        project_id=project_id,
        dataset_ids=[dataset_id],
        method_hash="method-hash",
        code_version="code-version",
        environment_hash="environment-hash",
        external_artifacts=[entity_artifact, activity_artifact],
        executed_by_user_id=user_id,
        executed_at=_WHEN,
        status=AnalysisStatus.COMMITTED,
        created_at=_WHEN,
    )
    citation = ExternalArtifactReference(
        source_system="doi",
        uri="https://doi.org/10.0000/example",
        content_hash="sha256:paper",
    )
    claim = Claim(
        claim_id=claim_id,
        project_id=project_id,
        statement="The perturbation increases the response.",
        confidence=85,
        status=ClaimStatus.SUPPORTED,
        falsification_criteria="The preregistered interval includes zero.",
        verification_plan="Repeat with an independent cohort.",
        refuting_outcome="The independent cohort shows a decrease.",
        supported_by_dataset_ids=[dataset_id],
        supported_by_analysis_ids=[analysis_id],
        answers_question_ids=[question_id],
        external_citations=[citation],
        created_at=_WHEN,
        created_by_user_id=user_id,
    )
    target_claim = Claim(
        claim_id=target_claim_id,
        project_id=project_id,
        statement="The perturbation has no effect.",
        confidence=25,
        status=ClaimStatus.REJECTED,
        created_at=_WHEN,
    )
    claim_edge = ClaimEdge(
        edge_id=_uuid(17),
        claim_id=claim_id,
        target_claim_id=target_claim_id,
        relation=ClaimRelation.REFUTES,
        created_at=_WHEN,
    )
    visualization = Visualization(
        viz_id=visualization_id,
        analysis_id=analysis_id,
        dataset_ids=[dataset_id],
        viz_type="figure",
        file_path="figures/response.svg",
        caption="Response by perturbation condition.",
        related_claim_ids=[claim_id],
        asset=VisualizationAsset(
            storage_id=_uuid(18),
            filename="response.svg",
            content_type="image/svg+xml",
            size_bytes=256,
            checksum="sha256:figure",
        ),
        created_at=_WHEN,
        created_by_user_id=user_id,
    )
    note = Note(
        note_id=note_id,
        project_id=project_id,
        raw_content="The treated traces separated cleanly.",
        transcribed_text="The treated traces separated cleanly.",
        targets=[EntityRef(entity_type=EntityType.CLAIM, entity_id=claim_id)],
        status=NoteStatus.COMMITTED,
        created_at=_WHEN,
        created_by_user_id=user_id,
    )
    session = Session(
        session_id=session_id,
        project_id=project_id,
        session_type=SessionType.SCIENTIFIC,
        status=SessionStatus.CLOSED,
        primary_question_id=question_id,
        started_at=_WHEN,
        ended_at=_WHEN,
        created_by_user_id=user_id,
    )
    exploration = ExplorationNode(
        node_id=exploration_id,
        project_id=project_id,
        node_type=ExplorationNodeType.DECISION,
        title="Use the preregistered model",
        target=EntityRef(entity_type=EntityType.CLAIM, entity_id=claim_id),
        status=ExplorationNodeStatus.COMMITTED,
        choice="Use the hierarchical model.",
        alternatives_considered=["Use a pooled model."],
        rationale="The design is nested.",
        evidence_refs=[EntityRef(entity_type=EntityType.DATASET, entity_id=dataset_id)],
        hypothesis="Partial pooling improves estimation.",
        failure_mode="The pooled model hid between-session variation.",
        lesson="Preserve the acquisition hierarchy.",
        tooling_context="Python analysis environment.",
        trigger="A divergent residual pattern.",
        invalidates_claim_id=target_claim_id,
        parent_node_ids=[parent_exploration_id],
        also_depends_on_node_ids=[parent_exploration_id],
        created_at=_WHEN,
        created_by_user_id=user_id,
    )
    goal_link = GoalLink(
        link_id=_uuid(19),
        goal_id=goal_id,
        target=EntityRef(entity_type=EntityType.CLAIM, entity_id=claim_id),
        relation=GoalRelation.SUPPORTING_EVIDENCE,
        link_status=GoalLinkStatus.COMMITTED,
        slot="result-1",
        created_at=_WHEN,
    )
    goal = Goal(
        goal_id=goal_id,
        project_id=project_id,
        goal_type=GoalType.PAPER,
        title="Perturbation paper",
        summary="Compile the perturbation result.",
        status=GoalStatus.IN_PROGRESS,
        external_ref="https://example.org/manuscript",
        links=[goal_link],
        created_at=_WHEN,
        created_by_user_id=user_id,
    )
    entity_version = EntityVersion(
        version_id=_uuid(20),
        entity_type=EntityType.CLAIM,
        entity_id=claim_id,
        version_number=1,
        snapshot={},
        change_set_id=change_set_id,
        committed_at=_WHEN,
        created_at=_WHEN,
    )

    claims = [claim, target_claim]
    record_export_records = RecordExportRecords(
        questions=[question],
        datasets=[dataset],
        sessions=[session],
        analyses=[analysis],
        claims=claims,
        claim_edges=[claim_edge],
        exploration_nodes=[exploration],
        notes=[note],
        visualizations=[visualization],
    )
    ara_records = AraArtifactRecords(
        questions=[question],
        datasets=[dataset],
        analyses=[analysis],
        claims=claims,
        claim_edges=[claim_edge],
        notes=[note],
        visualizations=[visualization],
        entity_versions=[entity_version],
        exploration_nodes=[exploration],
        goal=goal,
        goal_links=[goal_link],
    )
    return {
        "dataset": build_dataset_provenance_document(_BASE, dataset),
        "analysis": build_analysis_provenance_document(
            _BASE,
            analysis,
            datasets=[dataset],
            claims=claims,
            visualizations=[visualization],
            claim_edges=[claim_edge],
        ),
        "claim": build_claim_provenance_document(
            _BASE,
            claim,
            analyses=[analysis],
            datasets=[dataset],
            questions=[question],
            visualizations=[visualization],
            claim_edges=[claim_edge],
        ),
        "record_export": build_record_export_provenance_document(
            _BASE,
            record_export_records,
        ),
        "ara": build_ara_artifact_document(
            _BASE,
            scope_type=EntityType.GOAL,
            scope_id=goal_id,
            records=ara_records,
            generated_at=_WHEN,
        ),
    }


def _compact_mapping(context: dict[str, object], key: str) -> str | None:
    value = context.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        iri = value.get("@id")
        return iri if isinstance(iri, str) else None
    return None


def _walk_emitted_vocabulary(
    value: object,
    *,
    context: dict[str, object],
    custom_properties: set[str],
    custom_types: set[str],
    custom_values: set[str],
    unknown_keys: set[str],
) -> None:
    if isinstance(value, list):
        for item in value:
            _walk_emitted_vocabulary(
                item,
                context=context,
                custom_properties=custom_properties,
                custom_types=custom_types,
                custom_values=custom_values,
                unknown_keys=unknown_keys,
            )
        return
    if not isinstance(value, dict):
        return

    nested_context = value.get("@context")
    if isinstance(nested_context, dict):
        context = nested_context

    raw_types = value.get("@type", [])
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    if isinstance(raw_types, list):
        custom_types.update(
            item for item in raw_types if isinstance(item, str) and item.startswith("lab:")
        )

    raw_id = value.get("@id")
    if isinstance(raw_id, str) and raw_id.startswith("lab:"):
        custom_values.add(raw_id)

    for key, child in value.items():
        if key.startswith("@"):
            if key not in {"@context", "@id", "@type"}:
                _walk_emitted_vocabulary(
                    child,
                    context=context,
                    custom_properties=custom_properties,
                    custom_types=custom_types,
                    custom_values=custom_values,
                    unknown_keys=unknown_keys,
                )
            continue
        compact = _compact_mapping(context, key)
        if compact is None:
            if key not in _LAYER_MAP_KEYS:
                unknown_keys.add(key)
        elif compact.startswith("lab:"):
            custom_properties.add(compact)

        mapping = context.get(key)
        is_json = isinstance(mapping, dict) and mapping.get("@type") == "@json"
        if not is_json or key == "layers":
            _walk_emitted_vocabulary(
                child,
                context=context,
                custom_properties=custom_properties,
                custom_types=custom_types,
                custom_values=custom_values,
                unknown_keys=unknown_keys,
            )


def test_every_builder_custom_iri_resolves_to_a_typed_terms_entry():
    documents = _builder_documents()
    assert set(documents) == {"dataset", "analysis", "claim", "record_export", "ara"}

    custom_properties: set[str] = set()
    custom_types: set[str] = set()
    custom_values: set[str] = set()
    unknown_keys: set[str] = set()
    context = build_context(_BASE)
    for document in documents.values():
        _walk_emitted_vocabulary(
            document,
            context=context,
            custom_properties=custom_properties,
            custom_types=custom_types,
            custom_values=custom_values,
            unknown_keys=unknown_keys,
        )

    assert not unknown_keys
    emitted_iris = custom_properties | custom_types | custom_values
    namespace = terms_namespace(_BASE)
    expanded = {
        f"{namespace}{compact.removeprefix('lab:')}" for compact in emitted_iris
    }
    terms_document = build_terms_document(_BASE)
    terms_by_id = {node["@id"]: node for node in terms_document["@graph"]}
    for iri in expanded:
        node = terms_by_id[iri]
        assert node["@type"] in {
            "rdf:Property",
            "rdfs:Class",
            "skos:Concept",
            "skos:ConceptScheme",
        }
        assert str(node["comment"]).strip()

    assert custom_types == {
        "lab:AcquisitionSession",
        "lab:Analysis",
        "lab:AraArtifact",
        "lab:AraLayer",
        "lab:Claim",
        "lab:ClaimRelation",
        "lab:Dataset",
        "lab:EntityVersion",
        "lab:ExplorationNode",
        "lab:ForensicBinding",
        "lab:Goal",
        "lab:GoalLink",
        "lab:Note",
        "lab:QuestionLink",
        "lab:ResearchQuestion",
        "lab:Visualization",
    }
    assert {
        "lab:claim",
        "lab:codeEnvironment",
        "lab:crossLayerBinding",
        "lab:dataset",
        "lab:goalType",
        "lab:slot",
        "lab:summary",
    } <= custom_properties
