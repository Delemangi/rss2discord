FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY pyproject.toml ./

RUN uv pip install --system --no-cache -r pyproject.toml

COPY main.py ./
COPY strategies/ ./strategies/

VOLUME ["/app/config", "/app/data"]

ENV CONFIG_PATH=/app/config/config.yaml

WORKDIR /app/data

CMD ["python", "-u", "/app/main.py"]
