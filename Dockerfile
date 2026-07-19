FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim-bookworm AS runtime

ENV CONFIG_PATH=/app/config/config.yaml \
    LEGACY_STATE_PATH=/app/data/state.json \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DB_PATH=/app/data/state.db

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir --parents /app/config /app/data \
    && chown app:app /app/data \
    && chmod 0555 /app /app/config

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY app.py configuration.py delivery_store.py discord_client.py main.py models.py ./
COPY strategies/ ./strategies/

USER app

STOPSIGNAL SIGTERM

CMD ["python", "main.py"]
