"""Payload builders for assistant decision-context responses."""

from __future__ import annotations

from lab_tracker.decision_context_selection import envelope_items, meta_total
from lab_tracker.decision_context_types import JsonObject


def validate_context_limit(limit: int) -> int:
    try:
        resolved = int(limit)
    except (TypeError, ValueError):
        return 20
    return min(max(resolved, 1), 100)


def decision_error(code: str, message: str, **metadata: object) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    error.update(metadata)
    return {"error": error}


def entity_label(entity_type: str, entity: JsonObject) -> str:
    if entity_type == "project":
        return str(entity.get("name") or entity.get("project_id") or "")
    if entity_type == "question":
        return str(entity.get("text") or entity.get("question_id") or "")
    if entity_type == "note":
        text = str(
            entity.get("transcribed_text")
            or entity.get("raw_content")
            or entity.get("note_id")
            or ""
        )
        return text[:120]
    if entity_type == "dataset":
        return str(entity.get("commit_hash") or entity.get("dataset_id") or "")
    if entity_type == "session":
        return str(entity.get("link_code") or entity.get("session_id") or "")
    if entity_type == "analysis":
        return str(entity.get("method_hash") or entity.get("analysis_id") or "")
    if entity_type == "claim":
        return str(entity.get("statement") or entity.get("claim_id") or "")
    if entity_type == "visualization":
        return str(
            entity.get("caption") or entity.get("file_path") or entity.get("viz_id") or ""
        )
    return str(entity)


def entity_ref(entity_type: str, entity: JsonObject, id_key: str) -> JsonObject:
    return {
        "entity_type": entity_type,
        "entity_id": str(entity.get(id_key) or ""),
        "label": entity_label(entity_type, entity),
    }


def candidate_project(project: JsonObject, reason: str) -> JsonObject:
    return {
        "project_id": str(project.get("project_id") or ""),
        "name": project.get("name"),
        "status": project.get("status"),
        "reason": reason,
    }


def build_evidence_map(
    datasets: list[JsonObject],
    analyses: list[JsonObject],
    claims: list[JsonObject],
    visualizations: list[JsonObject],
) -> list[JsonObject]:
    evidence: list[JsonObject] = []
    for dataset in datasets:
        question_ids = [
            str(link.get("question_id"))
            for link in dataset.get("question_links", [])
            if isinstance(link, dict) and link.get("question_id")
        ]
        if dataset.get("primary_question_id"):
            question_ids.insert(0, str(dataset["primary_question_id"]))
        evidence.append(
            {
                "entity": entity_ref("dataset", dataset, "dataset_id"),
                "questions": sorted(set(question_ids)),
                "reason": "dataset_question_links",
            }
        )
    for analysis in analyses:
        evidence.append(
            {
                "entity": entity_ref("analysis", analysis, "analysis_id"),
                "datasets": [str(item) for item in analysis.get("dataset_ids", [])],
                "reason": "analysis_dataset_links",
            }
        )
    for claim in claims:
        evidence.append(
            {
                "entity": entity_ref("claim", claim, "claim_id"),
                "datasets": [
                    str(item) for item in claim.get("supported_by_dataset_ids", [])
                ],
                "analyses": [
                    str(item) for item in claim.get("supported_by_analysis_ids", [])
                ],
                "reason": "claim_support_links",
            }
        )
    for visualization in visualizations:
        evidence.append(
            {
                "entity": entity_ref("visualization", visualization, "viz_id"),
                "analysis_id": str(visualization.get("analysis_id") or ""),
                "claims": [
                    str(item) for item in visualization.get("related_claim_ids", [])
                ],
                "reason": "visualization_links",
            }
        )
    return evidence


def task_guidance(
    task_kind: str,
    query: str,
    questions: list[JsonObject],
    datasets: list[JsonObject],
    analyses: list[JsonObject],
    claims: list[JsonObject],
    visualizations: list[JsonObject],
) -> JsonObject:
    focus = [entity_label("question", item) for item in questions[:3]] or [query]
    caveats: list[str] = []
    missing_evidence: list[str] = []
    if not any(item.get("status") == "committed" for item in datasets):
        missing_evidence.append("No committed datasets were returned in this context.")
    if not analyses:
        missing_evidence.append("No prior analyses were returned in this context.")
    if not claims:
        missing_evidence.append("No claims were returned in this context.")
    if any(item.get("status") == "proposed" for item in claims):
        caveats.append("Some returned claims are still proposed, not supported.")
    if any(item.get("status") == "staged" for item in datasets + analyses):
        caveats.append("Some returned datasets or analyses are staged rather than committed.")

    if task_kind == "plot":
        candidate_outputs = [
            entity_ref("dataset", item, "dataset_id") for item in datasets[:5]
        ] + [entity_ref("visualization", item, "viz_id") for item in visualizations[:5]]
    elif task_kind == "analysis":
        candidate_outputs = [
            entity_ref("analysis", item, "analysis_id") for item in analyses[:5]
        ] + [entity_ref("dataset", item, "dataset_id") for item in datasets[:5]]
    elif task_kind == "slides":
        candidate_outputs = [
            entity_ref("visualization", item, "viz_id") for item in visualizations[:5]
        ] + [entity_ref("claim", item, "claim_id") for item in claims[:5]]
    elif task_kind == "experiment_plan":
        candidate_outputs = [
            entity_ref("question", item, "question_id") for item in questions[:5]
        ]
    elif task_kind == "research_writing":
        candidate_outputs = [
            entity_ref("claim", item, "claim_id") for item in claims[:5]
        ] + [entity_ref("visualization", item, "viz_id") for item in visualizations[:5]]
    else:
        candidate_outputs = [
            entity_ref("question", item, "question_id") for item in questions[:5]
        ]

    return {
        "recommended_focus": focus,
        "candidate_outputs": candidate_outputs,
        "caveats": caveats,
        "missing_evidence": missing_evidence,
    }


def truncation(sections: list[tuple[str, JsonObject, str | None]]) -> JsonObject:
    truncated_sections: list[JsonObject] = []
    for name, payload, fallback_key in sections:
        returned = len(envelope_items(payload))
        total = meta_total(payload, fallback_key)
        if total is not None and total > returned:
            truncated_sections.append(
                {"section": name, "returned": returned, "total": total}
            )
    return {
        "was_truncated": bool(truncated_sections),
        "sections": truncated_sections,
    }
