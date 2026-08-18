FROM python:3.13.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN pip install --no-cache-dir uv==0.12.2
COPY pyproject.toml uv.lock README.md ./
COPY model-routing.toml ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-compile-bytecode

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

ENV PATH="/app/.venv/bin:${PATH}"
CMD ["butler-api"]
