"""JSON-LD / PROV-O export helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

from lab_tracker.models import (
    Analysis,
    Claim,
    ClaimEdge,
    Dataset,
    DatasetCommitManifest,
    DatasetFile,
    EntityOrigin,
    EntityRef,
    EntityType,
    EntityVersion,
    ExplorationNode,
    ExternalArtifactKind,
    ExternalArtifactReference,
    Goal,
    GoalLink,
    Note,
    Question,
    QuestionLink,
    QuestionLinkRole,
    RecordExportRecords,
    Session,
    SupervisionEdge,
    Visualization,
)
from lab_tracker.provenance_ingestion import external_artifacts_from_metadata

_logger = logging.getLogger(__name__)


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _resource_iri(base_url: str, resource: str, entity_id: object) -> str:
    normalized = _normalize_base_url(base_url)
    return f"{normalized}/{resource}/{entity_id}"


def _synthetic_child_iri(parent_iri: str, *segments: object) -> str:
    encoded = [quote(str(segment), safe="") for segment in segments]
    return f"{parent_iri}/{'/'.join(encoded)}"


def _terms_iri(base_url: str) -> str:
    normalized = _normalize_base_url(base_url)
    return f"{normalized}/terms#"


def _context(base_url: str) -> dict[str, object]:
    return {
        "prov": "http://www.w3.org/ns/prov#",
        "lab": _terms_iri(base_url),
        "actedOnBehalfOf": {"@id": "prov:actedOnBehalfOf", "@type": "@id"},
        "analysis": {"@id": "lab:analysis", "@type": "@id"},
        "answersQuestion": {"@id": "lab:answersQuestion", "@type": "@id"},
        "caption": "lab:caption",
        "checksum": "lab:checksum",
        "codeVersion": "lab:codeVersion",
        "commitHash": "lab:commitHash",
        "confidence": "lab:confidence",
        "contentSize": "lab:contentSize",
        "contentUrl": {"@id": "lab:contentUrl", "@type": "@id"},
        "contentType": "lab:contentType",
        "createdAt": "lab:createdAt",
        "crossLayerBinding": {"@id": "lab:crossLayerBinding", "@type": "@id"},
        "aiModel": "lab:aiModel",
        "aiPromptVersion": "lab:aiPromptVersion",
        "aiProvider": "lab:aiProvider",
        "changeSet": {"@id": "lab:changeSet", "@type": "@id"},
        "cites": {"@id": "lab:cites", "@type": "@id"},
        "claimRelation": {"@id": "lab:claimRelation", "@type": "@id"},
        "claimRelationSource": {"@id": "lab:claimRelationSource", "@type": "@id"},
        "claimRelationTarget": {"@id": "lab:claimRelationTarget", "@type": "@id"},
        "claimRelationType": "lab:claimRelationType",
        "encodingFormat": "lab:encodingFormat",
        "environmentHash": "lab:environmentHash",
        "entityId": "lab:entityId",
        "entityType": "lab:entityType",
        "endedAt": "lab:endedAt",
        "executedAt": "lab:executedAt",
        "evidence": {"@id": "lab:evidence", "@type": "@id"},
        "explorationNode": {"@id": "lab:explorationNode", "@type": "@id"},
        "explorationNodeType": "lab:explorationNodeType",
        "externalArtifact": {"@id": "lab:externalArtifact", "@type": "@id"},
        "externalContentHash": "lab:externalContentHash",
        "externalMetadata": {"@id": "lab:externalMetadata", "@type": "@json"},
        "externalSourceSystem": "lab:externalSourceSystem",
        "externalUri": {"@id": "lab:externalUri", "@type": "@id"},
        "fileName": "lab:fileName",
        "filePath": "lab:filePath",
        "filename": "lab:filename",
        "falsificationCriteria": "lab:falsificationCriteria",
        "failureMode": "lab:failureMode",
        "groundingDataset": {"@id": "lab:groundingDataset", "@type": "@id"},
        "generatedAt": "lab:generatedAt",
        "goalLink": {"@id": "lab:goalLink", "@type": "@id"},
        "layer": "lab:layer",
        "layers": {"@id": "lab:layers", "@type": "@json"},
        "metadata": {"@id": "lab:metadata", "@type": "@json"},
        "methodHash": "lab:methodHash",
        "note": {"@id": "lab:note", "@type": "@id"},
        "nwbMetadata": {"@id": "lab:nwbMetadata", "@type": "@json"},
        "outcomeStatus": "lab:outcomeStatus",
        "origin": "lab:origin",
        "choice": "lab:choice",
        "alternativesConsidered": {"@id": "lab:alternativesConsidered", "@type": "@json"},
        "alsoDependsOn": {"@id": "lab:alsoDependsOn", "@type": "@id"},
        "hypothesis": "lab:hypothesis",
        "invalidates": {"@id": "lab:invalidates", "@type": "@id"},
        "lesson": "lab:lesson",
        "primaryQuestion": {"@id": "lab:primaryQuestion", "@type": "@id"},
        "question": {"@id": "lab:question", "@type": "@id"},
        "questionLink": {"@id": "lab:questionLink", "@type": "@id"},
        "questionType": "lab:questionType",
        "rawContent": "lab:rawContent",
        "relatedClaim": {"@id": "lab:relatedClaim", "@type": "@id"},
        "refutingOutcome": "lab:refutingOutcome",
        "rationale": "lab:rationale",
        "role": "lab:role",
        "scope": {"@id": "lab:scope", "@type": "@id"},
        "sizeBytes": "lab:sizeBytes",
        "sourceSession": {"@id": "lab:sourceSession", "@type": "@id"},
        "statement": "lab:statement",
        "status": "lab:status",
        "supportsAnalysis": {"@id": "lab:supportsAnalysis", "@type": "@id"},
        "supportsDataset": {"@id": "lab:supportsDataset", "@type": "@id"},
        "sha256": "lab:sha256",
        "sessionType": "lab:sessionType",
        "supervisionEndedAt": "lab:supervisionEndedAt",
        "supervisionStartedAt": "lab:supervisionStartedAt",
        "startedAt": "lab:startedAt",
        "target": {"@id": "lab:target", "@type": "@id"},
        "terminalReason": "lab:terminalReason",
        "text": "lab:text",
        "toolingContext": "lab:toolingContext",
        "trigger": "lab:trigger",
        "transcribedText": "lab:transcribedText",
        "updatedAt": "lab:updatedAt",
        "userId": "lab:userId",
        "versionNumber": "lab:versionNumber",
        "verificationPlan": "lab:verificationPlan",
        "vizType": "lab:vizType",
        "wasAttributedTo": {"@id": "prov:wasAttributedTo", "@type": "@id"},
        "wasDerivedFrom": {"@id": "prov:wasDerivedFrom", "@type": "@id"},
        "wasGeneratedBy": {"@id": "prov:wasGeneratedBy", "@type": "@id"},
        "wasRevisionOf": {"@id": "prov:wasRevisionOf", "@type": "@id"},
        "bidsMetadata": {"@id": "lab:bidsMetadata", "@type": "@json"},
    }


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _coerce_user_id(value: UUID | str | None) -> str | None:
    if value is None:
        return None
    user_id = str(value).strip()
    return user_id or None


def _creator_user_id(created_by_user_id: UUID | None, created_by: str | None) -> str | None:
    return _coerce_user_id(created_by_user_id) or _coerce_user_id(created_by)


def _agent_iri(base_url: str, user_id: str) -> str:
    normalized = _normalize_base_url(base_url)
    return f"{normalized}/agents/{quote(user_id, safe='')}"


def _attribution_value(base_url: str, user_ids: list[str]) -> dict[str, str] | list[dict[str, str]]:
    refs = [{"@id": _agent_iri(base_url, user_id)} for user_id in user_ids]
    if len(refs) == 1:
        return refs[0]
    return refs


def _unique_user_ids(user_ids: list[str | None]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for user_id in user_ids:
        if user_id is None or user_id in seen:
            continue
        seen.add(user_id)
        unique.append(user_id)
    return unique


def _person_node(base_url: str, user_id: str) -> dict[str, object]:
    return {
        "@id": _agent_iri(base_url, user_id),
        "@type": ["prov:Agent", "prov:Person"],
        "userId": user_id,
    }


def _draft_activity_iri(base_url: str, change_set_id: UUID) -> str:
    return _resource_iri(base_url, "graph-drafts", change_set_id)


def _software_agent_iri(base_url: str, change_set_id: UUID) -> str:
    return _synthetic_child_iri(_draft_activity_iri(base_url, change_set_id), "software-agent")


def _append_id_ref(node: dict[str, object], key: str, ref: dict[str, str]) -> None:
    existing = node.get(key)
    if existing is None:
        node[key] = ref
        return
    if isinstance(existing, list):
        if ref not in existing:
            existing.append(ref)
        return
    if existing != ref:
        node[key] = [existing, ref]


def _append_id_ref_list(node: dict[str, object], key: str, ref: dict[str, str]) -> None:
    existing = node.get(key)
    if existing is None:
        node[key] = [ref]
        return
    if isinstance(existing, list):
        if ref not in existing:
            existing.append(ref)
        return
    if existing != ref:
        node[key] = [existing, ref]


def _node_type_includes(node: dict[str, object], node_type: str) -> bool:
    raw_type = node.get("@type")
    if isinstance(raw_type, list):
        return node_type in raw_type
    return raw_type == node_type


def _entity_origin_value(entity: object) -> EntityOrigin:
    raw_origin = getattr(entity, "origin", EntityOrigin.USER)
    if isinstance(raw_origin, EntityOrigin):
        return raw_origin
    return EntityOrigin(str(raw_origin or EntityOrigin.USER.value))


# Keyed by domain class name so a foreign-key attribute (e.g. Visualization.analysis_id)
# never resolves to the wrong resource; the (own-id attr, resource) must match the
# entity's summary-node @id.
_ORIGIN_ENTITY_RESOURCES = {
    "Dataset": ("dataset_id", "datasets"),
    "Analysis": ("analysis_id", "analyses"),
    "Claim": ("claim_id", "claims"),
    "Visualization": ("viz_id", "visualizations"),
    "Question": ("question_id", "questions"),
    "Note": ("note_id", "notes"),
    "Goal": ("goal_id", "goals"),
    "ExplorationNode": ("node_id", "exploration-nodes"),
}


def _entity_iri(base_url: str, entity: object) -> str | None:
    """Resolve a domain entity to the same @id its summary node uses."""
    spec = _ORIGIN_ENTITY_RESOURCES.get(type(entity).__name__)
    if spec is None:
        return None
    attr, resource = spec
    value = getattr(entity, attr, None)
    if value is None:
        return None
    return _resource_iri(base_url, resource, value)


def _before_revision_iri(entity_iri: str, change_set_id: object) -> str:
    """IRI of the pre-revision (AI-drafted) state a USER_REVISED entity supersedes."""
    return _synthetic_child_iri(entity_iri, "versions", "before", change_set_id)


def _apply_origin_provenance(
    base_url: str,
    node: dict[str, object],
    entity: object,
) -> None:
    origin = _entity_origin_value(entity)
    node["origin"] = origin.value
    change_set_id = getattr(entity, "change_set_id", None)
    if change_set_id is None:
        return
    draft_activity = {"@id": _draft_activity_iri(base_url, change_set_id)}
    node["changeSet"] = draft_activity
    if origin == EntityOrigin.USER_REVISED:
        # The companion "before" node is materialized by _origin_provenance_nodes so
        # this reference resolves within @graph.
        node["prov:wasRevisionOf"] = {"@id": _before_revision_iri(str(node["@id"]), change_set_id)}
        _append_id_ref(node, "prov:wasInformedBy", draft_activity)
    elif _node_type_includes(node, "prov:Activity"):
        _append_id_ref(node, "prov:wasInformedBy", draft_activity)
    else:
        _append_id_ref(node, "prov:wasGeneratedBy", draft_activity)


def _origin_provenance_nodes(base_url: str, entity: object) -> list[dict[str, object]]:
    change_set_id = getattr(entity, "change_set_id", None)
    if change_set_id is None:
        return []
    activity_iri = _draft_activity_iri(base_url, change_set_id)
    agent_iri = _software_agent_iri(base_url, change_set_id)
    activity_node: dict[str, object] = {
        "@id": activity_iri,
        "@type": "prov:Activity",
        "entityType": "graph_change_set",
        "entityId": str(change_set_id),
        "prov:wasAssociatedWith": {"@id": agent_iri},
    }
    agent_node: dict[str, object] = {
        "@id": agent_iri,
        "@type": ["prov:Agent", "prov:SoftwareAgent"],
    }
    if getattr(entity, "origin_provider", None):
        agent_node["aiProvider"] = entity.origin_provider
    if getattr(entity, "origin_model", None):
        agent_node["aiModel"] = entity.origin_model
    if getattr(entity, "origin_prompt_version", None):
        agent_node["aiPromptVersion"] = entity.origin_prompt_version
    nodes = [activity_node, agent_node]
    if _entity_origin_value(entity) == EntityOrigin.USER_REVISED:
        # Materialize the pre-revision state that the entity's prov:wasRevisionOf
        # edge points at, so the JSON-LD graph is self-consistent.
        entity_iri = _entity_iri(base_url, entity)
        if entity_iri is not None:
            nodes.append(
                {
                    "@id": _before_revision_iri(entity_iri, change_set_id),
                    "@type": "prov:Entity",
                    "origin": EntityOrigin.AI_SUGGESTED.value,
                    "changeSet": {"@id": activity_iri},
                    "prov:wasGeneratedBy": {"@id": activity_iri},
                }
            )
    return nodes


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _supervision_edge_active_at(edge: SupervisionEdge, activity_time: datetime) -> bool:
    if edge.started_at > activity_time:
        return False
    return edge.ended_at is None or edge.ended_at > activity_time


def _supervision_relationship(
    base_url: str,
    edge: SupervisionEdge,
) -> dict[str, object]:
    relationship: dict[str, object] = {
        "@id": _agent_iri(base_url, str(edge.supervisor_user_id)),
        "supervisionStartedAt": _isoformat(edge.started_at),
    }
    if edge.ended_at is not None:
        relationship["supervisionEndedAt"] = _isoformat(edge.ended_at)
    return relationship


def _add_person_with_supervision(
    people: dict[str, dict[str, object]],
    base_url: str,
    user_id: str,
    *,
    activity_time: datetime | None,
    supervision_edges: list[SupervisionEdge],
) -> None:
    person_node = people.setdefault(user_id, _person_node(base_url, user_id))
    user_uuid = _uuid_or_none(user_id)
    if user_uuid is None or activity_time is None:
        return

    active_edges = [
        edge
        for edge in supervision_edges
        if edge.supervisee_user_id == user_uuid and _supervision_edge_active_at(edge, activity_time)
    ]
    if not active_edges:
        return

    existing_relationships = person_node.get("prov:actedOnBehalfOf")
    relationships: list[dict[str, object]]
    if isinstance(existing_relationships, list):
        relationships = list(existing_relationships)
    elif isinstance(existing_relationships, dict):
        relationships = [existing_relationships]
    else:
        relationships = []
    relationship_keys = {
        (
            str(relationship.get("@id")),
            str(relationship.get("supervisionStartedAt")),
            str(relationship.get("supervisionEndedAt")),
        )
        for relationship in relationships
    }

    for edge in sorted(active_edges, key=lambda value: (value.started_at, str(value.edge_id))):
        supervisor_user_id = str(edge.supervisor_user_id)
        people.setdefault(supervisor_user_id, _person_node(base_url, supervisor_user_id))
        relationship = _supervision_relationship(base_url, edge)
        key = (
            str(relationship.get("@id")),
            str(relationship.get("supervisionStartedAt")),
            str(relationship.get("supervisionEndedAt")),
        )
        if key in relationship_keys:
            continue
        relationships.append(relationship)
        relationship_keys.add(key)

    if len(relationships) == 1:
        person_node["prov:actedOnBehalfOf"] = relationships[0]
    elif relationships:
        person_node["prov:actedOnBehalfOf"] = relationships


def _file_entity_id(base_url: str, dataset: Dataset, file: DatasetFile) -> str:
    if file.file_id is not None:
        return _resource_iri(base_url, f"datasets/{dataset.dataset_id}/files", file.file_id)
    return _synthetic_child_iri(
        _resource_iri(base_url, "datasets", dataset.dataset_id),
        "provenance",
        "files",
        file.path,
    )


def _question_link_id(base_url: str, dataset: Dataset, question_id: object) -> str:
    return _synthetic_child_iri(
        _resource_iri(base_url, "datasets", dataset.dataset_id),
        "provenance",
        "question-links",
        question_id,
    )


_QUESTION_LINK_ORDER = {QuestionLinkRole.PRIMARY: 0, QuestionLinkRole.SECONDARY: 1}


def _sorted_dataset_files(files: list[DatasetFile]) -> list[DatasetFile]:
    return sorted(files, key=lambda file: (file.path, file.checksum, str(file.file_id or "")))


def _sorted_question_links(question_links: list[QuestionLink]) -> list[QuestionLink]:
    return sorted(
        question_links,
        key=lambda link: (
            _QUESTION_LINK_ORDER.get(link.role, 99),
            str(link.question_id),
        ),
    )


def _dataset_file_node(base_url: str, dataset: Dataset, file: DatasetFile) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _file_entity_id(base_url, dataset, file),
        "@type": "prov:Entity",
        "filePath": file.path,
        "checksum": file.checksum,
    }
    if file.size_bytes is not None:
        node["sizeBytes"] = file.size_bytes
    return node


def _manifest_external_artifacts(
    manifest: DatasetCommitManifest,
) -> list[ExternalArtifactReference]:
    if manifest.external_artifacts:
        return list(manifest.external_artifacts)
    try:
        return external_artifacts_from_metadata(manifest.metadata)
    except ValueError as exc:
        _logger.warning(
            "Ignoring malformed legacy external_artifacts provenance metadata: %s",
            exc,
        )
        return []


def _external_artifact_node(artifact: ExternalArtifactReference) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": artifact.uri,
        "@type": "prov:Entity" if artifact.kind == ExternalArtifactKind.ENTITY else "prov:Activity",
        "externalSourceSystem": artifact.source_system,
        "externalUri": artifact.uri,
        "externalContentHash": artifact.content_hash,
    }
    if artifact.metadata:
        node["externalMetadata"] = artifact.metadata
    return node


def _dataset_question_link_node(base_url: str, dataset: Dataset, link) -> dict[str, object]:
    return {
        "@id": _question_link_id(base_url, dataset, link.question_id),
        "question": {"@id": _resource_iri(base_url, "questions", link.question_id)},
        "role": link.role.value,
        "outcomeStatus": link.outcome_status.value,
    }


def build_dataset_provenance_document(
    base_url: str,
    dataset: Dataset,
    *,
    supervision_edges: list[SupervisionEdge] | None = None,
) -> dict[str, object]:
    dataset_iri = _resource_iri(base_url, "datasets", dataset.dataset_id)
    commit_activity_iri = _synthetic_child_iri(dataset_iri, "provenance", "commit")
    files = _sorted_dataset_files(dataset.commit_manifest.files)
    question_links = _sorted_question_links(dataset.commit_manifest.question_links)
    notes = sorted(dataset.commit_manifest.note_ids, key=str)
    external_artifacts = _manifest_external_artifacts(dataset.commit_manifest)
    people: dict[str, dict[str, object]] = {}
    supervision_edges = supervision_edges or []
    graph: list[dict[str, object]] = []

    dataset_node: dict[str, object] = {
        "@id": dataset_iri,
        "@type": "prov:Entity",
        "prov:wasGeneratedBy": {"@id": commit_activity_iri},
        "commitHash": dataset.commit_hash,
        "status": dataset.status.value,
    }
    _apply_origin_provenance(base_url, dataset_node, dataset)
    if dataset.terminal_reason:
        dataset_node["terminalReason"] = dataset.terminal_reason
    creator_user_id = _creator_user_id(dataset.created_by_user_id, dataset.created_by)
    if creator_user_id is not None:
        dataset_node["prov:wasAttributedTo"] = {"@id": _agent_iri(base_url, creator_user_id)}
        _add_person_with_supervision(
            people,
            base_url,
            creator_user_id,
            activity_time=dataset.created_at,
            supervision_edges=supervision_edges,
        )
    graph.append(dataset_node)

    commit_node: dict[str, object] = {
        "@id": commit_activity_iri,
        "@type": "prov:Activity",
    }

    used_files = [{"@id": _file_entity_id(base_url, dataset, file)} for file in files]
    used_external_entities = [
        {"@id": artifact.uri}
        for artifact in external_artifacts
        if artifact.kind == ExternalArtifactKind.ENTITY
    ]
    used_entities = [*used_files, *used_external_entities]
    if used_entities:
        commit_node["prov:used"] = used_entities

    informed_by = [
        {"@id": artifact.uri}
        for artifact in external_artifacts
        if artifact.kind == ExternalArtifactKind.ACTIVITY
    ]
    if informed_by:
        commit_node["prov:wasInformedBy"] = informed_by

    question_link_refs = [
        {"@id": _question_link_id(base_url, dataset, link.question_id)} for link in question_links
    ]
    if question_link_refs:
        commit_node["questionLink"] = question_link_refs

    note_refs = [{"@id": _resource_iri(base_url, "notes", note_id)} for note_id in notes]
    if note_refs:
        commit_node["note"] = note_refs

    if dataset.commit_manifest.source_session_id is not None:
        commit_node["sourceSession"] = {
            "@id": _resource_iri(
                base_url,
                "sessions",
                dataset.commit_manifest.source_session_id,
            )
        }

    if dataset.commit_manifest.metadata:
        commit_node["metadata"] = dataset.commit_manifest.metadata
    if dataset.commit_manifest.nwb_metadata:
        commit_node["nwbMetadata"] = dataset.commit_manifest.nwb_metadata
    if dataset.commit_manifest.bids_metadata:
        commit_node["bidsMetadata"] = dataset.commit_manifest.bids_metadata

    graph.append(commit_node)
    graph.extend(_origin_provenance_nodes(base_url, dataset))
    graph.extend(_dataset_file_node(base_url, dataset, file) for file in files)
    graph.extend(_external_artifact_node(artifact) for artifact in external_artifacts)
    graph.extend(_dataset_question_link_node(base_url, dataset, link) for link in question_links)
    graph.extend(people[user_id] for user_id in sorted(people))

    return {"@context": _context(base_url), "@graph": graph}


def _dataset_summary_node(base_url: str, dataset: Dataset) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "datasets", dataset.dataset_id),
        "@type": "prov:Entity",
        "commitHash": dataset.commit_hash,
        "status": dataset.status.value,
    }
    if dataset.terminal_reason:
        node["terminalReason"] = dataset.terminal_reason
    _apply_origin_provenance(base_url, node, dataset)
    return node


def _analysis_summary_node(
    base_url: str,
    analysis: Analysis,
    *,
    datasets: list[Dataset],
) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "analyses", analysis.analysis_id),
        "@type": "prov:Activity",
        "methodHash": analysis.method_hash,
        "codeVersion": analysis.code_version,
        "executedAt": _isoformat(analysis.executed_at),
        "status": analysis.status.value,
    }
    if analysis.terminal_reason:
        node["terminalReason"] = analysis.terminal_reason
    _apply_origin_provenance(base_url, node, analysis)
    if analysis.environment_hash is not None:
        node["environmentHash"] = analysis.environment_hash
    if datasets:
        node["prov:used"] = [
            {"@id": _resource_iri(base_url, "datasets", dataset.dataset_id)} for dataset in datasets
        ]
    for artifact in analysis.external_artifacts:
        if artifact.kind == ExternalArtifactKind.ENTITY:
            _append_id_ref_list(node, "prov:used", {"@id": artifact.uri})
        else:
            _append_id_ref_list(node, "prov:wasInformedBy", {"@id": artifact.uri})
    return node


def build_claim_provenance_document(
    base_url: str,
    claim: Claim,
    *,
    analyses: list[Analysis],
    datasets: list[Dataset],
    questions: list[Question],
    visualizations: list[Visualization],
    claim_edges: list[ClaimEdge] | None = None,
    supervision_edges: list[SupervisionEdge] | None = None,
) -> dict[str, object]:
    supervision_edges = supervision_edges or []
    claim_edges = claim_edges or []
    people: dict[str, dict[str, object]] = {}
    merged: dict[str, dict[str, Any]] = {}
    datasets_by_id = {dataset.dataset_id: dataset for dataset in datasets}
    analysis_attribution = {
        analysis.analysis_id: _creator_user_id(analysis.executed_by_user_id, analysis.executed_by)
        for analysis in analyses
    }
    dataset_attribution = {
        dataset.dataset_id: _creator_user_id(dataset.created_by_user_id, dataset.created_by)
        for dataset in datasets
    }

    for question in questions:
        node = _question_node(
            base_url,
            question,
            people=people,
            supervision_edges=supervision_edges,
        )
        merged.setdefault(str(node["@id"]), node)
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, question))

    for dataset in datasets:
        document = build_dataset_provenance_document(
            base_url,
            dataset,
            supervision_edges=supervision_edges,
        )
        graph = document.get("@graph", [])
        if isinstance(graph, list):
            _merge_graph_nodes(merged, graph)

    for analysis in analyses:
        analysis_datasets = [
            datasets_by_id[dataset_id]
            for dataset_id in analysis.dataset_ids
            if dataset_id in datasets_by_id
        ]
        document = build_analysis_provenance_document(
            base_url,
            analysis,
            datasets=analysis_datasets,
            claims=[claim] if analysis.analysis_id in claim.supported_by_analysis_ids else [],
            visualizations=[
                visualization
                for visualization in visualizations
                if visualization.analysis_id == analysis.analysis_id
            ],
            claim_edges=claim_edges,
            supervision_edges=supervision_edges,
        )
        graph = document.get("@graph", [])
        if isinstance(graph, list):
            _merge_graph_nodes(merged, graph)

    claim_creator_user_id = _creator_user_id(claim.created_by_user_id, claim.created_by)
    attributed_user_ids = _unique_user_ids(
        [
            claim_creator_user_id,
            *[dataset_attribution.get(dataset_id) for dataset_id in claim.supported_by_dataset_ids],
            *[
                analysis_attribution.get(analysis_id)
                for analysis_id in claim.supported_by_analysis_ids
            ],
        ]
    )
    for user_id in attributed_user_ids:
        _add_person_with_supervision(
            people,
            base_url,
            user_id,
            activity_time=claim.created_at,
            supervision_edges=supervision_edges,
        )
    claim_node = _claim_node(
        base_url,
        claim,
        attributed_user_ids=attributed_user_ids,
        claim_edges=claim_edges,
    )
    merged[str(claim_node["@id"])] = claim_node
    _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, claim))
    _merge_graph_nodes(
        merged,
        [_external_artifact_node(citation) for citation in claim.external_citations],
    )
    _merge_graph_nodes(
        merged,
        [
            _claim_relation_node(base_url, edge)
            for edge in claim_edges
            if edge.claim_id == claim.claim_id or edge.target_claim_id == claim.claim_id
        ],
    )
    for user_id in sorted(people):
        person = people[user_id]
        merged[str(person["@id"])] = person

    return {"@context": _context(base_url), "@graph": list(merged.values())}


def _analysis_agent_node(base_url: str, executed_by: str) -> dict[str, object]:
    return _person_node(base_url, executed_by)


def _claim_node(
    base_url: str,
    claim: Claim,
    *,
    attributed_user_ids: list[str],
    claim_edges: list[ClaimEdge] | None = None,
) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "claims", claim.claim_id),
        "@type": "prov:Entity",
        "statement": claim.statement,
        "confidence": claim.confidence,
        "status": claim.status.value,
    }
    if claim.terminal_reason:
        node["terminalReason"] = claim.terminal_reason
    if claim.falsification_criteria:
        node["falsificationCriteria"] = claim.falsification_criteria
    if claim.verification_plan:
        node["verificationPlan"] = claim.verification_plan
    if claim.refuting_outcome:
        node["refutingOutcome"] = claim.refuting_outcome
    _apply_origin_provenance(base_url, node, claim)
    if attributed_user_ids:
        node["prov:wasAttributedTo"] = _attribution_value(base_url, attributed_user_ids)
    if claim.supported_by_dataset_ids:
        node["supportsDataset"] = [
            {"@id": _resource_iri(base_url, "datasets", dataset_id)}
            for dataset_id in claim.supported_by_dataset_ids
        ]
    if claim.supported_by_analysis_ids:
        node["supportsAnalysis"] = [
            {"@id": _resource_iri(base_url, "analyses", analysis_id)}
            for analysis_id in claim.supported_by_analysis_ids
        ]
    if claim.answers_question_ids:
        node["answersQuestion"] = [
            {"@id": _resource_iri(base_url, "questions", question_id)}
            for question_id in claim.answers_question_ids
        ]
    outgoing_edges = [edge for edge in claim_edges or [] if edge.claim_id == claim.claim_id]
    if outgoing_edges:
        node["claimRelation"] = [
            {"@id": _claim_relation_iri(base_url, edge)} for edge in outgoing_edges
        ]
    if claim.external_citations:
        node["cites"] = [{"@id": citation.uri} for citation in claim.external_citations]
    return node


def _claim_relation_iri(base_url: str, edge: ClaimEdge) -> str:
    return _resource_iri(base_url, "claim-relations", edge.edge_id)


def _claim_relation_node(base_url: str, edge: ClaimEdge) -> dict[str, object]:
    return {
        "@id": _claim_relation_iri(base_url, edge),
        "@type": "lab:ClaimRelation",
        "claimRelationSource": {"@id": _resource_iri(base_url, "claims", edge.claim_id)},
        "claimRelationTarget": {"@id": _resource_iri(base_url, "claims", edge.target_claim_id)},
        "claimRelationType": edge.relation.value,
        "createdAt": _isoformat(edge.created_at),
    }


def _exploration_node_iri(base_url: str, node_id: UUID) -> str:
    return _resource_iri(base_url, "exploration-nodes", node_id)


def _exploration_node_node(
    base_url: str,
    node: ExplorationNode,
    *,
    people: dict[str, dict[str, object]],
    supervision_edges: list[SupervisionEdge],
) -> dict[str, object]:
    node_iri = _exploration_node_iri(base_url, node.node_id)
    payload: dict[str, object] = {
        "@id": node_iri,
        "@type": ["prov:Entity", "lab:ExplorationNode"],
        "entityType": "exploration_node",
        "entityId": str(node.node_id),
        "explorationNodeType": node.node_type.value,
        "text": node.title,
        "status": node.status.value,
        "createdAt": _isoformat(node.created_at),
        "target": {
            "@id": _entity_ref_iri(base_url, node.target),
            "entityType": node.target.entity_type.value,
            "entityId": str(node.target.entity_id),
        },
    }
    if node.choice:
        payload["choice"] = node.choice
    if node.alternatives_considered:
        payload["alternativesConsidered"] = list(node.alternatives_considered)
    if node.rationale:
        payload["rationale"] = node.rationale
    if node.evidence_refs:
        payload["evidence"] = [
            {"@id": _entity_ref_iri(base_url, evidence_ref)}
            for evidence_ref in node.evidence_refs
        ]
    if node.hypothesis:
        payload["hypothesis"] = node.hypothesis
    if node.failure_mode:
        payload["failureMode"] = node.failure_mode
    if node.lesson:
        payload["lesson"] = node.lesson
    if node.tooling_context:
        payload["toolingContext"] = node.tooling_context
    if node.trigger:
        payload["trigger"] = node.trigger
    if node.parent_node_ids:
        payload["prov:wasDerivedFrom"] = [
            {"@id": _exploration_node_iri(base_url, parent_id)}
            for parent_id in node.parent_node_ids
        ]
    if node.also_depends_on_node_ids:
        payload["alsoDependsOn"] = [
            {"@id": _exploration_node_iri(base_url, dependency_id)}
            for dependency_id in node.also_depends_on_node_ids
        ]
    if node.invalidates_node_id is not None:
        payload["invalidates"] = {
            "@id": _exploration_node_iri(base_url, node.invalidates_node_id)
        }
    if node.invalidates_claim_id is not None:
        payload["invalidates"] = {
            "@id": _resource_iri(base_url, "claims", node.invalidates_claim_id)
        }
    _apply_origin_provenance(base_url, payload, node)
    creator_user_id = _creator_user_id(node.created_by_user_id, node.created_by)
    if creator_user_id is not None:
        payload["prov:wasAttributedTo"] = {"@id": _agent_iri(base_url, creator_user_id)}
        _add_person_with_supervision(
            people,
            base_url,
            creator_user_id,
            activity_time=node.created_at,
            supervision_edges=supervision_edges,
        )
    return payload


def _visualization_node(
    base_url: str,
    visualization: Visualization,
    *,
    attributed_user_ids: list[str],
) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "visualizations", visualization.viz_id),
        "@type": "prov:Entity",
        "prov:wasGeneratedBy": {
            "@id": _resource_iri(base_url, "analyses", visualization.analysis_id)
        },
        "vizType": visualization.viz_type,
        "filePath": visualization.file_path,
    }
    _apply_origin_provenance(base_url, node, visualization)
    if attributed_user_ids:
        node["prov:wasAttributedTo"] = _attribution_value(base_url, attributed_user_ids)
    if visualization.caption:
        node["caption"] = visualization.caption
    if visualization.asset is not None:
        node["contentUrl"] = (
            f"{_resource_iri(base_url, 'visualizations', visualization.viz_id)}/file/download"
        )
        node["fileName"] = visualization.asset.filename
        node["encodingFormat"] = visualization.asset.content_type
        node["contentSize"] = visualization.asset.size_bytes
        node["sha256"] = visualization.asset.checksum
    if visualization.related_claim_ids:
        node["relatedClaim"] = [
            {"@id": _resource_iri(base_url, "claims", claim_id)}
            for claim_id in visualization.related_claim_ids
        ]
    if visualization.dataset_ids:
        dataset_refs = [
            {"@id": _resource_iri(base_url, "datasets", dataset_id)}
            for dataset_id in visualization.dataset_ids
        ]
        node["groundingDataset"] = dataset_refs
        node["prov:wasDerivedFrom"] = dataset_refs
    return node


def build_analysis_provenance_document(
    base_url: str,
    analysis: Analysis,
    *,
    datasets: list[Dataset],
    claims: list[Claim],
    visualizations: list[Visualization],
    claim_edges: list[ClaimEdge] | None = None,
    supervision_edges: list[SupervisionEdge] | None = None,
) -> dict[str, object]:
    analysis_iri = _resource_iri(base_url, "analyses", analysis.analysis_id)
    supervision_edges = supervision_edges or []
    claim_edges = claim_edges or []
    people: dict[str, dict[str, object]] = {}
    analysis_actor_user_id = _creator_user_id(
        analysis.executed_by_user_id,
        analysis.executed_by,
    )
    dataset_attribution = {
        dataset.dataset_id: _creator_user_id(dataset.created_by_user_id, dataset.created_by)
        for dataset in datasets
    }
    graph: list[dict[str, object]] = []

    analysis_node: dict[str, object] = {
        "@id": analysis_iri,
        "@type": "prov:Activity",
        "methodHash": analysis.method_hash,
        "codeVersion": analysis.code_version,
        "executedAt": _isoformat(analysis.executed_at),
        "status": analysis.status.value,
    }
    if analysis.terminal_reason:
        analysis_node["terminalReason"] = analysis.terminal_reason
    _apply_origin_provenance(base_url, analysis_node, analysis)
    if analysis.environment_hash is not None:
        analysis_node["environmentHash"] = analysis.environment_hash
    if datasets:
        analysis_node["prov:used"] = [
            {"@id": _resource_iri(base_url, "datasets", dataset.dataset_id)} for dataset in datasets
        ]
    for artifact in analysis.external_artifacts:
        if artifact.kind == ExternalArtifactKind.ENTITY:
            _append_id_ref_list(analysis_node, "prov:used", {"@id": artifact.uri})
        else:
            _append_id_ref_list(analysis_node, "prov:wasInformedBy", {"@id": artifact.uri})
    if analysis_actor_user_id is not None:
        _add_person_with_supervision(
            people,
            base_url,
            analysis_actor_user_id,
            activity_time=analysis.executed_at,
            supervision_edges=supervision_edges,
        )
        analysis_node["prov:wasAssociatedWith"] = {
            "@id": _agent_iri(base_url, analysis_actor_user_id)
        }
    graph.append(analysis_node)
    graph.extend(_origin_provenance_nodes(base_url, analysis))
    graph.extend(_external_artifact_node(artifact) for artifact in analysis.external_artifacts)

    for dataset in datasets:
        dataset_node: dict[str, object] = {
            "@id": _resource_iri(base_url, "datasets", dataset.dataset_id),
            "@type": "prov:Entity",
            "commitHash": dataset.commit_hash,
            "status": dataset.status.value,
        }
        _apply_origin_provenance(base_url, dataset_node, dataset)
        if dataset.terminal_reason:
            dataset_node["terminalReason"] = dataset.terminal_reason
        dataset_user_id = dataset_attribution[dataset.dataset_id]
        if dataset_user_id is not None:
            dataset_node["prov:wasAttributedTo"] = {"@id": _agent_iri(base_url, dataset_user_id)}
            _add_person_with_supervision(
                people,
                base_url,
                dataset_user_id,
                activity_time=dataset.created_at,
                supervision_edges=supervision_edges,
            )
        graph.append(dataset_node)
        graph.extend(_origin_provenance_nodes(base_url, dataset))
    for claim in claims:
        claim_creator_user_id = _creator_user_id(claim.created_by_user_id, claim.created_by)
        attributed_user_ids = _unique_user_ids(
            [
                claim_creator_user_id,
                *[
                    dataset_attribution.get(dataset_id)
                    for dataset_id in claim.supported_by_dataset_ids
                ],
                *[
                    analysis_actor_user_id
                    for analysis_id in claim.supported_by_analysis_ids
                    if analysis_id == analysis.analysis_id
                ],
            ]
        )
        for user_id in attributed_user_ids:
            _add_person_with_supervision(
                people,
                base_url,
                user_id,
                activity_time=claim.created_at,
                supervision_edges=supervision_edges,
            )
        graph.append(
            _claim_node(
                base_url,
                claim,
                attributed_user_ids=attributed_user_ids,
                claim_edges=claim_edges,
            )
        )
        graph.extend(_origin_provenance_nodes(base_url, claim))
        graph.extend(_external_artifact_node(citation) for citation in claim.external_citations)
    claim_ids = {claim.claim_id for claim in claims}
    graph.extend(
        _claim_relation_node(base_url, edge) for edge in claim_edges if edge.claim_id in claim_ids
    )
    for visualization in visualizations:
        visualization_creator_user_id = _creator_user_id(
            visualization.created_by_user_id,
            visualization.created_by,
        )
        attributed_user_ids = _unique_user_ids(
            [
                visualization_creator_user_id,
                (
                    analysis_actor_user_id
                    if visualization.analysis_id == analysis.analysis_id
                    else None
                ),
            ]
        )
        for user_id in attributed_user_ids:
            _add_person_with_supervision(
                people,
                base_url,
                user_id,
                activity_time=visualization.created_at,
                supervision_edges=supervision_edges,
            )
        graph.append(
            _visualization_node(
                base_url,
                visualization,
                attributed_user_ids=attributed_user_ids,
            )
        )
        graph.extend(_origin_provenance_nodes(base_url, visualization))
    graph.extend(people[user_id] for user_id in sorted(people))

    return {"@context": _context(base_url), "@graph": graph}


def _merge_graph_nodes(
    merged: dict[str, dict[str, Any]],
    graph: list[dict[str, object]],
) -> None:
    for node in graph:
        node_id = node.get("@id")
        if isinstance(node_id, str):
            merged.setdefault(node_id, dict(node))


def _question_node(
    base_url: str,
    question: Question,
    *,
    people: dict[str, dict[str, object]],
    supervision_edges: list[SupervisionEdge],
) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "questions", question.question_id),
        "@type": "prov:Entity",
        "text": question.text,
        "questionType": question.question_type.value,
        "status": question.status.value,
        "createdAt": _isoformat(question.created_at),
    }
    if question.terminal_reason:
        node["terminalReason"] = question.terminal_reason
    _apply_origin_provenance(base_url, node, question)
    creator_user_id = _creator_user_id(question.created_by_user_id, question.created_by)
    if creator_user_id is not None:
        node["prov:wasAttributedTo"] = {"@id": _agent_iri(base_url, creator_user_id)}
        _add_person_with_supervision(
            people,
            base_url,
            creator_user_id,
            activity_time=question.created_at,
            supervision_edges=supervision_edges,
        )
    return node


def _note_node(
    base_url: str,
    note: Note,
    *,
    people: dict[str, dict[str, object]],
    supervision_edges: list[SupervisionEdge],
) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "notes", note.note_id),
        "@type": "prov:Entity",
        "rawContent": note.raw_content,
        "status": note.status.value,
        "createdAt": _isoformat(note.created_at),
    }
    if note.transcribed_text:
        node["transcribedText"] = note.transcribed_text
    _apply_origin_provenance(base_url, node, note)
    if note.targets:
        node["target"] = [
            {
                "@id": _resource_iri(
                    base_url,
                    _entity_resource_name(target.entity_type.value),
                    target.entity_id,
                ),
                "entityType": target.entity_type.value,
                "entityId": str(target.entity_id),
            }
            for target in note.targets
        ]
    creator_user_id = _creator_user_id(note.created_by_user_id, note.created_by)
    if creator_user_id is not None:
        node["prov:wasAttributedTo"] = {"@id": _agent_iri(base_url, creator_user_id)}
        _add_person_with_supervision(
            people,
            base_url,
            creator_user_id,
            activity_time=note.created_at,
            supervision_edges=supervision_edges,
        )
    return node


def _session_node(
    base_url: str,
    session: Session,
    *,
    people: dict[str, dict[str, object]],
    supervision_edges: list[SupervisionEdge],
) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "sessions", session.session_id),
        "@type": "prov:Activity",
        "sessionType": session.session_type.value,
        "status": session.status.value,
        "startedAt": _isoformat(session.started_at),
        "updatedAt": _isoformat(session.updated_at),
    }
    if session.ended_at is not None:
        node["endedAt"] = _isoformat(session.ended_at)
    if session.primary_question_id is not None:
        node["primaryQuestion"] = {
            "@id": _resource_iri(base_url, "questions", session.primary_question_id)
        }
    _apply_origin_provenance(base_url, node, session)
    creator_user_id = _creator_user_id(session.created_by_user_id, session.created_by)
    if creator_user_id is not None:
        node["prov:wasAssociatedWith"] = {"@id": _agent_iri(base_url, creator_user_id)}
        _add_person_with_supervision(
            people,
            base_url,
            creator_user_id,
            activity_time=session.started_at,
            supervision_edges=supervision_edges,
        )
    return node


_ENTITY_RESOURCE_NAMES = {
    "analysis": "analyses",
    "claim": "claims",
    "dataset": "datasets",
    "exploration_node": "exploration-nodes",
    "goal": "goals",
    "note": "notes",
    "project": "projects",
    "question": "questions",
    "session": "sessions",
    "visualization": "visualizations",
}


def _entity_resource_name(entity_type: object) -> str:
    value = str(entity_type)
    return _ENTITY_RESOURCE_NAMES.get(value, f"{value}s")


ARA_LAYER_NAMES = ("logic", "src", "trace", "evidence")


@dataclass(frozen=True)
class AraArtifactRecords:
    """Scoped current-state records compiled into layered Ara JSON-LD."""

    questions: list[Question]
    datasets: list[Dataset]
    analyses: list[Analysis]
    claims: list[Claim]
    claim_edges: list[ClaimEdge]
    notes: list[Note]
    visualizations: list[Visualization]
    entity_versions: list[EntityVersion]
    exploration_nodes: list[ExplorationNode] = field(default_factory=list)
    goal: Goal | None = None
    goal_links: list[GoalLink] | None = None


def build_ara_artifact_document(
    base_url: str,
    *,
    scope_type: EntityType,
    scope_id: UUID,
    records: AraArtifactRecords,
    generated_at: datetime,
    layer_name: str | None = None,
    supervision_edges: list[SupervisionEdge] | None = None,
) -> dict[str, object]:
    if layer_name is not None and layer_name not in ARA_LAYER_NAMES:
        raise ValueError(f"Unknown Ara layer {layer_name!r}.")
    artifact_iri = _ara_artifact_iri(base_url, scope_type, scope_id)
    layers = {
        name: _build_ara_layer_document(
            base_url,
            scope_type=scope_type,
            scope_id=scope_id,
            artifact_iri=artifact_iri,
            layer_name=name,
            records=records,
            generated_at=generated_at,
            supervision_edges=supervision_edges or [],
        )
        for name in ARA_LAYER_NAMES
    }
    if layer_name is not None:
        return layers[layer_name]
    bindings = _claim_cross_layer_bindings(
        base_url,
        scope_type=scope_type,
        scope_id=scope_id,
        records=records,
    )
    return {
        "@context": _context(base_url),
        "@id": artifact_iri,
        "@type": "lab:AraArtifact",
        "scope": _scope_ref(base_url, scope_type, scope_id),
        "generatedAt": _isoformat(generated_at),
        "layers": layers,
        "crossLayerBinding": [{"@id": str(binding["@id"])} for binding in bindings],
        "crossLayerBindings": bindings,
    }


def _ara_artifact_iri(base_url: str, scope_type: EntityType, scope_id: UUID) -> str:
    if scope_type == EntityType.GOAL:
        return _resource_iri(base_url, "goals", scope_id) + "/ara-artifact"
    if scope_type == EntityType.QUESTION:
        return _resource_iri(base_url, "questions", scope_id) + "/ara-artifact"
    return _resource_iri(base_url, _entity_resource_name(scope_type.value), scope_id)


def _ara_layer_iri(
    base_url: str,
    scope_type: EntityType,
    scope_id: UUID,
    layer_name: str,
) -> str:
    return f"{_ara_artifact_iri(base_url, scope_type, scope_id)}/{layer_name}"


def _scope_ref(base_url: str, scope_type: EntityType, scope_id: UUID) -> dict[str, object]:
    return {
        "@id": _resource_iri(base_url, _entity_resource_name(scope_type.value), scope_id),
        "entityType": scope_type.value,
        "entityId": str(scope_id),
    }


def _build_ara_layer_document(
    base_url: str,
    *,
    scope_type: EntityType,
    scope_id: UUID,
    artifact_iri: str,
    layer_name: str,
    records: AraArtifactRecords,
    generated_at: datetime,
    supervision_edges: list[SupervisionEdge],
) -> dict[str, object]:
    return {
        "@context": _context(base_url),
        "@id": _ara_layer_iri(base_url, scope_type, scope_id, layer_name),
        "@type": ["prov:Bundle", "lab:AraLayer"],
        "layer": layer_name,
        "scope": _scope_ref(base_url, scope_type, scope_id),
        "prov:wasDerivedFrom": {"@id": artifact_iri},
        "generatedAt": _isoformat(generated_at),
        "crossLayerBindings": _claim_cross_layer_bindings(
            base_url,
            scope_type=scope_type,
            scope_id=scope_id,
            records=records,
            layer_filter=layer_name,
        ),
        "@graph": _ara_layer_graph(
            base_url,
            layer_name=layer_name,
            records=records,
            supervision_edges=supervision_edges,
        ),
    }


def _ara_layer_graph(
    base_url: str,
    *,
    layer_name: str,
    records: AraArtifactRecords,
    supervision_edges: list[SupervisionEdge],
) -> list[dict[str, object]]:
    builders = {
        "logic": _ara_logic_graph,
        "src": _ara_src_graph,
        "trace": _ara_trace_graph,
        "evidence": _ara_evidence_graph,
    }
    return builders[layer_name](
        base_url,
        records=records,
        supervision_edges=supervision_edges,
    )


def _ara_logic_graph(
    base_url: str,
    *,
    records: AraArtifactRecords,
    supervision_edges: list[SupervisionEdge],
) -> list[dict[str, object]]:
    people: dict[str, dict[str, object]] = {}
    merged: dict[str, dict[str, Any]] = {}
    for claim in records.claims:
        creator_user_id = _creator_user_id(claim.created_by_user_id, claim.created_by)
        attributed_user_ids = _unique_user_ids([creator_user_id])
        for user_id in attributed_user_ids:
            _add_person_with_supervision(
                people,
                base_url,
                user_id,
                activity_time=claim.created_at,
                supervision_edges=supervision_edges,
            )
        node = _claim_node(
            base_url,
            claim,
            attributed_user_ids=attributed_user_ids,
            claim_edges=records.claim_edges,
        )
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, claim))
        _merge_graph_nodes(
            merged,
            [_external_artifact_node(citation) for citation in claim.external_citations],
        )
    _merge_graph_nodes(
        merged,
        [_claim_relation_node(base_url, edge) for edge in records.claim_edges],
    )
    for exploration_node in records.exploration_nodes:
        node = _exploration_node_node(
            base_url,
            exploration_node,
            people=people,
            supervision_edges=supervision_edges,
        )
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, exploration_node))
    if records.goal is not None:
        goal_node = _goal_node(base_url, records.goal)
        merged[str(goal_node["@id"])] = goal_node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, records.goal))
    for link in records.goal_links or []:
        link_node = _goal_link_node(base_url, link)
        merged[str(link_node["@id"])] = link_node
    for user_id in sorted(people):
        person = people[user_id]
        merged[str(person["@id"])] = person
    return list(merged.values())


def _ara_src_graph(
    base_url: str,
    *,
    records: AraArtifactRecords,
    supervision_edges: list[SupervisionEdge],
) -> list[dict[str, object]]:
    del supervision_edges
    datasets_by_id = {dataset.dataset_id: dataset for dataset in records.datasets}
    merged: dict[str, dict[str, Any]] = {}
    for analysis in records.analyses:
        analysis_datasets = [
            datasets_by_id[dataset_id]
            for dataset_id in analysis.dataset_ids
            if dataset_id in datasets_by_id
        ]
        node = _analysis_summary_node(base_url, analysis, datasets=analysis_datasets)
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, analysis))
        _merge_graph_nodes(
            merged,
            [_external_artifact_node(artifact) for artifact in analysis.external_artifacts],
        )
    return list(merged.values())


def _ara_trace_graph(
    base_url: str,
    *,
    records: AraArtifactRecords,
    supervision_edges: list[SupervisionEdge],
) -> list[dict[str, object]]:
    people: dict[str, dict[str, object]] = {}
    merged: dict[str, dict[str, Any]] = {}
    for question in records.questions:
        node = _question_node(
            base_url,
            question,
            people=people,
            supervision_edges=supervision_edges,
        )
        if question.parent_question_ids:
            node["prov:wasDerivedFrom"] = [
                {"@id": _resource_iri(base_url, "questions", parent_id)}
                for parent_id in question.parent_question_ids
            ]
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, question))
    for dataset in records.datasets:
        node = _dataset_summary_node(base_url, dataset)
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, dataset))
    datasets_by_id = {dataset.dataset_id: dataset for dataset in records.datasets}
    for analysis in records.analyses:
        node = _analysis_summary_node(
            base_url,
            analysis,
            datasets=[
                datasets_by_id[dataset_id]
                for dataset_id in analysis.dataset_ids
                if dataset_id in datasets_by_id
            ],
        )
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, analysis))
    for claim in records.claims:
        node = _claim_node(
            base_url,
            claim,
            attributed_user_ids=[],
            claim_edges=records.claim_edges,
        )
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, claim))
    for visualization in records.visualizations:
        node = _visualization_node(base_url, visualization, attributed_user_ids=[])
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, visualization))
    for exploration_node in records.exploration_nodes:
        node = _exploration_node_node(
            base_url,
            exploration_node,
            people=people,
            supervision_edges=supervision_edges,
        )
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, exploration_node))
    for version in records.entity_versions:
        node = _entity_version_node(base_url, version)
        merged[str(node["@id"])] = node
    for user_id in sorted(people):
        person = people[user_id]
        merged[str(person["@id"])] = person
    return list(merged.values())


def _ara_evidence_graph(
    base_url: str,
    *,
    records: AraArtifactRecords,
    supervision_edges: list[SupervisionEdge],
) -> list[dict[str, object]]:
    people: dict[str, dict[str, object]] = {}
    merged: dict[str, dict[str, Any]] = {}
    for dataset in records.datasets:
        document = build_dataset_provenance_document(
            base_url,
            dataset,
            supervision_edges=supervision_edges,
        )
        graph = document.get("@graph", [])
        if isinstance(graph, list):
            _merge_graph_nodes(merged, graph)
    for visualization in records.visualizations:
        creator_user_id = _creator_user_id(
            visualization.created_by_user_id,
            visualization.created_by,
        )
        attributed_user_ids = _unique_user_ids([creator_user_id])
        for user_id in attributed_user_ids:
            _add_person_with_supervision(
                people,
                base_url,
                user_id,
                activity_time=visualization.created_at,
                supervision_edges=supervision_edges,
            )
        node = _visualization_node(
            base_url,
            visualization,
            attributed_user_ids=attributed_user_ids,
        )
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, visualization))
    for note in records.notes:
        node = _note_node(
            base_url,
            note,
            people=people,
            supervision_edges=supervision_edges,
        )
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, note))
    for exploration_node in records.exploration_nodes:
        node = _exploration_node_node(
            base_url,
            exploration_node,
            people=people,
            supervision_edges=supervision_edges,
        )
        merged[str(node["@id"])] = node
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, exploration_node))
    for user_id in sorted(people):
        person = people[user_id]
        merged[str(person["@id"])] = person
    return list(merged.values())


def _goal_node(base_url: str, goal: Goal) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "goals", goal.goal_id),
        "@type": "lab:Goal",
        "entityType": EntityType.GOAL.value,
        "entityId": str(goal.goal_id),
        "text": goal.title,
        "status": goal.status.value,
        "createdAt": _isoformat(goal.created_at),
    }
    if goal.summary:
        node["summary"] = goal.summary
    if goal.external_ref:
        node["externalUri"] = goal.external_ref
    _apply_origin_provenance(base_url, node, goal)
    return node


def _goal_link_node(base_url: str, link: GoalLink) -> dict[str, object]:
    goal_iri = _resource_iri(base_url, "goals", link.goal_id)
    node: dict[str, object] = {
        "@id": _synthetic_child_iri(goal_iri, "links", link.link_id),
        "@type": "lab:GoalLink",
        "goalLink": {"@id": goal_iri},
        "target": {
            "@id": _entity_ref_iri(base_url, link.target),
            "entityType": link.target.entity_type.value,
            "entityId": str(link.target.entity_id),
        },
        "role": link.relation.value,
        "status": link.link_status.value,
        "createdAt": _isoformat(link.created_at),
    }
    if link.slot is not None:
        node["slot"] = link.slot
    return node


def _entity_ref_iri(base_url: str, ref: EntityRef) -> str:
    return _resource_iri(base_url, _entity_resource_name(ref.entity_type.value), ref.entity_id)


def _entity_version_node(base_url: str, version: EntityVersion) -> dict[str, object]:
    entity_iri = _resource_iri(
        base_url,
        _entity_resource_name(version.entity_type.value),
        version.entity_id,
    )
    node: dict[str, object] = {
        "@id": _synthetic_child_iri(entity_iri, "versions", version.version_number),
        "@type": "lab:EntityVersion",
        "entityType": version.entity_type.value,
        "entityId": str(version.entity_id),
        "versionNumber": version.version_number,
        "createdAt": _isoformat(version.created_at),
        "metadata": version.snapshot,
        "prov:wasDerivedFrom": {"@id": entity_iri},
    }
    if version.change_set_id is not None:
        node["changeSet"] = {"@id": _draft_activity_iri(base_url, version.change_set_id)}
    if version.committed_at is not None:
        node["generatedAt"] = _isoformat(version.committed_at)
    return node


def _claim_cross_layer_bindings(
    base_url: str,
    *,
    scope_type: EntityType,
    scope_id: UUID,
    records: AraArtifactRecords,
    layer_filter: str | None = None,
) -> list[dict[str, object]]:
    if layer_filter is not None and layer_filter not in ARA_LAYER_NAMES:
        return []
    datasets_by_id = {dataset.dataset_id: dataset for dataset in records.datasets}
    analyses_by_id = {analysis.analysis_id: analysis for analysis in records.analyses}
    visualizations_by_claim: dict[UUID, list[Visualization]] = {}
    for visualization in records.visualizations:
        for claim_id in visualization.related_claim_ids:
            visualizations_by_claim.setdefault(claim_id, []).append(visualization)
    notes_by_target: dict[tuple[EntityType, UUID], list[Note]] = {}
    for note in records.notes:
        for target in note.targets:
            notes_by_target.setdefault((target.entity_type, target.entity_id), []).append(note)
    exploration_by_claim: dict[UUID, list[ExplorationNode]] = {}
    for exploration_node in records.exploration_nodes:
        if exploration_node.target.entity_type == EntityType.CLAIM:
            exploration_by_claim.setdefault(
                exploration_node.target.entity_id,
                [],
            ).append(exploration_node)
        if exploration_node.invalidates_claim_id is not None:
            exploration_by_claim.setdefault(
                exploration_node.invalidates_claim_id,
                [],
            ).append(exploration_node)

    bindings: list[dict[str, object]] = []
    for claim in sorted(records.claims, key=lambda item: (item.created_at, str(item.claim_id))):
        analysis_ids = [
            analysis_id
            for analysis_id in claim.supported_by_analysis_ids
            if analysis_id in analyses_by_id
        ]
        dataset_ids = {
            dataset_id
            for dataset_id in claim.supported_by_dataset_ids
            if dataset_id in datasets_by_id
        }
        for analysis_id in analysis_ids:
            dataset_ids.update(
                dataset_id
                for dataset_id in analyses_by_id[analysis_id].dataset_ids
                if dataset_id in datasets_by_id
            )
        question_ids = set(claim.answers_question_ids)
        for dataset_id in dataset_ids:
            dataset = datasets_by_id[dataset_id]
            question_ids.update(link.question_id for link in dataset.question_links)
        question_ids.update(
            question.question_id
            for question in records.questions
            if question.question_id in question_ids
        )
        evidence_refs = [
            {"@id": _resource_iri(base_url, "datasets", dataset_id)}
            for dataset_id in sorted(dataset_ids, key=str)
        ]
        evidence_refs.extend(
            {"@id": _resource_iri(base_url, "visualizations", visualization.viz_id)}
            for visualization in sorted(
                visualizations_by_claim.get(claim.claim_id, []),
                key=lambda item: (item.created_at, str(item.viz_id)),
            )
        )
        for note in sorted(
            notes_by_target.get((EntityType.CLAIM, claim.claim_id), []),
            key=lambda item: (item.created_at, str(item.note_id)),
        ):
            evidence_refs.append({"@id": _resource_iri(base_url, "notes", note.note_id)})
        for exploration_node in sorted(
            exploration_by_claim.get(claim.claim_id, []),
            key=lambda item: (item.created_at, str(item.node_id)),
        ):
            evidence_refs.append(
                {"@id": _exploration_node_iri(base_url, exploration_node.node_id)}
            )
        layer_refs: dict[str, dict[str, str]] = {
            name: {"@id": _ara_layer_iri(base_url, scope_type, scope_id, name)}
            for name in ARA_LAYER_NAMES
        }
        if layer_filter is not None:
            layer_refs = {layer_filter: layer_refs[layer_filter]}
        binding: dict[str, object] = {
            "@id": _synthetic_child_iri(
                _resource_iri(base_url, "claims", claim.claim_id),
                "ara-binding",
                scope_type.value,
                scope_id,
            ),
            "@type": "lab:ForensicBinding",
            "layer": layer_refs,
            "claim": {"@id": _resource_iri(base_url, "claims", claim.claim_id)},
            "analysis": [
                {"@id": _resource_iri(base_url, "analyses", analysis_id)}
                for analysis_id in sorted(analysis_ids, key=str)
            ],
            "dataset": [
                {"@id": _resource_iri(base_url, "datasets", dataset_id)}
                for dataset_id in sorted(dataset_ids, key=str)
            ],
            "question": [
                {"@id": _resource_iri(base_url, "questions", question_id)}
                for question_id in sorted(question_ids, key=str)
            ],
            "evidence": evidence_refs,
        }
        code_environment: list[dict[str, object]] = []
        for analysis_id in sorted(analysis_ids, key=str):
            analysis = analyses_by_id[analysis_id]
            item: dict[str, object] = {
                "@id": _resource_iri(base_url, "analyses", analysis.analysis_id),
                "codeVersion": analysis.code_version,
                "methodHash": analysis.method_hash,
            }
            if analysis.environment_hash is not None:
                item["environmentHash"] = analysis.environment_hash
            code_environment.append(item)
        if code_environment:
            binding["codeEnvironment"] = code_environment
        bindings.append(binding)
    return bindings


def build_record_export_provenance_document(
    base_url: str,
    records: RecordExportRecords,
    *,
    supervision_edges: list[SupervisionEdge] | None = None,
) -> dict[str, object]:
    supervision_edges = supervision_edges or []
    people: dict[str, dict[str, object]] = {}
    merged: dict[str, dict[str, Any]] = {}
    datasets_by_id = {dataset.dataset_id: dataset for dataset in records.datasets}

    for question in records.questions:
        node = _question_node(
            base_url,
            question,
            people=people,
            supervision_edges=supervision_edges,
        )
        merged.setdefault(str(node["@id"]), node)
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, question))

    for note in records.notes:
        node = _note_node(
            base_url,
            note,
            people=people,
            supervision_edges=supervision_edges,
        )
        merged.setdefault(str(node["@id"]), node)
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, note))

    for session in records.sessions:
        node = _session_node(
            base_url,
            session,
            people=people,
            supervision_edges=supervision_edges,
        )
        merged.setdefault(str(node["@id"]), node)
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, session))

    for dataset in records.datasets:
        document = build_dataset_provenance_document(
            base_url,
            dataset,
            supervision_edges=supervision_edges,
        )
        graph = document.get("@graph", [])
        if isinstance(graph, list):
            _merge_graph_nodes(merged, graph)

    for analysis in records.analyses:
        analysis_datasets = [
            datasets_by_id[dataset_id]
            for dataset_id in analysis.dataset_ids
            if dataset_id in datasets_by_id
        ]
        analysis_claims = [
            claim
            for claim in records.claims
            if analysis.analysis_id in claim.supported_by_analysis_ids
        ]
        analysis_claim_ids = {claim.claim_id for claim in analysis_claims}
        analysis_claim_edges = [
            edge for edge in records.claim_edges if edge.claim_id in analysis_claim_ids
        ]
        document = build_analysis_provenance_document(
            base_url,
            analysis,
            datasets=analysis_datasets,
            claims=analysis_claims,
            visualizations=[],
            claim_edges=analysis_claim_edges,
            supervision_edges=supervision_edges,
        )
        graph = document.get("@graph", [])
        if isinstance(graph, list):
            _merge_graph_nodes(merged, graph)

    covered_claim_ids = {
        claim.claim_id
        for analysis in records.analyses
        for claim in records.claims
        if analysis.analysis_id in claim.supported_by_analysis_ids
    }
    dataset_attribution = {
        dataset.dataset_id: _creator_user_id(dataset.created_by_user_id, dataset.created_by)
        for dataset in records.datasets
    }
    analysis_attribution = {
        analysis.analysis_id: _creator_user_id(analysis.executed_by_user_id, analysis.executed_by)
        for analysis in records.analyses
    }
    for visualization in records.visualizations:
        visualization_creator_user_id = _creator_user_id(
            visualization.created_by_user_id,
            visualization.created_by,
        )
        attributed_user_ids = _unique_user_ids(
            [
                visualization_creator_user_id,
                analysis_attribution.get(visualization.analysis_id),
            ]
        )
        for user_id in attributed_user_ids:
            _add_person_with_supervision(
                people,
                base_url,
                user_id,
                activity_time=visualization.created_at,
                supervision_edges=supervision_edges,
            )
        node = _visualization_node(
            base_url,
            visualization,
            attributed_user_ids=attributed_user_ids,
        )
        merged.setdefault(str(node["@id"]), node)
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, visualization))

    for claim in records.claims:
        if claim.claim_id in covered_claim_ids:
            continue
        claim_creator_user_id = _creator_user_id(claim.created_by_user_id, claim.created_by)
        attributed_user_ids = _unique_user_ids(
            [
                claim_creator_user_id,
                *[
                    dataset_attribution.get(dataset_id)
                    for dataset_id in claim.supported_by_dataset_ids
                ],
                *[
                    analysis_attribution.get(analysis_id)
                    for analysis_id in claim.supported_by_analysis_ids
                ],
            ]
        )
        for user_id in attributed_user_ids:
            _add_person_with_supervision(
                people,
                base_url,
                user_id,
                activity_time=claim.created_at,
                supervision_edges=supervision_edges,
            )
        node = _claim_node(
            base_url,
            claim,
            attributed_user_ids=attributed_user_ids,
            claim_edges=records.claim_edges,
        )
        merged.setdefault(str(node["@id"]), node)
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, claim))
        _merge_graph_nodes(
            merged,
            [_external_artifact_node(citation) for citation in claim.external_citations],
        )
        _merge_graph_nodes(
            merged,
            [
                _claim_relation_node(base_url, edge)
                for edge in records.claim_edges
                if edge.claim_id == claim.claim_id
            ],
        )

    for exploration_node in records.exploration_nodes:
        node = _exploration_node_node(
            base_url,
            exploration_node,
            people=people,
            supervision_edges=supervision_edges,
        )
        merged.setdefault(str(node["@id"]), node)
        _merge_graph_nodes(merged, _origin_provenance_nodes(base_url, exploration_node))

    for user_id in sorted(people):
        person = people[user_id]
        merged[str(person["@id"])] = person

    return {"@context": _context(base_url), "@graph": list(merged.values())}
