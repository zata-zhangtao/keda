# -----------------------------------------------------------------------------
# Backend Dockerfile — FastAPI + uv + SQLAlchemy
# Single-stage build to avoid network issues with multi-stage apt-get.
# -----------------------------------------------------------------------------

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install system dependencies, uv, and Python packages in one layer
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

# Copy dependency manifests first (maximises Docker layer caching)
COPY pyproject.toml uv.lock ./

# Create a virtual environment and install production dependencies.
RUN uv sync --frozen --no-dev

# Create a non-root user for security
RUN groupadd -r appgroup && useradd -r -g appgroup appuser

# Copy Alembic configuration and migrations
COPY alembic.ini ./
COPY alembic ./alembic

# Copy application source code and startup script
COPY src/backend ./backend

# Set ownership
RUN chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

# Run migrations then start uvicorn.
CMD ["sh", "backend/scripts/start.sh"]
