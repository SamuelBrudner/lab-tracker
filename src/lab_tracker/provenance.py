"""JSON-LD / PROV-O export helpers."""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote
from uuid import UUID

from lab_tracker.models import (
    Analysis,
    Claim,
    Dataset,
    DatasetCommitManifest,
    DatasetFile,
    ExternalArtifactKind,
    ExternalArtifactReference,
    QuestionLink,
    QuestionLinkRole,
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
        "answersQuestion": {"@id": "lab:answersQuestion", "@type": "@id"},
        "caption": "lab:caption",
        "checksum": "lab:checksum",
        "codeVersion": "lab:codeVersion",
        "commitHash": "lab:commitHash",
        "confidence": "lab:confidence",
        "contentSize": "lab:contentSize",
        "contentUrl": {"@id": "lab:contentUrl", "@type": "@id"},
        "contentType": "lab:contentType",
        "encodingFormat": "lab:encodingFormat",
        "environmentHash": "lab:environmentHash",
        "executedAt": "lab:executedAt",
        "externalArtifact": {"@id": "lab:externalArtifact", "@type": "@id"},
        "externalContentHash": "lab:externalContentHash",
        "externalMetadata": {"@id": "lab:externalMetadata", "@type": "@json"},
        "externalSourceSystem": "lab:externalSourceSystem",
        "externalUri": {"@id": "lab:externalUri", "@type": "@id"},
        "fileName": "lab:fileName",
        "filePath": "lab:filePath",
        "filename": "lab:filename",
        "metadata": {"@id": "lab:metadata", "@type": "@json"},
        "methodHash": "lab:methodHash",
        "note": {"@id": "lab:note", "@type": "@id"},
        "nwbMetadata": {"@id": "lab:nwbMetadata", "@type": "@json"},
        "outcomeStatus": "lab:outcomeStatus",
        "question": {"@id": "lab:question", "@type": "@id"},
        "questionLink": {"@id": "lab:questionLink", "@type": "@id"},
        "relatedClaim": {"@id": "lab:relatedClaim", "@type": "@id"},
        "role": "lab:role",
        "sizeBytes": "lab:sizeBytes",
        "sourceSession": {"@id": "lab:sourceSession", "@type": "@id"},
        "statement": "lab:statement",
        "status": "lab:status",
        "supportsAnalysis": {"@id": "lab:supportsAnalysis", "@type": "@id"},
        "supportsDataset": {"@id": "lab:supportsDataset", "@type": "@id"},
        "sha256": "lab:sha256",
        "supervisionEndedAt": "lab:supervisionEndedAt",
        "supervisionStartedAt": "lab:supervisionStartedAt",
        "userId": "lab:userId",
        "vizType": "lab:vizType",
        "wasAttributedTo": {"@id": "prov:wasAttributedTo", "@type": "@id"},
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
        "@type": "prov:Entity"
        if artifact.kind == ExternalArtifactKind.ENTITY
        else "prov:Activity",
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

    used_files = [
        {"@id": _file_entity_id(base_url, dataset, file)}
        for file in files
    ]
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
        {"@id": _question_link_id(base_url, dataset, link.question_id)}
        for link in question_links
    ]
    if question_link_refs:
        commit_node["questionLink"] = question_link_refs

    note_refs = [
        {"@id": _resource_iri(base_url, "notes", note_id)}
        for note_id in notes
    ]
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
    graph.extend(_dataset_file_node(base_url, dataset, file) for file in files)
    graph.extend(_external_artifact_node(artifact) for artifact in external_artifacts)
    graph.extend(
        _dataset_question_link_node(base_url, dataset, link)
        for link in question_links
    )
    graph.extend(people[user_id] for user_id in sorted(people))

    return {"@context": _context(base_url), "@graph": graph}


def _analysis_agent_node(base_url: str, executed_by: str) -> dict[str, object]:
    return _person_node(base_url, executed_by)


def _claim_node(
    base_url: str,
    claim: Claim,
    *,
    attributed_user_ids: list[str],
) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "claims", claim.claim_id),
        "@type": "prov:Entity",
        "statement": claim.statement,
        "confidence": claim.confidence,
        "status": claim.status.value,
    }
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
    return node


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
    return node


def build_analysis_provenance_document(
    base_url: str,
    analysis: Analysis,
    *,
    datasets: list[Dataset],
    claims: list[Claim],
    visualizations: list[Visualization],
    supervision_edges: list[SupervisionEdge] | None = None,
) -> dict[str, object]:
    analysis_iri = _resource_iri(base_url, "analyses", analysis.analysis_id)
    supervision_edges = supervision_edges or []
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
    if analysis.environment_hash is not None:
        analysis_node["environmentHash"] = analysis.environment_hash
    if datasets:
        analysis_node["prov:used"] = [
            {"@id": _resource_iri(base_url, "datasets", dataset.dataset_id)}
            for dataset in datasets
        ]
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

    for dataset in datasets:
        dataset_node: dict[str, object] = {
            "@id": _resource_iri(base_url, "datasets", dataset.dataset_id),
            "@type": "prov:Entity",
            "commitHash": dataset.commit_hash,
            "status": dataset.status.value,
        }
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
    for claim in claims:
        attributed_user_ids = _unique_user_ids(
            [
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
            )
        )
    for visualization in visualizations:
        attributed_user_ids = (
            [analysis_actor_user_id]
            if analysis_actor_user_id is not None
            and visualization.analysis_id == analysis.analysis_id
            else []
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
    graph.extend(people[user_id] for user_id in sorted(people))

    return {"@context": _context(base_url), "@graph": graph}
