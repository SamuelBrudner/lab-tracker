FROM python:3.11-slim

ARG LAB_TRACKER_SOURCE_REVISION=unknown
ARG LAB_TRACKER_SOURCE_VERSION=0.1.0

LABEL org.opencontainers.image.source="https://github.com/SamuelBrudner/lab-tracker" \
    org.opencontainers.image.revision="${LAB_TRACKER_SOURCE_REVISION}" \
    org.opencontainers.image.version="${LAB_TRACKER_SOURCE_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAB_TRACKER_SOURCE_REVISION="${LAB_TRACKER_SOURCE_REVISION}" \
    LAB_TRACKER_SOURCE_VERSION="${LAB_TRACKER_SOURCE_VERSION}"

WORKDIR /app

ENV PATH="/app/.venv/bin:${PATH}"

RUN addgroup --system labtracker \
    && adduser --system --ingroup labtracker --home /app --no-create-home labtracker

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md alembic.ini /app/
COPY src /app/src

RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-editable --compile-bytecode

COPY deploy/docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /app/data /var/data \
    && chown -R labtracker:labtracker /app /var/data

USER labtracker

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "lab_tracker.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
