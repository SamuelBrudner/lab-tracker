# Analysis Graph Drafts From Commits and CI

Lab Tracker can let an analysis repository or CI job ask an LLM to interpret
analysis evidence and propose graph updates without letting automation commit
those updates.

The safe workflow is:

1. An analysis run (or a git commit) produces evidence.
2. A note is created in Lab Tracker for that evidence, or an existing note is reused.
3. The client calls `POST /notes/{note_id}/analysis-graph-drafts`.
4. Lab Tracker calls the configured graph draft model and stores a `GraphChangeSet`.
5. A human reviews, edits, accepts, rejects, and commits operations in Lab Tracker.

Automation never calls the graph commit endpoint. Every operation is a draft for
human review; nothing commits without explicit acceptance.

## API

Create a draft from an existing evidence note:

```bash
curl -X POST \
  "$LAB_TRACKER_BASE_URL/notes/$NOTE_ID/analysis-graph-drafts" \
  -H "Authorization: Bearer $LAB_TRACKER_TOKEN"
```

The source note should contain the analysis evidence the model should reason over:
methods, code version, environment hash, dataset IDs, outputs, figures, metrics, and
the interpretation brief. The model also receives current project context from Lab
Tracker before proposing operations.

The API uses `analysis-graph-draft-v2` as the prompt version. Drafts use the same
`GraphChangeSet` and `GraphChangeOperation` review model as image and batch graph
drafts, so they appear in the existing review UI at `/app/graph-drafts/{change_set_id}`.

The model call happens inside the Lab Tracker API process, so that process must have
`LAB_TRACKER_OPENAI_API_KEY` (or the configured provider's key) and related model
settings configured. The graph-draft provider is selected by
`LAB_TRACKER_GRAPH_DRAFT_PROVIDER` (`openai`, `anthropic`, or `google`).

## CI Script

`scripts/create-analysis-graph-draft.py` creates the evidence note and requests the
draft in one call. It can draft from an existing note:

```bash
python scripts/create-analysis-graph-draft.py --note-id "$NOTE_ID"
```

from an evidence file:

```bash
python scripts/create-analysis-graph-draft.py \
  --project-id "$PROJECT_ID" \
  --evidence-file analysis-evidence.md
```

or directly from a git commit:

```bash
python scripts/create-analysis-graph-draft.py \
  --project-id "$PROJECT_ID" \
  --git-repo /path/to/analysis/repo \
  --git-commit HEAD
```

Commit evidence includes the commit metadata, file summary, and a capped textual
diff. Use `LAB_TRACKER_GIT_MAX_DIFF_LINES` or `--git-max-diff-lines` to adjust the
diff cap, and `LAB_TRACKER_GIT_CONTEXT_LINES` or `--git-context-lines` for the
unified diff context.

Required environment:

- `LAB_TRACKER_BASE_URL` or `LAB_TRACKER_MCP_BASE_URL`
- one of:
  - `LAB_TRACKER_TOKEN` (preferred — a personal access token)
  - `LAB_TRACKER_USERNAME` and `LAB_TRACKER_PASSWORD` (deprecated login; prefer a token)
  - no auth variables when the target API has local auth disabled

The script prints the created note id and the stored change set as JSON. Run it from
any CI job that can reach the Lab Tracker API; for a laptop or lab-network API, use a
self-hosted runner on that machine or network.

## Git Post-Commit Hook

For local analysis repositories, install a `post-commit` hook that sends each new
commit to Lab Tracker as staged analysis evidence:

```bash
cd /path/to/analysis-repo
lt hooks install --project "$PROJECT_ID" --dry-run
lt hooks install --project "$PROJECT_ID" --yes
```

The hook calls the packaged `lt git snapshot` after a commit succeeds. It only
lands the capture in the evidence inbox; proposal generation waits for the
configured daily-review schedule (or an explicit on-demand review trigger). It
does not block or rewrite the commit: if Lab Tracker is down or credentials are
missing, the evidence remains in the local outbox and the hook prints a warning
before exiting successfully. The installer writes a single managed block
(delimited by `# --- BEGIN/END LAB TRACKER GRAPH DRAFT HOOK ---`) and re-running
it updates that block in place; pass `--force` to append the block to a
pre-existing unmanaged hook. The older PowerShell installer upgrades in place
because it uses the same markers.

Set these environment variables to override the installed defaults without editing
the hook:

- `LAB_TRACKER_GIT_CAPTURE_ENABLED=0` disables the hook temporarily
- `LAB_TRACKER_GIT_DRAFT_ENABLED=0` is the legacy alias for disabling it
- `LAB_TRACKER_BASE_URL` points at a different Lab Tracker API
- `LAB_TRACKER_PROJECT_ID` changes the target project
- `LAB_TRACKER_LT` chooses the `lt` executable used by the hook

## Relationship to the evidence inbox

`lt import-folder` ingests files from a folder as staged evidence *notes* and does
not call a model. This analysis-graph-draft flow is distinct: it turns a single
note or commit into a *reviewable graph-draft proposal*. Use the folder inbox to
capture artifacts; use this flow when you want the model to propose graph edits a
scientist can approve.
