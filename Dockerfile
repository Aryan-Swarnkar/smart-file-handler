# syntax=docker/dockerfile:1
# Multi-stage: `builder` installs locked deps into a venv; `api`/`worker` are
# thin runtime stages that copy it. Build targets:
#   docker build --target api    -t sfh-api .
#   docker build --target worker -t sfh-worker .
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS builder
# Pinned to the host's uv that generated uv.lock, so lock format always matches.
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
# --frozen: refuse if lock is out of sync with pyproject (CI safety).
RUN uv sync --frozen --no-dev --no-install-project

FROM base AS runtime-common
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
COPY app ./app
# Non-root: object storage and network access need no privileges.
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

FROM runtime-common AS api
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime-common AS worker
# Wired up in Phase 4 when app.workers lands; harmless until compose runs it.
CMD ["arq", "app.workers.settings.WorkerSettings"]
