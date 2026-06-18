#!/usr/bin/env bash
# keda / iar CLI installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/zata-zhangtao/keda/main/install.sh | bash
#   curl -fsSL ... | bash -s -- --version v0.2.0
#   bash install.sh --check
#   bash install.sh --uninstall
#
# Behaviour:
#   * Detects host OS (macOS / Linux) and Python >= 3.11.
#   * Prefers `uv` (bootstrap if missing), then `pipx`, then `pip --user`.
#   * Installs the keda wheel from the GitHub Release tarball by default.
#   * Verifies `iar --version` exits 0 and emits a clear PATH hint if needed.
#   * Refuses to use sudo; never touches system package managers.
#
# Environment overrides:
#   KEDA_VERSION       Tag to install (default: latest non-draft release).
#   KEDA_SOURCE=auto|pypi|tarball  Install source (default: auto = GitHub tarball).
#   KEDA_PYPI=1        Legacy alias for `--source pypi` (kept for backward compatibility).
#   KEDA_INSTALL_METHOD=uv|pipx|pip  Force installer selection.

set -euo pipefail

readonly REPO_SLUG="${KEDA_REPO:-zata-zhangtao/keda}"
readonly PY_MIN_MAJOR=3
readonly PY_MIN_MINOR=11
readonly DEFAULT_TOOL_NAME="keda"
readonly TOOL_BIN_NAME="iar"

INSTALL_METHOD="${KEDA_INSTALL_METHOD:-}"
VERSION_TAG="${KEDA_VERSION:-}"
SOURCE="${KEDA_SOURCE:-auto}"
if [ "${KEDA_PYPI:-0}" = "1" ]; then
    SOURCE="pypi"
fi
UNINSTALL_ONLY=0
CHECK_ONLY=0
SHORT_HELP=0

log_info() { printf '\033[36m[install]\033[0m %s\n' "$*"; }
log_warn() { printf '\033[33m[install]\033[0m %s\n' "$*" >&2; }
log_err()  { printf '\033[31m[install]\033[0m %s\n' "$*" >&2; }

show_help() {
    cat <<'EOF'
keda / iar installer

Usage: install.sh [options]
  --version <tag>     Install a specific release tag (default: latest).
  --method uv|pipx|pip  Force a specific installer.
  --source auto|pypi|tarball  Install source (default: auto = GitHub tarball).
  --check             Dry-run; print the plan without writing anything.
  --uninstall         Remove the keda tool environment and the iar binary.
  -h, --help          Show this help.

Environment:
  KEDA_VERSION         Same as --version.
  KEDA_SOURCE=auto|pypi|tarball  Same as --source.
  KEDA_PYPI=1          Legacy alias for `--source pypi`.
  KEDA_INSTALL_METHOD  Same as --method.
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --version) VERSION_TAG="${2:-}"; shift 2 ;;
            --version=*) VERSION_TAG="${1#*=}"; shift ;;
            --method) INSTALL_METHOD="${2:-}"; shift 2 ;;
            --method=*) INSTALL_METHOD="${1#*=}"; shift ;;
            --source) SOURCE="${2:-}"; shift 2 ;;
            --source=*) SOURCE="${1#*=}"; shift ;;
            --check) CHECK_ONLY=1; shift ;;
            --uninstall) UNINSTALL_ONLY=1; shift ;;
            -h|--help) show_help; exit 0 ;;
            *) log_err "Unknown option: $1"; show_help; exit 2 ;;
        esac
    done
}

detect_os() {
    case "$(uname -s)" in
        Darwin) HOST_OS="darwin" ;;
        Linux)  HOST_OS="linux" ;;
        *) log_err "Unsupported OS: $(uname -s)"; exit 1 ;;
    esac
    case "$(uname -m)" in
        arm64|aarch64) HOST_ARCH="arm64" ;;
        x86_64|amd64)  HOST_ARCH="x86_64" ;;
        *) log_err "Unsupported architecture: $(uname -m)"; exit 1 ;;
    esac
}

detect_python() {
    if ! command -v python3 >/dev/null 2>&1; then
        log_err "python3 not found in PATH. Install Python >= ${PY_MIN_MAJOR}.${PY_MIN_MINOR} first."
        exit 1
    fi
    PY_BIN="$(command -v python3)"
    PY_VERSION="$("$PY_BIN" -c 'import sys;print("%d.%d.%d" % sys.version_info[:3])')"
    if ! "$PY_BIN" -c "import sys;sys.exit(0 if sys.version_info>=(${PY_MIN_MAJOR},${PY_MIN_MINOR}) else 1)"; then
        log_err "Python ${PY_VERSION} is too old; need >= ${PY_MIN_MAJOR}.${PY_MIN_MINOR}."
        exit 1
    fi
}

resolve_installer() {
    if [ -n "$INSTALL_METHOD" ]; then
        case "$INSTALL_METHOD" in
            uv|pipx|pip) ;;
            *) log_err "Unknown --method: $INSTALL_METHOD"; exit 2 ;;
        esac
        return
    fi
    if command -v uv >/dev/null 2>&1; then
        INSTALL_METHOD="uv"
    elif command -v pipx >/dev/null 2>&1; then
        INSTALL_METHOD="pipx"
    else
        INSTALL_METHOD="pip"
    fi
}

bootstrap_uv() {
    log_info "uv not found; bootstrapping via astral.sh/install.sh"
    if [ "$CHECK_ONLY" -eq 1 ]; then
        log_info "[check] would run: curl -LsSf https://astral.sh/uv/install.sh | sh"
        INSTALL_METHOD="uv"
        return
    fi
    curl -LsSf --max-time 60 https://astral.sh/uv/install.sh | sh >/dev/null
    # shellcheck disable=SC1091
    if [ -f "$HOME/.local/bin/env" ]; then
        . "$HOME/.local/bin/env"
    fi
    if ! command -v uv >/dev/null 2>&1; then
        log_err "uv bootstrap failed; please install uv manually: https://docs.astral.sh/uv/"
        exit 1
    fi
    INSTALL_METHOD="uv"
}

resolve_version() {
    if [ -n "$VERSION_TAG" ]; then
        return
    fi
    if [ "$CHECK_ONLY" -eq 1 ]; then
        VERSION_TAG="<latest>"
        return
    fi
    local latest
    if ! latest="$(curl -fsSL --max-time 30 "https://api.github.com/repos/${REPO_SLUG}/releases/latest" \
        | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"; then
        log_warn "Could not determine latest release; falling back to install from main branch."
        VERSION_TAG="main"
    else
        VERSION_TAG="$latest"
    fi
}

tarball_url() {
    case "$SOURCE" in
        pypi)
            printf 'pypi:%s' "$DEFAULT_TOOL_NAME"
            ;;
        auto|tarball)
            printf 'https://github.com/%s/archive/refs/tags/%s.tar.gz' "$REPO_SLUG" "$VERSION_TAG"
            ;;
        *)
            log_err "Unknown --source: $SOURCE (expected auto|pypi|tarball)"
            exit 2
            ;;
    esac
}

print_plan() {
    cat <<EOF
[install] plan:
  os:        ${HOST_OS}/${HOST_ARCH}
  python:    ${PY_VERSION}
  method:    ${INSTALL_METHOD}
  version:   ${VERSION_TAG:-<unset>}
  source:    $(tarball_url || true)
  tool:      ${DEFAULT_TOOL_NAME} (binary: ${TOOL_BIN_NAME})
EOF
}

run_install() {
    local source
    source="$(tarball_url)"
    log_info "Installing ${DEFAULT_TOOL_NAME} from ${source}"
    case "$INSTALL_METHOD" in
        uv)
            if ! command -v uv >/dev/null 2>&1; then bootstrap_uv; fi
            if [ "$CHECK_ONLY" -eq 1 ]; then
                log_info "[check] would run: uv tool install ${source}"
                return
            fi
            uv tool install --reinstall "$source"
            ;;
        pipx)
            if [ "$CHECK_ONLY" -eq 1 ]; then
                log_info "[check] would run: pipx install ${source}"
                return
            fi
            pipx install --force "$source"
            ;;
        pip)
            if [ "$CHECK_ONLY" -eq 1 ]; then
                log_info "[check] would run: python3 -m pip install --user ${source}"
                return
            fi
            "$PY_BIN" -m pip install --user --upgrade "$source"
            ;;
    esac
}

run_uninstall() {
    if [ "$CHECK_ONLY" -eq 1 ]; then
        log_info "[check] would remove tool env + iar binary"
        return
    fi
    case "$INSTALL_METHOD" in
        uv)    uv tool uninstall "$DEFAULT_TOOL_NAME" >/dev/null 2>&1 || true ;;
        pipx)  pipx uninstall "$DEFAULT_TOOL_NAME" >/dev/null 2>&1 || true ;;
        pip)   "$PY_BIN" -m pip uninstall -y "$DEFAULT_TOOL_NAME" >/dev/null 2>&1 || true ;;
    esac
    rm -f "$HOME/.local/bin/${TOOL_BIN_NAME}"
    log_info "Uninstall complete."
}

verify_iar() {
    if [ "$CHECK_ONLY" -eq 1 ]; then
        log_info "[check] would run: ${TOOL_BIN_NAME} --version"
        return
    fi
    if ! command -v "$TOOL_BIN_NAME" >/dev/null 2>&1; then
        log_err "Install reported success but '${TOOL_BIN_NAME}' is not on PATH."
        log_err "Add ~/.local/bin to your PATH (or restart the shell) and retry."
        exit 1
    fi
    if ! "$TOOL_BIN_NAME" --version >/dev/null 2>&1; then
        log_err "'${TOOL_BIN_NAME} --version' failed; the install may be incomplete."
        exit 1
    fi
    log_info "$($TOOL_BIN_NAME --version)"
}

main() {
    parse_args "$@"
    detect_os
    detect_python
    resolve_installer
    resolve_version

    if [ "$CHECK_ONLY" -eq 1 ]; then
        log_info "Dry-run; no changes will be made."
        print_plan
        return
    fi

    if [ "$UNINSTALL_ONLY" -eq 1 ]; then
        run_uninstall
        return
    fi

    log_info "Selected installer: ${INSTALL_METHOD}; source: $(tarball_url)"
    run_install
    verify_iar
    log_info "Done. Run \`iar init\` inside a Git repository to start."
}

main "$@"
