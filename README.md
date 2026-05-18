# Lab Tracker

Lab Tracker keeps the *reasoning* behind experiments connected to the data they produce. A file named `2025_12_10_Rig2_session001.nwb` tells you when, where, and what — but not *why* it was collected, what was expected, or what was actually observed at the bench. That context usually lives on paper towels, whiteboards, and in people's heads, and it disappears when people leave.

## What it does

- **Questions are first-class.** Projects contain Questions — descriptive, hypothesis-driven, method-development, or other — that are created, staged, activated, maintained explicitly by users, and linked into broad-to-atomic hierarchies with `parent_question_ids`.
- **Sessions and datasets.** Acquisition sessions capture outputs at the rig, are closed when done, and eligible sessions can be promoted into Datasets. Dataset staging and direct commit capture a provenance manifest.
- **Notes attached to entities.** Manual note capture — text, multipart raw file upload/download, and raw voice notes with editable transcripts — attached to the project, question, session, dataset, analysis, or claim they describe. Notes stay as the raw human record.
- **Analysis, claims, visualizations.** Explicit records linking analysis runs back to the datasets and questions they address, with claims and visualizations as first-class artifacts.
- **Mobile multimodal graph-aware draft review.** Phone capture stores raw photo, voice, photo+voice bundle, or text notes, builds a project-scoped graph context packet, asks GPT for reviewable draft operations, then humans edit, accept/reject, and commit through the same API validation as normal writes.
- **Analysis-to-graph draft review.** CI or assistant clients can submit analysis evidence notes for GPT-backed graph draft proposals, with the same human review boundary before commit.
- **Search.** Substring search over questions and notes so prior context is findable later.

What ships today is the minimum that preserves the core research record. The supported surface is defined in [`docs/retained-v1-surface.md`](docs/retained-v1-surface.md) — if it and this README disagree, that document wins. The broader vision (meeting-photo question capture, OCR, vector search, PI review gates) lives in [`idea.md`](idea.md) and is explicitly deferred.

## Who it's for

Wet labs (initially neuroscience) that produce high-bandwidth data on specialized rigs and want the semantic context preserved alongside it.

## Quickstart

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[test,lint]"
```

Install `uv` first if needed (for example: `brew install uv` or `pipx install uv`).

If you prefer pip/venv:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test,lint]"
```

Commands below use `uv run`. If you used pip/venv instead, drop the `uv run` prefix.

Windows fresh-clone notes, including Beads/Dolt setup, are in
[`docs/windows-fresh-clone.md`](docs/windows-fresh-clone.md).

## Run the API

```bash
uv run uvicorn lab_tracker.asgi:app --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Frontend:

Open `http://127.0.0.1:8000/app`.

### Multi-client runtime

For browser, Codex, Claude, scripts, and future workers writing at the same time,
use Postgres as the live source of truth and keep writes behind the Lab Tracker
API. Start only Postgres for local development:

```powershell
docker compose up postgres
$env:LAB_TRACKER_DATABASE_URL = "postgresql+psycopg://lab_tracker:lab_tracker@127.0.0.1:5432/lab_tracker"
uv run alembic upgrade head
uv run uvicorn lab_tracker.asgi:app --reload
```

Or run the full app stack:

```bash
docker compose up app
```

SQLite remains the default single-client local fallback.

To serve the same graph to other computers on a LAN, VPN, or overlay network
such as Tailscale, bind the API to all interfaces and use the printed host IP
from the serving machine:

```powershell
.\scripts\serve-lan.ps1 -UsePostgres
```

Then open `http://<host-ip>:8000/app` from the other computer or set
`LAB_TRACKER_MCP_BASE_URL=http://<host-ip>:8000` for MCP clients. For computers
on different networks, use a private overlay network or a protected tunnel
rather than raw public port forwarding. If remote clients time out, Windows
Firewall may need an administrator rule for TCP port 8000. See
[`docs/lan-shared-graph.md`](docs/lan-shared-graph.md).

For a workstation-hosted HTTPS endpoint suitable for off-campus browser access,
serve Lab Tracker on localhost and publish it through a managed outbound tunnel.
See [`docs/workstation-https-serving.md`](docs/workstation-https-serving.md).

Local development starts with authentication disabled so early testing can use
the app without creating accounts. Set `LAB_TRACKER_AUTH_ENABLED=true` to test
the login and role flow. Non-local environments keep authentication enabled by
default.

The retained v1 product surface is defined in
[`docs/retained-v1-surface.md`](docs/retained-v1-surface.md).
If this README and the retained-surface document disagree, the retained-surface
document defines the supported runtime.

### Frontend build

The frontend bundle is committed to the repo and served from `src/lab_tracker/frontend/app.js`.

If you change the frontend source in `src/lab_tracker/frontend_src`, rebuild the bundle:

```bash
npm install
npm run lint:frontend
npm run build
```

The committed frontend bundle ships without a source map by default.

Supported workflows in the frontend include:
- project dashboard
- question staging, activation, and parent-child hierarchy mapping
- phone-first photo, voice, photo+voice bundle, and text capture at `/app/capture`
- manual note creation and multipart upload/download handling
- graph-aware image note draft review with human edit, accept/reject, defer, and commit
- sessions and acquisition outputs
- dataset staging, file attachment, and direct commit with provenance capture
- analysis, claim, and visualization tracking

Authentication notes:
- register/login is available in the UI
- public registration creates viewer accounts
- write workflows require editor/admin role

## Configuration

Environment variables are loaded with the `LAB_TRACKER_` prefix. The defaults are suitable for local
development.

- `LAB_TRACKER_APP_NAME`: FastAPI title (default: `lab-tracker`)
- `LAB_TRACKER_ENVIRONMENT`: environment label (default: `local`)
- `LAB_TRACKER_LOG_LEVEL`: logging level (default: `INFO`)
- `LAB_TRACKER_DATABASE_URL`: SQLAlchemy database URL (default: `sqlite+pysqlite:///./lab_tracker.db`)
- `LAB_TRACKER_FILE_STORAGE_PATH`: file storage directory (default: `./file_storage`)
- `LAB_TRACKER_NOTE_STORAGE_PATH`: note storage directory (default: `./note_storage`)
- `LAB_TRACKER_AUTH_SECRET_KEY`: auth signing secret (default allowed only in `local`)
- `LAB_TRACKER_AUTH_TOKEN_TTL_MINUTES`: access token lifetime (default: `720`)
- `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN`: one-time token that allows the first
  admin account to be registered when auth is enabled and no users exist
- `LAB_TRACKER_AUTH_ENABLED`: enable login and role enforcement (default: `false`
  in `local`, `true` otherwise; non-local environments cannot disable auth)
- `LAB_TRACKER_OPENAI_API_KEY`: required for graph draft generation and
  voice-note transcription
- `LAB_TRACKER_OPENAI_MODEL`: OpenAI model for graph drafts (default:
  `gpt-5.4-mini`; set `gpt-5.5` or another compatible model to override)
- `LAB_TRACKER_OPENAI_TRANSCRIPTION_MODEL`: OpenAI model for voice-note
  transcription (default: `gpt-4o-mini-transcribe`)
- `LAB_TRACKER_OPENAI_BASE_URL`: OpenAI API base URL (default:
  `https://api.openai.com/v1`)
- `LAB_TRACKER_OPENAI_TIMEOUT_SECONDS`: graph draft API timeout in seconds
  (default: `60`)

### Multimodal graph draft review

To try the local image review loop:

```powershell
$env:LAB_TRACKER_OPENAI_API_KEY = "<your OpenAI API key>"
$env:LAB_TRACKER_OPENAI_MODEL = "gpt-5.4-mini"
uv run alembic upgrade head
uv run uvicorn lab_tracker.asgi:app --reload
```

Open `http://127.0.0.1:8000/app/capture` from a phone or desktop browser, then
capture a photo, voice note, photo+voice bundle, or text note. Select the
project and optional question/session/dataset/analysis/claim targets, add an
optional hint, then choose `Upload and draft`. Raw images and raw audio are
stored first as note artifacts in `LAB_TRACKER_NOTE_STORAGE_PATH`; voice notes
receive editable transcripts linked back to the raw audio. The draft is stored
separately as a `GraphChangeSet` linked back to the source note.

Draft mode defaults to `graph_context`. In that mode, Lab Tracker builds and
stores a compact context packet containing the source note, selected targets,
project, active/staged questions with parent links, recent notes, sessions,
datasets, analyses, claims, visualizations, and unresolved recent image
captures. Context build failures are loud API errors and do not silently fall
back to OCR or image-only interpretation. Image-only drafting is available only
when explicitly requested and records `draft_mode=image_only`.

The configured OpenAI-compatible provider receives the uploaded image bytes when
present, editable transcript text when present, optional user hint, graph
context packet, and strict operation schema. Voice transcription uses the
configured transcription model before drafting. Configure these routes with
`LAB_TRACKER_OPENAI_API_KEY`, `LAB_TRACKER_OPENAI_MODEL`,
`LAB_TRACKER_OPENAI_TRANSCRIPTION_MODEL`, and `LAB_TRACKER_OPENAI_BASE_URL`.
Third-party logging, retention, and residency depend on the configured provider
and base URL. For institutional deployments, point `LAB_TRACKER_OPENAI_BASE_URL`
at an approved gateway or model endpoint.

Authentication and role checks apply to raw images, drafts, draft edits, and
commits. Viewer accounts can inspect authorized records; editor/admin roles are
required for note upload, draft creation, operation edits, and graph commits.
Raw images and draft operations are not committed automatically. Accepted
operations still pass through the normal API validation path, and model output
that references unknown entity IDs or unsupported semantic operations is rejected.

The review screen records enough metadata to compare `graph_context` and
`image_only` behavior: draft mode, model/provider, context snapshot, uncertainty
fields, clarification requests, operation statuses, and commit timing. Suggested
evaluation metrics are accepted/edited/rejected operations, duplicate entity
proposals, reviewer edit burden, time from capture to commit, and uncertainty
quality. Offline queued capture is intentionally deferred in this release.

### Analysis graph drafts from CI

CI can submit analysis evidence for a model-backed graph draft without committing
the resulting graph changes automatically. The API endpoint is
`POST /notes/{note_id}/analysis-graph-drafts`, and
[`scripts/create-analysis-graph-draft.py`](scripts/create-analysis-graph-draft.py)
can create the source evidence note and request the draft from a CI runner.
End-of-day accumulated draft review reports can be generated with
[`scripts/create-graph-draft-review-report.py`](scripts/create-graph-draft-review-report.py)
as standalone HTML with inline evidence/results and links back to Lab Tracker
review pages.

See [`docs/analysis-graph-drafts-ci.md`](docs/analysis-graph-drafts-ci.md) for the
GitHub Actions workflow and required secrets.

The retained v1 runtime keeps note handling manual and uses direct substring
search for query flows. Deferred concepts live in
[`docs/retained-v1-surface.md`](docs/retained-v1-surface.md)
rather than the active product surface.

## Database migrations

```bash
uv run alembic upgrade head
```

## Validation

Core backend validation:

```bash
uv run pytest -q
```

Run the frontend checks only when you change `src/lab_tracker/frontend_src` or
the committed bundle in `src/lab_tracker/frontend`:

```bash
npm run test:frontend
npm run lint:frontend
npm run build
```

## MCP and Dolt mirror

Lab Tracker ships an API-backed MCP server for assistants:

```bash
LAB_TRACKER_MCP_BASE_URL=http://127.0.0.1:8000
LAB_TRACKER_MCP_USERNAME=<service-account-username>
LAB_TRACKER_MCP_PASSWORD=<service-account-password>
python -m lab_tracker.mcp_server
```

The MCP username/password are only required when `LAB_TRACKER_AUTH_ENABLED=true`.

Dolt is an export-only versioned mirror in v1:

```bash
python -m lab_tracker.dolt_mirror export --message "Lab Tracker snapshot"
```

The default local mirror path is `.lab-tracker-dolt/`. See
[`docs/lab-tracker-mcp-skills.md`](docs/lab-tracker-mcp-skills.md) for setup
details.
