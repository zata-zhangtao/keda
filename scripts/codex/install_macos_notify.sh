#!/usr/bin/env bash
# Install this repository's macOS Shortcut notifier into Codex CLI config.

set -euo pipefail

show_usage() {
    cat <<'EOF'
Usage:
  scripts/codex/install_macos_notify.sh [shortcut-name] [--test]

Defaults:
  shortcut-name: codex通知

Environment:
  CODEX_HOME  Override Codex config directory. Defaults to ~/.codex.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    show_usage
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NOTIFY_SCRIPT_PATH="${REPO_ROOT}/scripts/codex/notify_shortcut.sh"
SHORTCUT_NAME="${1:-codex通知}"
RUN_TEST_AFTER_INSTALL="${2:-}"
CODEX_HOME_DIR="${CODEX_HOME:-${HOME}/.codex}"
CODEX_CONFIG_PATH="${CODEX_HOME_DIR}/config.toml"

if [ "${RUN_TEST_AFTER_INSTALL}" != "" ] && [ "${RUN_TEST_AFTER_INSTALL}" != "--test" ]; then
    show_usage
    exit 1
fi

toml_basic_string_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

mkdir -p "${CODEX_HOME_DIR}"
chmod 755 "${NOTIFY_SCRIPT_PATH}" 2>/dev/null || true

if [ -f "${CODEX_CONFIG_PATH}" ]; then
    config_backup_path="${CODEX_CONFIG_PATH}.bak.$(date +%Y%m%d-%H%M%S)"
    cp "${CODEX_CONFIG_PATH}" "${config_backup_path}"
else
    config_backup_path=""
    touch "${CODEX_CONFIG_PATH}"
    chmod 600 "${CODEX_CONFIG_PATH}"
fi

shortcut_env_arg="CODEX_NOTIFY_SHORTCUT_NAME=${SHORTCUT_NAME}"
escaped_shortcut_env_arg="$(toml_basic_string_escape "${shortcut_env_arg}")"
escaped_notify_script_path="$(toml_basic_string_escape "${NOTIFY_SCRIPT_PATH}")"
notify_config_line="notify = [\"env\", \"${escaped_shortcut_env_arg}\", \"bash\", \"${escaped_notify_script_path}\"]"

config_without_root_notify_path="$(mktemp)"
next_config_path="$(mktemp)"

awk '
BEGIN { in_root_table = 1 }
/^[[:space:]]*\[/ { in_root_table = 0 }
in_root_table && /^[[:space:]]*notify[[:space:]]*=/ { next }
{ print }
' "${CODEX_CONFIG_PATH}" > "${config_without_root_notify_path}"

{
    printf '%s\n\n' "${notify_config_line}"
    cat "${config_without_root_notify_path}"
} > "${next_config_path}"

install -m 600 "${next_config_path}" "${CODEX_CONFIG_PATH}"
rm -f "${config_without_root_notify_path}" "${next_config_path}"

echo "Installed Codex notify config:"
echo "  config: ${CODEX_CONFIG_PATH}"
echo "  notify: ${notify_config_line}"
if [ -n "${config_backup_path}" ]; then
    echo "  backup: ${config_backup_path}"
fi

if [ "${RUN_TEST_AFTER_INSTALL}" = "--test" ]; then
    CODEX_NOTIFY_SHORTCUT_NAME="${SHORTCUT_NAME}" bash "${NOTIFY_SCRIPT_PATH}" '{"type":"agent-turn-complete","last-assistant-message":"Codex notify install test"}'
    echo "Triggered test notification via Shortcut: ${SHORTCUT_NAME}"
fi
