# Configuration reference

This is the configuration reference for Lab Tracker: the full `LAB_TRACKER_*`
environment variable list, the multimodal graph-draft-review configuration and
behavior, and the local evidence-inbox import (`lt import-folder`) configuration.

The supported runtime surface is defined in
[`retained-v1-surface.md`](retained-v1-surface.md); if it and this document
disagree, the retained-surface document defines the supported runtime.

## Environment variables

Environment variables are loaded with the `LAB_TRACKER_` prefix. The defaults are
suitable for local development.

### Application

- `LAB_TRACKER_APP_NAME`: FastAPI title (default: `lab-tracker`)
- `LAB_TRACKER_ENVIRONMENT`: environment label (default: `local`)
- `LAB_TRACKER_LOG_LEVEL`: logging level (default: `INFO`)

### Database and storage

- `LAB_TRACKER_DATABASE_URL`: SQLAlchemy database URL (default: `sqlite+pysqlite:///./lab_tracker.db`)
- `LAB_TRACKER_FILE_STORAGE_PATH`: file storage directory (default: `./file_storage`)
- `LAB_TRACKER_NOTE_STORAGE_PATH`: note storage directory (default: `./note_storage`)

SQLite is the default single-client local fallback. For multi-client runtimes,
point `LAB_TRACKER_DATABASE_URL` at Postgres and keep writes behind the Lab
Tracker API.

### Authentication and invitations

- `LAB_TRACKER_AUTH_SECRET_KEY`: auth signing secret (default allowed only in `local`)
- `LAB_TRACKER_AUTH_TOKEN_TTL_MINUTES`: access token lifetime (default: `720`)
- `LAB_TRACKER_AUTH_INVITE_TTL_HOURS`: signed invitation link lifetime
  (default: `168`)
- `LAB_TRACKER_AUTH_RATE_LIMIT_ATTEMPTS`: failed login attempts, or register
  attempts from one caller, allowed per window (default: `10`)
- `LAB_TRACKER_AUTH_RATE_LIMIT_WINDOW_SECONDS`: rate-limit window in seconds
  (default: `60`)
- `LAB_TRACKER_AUTH_PUBLIC_VIEWER_REGISTRATION_ENABLED`: allow public
  self-registration for viewer accounts (default: `true`). Set to `false` to
  require invites or an admin bearer token for new users.
- `LAB_TRACKER_AUTH_ENABLED`: enable login and role enforcement (default: `false`
  in `local`, `true` otherwise; non-local environments cannot disable auth)
- `LAB_TRACKER_PUBLIC_BASE_URL`: public URL used in email invitation links

### Uploads and managed files

- `LAB_TRACKER_MAX_UPLOAD_BYTES`: maximum raw upload size for note files,
  dataset files, and visualization assets (default: `104857600`, 100 MiB).
  Uploads that exceed the limit are rejected and partial local files are
  cleaned up.

### Bootstrap (first admin)

- `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN`: one-time token for creating the first
  admin on fresh auth-enabled deployments
- `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE`: `local` (default),
  `first_run`, or `never`; controls whether `/auth/bootstrap-status` can return
  the first-admin token before any users exist. In the default `local` mode the
  setup screen shows the token only when the request originates from a local,
  LAN, or VPN address and hides it on public hosts; use `first_run` to allow
  first-run browser display on public deployments; `never` always hides it. The
  token is never returned after any user exists.

### Graph draft providers and transcription

- `LAB_TRACKER_GRAPH_DRAFT_PROVIDER`: active drafting provider (default:
  `openai`; accepted values are `openai`, `anthropic`/`claude`, and
  `google`/`gemini`)
- `LAB_TRACKER_OPENAI_API_KEY`: required when the provider is `openai` and for
  OpenAI voice-note transcription
- `LAB_TRACKER_OPENAI_MODEL`: OpenAI model for graph drafts (default:
  `gpt-4o-mini`; set another compatible model to override)
- `LAB_TRACKER_OPENAI_TRANSCRIPTION_MODEL`: OpenAI model for voice-note
  transcription (default: `gpt-4o-mini-transcribe`)
- `LAB_TRACKER_OPENAI_BASE_URL`: OpenAI API base URL (default:
  `https://api.openai.com/v1`)
- `LAB_TRACKER_OPENAI_TIMEOUT_SECONDS`: OpenAI graph draft API timeout in
  seconds (default: `60`)
- `LAB_TRACKER_ANTHROPIC_API_KEY`: required when the provider is `anthropic` or
  `claude`
- `LAB_TRACKER_ANTHROPIC_MODEL`: Anthropic model for graph drafts (default:
  `claude-3-5-sonnet-latest`)
- `LAB_TRACKER_ANTHROPIC_BASE_URL`: Anthropic API base URL (default:
  `https://api.anthropic.com/v1`)
- `LAB_TRACKER_ANTHROPIC_TIMEOUT_SECONDS`: Anthropic graph draft API timeout in
  seconds (default: `60`)
- `LAB_TRACKER_GOOGLE_API_KEY`: required when the provider is `google` or
  `gemini`; also required for Google voice-note transcription
- `LAB_TRACKER_GOOGLE_MODEL`: Google Gemini model for graph drafts and
  transcription (default: `gemini-2.5-flash`)
- `LAB_TRACKER_GOOGLE_BASE_URL`: Google Generative Language API base URL
  (default: `https://generativelanguage.googleapis.com/v1beta`)
- `LAB_TRACKER_GOOGLE_TIMEOUT_SECONDS`: Google graph draft API timeout in
  seconds (default: `60`)

## Authentication behavior

Local development starts with authentication disabled so early testing can use
the app without creating accounts. Set `LAB_TRACKER_AUTH_ENABLED=true` to test
the login and role flow. Non-local environments keep authentication enabled by
default and cannot disable auth.

Public registration creates viewer accounts when
`LAB_TRACKER_AUTH_PUBLIC_VIEWER_REGISTRATION_ENABLED=true`. Viewer accounts can
inspect authorized records; write workflows (note upload, draft creation,
operation edits, and graph commits) require an editor or admin role. A fresh
auth-enabled instance shows first-admin setup when
`LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN` is configured. `/health` remains public for
uptime probes; `/readiness` and `/metrics` require credentials when
authentication is enabled.

## Multimodal graph draft review

Multimodal draft generation defaults to OpenAI and requires
`LAB_TRACKER_OPENAI_API_KEY` unless `LAB_TRACKER_GRAPH_DRAFT_PROVIDER` selects
another provider. To try the local image review loop with the default provider:

```powershell
$env:LAB_TRACKER_OPENAI_API_KEY = "<your OpenAI API key>"
$env:LAB_TRACKER_OPENAI_MODEL = "gpt-4o-mini"
uv run alembic upgrade head
uv run uvicorn lab_tracker.asgi:app --reload
```

Pair a phone from `Devices`, or use the LAN helper's QR code, then open the
phone capture URL. Capture a photo, voice note, photo+voice bundle, or text
note. Select the project and optional question/session/dataset/analysis/claim
targets, add an optional hint, then choose `Upload and draft`. Raw images and
raw audio are stored first as note artifacts in `LAB_TRACKER_NOTE_STORAGE_PATH`;
voice notes receive editable transcripts linked back to the raw audio. The draft
is stored separately as a `GraphChangeSet` linked back to the source note.

### Draft modes

Draft mode defaults to `graph_context`. In that mode, Lab Tracker builds and
stores a compact context packet containing the source note, selected targets,
project, active/staged questions with parent links, recent notes, sessions,
datasets, analyses, claims, visualizations, and unresolved recent image
captures. Context build failures are loud API errors and do not silently fall
back to OCR or image-only interpretation. Image-only drafting is available only
when explicitly requested and records `draft_mode=image_only`.

### Provider, model, and residency

The configured graph-draft provider receives uploaded image bytes when present,
editable transcript text when present, optional user hint, graph context packet,
and strict operation schema. OpenAI and Google clients can transcribe voice
notes; Anthropic drafting does not provide native audio transcription in this
runtime. Configure provider, model, API key, base URL, and timeout with the
provider-specific variables above. Third-party logging, retention, and
residency depend on the selected provider and base URL. For institutional
deployments, point the active provider's base URL at an approved gateway or
model endpoint.

### Auth, validation, and committed records

Authentication and role checks apply to raw images, drafts, draft edits, and
commits. Viewer accounts can inspect authorized records; editor/admin roles are
required for note upload, draft creation, operation edits, and graph commits.
Raw images and draft operations are not committed automatically. Accepted
operations still pass through the normal API validation path, and model output
that references unknown entity IDs or unsupported semantic operations is rejected.

### Review metadata and evaluation

The review screen records enough metadata to compare `graph_context` and
`image_only` behavior: draft mode, model/provider, context snapshot, uncertainty
fields, clarification requests, operation statuses, and commit timing. Suggested
evaluation metrics are accepted/edited/rejected operations, duplicate entity
proposals, reviewer edit burden, time from capture to commit, and uncertainty
quality. Offline queued capture is intentionally deferred in this release.

## Local evidence inbox imports

Use `lt import-folder` to turn files from a local or synced folder into staged
evidence notes. This works for folders synced by Google Drive, Dropbox, OneDrive,
or similar tools without adding a provider-specific OAuth workflow:

```bash
LAB_TRACKER_BASE_URL=http://127.0.0.1:8000 \
LAB_TRACKER_PROJECT_ID=<project-id> \
lt import-folder --project "$LAB_TRACKER_PROJECT_ID" --root /path/to/lab-inbox
```

The adapter records `evidence_source_*` metadata, skips duplicates by source ID
and content hash, and never commits graph changes — imported files land as
staged evidence notes, and human review remains the commit boundary. See
[`evidence-source-metadata.md`](evidence-source-metadata.md).

The retained v1 runtime keeps note handling manual and uses direct substring
search for query flows. Deferred concepts live in
[`retained-v1-surface.md`](retained-v1-surface.md) rather than the active
product surface.
