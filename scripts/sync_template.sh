#!/usr/bin/env bash
# Backward-compatible wrapper for the template sync script.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "${REPO_ROOT}/scripts/template/sync_template.sh" "$@"
