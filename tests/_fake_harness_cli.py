"""A fake external-harness CLI standing in for Claude Code in tests.

The real runner launches a vendor CLI with a ``--mcp-config`` JSON pointing at
the per-run loopback MCP server. This stand-in parses that config, connects as an
MCP client, performs one scoped read, and submits a graph patch through the
propose-only ``submit_graph_patch`` tool — exercising the full runtime path
(loopback serve -> live scoped read -> server-side capture) without a real CLI.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _mcp_config_from_argv(argv: list[str]) -> dict[str, Any]:
    for index, arg in enumerate(argv):
        if arg == "--mcp-config" and index + 1 < len(argv):
            return json.loads(argv[index + 1])
    raise SystemExit("fake harness CLI: no --mcp-config provided")


def _note_dispositions_from_prompt(prompt: str) -> list[dict[str, Any]]:
    """Honor the per-note checklist in the batch context packet.

    The prompt embeds the packet between <untrusted_batch_context> tags; its
    note_ids_requiring_disposition array is the coverage contract every
    drafter must satisfy (bead lab-tracker-hymd).
    """
    start_tag = "<untrusted_batch_context>"
    end_tag = "</untrusted_batch_context>"
    start = prompt.find(start_tag)
    end = prompt.find(end_tag)
    if start < 0 or end < 0:
        return []
    packet = json.loads(prompt[start + len(start_tag) : end].strip())
    unavailable = {
        str(note.get("id"))
        for note in packet.get("batch_notes") or []
        if isinstance(note, dict)
        and (note.get("sensitive_content_omitted") or note.get("sensitive_content_redacted"))
    }
    return [
        {
            "note_id": str(note_id),
            "disposition": (
                "insufficient_info" if str(note_id) in unavailable else "no_change"
            ),
            "reason": "fake harness considered this note and proposes no change",
            "evidence_quote": "",
            "client_refs": [],
        }
        for note_id in packet.get("note_ids_requiring_disposition") or []
    ]


async def _run() -> None:
    prompt = sys.stdin.read()
    if not prompt.strip():
        raise SystemExit("fake harness CLI: no stdin prompt provided")
    config = _mcp_config_from_argv(sys.argv)
    server = config["mcpServers"]["lt"]
    async with streamablehttp_client(server["url"], headers=server.get("headers") or {}) as (
        read,
        write,
        _,
    ), ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("search", {"query": "gel"})
        await session.call_tool(
            "submit_graph_patch",
            {
                "graph_patch": {
                    "summary": "fake harness proposal",
                    "uncertain_fields": [],
                    "clarification_requests": [],
                    "operations": [],
                    "note_dispositions": _note_dispositions_from_prompt(prompt),
                }
            },
        )
    print(json.dumps({"result": "ok"}))


if __name__ == "__main__":
    asyncio.run(_run())
