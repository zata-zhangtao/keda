#!/usr/bin/env bash
# Backward-compatible worktree creation entrypoint.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "${REPO_ROOT}/scripts/worktree/create.sh" "$@"
