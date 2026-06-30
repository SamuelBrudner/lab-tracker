# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Build & Test

Python setup:

```bash
uv venv
uv pip install -e ".[test,lint]"
uv run alembic upgrade head
```

Backend validation:

```bash
uv run pytest -q
uv run ruff check .
```

Frontend validation, only when `src/lab_tracker/frontend_src` or the committed bundle changes:

```bash
npm install
npm run test:frontend
npm run lint:frontend
npm run build
```

Run the API with `uv run uvicorn lab_tracker.asgi:app --reload`; the app is served at `http://127.0.0.1:8000/app`.

### Database migrations

Alembic owns the schema. Filename prefixes (`NNNN_`) are **decorative** — Alembic chains on the `revision`/`down_revision` strings, not the number, and revision IDs are immutable once deployed (they are recorded in each database's `alembic_version`). So never renumber an existing migration, and never assume the highest-numbered file is the head — branches exist (e.g. the `0017_*` fork reconciled by `0019_merge_*`).

Before adding a migration:

```bash
uv run alembic heads   # must print exactly one; set your down_revision to it
```

If two heads ever appear, reconcile them with `uv run alembic merge` rather than editing existing revisions. The `test_alembic_has_single_head` test enforces a single head.

## Architecture Overview

Lab Tracker is a FastAPI and SQLAlchemy app for preserving the reasoning around lab work: projects, questions, acquisition sessions, datasets, notes, analyses, claims, and visualizations. Alembic owns database migrations. The frontend source lives in `src/lab_tracker/frontend_src`, and the committed bundle served by the API lives in `src/lab_tracker/frontend`.

## Conventions & Patterns

The supported runtime surface is defined by `docs/retained-v1-surface.md`; if it conflicts with README prose, the retained surface wins. Windows fresh-clone setup, including Beads/Dolt bootstrap notes, lives in `docs/windows-fresh-clone.md`.
