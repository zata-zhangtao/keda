#!/usr/bin/env bash
# export_env_encrypted.sh — Pack all gitignored .env* files into a password-protected zip.
#
# - Only includes files that are both matching .env* AND gitignored.
# - Output: ../mysecrets/<project_name>.zip  (one fixed file per project)
# - Password is prompted interactively; required again to extract.
# - Uses AES-256 if the local zip binary supports it, otherwise ZipCrypto.
#
# Usage:
#   ./scripts/shared/secrets/export_env_encrypted.sh

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
OUTPUT_ZIP="$PROJECT_ROOT/${PROJECT_NAME}.zip"

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
        | grep -E '\.env[^/]*$' \
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

# ── Show changes relative to existing archive ────────────────────────────────
# zip stores paths without the leading "./", so normalize both sides before comparing.
if [ -f "$OUTPUT_ZIP" ] && command -v unzip &>/dev/null; then
    echo "📦 Existing archive detected. Calculating changes..."

    current_names=()
    for f in "${env_files[@]}"; do
        current_names+=("${f#./}")
    done

    archived_names=()
    while IFS= read -r f; do
        [ -n "$f" ] && archived_names+=("${f#./}")
    done < <(unzip -Z -1 "$OUTPUT_ZIP" | sort)

    added=()
    while IFS= read -r f; do
        [ -n "$f" ] && added+=("$f")
    done < <(comm -23 <(printf '%s\n' "${current_names[@]}" | sort) <(printf '%s\n' "${archived_names[@]}" | sort))

    removed=()
    while IFS= read -r f; do
        [ -n "$f" ] && removed+=("$f")
    done < <(comm -13 <(printf '%s\n' "${current_names[@]}" | sort) <(printf '%s\n' "${archived_names[@]}" | sort))

    common=()
    while IFS= read -r f; do
        [ -n "$f" ] && common+=("$f")
    done < <(comm -12 <(printf '%s\n' "${current_names[@]}" | sort) <(printf '%s\n' "${archived_names[@]}" | sort))

    modified=()
    unchanged=()
    _compare_contents=false

    if [ "${#common[@]}" -gt 0 ]; then
        printf "   Compare contents of files that exist in both versions? [y/N] "
        read -r compare_choice </dev/tty
        case "$compare_choice" in
            y|Y)
                printf "   Enter password for existing archive: "
                read -rs archive_password </dev/tty
                echo ""
                _tmp_extract_dir=$(mktemp -d)
                chmod 700 "$_tmp_extract_dir"
                _cleanup_extract() { rm -rf "$_tmp_extract_dir"; }
                trap _cleanup_extract EXIT

                if unzip -q -P "$archive_password" "$OUTPUT_ZIP" -d "$_tmp_extract_dir" 2>/dev/null; then
                    _compare_contents=true
                    for f in "${common[@]}"; do
                        _current_path="$PROJECT_ROOT/$f"
                        _old_path="$_tmp_extract_dir/$f"
                        if [ -f "$_old_path" ] && ! cmp -s "$_old_path" "$_current_path"; then
                            modified+=("$f")
                        else
                            unchanged+=("$f")
                        fi
                    done
                else
                    echo "   ⚠️  Could not unlock archive. Content comparison skipped."
                    unchanged=("${common[@]}")
                fi
                unset archive_password
                ;;
            *)
                unchanged=("${common[@]}")
                ;;
        esac
    fi

    if [ "${#added[@]}" -eq 0 ] && [ "${#removed[@]}" -eq 0 ] && { [ "$_compare_contents" != true ] || [ "${#modified[@]}" -eq 0 ]; }; then
        if [ "$_compare_contents" = true ]; then
            echo "   No file additions, removals, or content changes since the last archive."
        else
            echo "   No file additions or removals since the last archive."
        fi
    else
        echo "   Changes since the last archive:"
        if [ "${#added[@]}" -gt 0 ]; then
            echo "   + Added (${#added[@]}):"
            for f in "${added[@]}"; do
                echo "      + $f"
            done
        fi
        if [ "${#removed[@]}" -gt 0 ]; then
            echo "   - Removed (${#removed[@]}):"
            for f in "${removed[@]}"; do
                echo "      - $f"
            done
        fi
        if [ "${#modified[@]}" -gt 0 ]; then
            echo "   ~ Modified (${#modified[@]}):"
            for f in "${modified[@]}"; do
                echo "      ~ $f"
            done
        fi
    fi

    if [ "${#unchanged[@]}" -gt 0 ]; then
        echo "   = Unchanged (${#unchanged[@]}):"
        for f in "${unchanged[@]}"; do
            echo "      = $f"
        done
    fi

    if [ "${#modified[@]}" -gt 0 ]; then
        printf "   Show detailed diff for modified files? [Y/n] "
        read -r show_diff </dev/tty
        case "$show_diff" in
            n|N) ;;
            *)
                echo ""
                for f in "${modified[@]}"; do
                    echo "   --- diff: $f ---"
                    diff -u --label "old/$f" --label "new/$f" "$_tmp_extract_dir/$f" "$PROJECT_ROOT/$f" || true
                    echo ""
                done
                ;;
        esac
    fi
    echo ""
elif [ -f "$OUTPUT_ZIP" ]; then
    echo "⚠️  Existing archive detected, but unzip is not installed. Cannot show changes."
    echo ""
fi

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
