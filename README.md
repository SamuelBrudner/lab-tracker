# Lab Tracker

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

Frontend MVP:

Open `http://127.0.0.1:8000/app`.

### Frontend build

The frontend bundle is committed to the repo and served from `src/lab_tracker/frontend/app.js`.

If you change the frontend source in `src/lab_tracker/frontend_src/app.jsx`, rebuild the bundle:

```bash
npm install
npm run build
```

The frontend includes:
- project dashboard
- question staging and activate (commit) workflow
- note creation (text + photo upload)
- dataset staging and commit review

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
- `LAB_TRACKER_SEARCH_BACKEND`: search backend (default: `in_memory_substring`, options: `in_memory_substring`, `chromadb`)
- `LAB_TRACKER_CHROMADB_PERSIST_PATH`: ChromaDB persistence directory (default: `./.lab-tracker/chromadb`)
- `LAB_TRACKER_AUTH_SECRET_KEY`: auth signing secret (default allowed only in `local`)
- `LAB_TRACKER_AUTH_TOKEN_TTL_MINUTES`: access token lifetime (default: `720`)
- `LAB_TRACKER_OCR_TESSERACT_CMD`: optional path to the `tesseract` binary for OCR
- `LAB_TRACKER_OCR_TESSERACT_LANGUAGES`: Tesseract language packs (default: `eng`, example: `eng+deu`)
- `LAB_TRACKER_EMBEDDING_PROVIDER`: embedding backend for vector search (default: `chroma_default`, options: `sentence_transformers`, `openai`)
- `OPENAI_API_KEY` (or `LAB_TRACKER_OPENAI_API_KEY`): required when `LAB_TRACKER_EMBEDDING_PROVIDER=openai`

### Reindex search backend

If you switch to a persistent backend (for example, `LAB_TRACKER_SEARCH_BACKEND=chromadb`),
backfill the index from the database:

```bash
uv run python -m lab_tracker.reindex --reset
```

## Database migrations

```bash
uv run alembic upgrade head
```

## Tests

```bash
uv run pytest -q
```
