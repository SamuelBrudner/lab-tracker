# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
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

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
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

## Architecture Overview

Lab Tracker is a FastAPI and SQLAlchemy app for preserving the reasoning around lab work: projects, questions, acquisition sessions, datasets, notes, analyses, claims, and visualizations. Alembic owns database migrations. The frontend source lives in `src/lab_tracker/frontend_src`, and the committed bundle served by the API lives in `src/lab_tracker/frontend`.

## Conventions & Patterns

The supported runtime surface is defined by `docs/retained-v1-surface.md`; if it conflicts with README prose, the retained surface wins. Windows fresh-clone setup, including Beads/Dolt bootstrap notes, lives in `docs/windows-fresh-clone.md`.
