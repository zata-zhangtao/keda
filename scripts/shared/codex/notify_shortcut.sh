#!/usr/bin/env bash
# Trigger a macOS Shortcut from Codex CLI notify events.

set -euo pipefail

SHORTCUTS_BIN="${SHORTCUTS_BIN:-/usr/bin/shortcuts}"
CODEX_NOTIFY_SHORTCUT_NAME="${CODEX_NOTIFY_SHORTCUT_NAME:-codex通知}"
CODEX_NOTIFY_PAYLOAD_JSON="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [ "$(uname -s)" != "Darwin" ]; then
    exit 0
fi

if [ ! -x "${SHORTCUTS_BIN}" ]; then
    exit 0
fi

notification_type=""
if [ -n "${CODEX_NOTIFY_PAYLOAD_JSON}" ]; then
    notification_type="$(
        CODEX_NOTIFY_PAYLOAD_JSON="${CODEX_NOTIFY_PAYLOAD_JSON}" python3 - <<'PY' 2>/dev/null || true
import json
import os
import sys

raw_notification_payload_json = os.environ.get("CODEX_NOTIFY_PAYLOAD_JSON", "")
if not raw_notification_payload_json:
    raise SystemExit(0)

try:
    notification_payload_obj = json.loads(raw_notification_payload_json)
except json.JSONDecodeError:
    raise SystemExit(0)

notification_type_value = notification_payload_obj.get("type", "")
if isinstance(notification_type_value, str):
    sys.stdout.write(notification_type_value)
PY
    )"
fi

if [ -n "${notification_type}" ] && [ "${notification_type}" != "agent-turn-complete" ]; then
    exit 0
fi

notification_context_dir="${CODEX_NOTIFY_CONTEXT_DIR:-}"
if [ -z "${notification_context_dir}" ] && [ -n "${CODEX_NOTIFY_PAYLOAD_JSON}" ]; then
    notification_context_dir="$(
        CODEX_NOTIFY_PAYLOAD_JSON="${CODEX_NOTIFY_PAYLOAD_JSON}" python3 - <<'PY' 2>/dev/null || true
import json
import os
import sys

raw_notification_payload_json = os.environ.get("CODEX_NOTIFY_PAYLOAD_JSON", "")
if not raw_notification_payload_json:
    raise SystemExit(0)

try:
    notification_payload_obj = json.loads(raw_notification_payload_json)
except json.JSONDecodeError:
    raise SystemExit(0)

for context_key in ("cwd", "workdir", "working_directory", "repo_path"):
    context_value = notification_payload_obj.get(context_key, "")
    if isinstance(context_value, str) and context_value:
        sys.stdout.write(context_value)
        break
PY
    )"
fi

if [ -z "${notification_context_dir}" ]; then
    notification_context_dir="${PWD:-${SCRIPT_REPO_ROOT}}"
fi

if [ ! -d "${notification_context_dir}" ]; then
    notification_context_dir="${SCRIPT_REPO_ROOT}"
fi

repo_root=""
if command -v git >/dev/null 2>&1; then
    repo_root="$(git -C "${notification_context_dir}" rev-parse --show-toplevel 2>/dev/null || true)"
fi

if [ -n "${repo_root}" ]; then
    repo_display_dir="${repo_root}"
else
    repo_display_dir="${notification_context_dir}"
fi

repo_name="$(basename "${repo_display_dir}")"
repo_branch=""
if command -v git >/dev/null 2>&1; then
    repo_branch="$(git -C "${repo_display_dir}" branch --show-current 2>/dev/null || true)"
fi

if [ -n "${repo_branch}" ]; then
    notification_message="Codex task complete: ${repo_name} (${repo_branch})"
else
    notification_message="Codex task complete: ${repo_name}"
fi

if [ "${CODEX_NOTIFY_VERBOSE:-}" = "1" ]; then
    echo "Shortcut input: ${notification_message}"
fi

notification_input_dir="$(mktemp -d -t codex-notify.XXXXXX)"
notification_input_path="${notification_input_dir}/notification.txt"
trap 'rm -rf "${notification_input_dir}"' EXIT
printf '%s\n' "${notification_message}" > "${notification_input_path}"

"${SHORTCUTS_BIN}" run "${CODEX_NOTIFY_SHORTCUT_NAME}" --input-path "${notification_input_path}" >/dev/null 2>&1 || true
