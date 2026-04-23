# Internal Boundaries

This document describes the active runtime boundaries only. Compatibility
surfaces that still exist for historical data handling or staged cleanup are
intentionally omitted unless they participate in the retained v1 runtime.

## Request Context Lifecycle

Each HTTP request gets an explicit `LabTrackerRequestContext` in
[`src/lab_tracker/request_context.py`](../src/lab_tracker/request_context.py).

The lifecycle is:

1. The database middleware creates a request-scoped SQLAlchemy repository.
2. `LabTrackerAPI.build_request_context(...)` creates a context that owns:
   - the active repository
   - deferred `after_commit` and `after_rollback` actions
3. `LabTrackerAPI.bind_request_context(...)` attaches that context to the request-local API instance.
4. Route handlers use `request.state.lab_tracker_api` via `api_from_request(...)`.
5. On exit, middleware commits or rolls back the SQLAlchemy session, then calls `request_context.finish(...)` to run the matching deferred side effects.

Service logic should not depend on hidden globals or `ContextVar` state for request orchestration.

## Repository Layout

The SQLAlchemy repository is now split into focused modules under
[`src/lab_tracker/sqlalchemy_repository_parts`](../src/lab_tracker/sqlalchemy_repository_parts).

- `common.py`: shared pagination/count helpers and the generic model repository
- `core.py`: projects and questions
- `datasets.py`: datasets and attached files
- `notes.py`: notes and note child rows
- `sessions.py`: sessions and acquisition outputs
- `analyses.py`: analyses, claims, and visualizations
- `repository.py`: the top-level `SQLAlchemyLabTrackerRepository` query surface

[`src/lab_tracker/sqlalchemy_repository.py`](../src/lab_tracker/sqlalchemy_repository.py) remains as the import-stable compatibility barrel.

## Route Layout

Mixed-resource route modules have been replaced with one-resource routers under
[`src/lab_tracker/routes`](../src/lab_tracker/routes).

Examples:

- `projects.py`, `questions.py`
- `datasets.py`, `dataset_files.py`
- `notes.py`, `search.py`
- `sessions.py`, `analyses.py`, `claims.py`, `visualizations.py`

Routes keep their existing URLs, envelopes, pagination, and auth requirements.
`search.py` is the retained query surface and stays on the simple substring
behavior documented in
[`docs/retained-v1-surface.md`](retained-v1-surface.md),
not semantic/vector retrieval.

## Frontend Data Loading and Downloads

Workspace state is no longer concentrated in one hook.

- [`useProjectWorkspaceData.js`](../src/lab_tracker/frontend_src/hooks/useProjectWorkspaceData.js) owns project/resource loading and selection
- [`useProjectWorkspaceForms.js`](../src/lab_tracker/frontend_src/hooks/useProjectWorkspaceForms.js) owns form state
- [`useProjectWorkspaceActions.js`](../src/lab_tracker/frontend_src/hooks/useProjectWorkspaceActions.js) owns mutations and refresh behavior

Protected browser downloads must go through
[`downloadProtectedResource(...)`](../src/lab_tracker/frontend_src/shared/api.js),
not plain anchors, so bearer-token auth is preserved for note raw assets and dataset files.

Oversized feature modules now export smaller workflow components from focused folders under
[`src/lab_tracker/frontend_src/features`](../src/lab_tracker/frontend_src/features).
