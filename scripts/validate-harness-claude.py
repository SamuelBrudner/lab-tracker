"""Live validation of the external-harness wiring against the REAL Claude Code CLI.

Run this on a machine where `claude` is on PATH and authenticated. It:
  1. builds a per-run scoped MCP over loopback (fake read tool + submit tool),
  2. invokes the real Claude Code CLI with the SAME argv and stdin prompt the
     production runner builds (via graph_drafting._build_harness_launch_argv),
  3. reports whether Claude connected, called the scoped read tool, and submitted
     a graph patch captured server-side.

It deliberately bypasses the sandbox fail-closed gate (this is a wiring check,
not a production run) and makes a real model call, so it costs a few tokens.

Usage:
    uv run python scripts/validate-harness-claude.py            # uses `claude`
    uv run python scripts/validate-harness-claude.py --claude "C:/path/to/claude.cmd"
    uv run python scripts/validate-harness-claude.py --timeout 180
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

from lab_tracker.graph_drafting import (
    EXTERNAL_HARNESS_LAUNCH_TABLE,
    _build_harness_launch_argv,
)
from lab_tracker.services.graph_draft_harness_mcp import (
    HarnessGraphDraftMCPServer,
    HarnessMCPLoopbackServer,
)


class _FakeScopedExecutor:
    """A tiny scoped read surface so Claude has something concrete to read."""

    def __init__(self) -> None:
        self.sensitivity_policy = "omit"
        self.calls: list[str] = []

    def mcp_tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "list_notes",
                "description": "List the staged notes in the current project's batch.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        ]

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(name)
        return {
            "notes": [
                {"note_id": "note-1", "preview": "Gel photo A looked clean at lane 2."},
                {"note_id": "note-2", "preview": "Voice memo: repeat the ATF4 titration."},
            ]
        }


PROMPT = (
    "You are drafting a graph patch for a lab-tracking system. First call the "
    "lab-tracker MCP tool `list_notes` to see the staged notes. Then call "
    "`submit_graph_patch` exactly once with a minimal valid patch: an object with "
    'keys "summary" (a short string), "uncertain_fields" (empty array), '
    '"clarification_requests" (empty array), and "operations" (empty array). Use '
    "no other tools."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude", default="claude", help="Claude Code executable/command")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    executor = _FakeScopedExecutor()
    mcp = HarnessGraphDraftMCPServer(executor=executor, max_tool_calls=6)
    launch = EXTERNAL_HARNESS_LAUNCH_TABLE["claude_code"]
    # Point the launch at the real (or user-specified) claude command.
    from dataclasses import replace

    launch = replace(launch, command=tuple(args.claude.split()))

    with HarnessMCPLoopbackServer(mcp) as loopback:
        argv = _build_harness_launch_argv(
            launch,
            mcp_url=loopback.url,
            mcp_token=loopback.token,
            trace={},
        )
        print("Loopback MCP:", loopback.url)
        print("Invoking:", " ".join(a if len(a) < 80 else a[:77] + "..." for a in argv))
        print("-" * 70)
        try:
            completed = subprocess.run(
                argv,
                input=PROMPT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=args.timeout,
                check=False,
            )
        except FileNotFoundError:
            print(f"FAIL: claude executable not found: {args.claude!r}")
            print("Pass the full path with --claude if it is not on PATH.")
            return 2
        except subprocess.TimeoutExpired:
            print(f"FAIL: claude did not finish within {args.timeout}s.")
            return 3

        print("claude exit code:", completed.returncode)
        print("--- claude stdout (truncated) ---")
        print((completed.stdout or "")[:4000])
        if completed.stderr:
            print("--- claude stderr (truncated) ---")
            print(completed.stderr[:2000])
        print("-" * 70)
        submitted = mcp.graph_patch

    print("Scoped read tools the child called:", executor.calls or "(none)")
    print("Patch submitted via scoped MCP:", json.dumps(submitted) if submitted else "(none)")
    ok = bool(executor.calls) and submitted is not None
    print()
    result = (
        "PASS -- Claude connected, read via the scoped tool, and submitted."
        if ok
        else "FAIL -- see output above; the argv or MCP config likely needs adjustment."
    )
    print("RESULT:", result)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
