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
    merge_entities,
    search_items,
)
from lab_tracker.decision_context_types import DecisionContextReader, JsonObject

NOTE_TEXT_FIELD_LIMIT = 1000
# An ambiguous_project error lists at most this many search-matched projects
# (one get_project read each); the rest are counted, not read.
AMBIGUOUS_PROJECT_CANDIDATE_LIMIT = 10
AMBIGUOUS_PROJECT_MESSAGE = "Decision context needs a project or a more specific anchor."


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
    created_by: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
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

    # Projects are always resolved by id: a list_projects window (oldest first)
    # would hide newer projects and produce false anchor_not_found errors.
    resolved_project_id = str(project_id) if project_id else None
    explicit_project = reader.get_project(resolved_project_id) if resolved_project_id else None
    if resolved_project_id and explicit_project is None:
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
        question = reader.get_question(str(question_id))
        if question is None or not _matches_project_anchor(question, resolved_project_id):
            return decision_error(
                "anchor_not_found",
                f"Question {question_id!r} was not found.",
                anchor={"entity_type": "question", "entity_id": str(question_id)},
            )
        anchor_entities["questions"].append(question)
        anchor_project_ids.add(str(question["project_id"]))

    if dataset_id:
        dataset = reader.get_dataset(str(dataset_id))
        if dataset is None or not _matches_project_anchor(dataset, resolved_project_id):
            return decision_error(
                "anchor_not_found",
                f"Dataset {dataset_id!r} was not found.",
                anchor={"entity_type": "dataset", "entity_id": str(dataset_id)},
            )
        anchor_entities["datasets"].append(dataset)
        anchor_project_ids.add(str(dataset["project_id"]))

    if analysis_id:
        analysis = reader.get_analysis(str(analysis_id))
        if analysis is None or not _matches_project_anchor(analysis, resolved_project_id):
            return decision_error(
                "anchor_not_found",
                f"Analysis {analysis_id!r} was not found.",
                anchor={"entity_type": "analysis", "entity_id": str(analysis_id)},
            )
        anchor_entities["analyses"].append(analysis)
        anchor_project_ids.add(str(analysis["project_id"]))

    if claim_id:
        claim = reader.get_claim(str(claim_id))
        if claim is None or not _matches_project_anchor(claim, resolved_project_id):
            return decision_error(
                "anchor_not_found",
                f"Claim {claim_id!r} was not found.",
                anchor={"entity_type": "claim", "entity_id": str(claim_id)},
            )
        anchor_entities["claims"].append(claim)
        anchor_project_ids.add(str(claim["project_id"]))

    if visualization_id:
        visualization = reader.get_visualization(str(visualization_id))
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
        analysis_id_value = visualization.get("analysis_id")
        analysis = (
            reader.get_analysis(str(analysis_id_value)) if analysis_id_value else None
        )
        if analysis is not None and not _matches_project_anchor(
            analysis,
            resolved_project_id,
        ):
            return decision_error(
                "anchor_not_found",
                f"Visualization {visualization_id!r} was not found.",
                anchor={
                    "entity_type": "visualization",
                    "entity_id": str(visualization_id),
                },
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
        search_project_ids = reader.project_ids_with_search_matches(
            cleaned_query,
            limit=CONTEXT_LOOKUP_LIMIT,
        )
        # A lookup cut at its limit may hide further matching projects, so a
        # single returned id is only unique when the lookup was not cut.
        search_truncated = len(search_project_ids) >= CONTEXT_LOOKUP_LIMIT
        if len(search_project_ids) == 1 and not search_truncated:
            resolved_project_id = next(iter(search_project_ids))
        else:
            return _ambiguous_project_error(
                reader,
                search_project_ids,
                search_truncated=search_truncated,
                limit=resolved_limit,
            )

    # Every anchor was checked against an explicit project id, so it still
    # names the resolved project; reuse that read instead of repeating it.
    project = (
        explicit_project
        if explicit_project is not None
        else reader.get_project(resolved_project_id)
    )
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
        recent_first=True,
    )
    notes_payload = reader.list_notes(
        project_id=resolved_project_id,
        created_by=created_by,
        since=since,
        until=until,
        limit=resolved_limit,
        recent_first=True,
    )
    sessions_payload = reader.list_sessions(
        project_id=resolved_project_id,
        created_by=created_by,
        since=since,
        until=until,
        limit=resolved_limit,
        recent_first=True,
    )
    datasets_payload = reader.list_datasets(
        project_id=resolved_project_id,
        created_by=created_by,
        since=since,
        until=until,
        limit=resolved_limit,
        recent_first=True,
    )
    analyses_payload = reader.list_analyses(
        project_id=resolved_project_id,
        created_by=created_by,
        since=since,
        until=until,
        limit=resolved_limit,
        recent_first=True,
    )
    claims_payload = reader.list_claims(
        project_id=resolved_project_id,
        created_by=created_by,
        since=since,
        until=until,
        limit=resolved_limit,
        recent_first=True,
    )
    visualizations_payload = reader.list_visualizations(
        project_id=resolved_project_id,
        created_by=created_by,
        since=since,
        until=until,
        limit=resolved_limit,
        recent_first=True,
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
    notes = _compact_notes(notes)
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
                    (
                        "search.questions",
                        _search_truncation_payload(
                            search_payload,
                            "questions",
                            "questions_count",
                            resolved_limit,
                        ),
                        None,
                    ),
                    (
                        "search.notes",
                        _search_truncation_payload(
                            search_payload,
                            "notes",
                            "notes_count",
                            resolved_limit,
                        ),
                        None,
                    ),
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


def _matches_project_anchor(entity: JsonObject, project_id: str | None) -> bool:
    if project_id is None:
        return True
    return str(entity.get("project_id")) == project_id


def _ambiguous_project_error(
    reader: DecisionContextReader,
    search_project_ids: set[str],
    *,
    search_truncated: bool,
    limit: int,
) -> JsonObject:
    # The match lookup returns ids only, so each listed candidate costs one
    # read; list a bounded prefix and count the rest instead of reading them.
    # The prefix honours the caller's limit, as the active-project fallback
    # does, and never exceeds the read budget.
    listed_project_ids = sorted(search_project_ids)[
        : min(limit, AMBIGUOUS_PROJECT_CANDIDATE_LIMIT)
    ]
    candidates: list[JsonObject] = []
    for matched_project_id in listed_project_ids:
        matched_project = reader.get_project(matched_project_id)
        if matched_project is not None:
            candidates.append(candidate_project(matched_project, "search_match"))
    if candidates:
        candidates_total = len(search_project_ids)
        candidates_truncated = search_truncated or candidates_total > len(candidates)
        matched_text = (
            f"{candidates_total} or more projects match"
            if search_truncated
            else f"{candidates_total} projects match"
        )
        listed_text = (
            "1 is listed" if len(candidates) == 1 else f"{len(candidates)} are listed"
        )
        message = (
            f"{AMBIGUOUS_PROJECT_MESSAGE} {matched_text} the query; "
            f"{listed_text}. Pass project_id, an entity anchor, "
            "or a more specific query."
        )
    else:
        active_payload = reader.list_projects(status="active", limit=limit)
        candidates = [
            candidate_project(item, "active_project")
            for item in envelope_items(active_payload)
        ]
        candidates_total = _envelope_total(active_payload, "projects")
        candidates_truncated = candidates_total > len(candidates)
        message = AMBIGUOUS_PROJECT_MESSAGE
    return decision_error(
        "ambiguous_project",
        message,
        candidate_projects=candidates,
        candidate_projects_total=candidates_total,
        candidate_projects_truncated=candidates_truncated,
        candidate_projects_omitted=candidates_total - len(candidates),
    )


def _envelope_total(payload: JsonObject, label: str) -> int:
    meta = payload.get("meta")
    total = meta.get("total") if isinstance(meta, dict) else None
    if not isinstance(total, int) or isinstance(total, bool):
        raise ValueError(f"Decision-context {label} listing did not report an integer total.")
    return total


def _compact_notes(notes: list[JsonObject]) -> list[JsonObject]:
    return [_compact_note(note) for note in notes]


def _search_truncation_payload(
    search_payload: JsonObject,
    key: str,
    total_key: str,
    limit: int,
) -> JsonObject:
    items = search_items(search_payload, key)
    meta = search_payload.get("meta")
    total = meta.get(total_key) if isinstance(meta, dict) else None
    return {
        "data": items,
        "meta": {
            "limit": limit,
            "offset": 0,
            "total": total if isinstance(total, int) else len(items),
        },
    }


def _compact_note(note: JsonObject) -> JsonObject:
    compacted = dict(note)
    truncated_fields: JsonObject = {}
    for field in ("raw_content", "transcribed_text"):
        value = compacted.get(field)
        if not isinstance(value, str):
            continue
        truncated, was_truncated = _truncate_text(value, NOTE_TEXT_FIELD_LIMIT)
        if was_truncated:
            compacted[field] = truncated
            truncated_fields[field] = {
                "original_length": len(value),
                "returned_length": len(truncated),
            }
    if truncated_fields:
        existing = compacted.get("truncated_fields")
        if isinstance(existing, dict):
            truncated_fields = {**existing, **truncated_fields}
        compacted["truncated_fields"] = truncated_fields
    return compacted


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return f"{value[: limit - 3]}...", True
