"""Selection and compaction helpers for decision-context payloads."""

from __future__ import annotations

from lab_tracker.decision_context_types import JsonObject


def envelope_items(payload: JsonObject) -> list[JsonObject]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [dict(item) for item in data if isinstance(item, dict)]


def search_items(payload: JsonObject, key: str) -> list[JsonObject]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    items = data.get(key)
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def meta_total(payload: JsonObject, fallback_key: str | None = None) -> int | None:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("total")
    if value is None and fallback_key is not None:
        value = meta.get(fallback_key)
    return value if isinstance(value, int) else None


def find_by_id(
    items: list[JsonObject],
    id_key: str,
    entity_id: str,
) -> JsonObject | None:
    return next((item for item in items if str(item.get(id_key)) == entity_id), None)


def project_lookup(projects: list[JsonObject]) -> dict[str, JsonObject]:
    return {
        str(project["project_id"]): project
        for project in projects
        if project.get("project_id") is not None
    }


def project_ids_from_search(search_payload: JsonObject) -> set[str]:
    project_ids: set[str] = set()
    for question in search_items(search_payload, "questions"):
        if question.get("project_id") is not None:
            project_ids.add(str(question["project_id"]))
    for note in search_items(search_payload, "notes"):
        if note.get("project_id") is not None:
            project_ids.add(str(note["project_id"]))
    return project_ids


def merge_entities(
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
