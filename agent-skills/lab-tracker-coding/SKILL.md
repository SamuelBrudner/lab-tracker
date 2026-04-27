---
name: lab-tracker-coding
description: Use when coding on Lab Tracker, including domain-model changes, MCP/tooling changes, migrations, tests, or scientific workflow behavior that should account for live Lab Tracker project/question/note/session context.
---

# Lab Tracker Coding

Use this skill when changing Lab Tracker code or tests.

1. Read `AGENTS.md` first and follow the bead, validation, commit, and push workflow.
2. Inspect relevant repo docs before editing:
   - `README.md` for runtime setup and supported surfaces.
   - `docs/retained-v1-surface.md` for product boundaries.
   - `docs/mcp.md` and `docs/coding-agent-support.md` for LLM/MCP surfaces.
3. Use normal filesystem/search tools for source code, schemas, migrations, tests, and docs.
4. When live lab context could affect the implementation, connect the read-only coding MCP
   profile and query it before deciding:
   - Start with `coding_lab_context`.
   - Use `coding_search_lab` for relevant questions or notes.
   - Use `coding_project_context` when the task names a project id.
5. Keep coding-agent MCP access read-only. Do not create, update, activate, close, archive,
   delete, or commit Lab Tracker records through the coding profile.
6. Preserve the existing ChatGPT App MCP surface unless the user explicitly asks to change it.

Prefer small, domain-aligned changes with focused tests. For shared domain behavior, update
backend tests first; run `uv run pytest -q`, `uv run ruff check .`, and any frontend checks
only when frontend tooling or source changes.
