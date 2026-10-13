"""Structured MCP tool response hints."""

from __future__ import annotations

from uuid import UUID

from lab_tracker.mcp_api_client import JsonObject


def next_action(
    tool: str | None,
    reason: str,
    *,
    arguments: JsonObject | None = None,
    action: str | None = None,
) -> JsonObject:
    payload: JsonObject = {
        "tool": tool,
        "arguments": arguments or {},
        "reason": reason,
    }
    if action is not None:
        payload["action"] = action
    return payload


def with_next_action(payload: JsonObject, hint: JsonObject) -> JsonObject:
    if "next_action" in payload:
        return payload
    enriched = dict(payload)
    enriched["next_action"] = hint
    return enriched


ORIENTATION_CHECKPOINT_ACTION = "suggest_orientation_checkpoint"
# Every list a decision-context packet fills from the project's records.
_DECISION_CONTEXT_RECORD_LISTS = (
    "questions",
    "notes",
    "sessions",
    "datasets",
    "analyses",
    "claims",
    "visualizations",
    "exploration_nodes",
)


def empty_project_id(overview: JsonObject) -> str | None:
    """The project id when a graph-overview payload shows no records at all.

    Empty means every persisted graph type counts zero and nothing was ever
    captured. Anything else, including a payload without those fields (an
    error, an older server), is not called empty, so the hint is never guessed.
    """

    data = overview.get("data")
    if not isinstance(data, dict):
        return None
    counts = data.get("counts")
    coverage = data.get("coverage")
    project = data.get("project")
    if not (isinstance(counts, dict) and counts and isinstance(coverage, dict)):
        return None
    if not isinstance(project, dict) or "last_capture_at" not in coverage:
        return None
    if coverage["last_capture_at"] is not None:
        return None
    totals = [count.get("total") if isinstance(count, dict) else None for count in counts.values()]
    if not all(type(total) is int and total == 0 for total in totals):
        return None
    return _project_uuid(project.get("project_id"))


def empty_decision_context_project_id(payload: JsonObject) -> str | None:
    """The scoped project id when a decision-context packet holds no records.

    Only a hint to confirm with the graph overview: the packet's lists are
    query- and filter-bound, so empty lists alone do not prove an empty project.
    """

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    for name in _DECISION_CONTEXT_RECORD_LISTS:
        records = data.get(name)
        if not isinstance(records, list) or records:
            return None
    scope = data.get("scope")
    project = scope.get("project") if isinstance(scope, dict) else None
    return _project_uuid(project.get("project_id") if isinstance(project, dict) else None)


def _project_uuid(value: object) -> str | None:
    """A canonical project UUID, or ``None``: returned fields are untrusted, and
    this one is written into the hint an agent reads."""

    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def orientation_checkpoint_hint(project_id: str) -> JsonObject:
    """Suggest the web app's Orient checkpoint for a project with no records."""

    return next_action(
        None,
        "This project has no records yet: every graph type counts zero and "
        "nothing has been captured. If the person's work is already underway, "
        'suggest once that they use "Orient me to this project" in the Lab '
        f"Tracker web app (/app/projects/{project_id}/onboarding). It records "
        "one attributed checkpoint of where the work stands, its live "
        "questions, and the next move, and capture continues from there; "
        "earlier history is not imported. Orienting is the person's action, "
        "so do not create records for it.",
        action=ORIENTATION_CHECKPOINT_ACTION,
    )


def with_orientation_hint(payload: JsonObject, overview: JsonObject) -> JsonObject:
    """``payload`` with the orientation hint when ``overview`` shows no records.

    A ``next_action`` the payload already carries wins, as in
    :func:`with_next_action`.
    """

    project_id = empty_project_id(overview)
    if project_id is None or "next_action" in payload:
        return payload
    enriched = dict(payload)
    enriched["next_action"] = orientation_checkpoint_hint(project_id)
    return enriched
