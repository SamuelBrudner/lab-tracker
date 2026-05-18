# Analysis Graph Drafts From CI

Lab Tracker can let CI ask an LLM to interpret analysis evidence and propose graph
updates without letting CI commit those updates.

The safe workflow is:

1. An analysis job writes a UTF-8 text or Markdown evidence bundle.
2. CI creates a Lab Tracker note for that evidence, or points at an existing note.
3. CI calls `POST /notes/{note_id}/analysis-graph-drafts`.
4. Lab Tracker calls the configured graph draft model and stores a `GraphChangeSet`.
5. A human reviews, edits, accepts, rejects, and commits operations in Lab Tracker.

CI never calls the graph commit endpoint.

For end-of-day review, generate a single HTML report that summarizes accumulated
drafts, renders source evidence inline, and links each proposal back to the Lab
Tracker review page where the scientist can approve, reject, or edit.

## API

Create a draft from an evidence note:

```bash
curl -X POST \
  "$LAB_TRACKER_BASE_URL/notes/$NOTE_ID/analysis-graph-drafts" \
  -H "Authorization: Bearer $LAB_TRACKER_TOKEN"
```

The source note should contain the analysis evidence the model should reason over:
methods, code version, environment hash, dataset IDs, outputs, figures, metrics, and
the interpretation brief. The model also receives current project context from Lab
Tracker before proposing operations.

The API uses `analysis-graph-draft-v1` as the prompt version. Drafts use the same
`GraphChangeSet` and `GraphChangeOperation` review model as image graph drafts.

## CI Script

The helper script can create the evidence note and request the draft:

```bash
python scripts/create-analysis-graph-draft.py \
  --project-id "$PROJECT_ID" \
  --evidence-file analysis-evidence.md
```

It can also draft from an existing note:

```bash
python scripts/create-analysis-graph-draft.py --note-id "$NOTE_ID"
```

It can also build the evidence bundle directly from a git commit:

```bash
python scripts/create-analysis-graph-draft.py \
  --project-id "$PROJECT_ID" \
  --git-repo /path/to/analysis/repo \
  --git-commit HEAD
```

Commit evidence includes the commit metadata, file summary, and a capped textual
diff. Use `LAB_TRACKER_GIT_MAX_DIFF_LINES` or `--git-max-diff-lines` to adjust
the diff cap.

Required environment:

- `LAB_TRACKER_BASE_URL` or `LAB_TRACKER_MCP_BASE_URL`
- one of:
  - `LAB_TRACKER_TOKEN`
  - `LAB_TRACKER_USERNAME` and `LAB_TRACKER_PASSWORD`
  - no auth variables when the target API has local auth disabled

The model call happens inside the Lab Tracker API process, so that process must have
`LAB_TRACKER_OPENAI_API_KEY` and related model settings configured.

## Git Post-Commit Hook

For local analysis repositories, install a `post-commit` hook that sends each new
commit to Lab Tracker as analysis evidence and asks for a reviewable graph draft:

```powershell
.\scripts\install-git-graph-draft-hook.ps1 `
  -TargetRepo C:\path\to\analysis-repo `
  -ProjectId "$PROJECT_ID"
```

The hook calls `scripts/create-analysis-graph-draft.py --git-commit HEAD` after a
commit succeeds. It does not block or rewrite the commit: if Lab Tracker is down,
credentials are missing, or graph drafting fails, the hook prints a warning and
exits successfully. Set these environment variables to override the installed
defaults without editing the hook:

- `LAB_TRACKER_GIT_DRAFT_ENABLED=0` disables the hook temporarily
- `LAB_TRACKER_BASE_URL` points at a different Lab Tracker API
- `LAB_TRACKER_PROJECT_ID` changes the target project
- `LAB_TRACKER_ROOT` points at a different Lab Tracker checkout
- `LAB_TRACKER_PYTHON` chooses the Python interpreter used by the hook

## GitHub Actions

The reusable/manual workflow lives at
`.github/workflows/analysis-graph-draft.yml`. It expects a runner that can reach the
Lab Tracker API. For a laptop or lab-network API, use a self-hosted runner on that
machine or network. For a deployed API, `ubuntu-latest` can work if the endpoint and
auth are reachable.

Repository or environment secrets:

- `LAB_TRACKER_BASE_URL`
- `LAB_TRACKER_TOKEN`, or `LAB_TRACKER_USERNAME` and `LAB_TRACKER_PASSWORD`

The target Lab Tracker API still needs its own model secret:

- `LAB_TRACKER_OPENAI_API_KEY`

Example analysis job handoff:

```yaml
jobs:
  analyze:
    runs-on: self-hosted
    steps:
      - uses: actions/checkout@v4
      - run: python analysis/run.py > analysis-evidence.md
      - uses: actions/upload-artifact@v4
        with:
          name: analysis-evidence
          path: analysis-evidence.md

  draft-graph-update:
    needs: analyze
    uses: ./.github/workflows/analysis-graph-draft.yml
    with:
      runner: self-hosted
      project_id: ${{ vars.LAB_TRACKER_PROJECT_ID }}
      artifact_name: analysis-evidence
      evidence_path: analysis-evidence.md
    secrets:
      LAB_TRACKER_BASE_URL: ${{ secrets.LAB_TRACKER_BASE_URL }}
      LAB_TRACKER_TOKEN: ${{ secrets.LAB_TRACKER_TOKEN }}
```

## End-Of-Day HTML Report

Generate a review report for all ready drafts:

```bash
python scripts/create-graph-draft-review-report.py \
  --project-id "$PROJECT_ID" \
  --output graph-draft-review.html
```

The report includes:

- an LLM-written review brief when `LAB_TRACKER_OPENAI_API_KEY` is available
- every ready graph draft matching the filters
- proposed operation payloads and rationale
- source note text, Markdown-ish analysis evidence, raw text assets, and image
  assets rendered inline when available
- links to `/app/graph-drafts/{change_set_id}` for approve/edit/reject review

The workflow `.github/workflows/graph-draft-review-report.yml` creates the same
HTML report and uploads it as an Actions artifact. It is manual/callable by
default; add a repository-specific `schedule` trigger if the lab wants it to run
automatically at a fixed end-of-day time.
