#!/usr/bin/env bash
# Codex wrapper for this repository.
# Runs the repository session-start hook before launching Codex and
# passes the latest Codex transcript to the session-end hook on exit.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CODEX_SESSIONS_DIR="${HOME}/.codex/sessions"
SESSION_START_SCRIPT="${REPO_ROOT}/scripts/hooks/session-start.sh"
SESSION_END_SCRIPT="${REPO_ROOT}/scripts/hooks/session-end.sh"

get_latest_codex_session_path() {
    if [ ! -d "${CODEX_SESSIONS_DIR}" ]; then
        return 0
    fi

    find "${CODEX_SESSIONS_DIR}" -type f -name 'rollout-*.jsonl' -print 2>/dev/null | sort | tail -n 1
}

extract_hook_result_text() {
    local hook_output_json="$1"

    HOOK_OUTPUT_JSON="${hook_output_json}" python3 - <<'PY'
import json
import os
import sys

raw_hook_output_json = os.environ.get("HOOK_OUTPUT_JSON", "").strip()
if not raw_hook_output_json:
    raise SystemExit(0)

try:
    hook_result_obj = json.loads(raw_hook_output_json)
except json.JSONDecodeError:
    raise SystemExit(0)

result_text = hook_result_obj.get("result", "")
if isinstance(result_text, str) and result_text.strip():
    sys.stdout.write(result_text)
PY
}

build_session_end_payload() {
    local transcript_path="$1"

    TRANSCRIPT_PATH="${transcript_path}" python3 - <<'PY'
import json
import os

transcript_path = os.environ.get("TRANSCRIPT_PATH", "")
print(json.dumps({"transcript_path": transcript_path}))
PY
}

latest_session_before_launch="$(get_latest_codex_session_path)"

startup_hook_output_json=""
if [ -x "${SESSION_START_SCRIPT}" ] || [ -f "${SESSION_START_SCRIPT}" ]; then
    startup_hook_output_json="$(bash "${SESSION_START_SCRIPT}" 2>/dev/null || true)"
fi

startup_context_text="$(extract_hook_result_text "${startup_hook_output_json}" || true)"

codex_exit_code=0

if [ "$#" -eq 0 ] && [ -n "${startup_context_text}" ]; then
    initial_prompt_text=$'Session startup context:\n\n'"${startup_context_text}"$'\n\nLoad this context for this session, then wait for the user\'s task.'
    codex "${initial_prompt_text}" || codex_exit_code=$?
else
    codex "$@" || codex_exit_code=$?
fi

latest_session_after_launch="$(get_latest_codex_session_path)"
transcript_path_for_summary=""

if [ -n "${latest_session_after_launch}" ] && [ "${latest_session_after_launch}" != "${latest_session_before_launch}" ]; then
    transcript_path_for_summary="${latest_session_after_launch}"
fi

if [ -x "${SESSION_END_SCRIPT}" ] || [ -f "${SESSION_END_SCRIPT}" ]; then
    build_session_end_payload "${transcript_path_for_summary}" | bash "${SESSION_END_SCRIPT}" >/dev/null 2>&1 || true
fi

exit "${codex_exit_code}"
