#!/usr/bin/env bash
# export_env_encrypted.sh — Pack all gitignored .env* files into a password-protected zip.
#
# - Only includes files that are both matching .env* AND gitignored.
# - Output: ../mysecrets/<project_name>_secrets.zip  (one fixed file per project)
# - Password is prompted interactively; required again to extract.
# - Uses AES-256 if the local zip binary supports it, otherwise ZipCrypto.
#
# Usage:
#   ./scripts/secrets/export_env_encrypted.sh

set -euo pipefail

# ── Ensure zip is installed ──────────────────────────────────────────────────
if ! command -v zip &>/dev/null; then
    echo "⚠️  zip is not installed (required for creating encrypted archives)."
    printf "   Install now? [y/N] "
    read -r _zip_choice </dev/tty
    case "$_zip_choice" in
        y|Y) ;;
        *) echo "Aborted."; exit 1 ;;
    esac

    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command -v brew &>/dev/null; then
            brew install zip
        else
            echo "❌ Homebrew not found. Install zip manually."
            exit 1
        fi
    elif grep -qi microsoft /proc/version 2>/dev/null || [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if command -v apt-get &>/dev/null; then
            sudo apt-get install -y zip
        elif command -v apt &>/dev/null; then
            sudo apt install -y zip
        else
            echo "❌ apt not found. Install zip manually."
            exit 1
        fi
    else
        echo "❌ Cannot auto-install on this platform. Install zip manually."
        exit 1
    fi
    echo "✅ zip installed."
    echo ""
fi

PROJECT_ROOT="$(git rev-parse --show-toplevel)"
PROJECT_NAME="$(basename "$PROJECT_ROOT")"
OUTPUT_ZIP="$PROJECT_ROOT/${PROJECT_NAME}_secrets.zip"

cd "$PROJECT_ROOT"

# ── Collect gitignored .env* files ──────────────────────────────────────────
# Uses git ls-files --others --ignored to find files that are both:
#   1. untracked (not committed to git), AND
#   2. matched by gitignore rules.
# This avoids including tracked files that happen to match .env* patterns.
echo "🔍 Scanning for gitignored .env* files..."

env_files=()
while IFS= read -r f; do
    env_files+=("./$f")
done < <(
    git ls-files --others --ignored --exclude-standard \
        | grep -E '(^|/)\.env[^/]*$' \
        | sort
)

if [ "${#env_files[@]}" -eq 0 ]; then
    echo "No gitignored .env* files found. Nothing to archive."
    exit 0
fi

echo "Found ${#env_files[@]} file(s):"
for f in "${env_files[@]}"; do
    echo "  $f"
done
echo ""

# ── Check for existing archive ───────────────────────────────────────────────
if [ -f "$OUTPUT_ZIP" ]; then
    echo "⚠️  Archive already exists: $OUTPUT_ZIP"
    printf "   Overwrite? [y/N] "
    read -r overwrite_choice </dev/tty
    case "$overwrite_choice" in
        y|Y)
            rm -f "$OUTPUT_ZIP"
            echo ""
            ;;
        *)
            echo "Aborted."
            exit 0
            ;;
    esac
fi

# ── Detect AES-256 support ───────────────────────────────────────────────────
ZIP_AES_SUPPORTED=false
if zip --help 2>&1 | grep -iq "aes"; then
    ZIP_AES_SUPPORTED=true
fi

# ── Create encrypted archive ─────────────────────────────────────────────────
echo "Creating encrypted archive: $OUTPUT_ZIP"
if $ZIP_AES_SUPPORTED; then
    echo "Encryption: AES-256"
else
    echo "Encryption: ZipCrypto (AES-256 not available; install Info-ZIP for stronger encryption)"
fi
echo "You will be prompted for a password (entered twice)."
echo ""

if $ZIP_AES_SUPPORTED; then
    zip -e -Z aes256 "$OUTPUT_ZIP" "${env_files[@]}"
else
    zip -e "$OUTPUT_ZIP" "${env_files[@]}"
fi

echo ""
echo "✅ Done: $OUTPUT_ZIP"
echo "   ${#env_files[@]} file(s) encrypted."
echo ""
echo "To extract:"
echo "  unzip \"$OUTPUT_ZIP\""
