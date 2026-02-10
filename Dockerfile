FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md alembic.ini /app/
COPY src /app/src
COPY alembic /app/alembic

RUN pip install --no-cache-dir uv \
    && uv pip install --system . \
    && uv pip install --system "psycopg[binary]>=3.1"

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "lab_tracker.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
