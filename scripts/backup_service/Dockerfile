# -----------------------------------------------------------------------------
# Backup Service Dockerfile
# Stand-alone image for database, logs, and resources backup to S3.
# -----------------------------------------------------------------------------

FROM python:3.13-slim-bookworm

ARG POSTGRES_CLIENT_MAJOR=17

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client-${POSTGRES_CLIENT_MAJOR} \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir boto3==1.37.0 python-dotenv==1.1.0

WORKDIR /app
COPY scripts/backup_service /app/scripts/backup_service

ENV PYTHONPATH=/app
CMD ["python", "-m", "scripts.backup_service", "backup"]
