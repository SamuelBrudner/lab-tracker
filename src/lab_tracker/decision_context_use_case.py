"""Orchestration for assistant decision-context assembly."""

from __future__ import annotations

from datetime import datetime, timezone

from lab_tracker.decision_context_builders import (
    build_evidence_map,
    candidate_project,
    decision_error,
    entity_ref,
    task_guidance,
    truncation,
    validate_context_limit,
    write_front_door,
)
from lab_tracker.decision_context_constants import (
    CONTEXT_LOOKUP_LIMIT,
    TASK_KIND_TEXT,
    TASK_KIND_VALUES,
)
from lab_tracker.decision_context_selection import (
    envelope_items,
    find_by_id,
    merge_entities,
    project_ids_from_search,
    project_lookup,
    search_items,
)
from lab_tracker.decision_context_types import DecisionContextReader, JsonObject


def build_decision_context(
    reader: DecisionContextReader,
    *,
    task_kind: str,
    query: str,
    project_id: str | None = None,
    question_id: str | None = None,
    dataset_id: str | None = None,
    analysis_id: str | None = None,
    claim_id: str | None = None,
    visualization_id: str | None = None,
    limit: int = 20,
) -> JsonObject:
    cleaned_task_kind = (task_kind or "").strip()
    if cleaned_task_kind not in TASK_KIND_VALUES:
        return decision_error(
            "invalid_task_kind",
            f"Invalid task kind {task_kind!r}. Allowed task kinds: {TASK_KIND_TEXT}.",
            allowed_task_kinds=list(TASK_KIND_VALUES),
        )
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        return decision_error("invalid_query", "Decision-context query must not be empty.")

    resolved_limit = validate_context_limit(limit)
    projects_payload = reader.list_projects(limit=CONTEXT_LOOKUP_LIMIT)
    projects = envelope_items(projects_payload)
    projects_by_id = project_lookup(projects)

    resolved_project_id = str(project_id) if project_id else None
    if resolved_project_id and resolved_project_id not in projects_by_id:
        return decision_error(
            "anchor_not_found",
            f"Project {resolved_project_id!r} was not found.",
            anchor={"entity_type": "project", "entity_id": resolved_project_id},
        )

    anchor_entities: dict[str, list[JsonObject]] = {
        "questions": [],
        "datasets": [],
        "analyses": [],
        "claims": [],
        "visualizations": [],
    }
    anchor_project_ids: set[str] = set()

    if question_id:
        questions = envelope_items(
            reader.list_questions(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        question = find_by_id(questions, "question_id", str(question_id))
        if question is None:
            return decision_error(
                "anchor_not_found",
                f"Question {question_id!r} was not found.",
                anchor={"entity_type": "question", "entity_id": str(question_id)},
            )
        anchor_entities["questions"].append(question)
        anchor_project_ids.add(str(question["project_id"]))

    if dataset_id:
        datasets = envelope_items(
            reader.list_datasets(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        dataset = find_by_id(datasets, "dataset_id", str(dataset_id))
        if dataset is None:
            return decision_error(
                "anchor_not_found",
                f"Dataset {dataset_id!r} was not found.",
                anchor={"entity_type": "dataset", "entity_id": str(dataset_id)},
            )
        anchor_entities["datasets"].append(dataset)
        anchor_project_ids.add(str(dataset["project_id"]))

    if analysis_id:
        analyses = envelope_items(
            reader.list_analyses(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        analysis = find_by_id(analyses, "analysis_id", str(analysis_id))
        if analysis is None:
            return decision_error(
                "anchor_not_found",
                f"Analysis {analysis_id!r} was not found.",
                anchor={"entity_type": "analysis", "entity_id": str(analysis_id)},
            )
        anchor_entities["analyses"].append(analysis)
        anchor_project_ids.add(str(analysis["project_id"]))

    if claim_id:
        claims = envelope_items(
            reader.list_claims(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        claim = find_by_id(claims, "claim_id", str(claim_id))
        if claim is None:
            return decision_error(
                "anchor_not_found",
                f"Claim {claim_id!r} was not found.",
                anchor={"entity_type": "claim", "entity_id": str(claim_id)},
            )
        anchor_entities["claims"].append(claim)
        anchor_project_ids.add(str(claim["project_id"]))

    if visualization_id:
        visualizations = envelope_items(
            reader.list_visualizations(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        visualization = find_by_id(visualizations, "viz_id", str(visualization_id))
        if visualization is None:
            return decision_error(
                "anchor_not_found",
                f"Visualization {visualization_id!r} was not found.",
                anchor={
                    "entity_type": "visualization",
                    "entity_id": str(visualization_id),
                },
            )
        anchor_entities["visualizations"].append(visualization)
        analyses = envelope_items(
            reader.list_analyses(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        analysis = find_by_id(
            analyses,
            "analysis_id",
            str(visualization.get("analysis_id")),
        )
        if analysis is not None:
            anchor_project_ids.add(str(analysis["project_id"]))

    if resolved_project_id:
        anchor_project_ids.add(resolved_project_id)
    if len(anchor_project_ids) > 1:
        return decision_error(
            "conflicting_anchors",
            "Decision-context anchors resolve to multiple projects.",
            project_ids=sorted(anchor_project_ids),
        )
    if not resolved_project_id and anchor_project_ids:
        resolved_project_id = next(iter(anchor_project_ids))

    if not resolved_project_id:
        global_search_payload = reader.search(cleaned_query, limit=resolved_limit)
        search_project_ids = project_ids_from_search(global_search_payload)
        if len(search_project_ids) == 1:
            resolved_project_id = next(iter(search_project_ids))
        else:
            candidates = [
                candidate_project(projects_by_id[item], "search_match")
                for item in sorted(search_project_ids)
                if item in projects_by_id
            ]
            if not candidates:
                candidates = [
                    candidate_project(project, "active_project")
                    for project in projects
                    if project.get("status") == "active"
                ][:resolved_limit]
            return decision_error(
                "ambiguous_project",
                "Decision context needs a project or a more specific anchor.",
                candidate_projects=candidates,
            )

    project = projects_by_id.get(resolved_project_id)
    if project is None:
        return decision_error(
            "anchor_not_found",
            f"Project {resolved_project_id!r} was not found.",
            anchor={"entity_type": "project", "entity_id": resolved_project_id},
        )

    search_payload = reader.search(
        cleaned_query,
        project_id=resolved_project_id,
        limit=resolved_limit,
    )
    questions_payload = reader.list_questions(
        project_id=resolved_project_id,
        limit=resolved_limit,
    )
    notes_payload = reader.list_notes(project_id=resolved_project_id, limit=resolved_limit)
    sessions_payload = reader.list_sessions(
        project_id=resolved_project_id,
        limit=resolved_limit,
    )
    datasets_payload = reader.list_datasets(
        project_id=resolved_project_id,
        limit=resolved_limit,
    )
    analyses_payload = reader.list_analyses(
        project_id=resolved_project_id,
        limit=resolved_limit,
    )
    claims_payload = reader.list_claims(project_id=resolved_project_id, limit=resolved_limit)
    visualizations_payload = reader.list_visualizations(
        project_id=resolved_project_id,
        limit=resolved_limit,
    )

    questions = merge_entities(
        "question_id",
        (anchor_entities["questions"], "anchor"),
        (search_items(search_payload, "questions"), "search_match"),
        (envelope_items(questions_payload), "recent_activity"),
    )
    notes = merge_entities(
        "note_id",
        (search_items(search_payload, "notes"), "search_match"),
        (envelope_items(notes_payload), "recent_activity"),
    )
    sessions = merge_entities(
        "session_id",
        (envelope_items(sessions_payload), "recent_activity"),
    )
    datasets = merge_entities(
        "dataset_id",
        (anchor_entities["datasets"], "anchor"),
        (envelope_items(datasets_payload), "recent_activity"),
    )
    analyses = merge_entities(
        "analysis_id",
        (anchor_entities["analyses"], "anchor"),
        (envelope_items(analyses_payload), "recent_activity"),
    )
    claims = merge_entities(
        "claim_id",
        (anchor_entities["claims"], "anchor"),
        (envelope_items(claims_payload), "recent_activity"),
    )
    visualizations = merge_entities(
        "viz_id",
        (anchor_entities["visualizations"], "anchor"),
        (envelope_items(visualizations_payload), "recent_activity"),
    )

    anchors = [
        entity_ref("question", item, "question_id")
        for item in anchor_entities["questions"]
    ] + [
        entity_ref("dataset", item, "dataset_id")
        for item in anchor_entities["datasets"]
    ] + [
        entity_ref("analysis", item, "analysis_id")
        for item in anchor_entities["analyses"]
    ] + [
        entity_ref("claim", item, "claim_id") for item in anchor_entities["claims"]
    ] + [
        entity_ref("visualization", item, "viz_id")
        for item in anchor_entities["visualizations"]
    ]

    evidence_map = build_evidence_map(datasets, analyses, claims, visualizations)
    return {
        "data": {
            "task_kind": cleaned_task_kind,
            "query": cleaned_query,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scope": {
                "project": {
                    "project_id": str(project["project_id"]),
                    "name": project.get("name"),
                    "status": project.get("status"),
                },
                "anchors": anchors,
            },
            "context_summary": (
                f"Found {len(questions)} questions, {len(notes)} notes, "
                f"{len(datasets)} datasets, {len(analyses)} analyses, "
                f"{len(claims)} claims, and {len(visualizations)} visualizations "
                f"for {cleaned_task_kind}."
            ),
            "task_guidance": task_guidance(
                cleaned_task_kind,
                cleaned_query,
                questions,
                datasets,
                analyses,
                claims,
                visualizations,
            ),
            "write_front_door": write_front_door(
                task_kind=cleaned_task_kind,
                project=project,
                anchors=anchors,
                questions=questions,
                sessions=sessions,
                datasets=datasets,
                analyses=analyses,
                claims=claims,
                visualizations=visualizations,
            ),
            "questions": questions,
            "notes": notes,
            "sessions": sessions,
            "datasets": datasets,
            "analyses": analyses,
            "claims": claims,
            "visualizations": visualizations,
            "evidence_map": evidence_map,
            "truncation": truncation(
                [
                    ("questions", questions_payload, None),
                    ("notes", notes_payload, None),
                    ("sessions", sessions_payload, None),
                    ("datasets", datasets_payload, None),
                    ("analyses", analyses_payload, None),
                    ("claims", claims_payload, None),
                    ("visualizations", visualizations_payload, None),
                ]
            ),
        },
        "meta": {
            "retrieval_policy": "explicit_links_then_search_then_recency",
            "limit": resolved_limit,
        },
    }
