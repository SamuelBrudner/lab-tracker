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


async def _run() -> None:
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
                }
            },
        )
    print(json.dumps({"result": "ok"}))


if __name__ == "__main__":
    asyncio.run(_run())
