"""JSON-LD / PROV-O export helpers."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from lab_tracker.models import (
    Analysis,
    Claim,
    Dataset,
    DatasetFile,
    QuestionLink,
    QuestionLinkRole,
    Visualization,
)


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
        "caption": "lab:caption",
        "checksum": "lab:checksum",
        "codeVersion": "lab:codeVersion",
        "commitHash": "lab:commitHash",
        "confidence": "lab:confidence",
        "contentType": "lab:contentType",
        "environmentHash": "lab:environmentHash",
        "executedAt": "lab:executedAt",
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
        "userId": "lab:userId",
        "vizType": "lab:vizType",
        "bidsMetadata": {"@id": "lab:bidsMetadata", "@type": "@json"},
    }


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


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


def _dataset_question_link_node(base_url: str, dataset: Dataset, link) -> dict[str, object]:
    return {
        "@id": _question_link_id(base_url, dataset, link.question_id),
        "question": {"@id": _resource_iri(base_url, "questions", link.question_id)},
        "role": link.role.value,
        "outcomeStatus": link.outcome_status.value,
    }


def build_dataset_provenance_document(base_url: str, dataset: Dataset) -> dict[str, object]:
    dataset_iri = _resource_iri(base_url, "datasets", dataset.dataset_id)
    commit_activity_iri = _synthetic_child_iri(dataset_iri, "provenance", "commit")
    files = _sorted_dataset_files(dataset.commit_manifest.files)
    question_links = _sorted_question_links(dataset.commit_manifest.question_links)
    notes = sorted(dataset.commit_manifest.note_ids, key=str)
    graph: list[dict[str, object]] = []

    dataset_node: dict[str, object] = {
        "@id": dataset_iri,
        "@type": "prov:Entity",
        "prov:wasGeneratedBy": {"@id": commit_activity_iri},
        "commitHash": dataset.commit_hash,
        "status": dataset.status.value,
    }
    graph.append(dataset_node)

    commit_node: dict[str, object] = {
        "@id": commit_activity_iri,
        "@type": "prov:Activity",
    }

    used_files = [
        {"@id": _file_entity_id(base_url, dataset, file)}
        for file in files
    ]
    if used_files:
        commit_node["prov:used"] = used_files

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
    graph.extend(
        _dataset_question_link_node(base_url, dataset, link)
        for link in question_links
    )

    return {"@context": _context(base_url), "@graph": graph}


def _analysis_agent_node(base_url: str, executed_by: str) -> dict[str, object]:
    normalized = _normalize_base_url(base_url)
    agent_id = f"{normalized}/agents/{quote(executed_by, safe='')}"
    return {
        "@id": agent_id,
        "@type": "prov:Agent",
        "userId": executed_by,
    }


def _claim_node(base_url: str, claim: Claim) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "claims", claim.claim_id),
        "@type": "prov:Entity",
        "statement": claim.statement,
        "confidence": claim.confidence,
        "status": claim.status.value,
    }
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
    return node


def _visualization_node(base_url: str, visualization: Visualization) -> dict[str, object]:
    node: dict[str, object] = {
        "@id": _resource_iri(base_url, "visualizations", visualization.viz_id),
        "@type": "prov:Entity",
        "prov:wasGeneratedBy": {
            "@id": _resource_iri(base_url, "analyses", visualization.analysis_id)
        },
        "vizType": visualization.viz_type,
        "filePath": visualization.file_path,
    }
    if visualization.caption:
        node["caption"] = visualization.caption
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
) -> dict[str, object]:
    analysis_iri = _resource_iri(base_url, "analyses", analysis.analysis_id)
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
    if analysis.executed_by:
        agent = _analysis_agent_node(base_url, analysis.executed_by)
        analysis_node["prov:wasAssociatedWith"] = {"@id": agent["@id"]}
        graph.append(agent)
    graph.append(analysis_node)

    graph.extend(
        {
            "@id": _resource_iri(base_url, "datasets", dataset.dataset_id),
            "@type": "prov:Entity",
            "commitHash": dataset.commit_hash,
            "status": dataset.status.value,
        }
        for dataset in datasets
    )
    graph.extend(_claim_node(base_url, claim) for claim in claims)
    graph.extend(_visualization_node(base_url, visualization) for visualization in visualizations)

    return {"@context": _context(base_url), "@graph": graph}
