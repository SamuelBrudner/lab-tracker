# Lab Tracker

Lab Tracker keeps the *reasoning* behind experiments connected to the data they produce. A file named `2025_12_10_Rig2_session001.nwb` tells you when, where, and what — but not *why* it was collected, what was expected, or what was actually observed at the bench. That context usually lives on paper towels, whiteboards, and in people's heads, and it disappears when people leave.

## What it does

- **Questions are first-class.** Projects contain Questions — descriptive, hypothesis-driven, method-development, or other — that are created, staged, activated, and maintained explicitly by users.
- **Sessions and datasets.** Acquisition sessions capture outputs at the rig, are closed when done, and eligible sessions can be promoted into Datasets. Dataset staging and direct commit capture a provenance manifest.
- **Notes attached to entities.** Manual note capture — text or raw file upload/download — attached to the question, session, dataset, or analysis they describe. Notes stay as the raw human record.
- **Analysis, claims, visualizations.** Explicit records linking analysis runs back to the datasets and questions they address, with claims and visualizations as first-class artifacts.
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
- question staging and activate workflow
- manual note creation and upload/download handling
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
