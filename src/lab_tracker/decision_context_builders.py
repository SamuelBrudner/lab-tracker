"""Payload builders for assistant decision-context responses."""

from __future__ import annotations

from lab_tracker.decision_context_constants import TASK_KIND_VALUES
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
    if entity_type == "experiment":
        return str(entity.get("name") or entity.get("experiment_id") or "")
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
    *,
    experiments: list[JsonObject] | None = None,
) -> JsonObject:
    experiments = experiments or []
    focus = [entity_label("question", item) for item in questions[:3]] or [query]
    caveats: list[str] = []
    missing_evidence: list[str] = []
    if not any(item.get("status") == "committed" for item in datasets):
        missing_evidence.append("No committed datasets were returned in this context.")
    if not analyses:
        missing_evidence.append("No prior analyses were returned in this context.")
    if not claims:
        missing_evidence.append("No claims were returned in this context.")
    if any(item.get("status") == "rejected" for item in claims):
        caveats.append("Some returned claims are REJECTED — do not rely on them as evidence.")
    if any(item.get("status") in {"proposed", "testing"} for item in claims):
        caveats.append(
            "Some returned claims are still proposed or under testing, not supported."
        )
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
            entity_ref("experiment", item, "experiment_id")
            for item in experiments[:5]
        ] + [
            entity_ref("question", item, "question_id") for item in questions[:5]
        ]
    elif task_kind == "research_writing":
        candidate_outputs = [
            entity_ref("claim", item, "claim_id") for item in claims[:5]
        ] + [entity_ref("visualization", item, "viz_id") for item in visualizations[:5]]
    elif task_kind == "summary":
        # A summary draws on supported claims, the analyses behind them, and figures.
        candidate_outputs = (
            [entity_ref("claim", item, "claim_id") for item in claims[:5]]
            + [entity_ref("analysis", item, "analysis_id") for item in analyses[:5]]
            + [entity_ref("visualization", item, "viz_id") for item in visualizations[:5]]
        )
    elif task_kind == "progress_review":
        # A meeting briefing reviews what was done in the window: the advances
        # (analyses), the plots (visualizations), and the claims they support.
        candidate_outputs = (
            [entity_ref("analysis", item, "analysis_id") for item in analyses[:5]]
            + [entity_ref("visualization", item, "viz_id") for item in visualizations[:5]]
            + [entity_ref("claim", item, "claim_id") for item in claims[:5]]
        )
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


def write_front_door(
    *,
    task_kind: str,
    project: JsonObject,
    anchors: list[JsonObject],
    questions: list[JsonObject],
    sessions: list[JsonObject],
    datasets: list[JsonObject],
    analyses: list[JsonObject],
    claims: list[JsonObject],
    visualizations: list[JsonObject],
    experiments: list[JsonObject] | None = None,
) -> JsonObject:
    """Build write-oriented affordances for follow-on MCP create calls."""
    experiments = experiments or []
    return {
        "call_first_for": (
            "Call lab_tracker_get_decision_context before research-facing "
            "read-then-write tasks so subsequent proposed creates can reuse stable IDs."
        ),
        "allowed_task_kinds": list(TASK_KIND_VALUES),
        "resolved_scope": {
            "project_id": str(project.get("project_id") or ""),
            "project": entity_ref("project", project, "project_id"),
            "anchors": anchors,
        },
        "candidate_ids": {
            "questions": [
                entity_ref("question", item, "question_id") for item in questions[:10]
            ],
            "sessions": [
                entity_ref("session", item, "session_id") for item in sessions[:10]
            ],
            "experiments": [
                entity_ref("experiment", item, "experiment_id")
                for item in experiments[:10]
            ],
            "datasets": [
                entity_ref("dataset", item, "dataset_id") for item in datasets[:10]
            ],
            "analyses": [
                entity_ref("analysis", item, "analysis_id") for item in analyses[:10]
            ],
            "claims": [
                entity_ref("claim", item, "claim_id") for item in claims[:10]
            ],
            "visualizations": [
                entity_ref("visualization", item, "viz_id")
                for item in visualizations[:10]
            ],
        },
        "create_guidance": _create_guidance(task_kind),
    }


def _create_guidance(task_kind: str) -> list[str]:
    guidance = [
        "Propose these creates to the user; do not create or mutate canonical records "
        "unless the user explicitly asks — a person commits.",
        "Use resolved_scope.project_id as the project_id for follow-on create calls.",
        "Prefer candidate_ids and evidence_map before creating duplicate records.",
        "Call lab_tracker_describe_schema for required fields, enums, and lifecycle values.",
    ]
    if task_kind in {"plot", "analysis", "research_writing"}:
        guidance.append(
            "For evidence writes, keep the order dataset -> analysis -> claim -> "
            "visualization when new records are needed."
        )
    if task_kind == "experiment_plan":
        guidance.append(
            "Prefer adding atomic child questions under returned motivating questions."
        )
    if task_kind == "slides":
        guidance.append(
            "Prefer returned visualizations and supported claims as slide evidence anchors."
        )
    return guidance


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
