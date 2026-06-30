"""Rank open Lab Tracker questions that advance active goals."""

from __future__ import annotations

from typing import Any

JsonObject = dict[str, Any]

OPEN_GOAL_STATUSES = ("planned", "in_progress")
OPEN_QUESTION_STATUSES = ("active", "staged")

RESEARCH_PROMPT_TRIGGERS = (
    "analysis",
    "analyze",
    "abstract",
    "claim",
    "control",
    "dataset",
    "discussion",
    "experiment",
    "figure",
    "grant",
    "manuscript",
    "plot",
    "results",
    "slide",
    "summary",
    "visualization",
)


def is_research_facing_prompt(text: str) -> bool:
    """Return true when a prompt should receive Lab Tracker graph context."""

    folded = text.casefold()
    return any(trigger in folded for trigger in RESEARCH_PROMPT_TRIGGERS)


def build_next_questions_payload(
    goals: list[JsonObject],
    questions: list[JsonObject],
    claims: list[JsonObject],
    *,
    limit: int = 5,
) -> JsonObject:
    """Build a ranked ready-work-style list from goal/question graph records."""

    resolved_limit = min(max(int(limit or 5), 1), 20)
    open_goals = [
        goal for goal in goals if str(goal.get("status") or "") in OPEN_GOAL_STATUSES
    ]
    answered_question_ids = _answered_question_ids(claims)
    open_questions = [
        question
        for question in questions
        if str(question.get("status") or "") in OPEN_QUESTION_STATUSES
        and _record_id(question, "question_id") not in answered_question_ids
    ]
    if not open_goals:
        return _empty_payload(
            "No planned or in-progress goals are visible. Create or activate a goal "
            "before asking what research thread to advance."
        )
    if not open_questions:
        return _empty_payload(
            "No active or staged unanswered questions are visible for the active goals."
        )

    ranked: list[JsonObject] = []
    seen_pairs: set[tuple[str, str]] = set()
    for goal in open_goals:
        goal_id = _record_id(goal, "goal_id")
        goal_project_id = _optional_record_id(goal, "project_id")
        goal_question_links = _goal_question_links(goal)
        for question in open_questions:
            question_id = _record_id(question, "question_id")
            question_project_id = _optional_record_id(question, "project_id")
            if (
                question_id not in goal_question_links
                and goal_project_id
                and question_project_id
                and goal_project_id != question_project_id
            ):
                continue
            pair = (goal_id, question_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            ranked.append(_ranked_item(goal, question, goal_question_links))

    ranked.sort(
        key=lambda item: (
            -int(item["score"]),
            str(item["goal"]["title"]),
            str(item["question"]["text"]),
        )
    )
    selected = ranked[:resolved_limit]
    payload: JsonObject = {
        "data": selected,
        "meta": {
            "limit": resolved_limit,
            "total_candidates": len(ranked),
            "goal_statuses": list(OPEN_GOAL_STATUSES),
            "question_statuses": list(OPEN_QUESTION_STATUSES),
            "ranking": (
                "direct goal-question links first, then active over staged status, "
                "then questions carrying hypotheses."
            ),
        },
    }
    if selected:
        first = selected[0]
        payload["next_action"] = {
            "tool": "lab_tracker_get_decision_context",
            "arguments": {
                "task_kind": "analysis",
                "query": first["question"]["text"],
                "project_id": first["question"].get("project_id"),
                "question_id": first["question"]["question_id"],
            },
            "reason": "Load graph context for the highest-ranked open question.",
        }
    return payload


def _ranked_item(
    goal: JsonObject,
    question: JsonObject,
    goal_question_links: dict[str, JsonObject],
) -> JsonObject:
    question_id = _record_id(question, "question_id")
    score = 0
    rationale: list[str] = []
    if question_id in goal_question_links:
        score += 100
        rationale.append("question is linked directly to the active goal")
        if goal_question_links[question_id].get("link_status") == "committed":
            score += 5
            rationale.append("goal link is committed")
    else:
        score += 10
        rationale.append("open question is in the same project as the active goal")

    status = str(question.get("status") or "")
    if status == "active":
        score += 20
        rationale.append("question is active")
    elif status == "staged":
        score += 10
        rationale.append("question is staged")

    if question.get("hypothesis"):
        score += 3
        rationale.append("question includes a hypothesis")

    return {
        "goal": {
            "goal_id": _record_id(goal, "goal_id"),
            "title": str(goal.get("title") or ""),
            "status": str(goal.get("status") or ""),
            "project_id": _optional_record_id(goal, "project_id"),
        },
        "question": {
            "question_id": question_id,
            "text": str(question.get("text") or ""),
            "status": status,
            "project_id": _optional_record_id(question, "project_id"),
        },
        "score": score,
        "rationale": rationale,
    }


def _empty_payload(reason: str) -> JsonObject:
    return {
        "data": [],
        "meta": {
            "limit": 0,
            "total_candidates": 0,
            "empty_reason": reason,
            "goal_statuses": list(OPEN_GOAL_STATUSES),
            "question_statuses": list(OPEN_QUESTION_STATUSES),
        },
        "next_action": {
            "tool": "lab_tracker_list_goals",
            "arguments": {},
            "reason": (
                f"{reason} Review existing goals; ask the user before creating or "
                "activating one — do not create a goal unprompted."
            ),
        },
    }


def _answered_question_ids(claims: list[JsonObject]) -> set[str]:
    answered: set[str] = set()
    for claim in claims:
        for value in claim.get("answers_question_ids") or []:
            answered.add(str(value))
    return answered


def _goal_question_links(goal: JsonObject) -> dict[str, JsonObject]:
    links: dict[str, JsonObject] = {}
    for link in goal.get("links") or []:
        if not isinstance(link, dict):
            continue
        if str(link.get("entity_type") or "") != "question":
            continue
        if str(link.get("link_status") or "") == "dropped":
            continue
        entity_id = str(link.get("entity_id") or "")
        if entity_id:
            links[entity_id] = link
    return links


def _record_id(record: JsonObject, key: str) -> str:
    return str(record.get(key) or "")


def _optional_record_id(record: JsonObject, key: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    return str(value)
