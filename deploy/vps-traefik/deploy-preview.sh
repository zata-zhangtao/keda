#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Non-interactive preview stack deploy/teardown helper
# -----------------------------------------------------------------------------
# Usage:
#   deploy-preview.sh up   APP_DIR=/opt/preview/keda-pr-123 COMPOSE_PROJECT_NAME=...
#   deploy-preview.sh down APP_DIR=/opt/preview/keda-pr-123 COMPOSE_PROJECT_NAME=...
#
# Environment variables required for both actions:
#   APP_DIR               Remote directory containing docker-compose.preview.yml
#   COMPOSE_PROJECT_NAME  Docker Compose project name (per-PR unique)
#
# For "up", additionally required:
#   PREVIEW_DOMAIN, BACKEND_IMAGE, FRONTEND_IMAGE, TRAEFIK_NETWORK,
#   TRAEFIK_ROUTER_NAME, TRAEFIK_SERVICE_NAME,
#   POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, DATABASE_URL
# -----------------------------------------------------------------------------

set -euo pipefail

ACTION="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -z "${APP_DIR:-}" ] || [ -z "${COMPOSE_PROJECT_NAME:-}" ]; then
  echo "ERROR: APP_DIR and COMPOSE_PROJECT_NAME must be set." >&2
  exit 1
fi

compose_args=(
  -p "${COMPOSE_PROJECT_NAME}"
  -f "${SCRIPT_DIR}/docker-compose.preview.yml"
)

up() {
  mkdir -p "${APP_DIR}"

  if [ ! -f "${APP_DIR}/.env" ]; then
    {
      echo "PREVIEW_DOMAIN=${PREVIEW_DOMAIN}"
      echo "COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME}"
      echo "APP_DIR=${APP_DIR}"
      echo "PREVIEW_URL_SCHEME=${PREVIEW_URL_SCHEME}"
      echo "BACKEND_IMAGE=${BACKEND_IMAGE}"
      echo "FRONTEND_IMAGE=${FRONTEND_IMAGE}"
      echo "REGISTRY_HOST=${REGISTRY_HOST}"
      echo "REGISTRY_NAMESPACE=${REGISTRY_NAMESPACE}"
      echo "TRAEFIK_NETWORK=${TRAEFIK_NETWORK}"
      echo "TRAEFIK_ROUTER_NAME=${TRAEFIK_ROUTER_NAME}"
      echo "TRAEFIK_SERVICE_NAME=${TRAEFIK_SERVICE_NAME}"
      echo "POSTGRES_USER=${POSTGRES_USER}"
      echo "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}"
      echo "POSTGRES_DB=${POSTGRES_DB}"
      echo "DATABASE_URL=${DATABASE_URL}"
    } > "${APP_DIR}/.env"
  fi

  if [ -n "${REGISTRY_USERNAME:-}" ] && [ -n "${REGISTRY_PASSWORD:-}" ] && [ -n "${REGISTRY_HOST:-}" ]; then
    echo "Logging in to ${REGISTRY_HOST}..."
    echo "${REGISTRY_PASSWORD}" | docker login "${REGISTRY_HOST}" -u "${REGISTRY_USERNAME}" --password-stdin
  fi

  docker compose "${compose_args[@]}" --env-file "${APP_DIR}/.env" pull
  docker compose "${compose_args[@]}" --env-file "${APP_DIR}/.env" up -d --remove-orphans

  echo "Waiting for backend health check..."
  for _ in $(seq 1 30); do
    if docker compose "${compose_args[@]}" --env-file "${APP_DIR}/.env" exec -T backend \
      curl -fsS "http://localhost:8000/api/v1/agent-runner/health" > /dev/null 2>&1; then
      echo "Backend is healthy."
      return 0
    fi
    sleep 2
  done

  echo "ERROR: Backend failed to become healthy." >&2
  docker compose "${compose_args[@]}" --env-file "${APP_DIR}/.env" logs --tail=50 backend
  exit 1
}

down() {
  docker compose "${compose_args[@]}" --env-file "${APP_DIR}/.env" down -v || true
  rm -f "${APP_DIR}/.env"
  docker image prune -f || true
}

case "${ACTION}" in
  up)
    up
    ;;
  down)
    down
    ;;
  *)
    echo "Usage: $0 {up|down}" >&2
    exit 1
    ;;
esac
