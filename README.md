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

## Configuration

Environment variables are loaded with the `LAB_TRACKER_` prefix. The defaults are suitable for local
development.

- `LAB_TRACKER_APP_NAME`: FastAPI title (default: `lab-tracker`)
- `LAB_TRACKER_ENVIRONMENT`: environment label (default: `local`)
- `LAB_TRACKER_LOG_LEVEL`: logging level (default: `INFO`)
- `LAB_TRACKER_DATABASE_URL`: SQLAlchemy database URL (default: `sqlite+pysqlite:///./lab_tracker.db`)

## Database migrations

```bash
uv run alembic upgrade head
```

## Tests

```bash
uv run pytest -q
```
