"""Structured MCP tool response hints."""

from __future__ import annotations

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
