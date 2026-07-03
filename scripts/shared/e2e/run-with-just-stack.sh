#!/usr/bin/env bash
# Run Playwright E2E tests locally against a `just run` stack.
#
# Usage:
#   ./scripts/shared/e2e/run-with-just-stack.sh [filter]
#
# Behavior:
#   1. Loads tests/playwright-e2e/.env.e2e.local if present.
#   2. Reads ports from .env.run-state in the repo root (fallback: 8000/5173/3000).
#   3. If backend/admin/public ports are already listening, reuses them.
#   4. Otherwise starts `just run all` in the background and waits for readiness.
#   5. Runs Playwright E2E with PLAYWRIGHT_SKIP_STACK_BOOT=1.
#   6. Tears down the stack with `just down` only if this script started it.
#
# The filter argument is passed through to `pnpm test`, so you can run a single
# spec file, a directory, or a grep tag. Examples:
#   ./scripts/shared/e2e/run-with-just-stack.sh tests/smoke/pages.spec.ts
#   ./scripts/shared/e2e/run-with-just-stack.sh @visual

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"
e2e_root="$repo_root/tests/playwright-e2e"
cd "$repo_root"

# Load project root .env.local so AUTH_ADMIN_BOOTSTRAP_* / APP_BOOTSTRAP_* are
# available to Playwright. E2E-specific overrides in .env.e2e.local take precedence.
if [ -f "$repo_root/.env.local" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$repo_root/.env.local"
  set +a
fi

# Load local E2E env overrides (gitignored, never committed).
if [ -f "$e2e_root/.env.e2e.local" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$e2e_root/.env.e2e.local"
  set +a
fi

filter="${1:-}"
run_state_file="$repo_root/.env.run-state"
started_by_us="false"

load_run_ports() {
  BACKEND_PORT=8000
  FRONTEND_ADMIN_PORT=5173
  FRONTEND_PUBLIC_PORT=3000
  if [ -f "$run_state_file" ]; then
    # shellcheck disable=SC1090
    source "$run_state_file"
  fi
  # Backward compatibility: legacy run-state used FRONTEND_PORT for admin frontend.
  FRONTEND_ADMIN_PORT="${FRONTEND_ADMIN_PORT:-${FRONTEND_PORT:-5173}}"
}

is_port_listening() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

are_services_running() {
  load_run_ports
  is_port_listening "$BACKEND_PORT" \
    && is_port_listening "$FRONTEND_ADMIN_PORT" \
    && is_port_listening "$FRONTEND_PUBLIC_PORT"
}

is_job_alive() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null
}

wait_for_run_state_file() {
  local just_run_pid="${1:-}"
  local attempts=60
  local i
  for ((i = 0; i < attempts; i++)); do
    if [ -n "$just_run_pid" ] && ! is_job_alive "$just_run_pid"; then
      echo "ERROR: 'just run all' exited before writing $run_state_file" >&2
      return 1
    fi
    if [ -f "$run_state_file" ]; then
      return 0
    fi
    sleep 1
  done
  echo "ERROR: timed out waiting for $run_state_file" >&2
  return 1
}

wait_for_ports() {
  local just_run_pid="${1:-}"
  local attempts=120
  local i
  for ((i = 0; i < attempts; i++)); do
    if [ -n "$just_run_pid" ] && ! is_job_alive "$just_run_pid"; then
      echo "ERROR: 'just run all' exited before services became ready" >&2
      return 1
    fi
    if are_services_running; then
      return 0
    fi
    sleep 1
  done
  echo "ERROR: timed out waiting for services on ports $BACKEND_PORT/$FRONTEND_ADMIN_PORT/$FRONTEND_PUBLIC_PORT" >&2
  return 1
}

cleanup() {
  if [ "$started_by_us" = "true" ]; then
    echo "Shutting down just-run services..."
    just down || true
  fi
}
trap cleanup EXIT INT TERM

if are_services_running; then
  echo "Services already running (ports $BACKEND_PORT/$FRONTEND_ADMIN_PORT/$FRONTEND_PUBLIC_PORT), reusing them."
else
  echo "Starting services with 'just run all'..."
  just run all &
  just_run_pid=$!
  started_by_us="true"

  wait_for_run_state_file "$just_run_pid"
  load_run_ports
  wait_for_ports "$just_run_pid"
  echo "Services ready on ports $BACKEND_PORT/$FRONTEND_ADMIN_PORT/$FRONTEND_PUBLIC_PORT."
fi

# Point Playwright global setup / tests to the actual ports from .env.run-state.
# Users can still override these via their own environment.
export PLAYWRIGHT_SKIP_STACK_BOOT=1
export PLAYWRIGHT_BASE_URL="${PLAYWRIGHT_BASE_URL:-http://127.0.0.1:$FRONTEND_PUBLIC_PORT}"
# Admin Vite dev server binds to localhost (IPv6 loopback on macOS), so use
# localhost rather than 127.0.0.1 to avoid ERR_CONNECTION_REFUSED.
export PLAYWRIGHT_ADMIN_BASE_URL="${PLAYWRIGHT_ADMIN_BASE_URL:-http://localhost:$FRONTEND_ADMIN_PORT}"
export PLAYWRIGHT_HEALTH_URL="${PLAYWRIGHT_HEALTH_URL:-http://127.0.0.1:$BACKEND_PORT/health}"
export PLAYWRIGHT_API_BASE_URL="${PLAYWRIGHT_API_BASE_URL:-http://127.0.0.1:$BACKEND_PORT}"

# Put this run's artifacts under a single timestamped directory so multiple
# runs do not overwrite each other. Each Playwright worker inherits this env
# var, so all artifacts land in the same directory.
run_timestamp=$(date -u +%Y-%m-%dT%H-%M-%S)
export PLAYWRIGHT_TEST_RESULTS_DIR="${PLAYWRIGHT_TEST_RESULTS_DIR:-$e2e_root/test-results/$run_timestamp}"
export PLAYWRIGHT_JUNIT_OUTPUT_FILE="${PLAYWRIGHT_JUNIT_OUTPUT_FILE:-$PLAYWRIGHT_TEST_RESULTS_DIR/junit.xml}"
echo "E2E artifacts will be written to: $PLAYWRIGHT_TEST_RESULTS_DIR"

cd "$e2e_root"

case "$filter" in
  "")
    pnpm test
    ;;
  headed|headed\ *)
    # Support both `just e2e headed` and `just e2e "headed <file-or-filter>"`.
    headed_filter="${filter#headed}"
    headed_filter="${headed_filter# }"
    if [ -z "$headed_filter" ]; then
      pnpm test:headed
    else
      # shellcheck disable=SC2086
      pnpm test:headed $headed_filter
    fi
    ;;
  smoke)
    pnpm test:smoke
    ;;
  no-auth)
    pnpm test:no-auth
    ;;
  *)
    # Allow flags like `--headed` to be passed through as separate arguments,
    # e.g. `just e2e tests/smoke/public-home.no-auth.spec.ts --headed`.
    # shellcheck disable=SC2086
    pnpm test $filter
    ;;
esac
