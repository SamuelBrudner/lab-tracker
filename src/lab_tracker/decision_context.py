"""Read-only decision context assembly for assistant clients."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from lab_tracker.repository import LabTrackerRepository

JsonObject = dict[str, Any]

TASK_KIND_VALUES = (
    "plot",
    "analysis",
    "slides",
    "experiment_plan",
    "summary",
    "research_writing",
)
TASK_KIND_TEXT = ", ".join(TASK_KIND_VALUES)
CONTEXT_LOOKUP_LIMIT = 500

AGENT_CONSULTATION_POLICY = """# Lab Tracker Agent Consultation Policy

Before research-facing decisions, consult the Lab Tracker MCP server. This
includes choosing variables to plot, analyses to run, figures or slides to make,
experimental controls to prioritize, summaries to write, and research writing
such as manuscripts, grants, abstracts, results, discussion text, and figure
legends.

Prefer `lab_tracker_get_decision_context` when available. Otherwise use
`lab_tracker_list_projects`, `lab_tracker_search`, `lab_tracker_list_questions`,
`lab_tracker_list_notes`, and the low-level dataset, analysis, claim, and
visualization read tools.

If Lab Tracker is unavailable or ambiguous, state that explicitly. Do not create
or mutate Lab Tracker records unless the user explicitly asks.
"""


class DecisionContextReader(Protocol):
    """Read-only envelope-returning graph reader for decision context."""

    def list_projects(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        ...

    def list_questions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        parent_question_id: str | None = None,
        ancestor_question_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        ...

    def list_notes(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        ...

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        include: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> JsonObject:
        ...

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        ...

    def list_datasets(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        ...

    def list_analyses(
        self,
        *,
        project_id: str | None = None,
        dataset_id: str | None = None,
        question_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        ...

    def list_claims(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        dataset_id: str | None = None,
        analysis_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        ...

    def list_visualizations(
        self,
        *,
        project_id: str | None = None,
        analysis_id: str | None = None,
        claim_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        ...


class RepositoryDecisionContextReader:
    """Decision-context reader backed by a request-scoped repository."""

    def __init__(self, repository: LabTrackerRepository) -> None:
        self._repository = repository

    def list_projects(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        items, total = self._repository.query_projects(
            status=status,
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def list_questions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        parent_question_id: str | None = None,
        ancestor_question_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        items, total = self._repository.query_questions(
            project_id=_uuid_or_none(project_id),
            status=status,
            question_type=question_type,
            search=search,
            parent_question_id=_uuid_or_none(parent_question_id),
            ancestor_question_id=_uuid_or_none(ancestor_question_id),
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def list_notes(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        items, total = self._repository.query_notes(
            project_id=_uuid_or_none(project_id),
            status=status,
            target_entity_type=target_entity_type,
            target_entity_id=_uuid_or_none(target_entity_id),
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        include: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> JsonObject:
        include_set = {
            item.strip().casefold()
            for item in (include.split(",") if include else ["questions", "notes"])
            if item.strip()
        }
        resolved_project_id = _uuid_or_none(project_id)
        questions = (
            self._repository.query_questions(
                project_id=resolved_project_id,
                search=query,
                limit=limit,
                offset=offset,
            )[0]
            if not include_set or "questions" in include_set
            else []
        )
        notes = (
            self._repository.query_notes(
                project_id=resolved_project_id,
                search=query,
                limit=limit,
                offset=offset,
            )[0]
            if not include_set or "notes" in include_set
            else []
        )
        return {
            "data": {
                "questions": [_entity_to_json(item) for item in questions],
                "notes": [_entity_to_json(item) for item in notes],
            },
            "meta": {"questions_count": len(questions), "notes_count": len(notes)},
        }

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        items, total = self._repository.query_sessions(
            project_id=_uuid_or_none(project_id),
            status=status,
            session_type=session_type,
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def list_datasets(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        items, total = self._repository.query_datasets(
            project_id=_uuid_or_none(project_id),
            status=status,
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def list_analyses(
        self,
        *,
        project_id: str | None = None,
        dataset_id: str | None = None,
        question_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        items, total = self._repository.query_analyses(
            project_id=_uuid_or_none(project_id),
            dataset_id=_uuid_or_none(dataset_id),
            question_id=_uuid_or_none(question_id),
            status=status,
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def list_claims(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        dataset_id: str | None = None,
        analysis_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        items, total = self._repository.query_claims(
            project_id=_uuid_or_none(project_id),
            status=status,
            dataset_id=_uuid_or_none(dataset_id),
            analysis_id=_uuid_or_none(analysis_id),
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def list_visualizations(
        self,
        *,
        project_id: str | None = None,
        analysis_id: str | None = None,
        claim_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        items, total = self._repository.query_visualizations(
            project_id=_uuid_or_none(project_id),
            analysis_id=_uuid_or_none(analysis_id),
            claim_id=_uuid_or_none(claim_id),
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)


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
        return _decision_error(
            "invalid_task_kind",
            f"Invalid task kind {task_kind!r}. Allowed task kinds: {TASK_KIND_TEXT}.",
            allowed_task_kinds=list(TASK_KIND_VALUES),
        )
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        return _decision_error("invalid_query", "Decision-context query must not be empty.")

    resolved_limit = _validate_context_limit(limit)
    projects_payload = reader.list_projects(limit=CONTEXT_LOOKUP_LIMIT)
    projects = _envelope_items(projects_payload)
    projects_by_id = _project_lookup(projects)

    resolved_project_id = str(project_id) if project_id else None
    if resolved_project_id and resolved_project_id not in projects_by_id:
        return _decision_error(
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
        questions = _envelope_items(
            reader.list_questions(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        question = _find_by_id(questions, "question_id", str(question_id))
        if question is None:
            return _decision_error(
                "anchor_not_found",
                f"Question {question_id!r} was not found.",
                anchor={"entity_type": "question", "entity_id": str(question_id)},
            )
        anchor_entities["questions"].append(question)
        anchor_project_ids.add(str(question["project_id"]))

    if dataset_id:
        datasets = _envelope_items(
            reader.list_datasets(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        dataset = _find_by_id(datasets, "dataset_id", str(dataset_id))
        if dataset is None:
            return _decision_error(
                "anchor_not_found",
                f"Dataset {dataset_id!r} was not found.",
                anchor={"entity_type": "dataset", "entity_id": str(dataset_id)},
            )
        anchor_entities["datasets"].append(dataset)
        anchor_project_ids.add(str(dataset["project_id"]))

    if analysis_id:
        analyses = _envelope_items(
            reader.list_analyses(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        analysis = _find_by_id(analyses, "analysis_id", str(analysis_id))
        if analysis is None:
            return _decision_error(
                "anchor_not_found",
                f"Analysis {analysis_id!r} was not found.",
                anchor={"entity_type": "analysis", "entity_id": str(analysis_id)},
            )
        anchor_entities["analyses"].append(analysis)
        anchor_project_ids.add(str(analysis["project_id"]))

    if claim_id:
        claims = _envelope_items(
            reader.list_claims(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        claim = _find_by_id(claims, "claim_id", str(claim_id))
        if claim is None:
            return _decision_error(
                "anchor_not_found",
                f"Claim {claim_id!r} was not found.",
                anchor={"entity_type": "claim", "entity_id": str(claim_id)},
            )
        anchor_entities["claims"].append(claim)
        anchor_project_ids.add(str(claim["project_id"]))

    if visualization_id:
        visualizations = _envelope_items(
            reader.list_visualizations(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        visualization = _find_by_id(visualizations, "viz_id", str(visualization_id))
        if visualization is None:
            return _decision_error(
                "anchor_not_found",
                f"Visualization {visualization_id!r} was not found.",
                anchor={
                    "entity_type": "visualization",
                    "entity_id": str(visualization_id),
                },
            )
        anchor_entities["visualizations"].append(visualization)
        analyses = _envelope_items(
            reader.list_analyses(
                project_id=resolved_project_id,
                limit=CONTEXT_LOOKUP_LIMIT,
            )
        )
        analysis = _find_by_id(analyses, "analysis_id", str(visualization.get("analysis_id")))
        if analysis is not None:
            anchor_project_ids.add(str(analysis["project_id"]))

    if resolved_project_id:
        anchor_project_ids.add(resolved_project_id)
    if len(anchor_project_ids) > 1:
        return _decision_error(
            "conflicting_anchors",
            "Decision-context anchors resolve to multiple projects.",
            project_ids=sorted(anchor_project_ids),
        )
    if not resolved_project_id and anchor_project_ids:
        resolved_project_id = next(iter(anchor_project_ids))

    if not resolved_project_id:
        global_search_payload = reader.search(cleaned_query, limit=resolved_limit)
        search_project_ids = _project_ids_from_search(global_search_payload)
        if len(search_project_ids) == 1:
            resolved_project_id = next(iter(search_project_ids))
        else:
            candidates = [
                _candidate_project(projects_by_id[item], "search_match")
                for item in sorted(search_project_ids)
                if item in projects_by_id
            ]
            if not candidates:
                candidates = [
                    _candidate_project(project, "active_project")
                    for project in projects
                    if project.get("status") == "active"
                ][:resolved_limit]
            return _decision_error(
                "ambiguous_project",
                "Decision context needs a project or a more specific anchor.",
                candidate_projects=candidates,
            )

    project = projects_by_id.get(resolved_project_id)
    if project is None:
        return _decision_error(
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

    questions = _merge_entities(
        "question_id",
        (anchor_entities["questions"], "anchor"),
        (_search_items(search_payload, "questions"), "search_match"),
        (_envelope_items(questions_payload), "recent_activity"),
    )
    notes = _merge_entities(
        "note_id",
        (_search_items(search_payload, "notes"), "search_match"),
        (_envelope_items(notes_payload), "recent_activity"),
    )
    sessions = _merge_entities(
        "session_id",
        (_envelope_items(sessions_payload), "recent_activity"),
    )
    datasets = _merge_entities(
        "dataset_id",
        (anchor_entities["datasets"], "anchor"),
        (_envelope_items(datasets_payload), "recent_activity"),
    )
    analyses = _merge_entities(
        "analysis_id",
        (anchor_entities["analyses"], "anchor"),
        (_envelope_items(analyses_payload), "recent_activity"),
    )
    claims = _merge_entities(
        "claim_id",
        (anchor_entities["claims"], "anchor"),
        (_envelope_items(claims_payload), "recent_activity"),
    )
    visualizations = _merge_entities(
        "viz_id",
        (anchor_entities["visualizations"], "anchor"),
        (_envelope_items(visualizations_payload), "recent_activity"),
    )

    anchors = [
        _entity_ref("question", item, "question_id")
        for item in anchor_entities["questions"]
    ] + [
        _entity_ref("dataset", item, "dataset_id")
        for item in anchor_entities["datasets"]
    ] + [
        _entity_ref("analysis", item, "analysis_id")
        for item in anchor_entities["analyses"]
    ] + [
        _entity_ref("claim", item, "claim_id") for item in anchor_entities["claims"]
    ] + [
        _entity_ref("visualization", item, "viz_id")
        for item in anchor_entities["visualizations"]
    ]

    evidence_map = _build_evidence_map(datasets, analyses, claims, visualizations)
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
            "task_guidance": _task_guidance(
                cleaned_task_kind,
                cleaned_query,
                questions,
                datasets,
                analyses,
                claims,
                visualizations,
            ),
            "questions": questions,
            "notes": notes,
            "sessions": sessions,
            "datasets": datasets,
            "analyses": analyses,
            "claims": claims,
            "visualizations": visualizations,
            "evidence_map": evidence_map,
            "truncation": _truncation(
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


def _uuid_or_none(value: str | None) -> UUID | None:
    if value is None:
        return None
    return UUID(str(value))


def _entity_to_json(entity: object) -> JsonObject:
    return entity.model_dump(mode="json")  # type: ignore[attr-defined]


def _list_payload(items: list[object], total: int, limit: int, offset: int) -> JsonObject:
    return {
        "data": [_entity_to_json(item) for item in items],
        "meta": {"limit": limit, "offset": offset, "total": total},
    }


def _validate_context_limit(limit: int) -> int:
    try:
        resolved = int(limit)
    except (TypeError, ValueError):
        return 20
    return min(max(resolved, 1), 100)


def _decision_error(code: str, message: str, **metadata: object) -> JsonObject:
    error: JsonObject = {"code": code, "message": message}
    error.update(metadata)
    return {"error": error}


def _envelope_items(payload: JsonObject) -> list[JsonObject]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [dict(item) for item in data if isinstance(item, dict)]


def _search_items(payload: JsonObject, key: str) -> list[JsonObject]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get(key)
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def _meta_total(payload: JsonObject, fallback_key: str | None = None) -> int | None:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("total")
    if value is None and fallback_key is not None:
        value = meta.get(fallback_key)
    return value if isinstance(value, int) else None


def _find_by_id(items: list[JsonObject], id_key: str, entity_id: str) -> JsonObject | None:
    return next((item for item in items if str(item.get(id_key)) == entity_id), None)


def _entity_label(entity_type: str, entity: JsonObject) -> str:
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
        return str(entity.get("caption") or entity.get("file_path") or entity.get("viz_id") or "")
    return str(entity)


def _entity_ref(entity_type: str, entity: JsonObject, id_key: str) -> JsonObject:
    return {
        "entity_type": entity_type,
        "entity_id": str(entity.get(id_key) or ""),
        "label": _entity_label(entity_type, entity),
    }


def _candidate_project(project: JsonObject, reason: str) -> JsonObject:
    return {
        "project_id": str(project.get("project_id") or ""),
        "name": project.get("name"),
        "status": project.get("status"),
        "reason": reason,
    }


def _project_lookup(projects: list[JsonObject]) -> dict[str, JsonObject]:
    return {
        str(project["project_id"]): project
        for project in projects
        if project.get("project_id") is not None
    }


def _project_ids_from_search(search_payload: JsonObject) -> set[str]:
    project_ids: set[str] = set()
    for question in _search_items(search_payload, "questions"):
        if question.get("project_id") is not None:
            project_ids.add(str(question["project_id"]))
    for note in _search_items(search_payload, "notes"):
        if note.get("project_id") is not None:
            project_ids.add(str(note["project_id"]))
    return project_ids


def _merge_entities(
    id_key: str,
    *groups: tuple[list[JsonObject], str],
) -> list[JsonObject]:
    merged: dict[str, JsonObject] = {}
    order: list[str] = []
    for items, reason in groups:
        for item in items:
            entity_id = item.get(id_key)
            if entity_id is None:
                continue
            key = str(entity_id)
            if key not in merged:
                next_item = dict(item)
                next_item["relevance_reasons"] = []
                merged[key] = next_item
                order.append(key)
            reasons = merged[key]["relevance_reasons"]
            if isinstance(reasons, list) and reason not in reasons:
                reasons.append(reason)
    return [merged[key] for key in order]


def _build_evidence_map(
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
                "entity": _entity_ref("dataset", dataset, "dataset_id"),
                "questions": sorted(set(question_ids)),
                "reason": "dataset_question_links",
            }
        )
    for analysis in analyses:
        evidence.append(
            {
                "entity": _entity_ref("analysis", analysis, "analysis_id"),
                "datasets": [str(item) for item in analysis.get("dataset_ids", [])],
                "reason": "analysis_dataset_links",
            }
        )
    for claim in claims:
        evidence.append(
            {
                "entity": _entity_ref("claim", claim, "claim_id"),
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
                "entity": _entity_ref("visualization", visualization, "viz_id"),
                "analysis_id": str(visualization.get("analysis_id") or ""),
                "claims": [
                    str(item) for item in visualization.get("related_claim_ids", [])
                ],
                "reason": "visualization_links",
            }
        )
    return evidence


def _task_guidance(
    task_kind: str,
    query: str,
    questions: list[JsonObject],
    datasets: list[JsonObject],
    analyses: list[JsonObject],
    claims: list[JsonObject],
    visualizations: list[JsonObject],
) -> JsonObject:
    focus = [_entity_label("question", item) for item in questions[:3]] or [query]
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
            _entity_ref("dataset", item, "dataset_id") for item in datasets[:5]
        ] + [_entity_ref("visualization", item, "viz_id") for item in visualizations[:5]]
    elif task_kind == "analysis":
        candidate_outputs = [
            _entity_ref("analysis", item, "analysis_id") for item in analyses[:5]
        ] + [_entity_ref("dataset", item, "dataset_id") for item in datasets[:5]]
    elif task_kind == "slides":
        candidate_outputs = [
            _entity_ref("visualization", item, "viz_id") for item in visualizations[:5]
        ] + [_entity_ref("claim", item, "claim_id") for item in claims[:5]]
    elif task_kind == "experiment_plan":
        candidate_outputs = [
            _entity_ref("question", item, "question_id") for item in questions[:5]
        ]
    elif task_kind == "research_writing":
        candidate_outputs = [
            _entity_ref("claim", item, "claim_id") for item in claims[:5]
        ] + [_entity_ref("visualization", item, "viz_id") for item in visualizations[:5]]
    else:
        candidate_outputs = [
            _entity_ref("question", item, "question_id") for item in questions[:5]
        ]

    return {
        "recommended_focus": focus,
        "candidate_outputs": candidate_outputs,
        "caveats": caveats,
        "missing_evidence": missing_evidence,
    }


def _truncation(sections: list[tuple[str, JsonObject, str | None]]) -> JsonObject:
    truncated_sections: list[JsonObject] = []
    for name, payload, fallback_key in sections:
        returned = len(_envelope_items(payload))
        total = _meta_total(payload, fallback_key)
        if total is not None and total > returned:
            truncated_sections.append(
                {"section": name, "returned": returned, "total": total}
            )
    return {
        "was_truncated": bool(truncated_sections),
        "sections": truncated_sections,
    }
