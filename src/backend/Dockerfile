# -----------------------------------------------------------------------------
# Backend Dockerfile — FastAPI + uv + SQLAlchemy
# Single-stage build to avoid network issues with multi-stage apt-get.
# -----------------------------------------------------------------------------

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    LOG_DIR="/app/backend/logs" \
    LOG_FILE="/app/backend/logs/app.log"

WORKDIR /app

# Install system dependencies, uv, and Python packages in one layer
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

# Copy dependency manifests and package source needed for the editable install.
# README.md and the src tree are required by setuptools (see pyproject.toml).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Create a virtual environment and install production dependencies.
RUN uv sync --frozen --no-dev

# Create a non-root user for security
RUN groupadd -r appgroup && useradd -r -g appgroup appuser

# Preserve the src-based project layout expected by settings.py and expose
# the startup script at the path used by the CMD.
RUN ln -s /app/src/backend /app/backend

# Pre-create runtime directories and ensure they are writable by the runtime user.
RUN mkdir -p /app/backend/logs /app/backend/data && chown -R appuser:appgroup /app

# Copy Alembic configuration and migrations
COPY --chown=appuser:appgroup alembic.ini ./
COPY --chown=appuser:appgroup alembic ./alembic

USER appuser

EXPOSE 8000

# Run migrations then start uvicorn.
CMD ["sh", "backend/scripts/start.sh"]
