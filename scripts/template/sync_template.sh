#!/usr/bin/env bash
# sync_template.sh — Compare local project files with the upstream template
# repository and interactively offer to apply updates.
#
# Usage:
#   ./scripts/sync_template.sh
#   ./scripts/sync_template.sh --all   # also show project-specific files

set -euo pipefail

TEMPLATE_REPO="${SYNC_TEMPLATE_TEMPLATE_REPO:-https://github.com/zata-zhangtao/zata-codes-template.git}"
LOCAL_ROOT="$(git rev-parse --show-toplevel)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

LIST_ONLY_MODE="${SYNC_TEMPLATE_LIST_ONLY:-0}"
DEFAULT_PROJECT_SKIP_PATHS=(
    "src/backend/"
    "frontend/"
    "docs/"
    "tests/"
    "apps/"
    "services/"
    "infra/"
    "deploy/"
    "helm/"
    "terraform/"
    "ansible/"
    "data/"
    "uploads/"
    "artifacts/"
    "tmp/"
)
PROJECT_SKIP_PATHS=()
PROJECT_INCLUDE_PATHS=()
PROJECT_SKIP_PATH_COUNT=0
PROJECT_INCLUDE_PATH_COUNT=0

SHOW_ALL=false

while [ $# -gt 0 ]; do
    case "$1" in
        --all)
            SHOW_ALL=true
            shift
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Usage: $0 [--all]" >&2
            exit 1
            ;;
    esac
done

# ──────────────────────────────────────────────────────────────
# Skip rules — files that should never or optionally be synced
# ──────────────────────────────────────────────────────────────
_append_split_paths() {
    local target_array_name="$1"
    local target_count_name="$2"
    local raw_paths="$3"
    local normalized_paths path_entry

    normalized_paths="${raw_paths//,/ }"
    for path_entry in $normalized_paths; do
        if [ -n "$path_entry" ]; then
            eval "$target_array_name+=(\"\$path_entry\")"
            eval "$target_count_name=\$(( \$$target_count_name + 1 ))"
        fi
    done
}

_load_configured_project_paths() {
    local config_file="$LOCAL_ROOT/config.toml"
    local config_output=""
    local configured_skip=false
    local configured_include=false
    local loaded_skip_paths=()
    local loaded_include_paths=()
    local loaded_skip_path_count=0
    local loaded_include_path_count=0
    local config_line config_key config_value

    PROJECT_SKIP_PATHS=("${DEFAULT_PROJECT_SKIP_PATHS[@]}")
    PROJECT_SKIP_PATH_COUNT=${#DEFAULT_PROJECT_SKIP_PATHS[@]}
    PROJECT_INCLUDE_PATHS=()
    PROJECT_INCLUDE_PATH_COUNT=0

    if [ -f "$config_file" ]; then
        if ! config_output="$(python3 - "$config_file" <<'PYEOF'
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit(0)

config_path = Path(sys.argv[1])
try:
    config_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001 - surface TOML parse errors to shell users.
    raise SystemExit(f"failed to parse {config_path}: {exc}") from exc

template_sync_config = config_data.get("template_sync", {})
if not isinstance(template_sync_config, dict):
    raise SystemExit("[template_sync] must be a TOML table")


def emit_paths(config_key: str, output_key: str) -> None:
    if config_key not in template_sync_config:
        return

    value = template_sync_config[config_key]
    if not isinstance(value, list):
        raise SystemExit(f"[template_sync].{config_key} must be a list of strings")

    print(f"{output_key}_configured\t1")
    for raw_path in value:
        if not isinstance(raw_path, str):
            raise SystemExit(f"[template_sync].{config_key} must be a list of strings")
        path_text = raw_path.strip()
        if path_text:
            print(f"{output_key}\t{path_text}")


emit_paths("project_skip_paths", "skip")
emit_paths("project_include_paths", "include")
PYEOF
)"; then
            echo "❌ Invalid template sync config in $config_file" >&2
            echo "$config_output" >&2
            exit 1
        fi

        while IFS= read -r config_line; do
            if [ -z "$config_line" ]; then
                continue
            fi
            config_key="${config_line%%$'\t'*}"
            config_value="${config_line#*$'\t'}"
            case "$config_key" in
                skip_configured) configured_skip=true ;;
                include_configured) configured_include=true ;;
                skip)
                    loaded_skip_paths+=("$config_value")
                    ((loaded_skip_path_count++)) || true
                    ;;
                include)
                    loaded_include_paths+=("$config_value")
                    ((loaded_include_path_count++)) || true
                    ;;
            esac
        done <<< "$config_output"

        if $configured_skip; then
            PROJECT_SKIP_PATHS=("${loaded_skip_paths[@]}")
            PROJECT_SKIP_PATH_COUNT=$loaded_skip_path_count
        fi
        if $configured_include; then
            PROJECT_INCLUDE_PATHS=("${loaded_include_paths[@]}")
            PROJECT_INCLUDE_PATH_COUNT=$loaded_include_path_count
        fi
    fi

    if [ -n "${SYNC_TEMPLATE_PROJECT_SKIP_PATHS:-}" ]; then
        PROJECT_SKIP_PATHS=()
        PROJECT_SKIP_PATH_COUNT=0
        _append_split_paths PROJECT_SKIP_PATHS PROJECT_SKIP_PATH_COUNT "$SYNC_TEMPLATE_PROJECT_SKIP_PATHS"
    fi

    if [ -n "${SYNC_TEMPLATE_PROJECT_INCLUDE_PATHS:-}" ]; then
        PROJECT_INCLUDE_PATHS=()
        PROJECT_INCLUDE_PATH_COUNT=0
        _append_split_paths PROJECT_INCLUDE_PATHS PROJECT_INCLUDE_PATH_COUNT "$SYNC_TEMPLATE_PROJECT_INCLUDE_PATHS"
    fi
}

_path_matches_configured_path() {
    local rel_path="$1"
    local configured_path="$2"

    configured_path="${configured_path#./}"
    configured_path="${configured_path%/}"
    if [ -z "$configured_path" ]; then
        return 1
    fi

    if [ "$rel_path" = "$configured_path" ] || [[ "$rel_path" == "$configured_path/"* ]]; then
        return 0
    fi
    return 1
}

_path_matches_any_configured_path() {
    local rel_path="$1"
    shift

    local configured_path
    for configured_path in "$@"; do
        if _path_matches_configured_path "$rel_path" "$configured_path"; then
            return 0
        fi
    done
    return 1
}

_is_project_skipped_by_default() {
    local p="$1"

    if [ "$PROJECT_INCLUDE_PATH_COUNT" -gt 0 ] \
        && _path_matches_any_configured_path "$p" "${PROJECT_INCLUDE_PATHS[@]}"; then
        return 1
    fi

    if [ "$PROJECT_SKIP_PATH_COUNT" -gt 0 ]; then
        _path_matches_any_configured_path "$p" "${PROJECT_SKIP_PATHS[@]}"
        return $?
    fi

    return 1
}

_is_never_synced() {
    local p="$1"
    case "$p" in
        tasks/*) return 0 ;;
    esac
    return 1
}

_is_skipped_by_default() {
    local p="$1"
    case "$p" in
        README.md|pyproject.toml|config.toml|mkdocs.yml|uv.lock) return 0 ;;
        CLAUDE.md|main.py|justfile) return 0 ;;
        findings.md|progress.md|task_plan.md) return 0 ;;
        .DS_Store) return 0 ;;
    esac
    case "$p" in
        .git/*|.venv/*|.uv-cache/*|__pycache__/*|logs/*|site/*) return 0 ;;
        .pytest_cache/*|.ruff_cache/*|prompt/*|skills/*) return 0 ;;
        .claude/*) return 0 ;;
    esac
    case "$p" in
        *.pyc|*.egg-info|.env|.env.*) return 0 ;;
    esac
    if _is_project_skipped_by_default "$p"; then
        return 0
    fi
    return 1
}

# ──────────────────────────────────────────────────────────────
# OS detection & fzf install
# ──────────────────────────────────────────────────────────────
_detect_os() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    elif grep -qi microsoft /proc/version 2>/dev/null; then
        echo "wsl"
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        echo "linux"
    else
        echo "unknown"
    fi
}

_install_fzf() {
    local os
    os="$(_detect_os)"
    case "$os" in
        macos)
            if ! command -v brew &>/dev/null; then
                echo "  ❌ Homebrew not found. Install fzf manually: https://github.com/junegunn/fzf"
                return 1
            fi
            echo "  Running: brew install fzf"
            brew install fzf
            ;;
        wsl|linux)
            if command -v apt-get &>/dev/null; then
                echo "  Running: sudo apt-get install -y fzf"
                sudo apt-get install -y fzf
            elif command -v apt &>/dev/null; then
                echo "  Running: sudo apt install -y fzf"
                sudo apt install -y fzf
            else
                echo "  ❌ apt not found. Install fzf manually: https://github.com/junegunn/fzf"
                return 1
            fi
            ;;
        *)
            echo "  ❌ Cannot auto-install on this platform. Install manually: https://github.com/junegunn/fzf"
            return 1
            ;;
    esac
}

_ensure_fzf() {
    if command -v fzf &>/dev/null; then
        return 0
    fi
    echo "⚠️  fzf is not installed (used for interactive file selection)."
    printf "   Install now? [y/N] "
    read -r choice </dev/tty
    case "$choice" in
        y|Y)
            if _install_fzf; then
                echo "  ✅ fzf installed."
                return 0
            else
                return 1
            fi
            ;;
        *)
            echo "   Falling back to numbered list mode."
            return 1
            ;;
    esac
}

CC_SWITCH_SKILLS_DIR="${CC_SWITCH_SKILLS_DIR:-}"
SKILL_INSTALL_TARGET_DIR=""

_resolve_skill_install_target_dir() {
    if [ -n "$SKILL_INSTALL_TARGET_DIR" ]; then
        return 0
    fi

    if [ -n "$CC_SWITCH_SKILLS_DIR" ]; then
        SKILL_INSTALL_TARGET_DIR="$CC_SWITCH_SKILLS_DIR"
        return 0
    fi

    if [ -d "$HOME/.cc-switch" ]; then
        SKILL_INSTALL_TARGET_DIR="$HOME/.cc-switch/skills"
        return 0
    fi

    echo "No ~/.cc-switch directory found."
    echo "Choose a skill install target:"
    echo "  [1] Codex  -> $HOME/.codex/skills"
    echo "  [2] Claude -> $HOME/.claude/skills"
    echo "  [q] Skip skill installation"

    while true; do
        local target_choice
        printf "Your choice: "
        read -r target_choice </dev/tty
        case "$target_choice" in
            1)
                SKILL_INSTALL_TARGET_DIR="$HOME/.codex/skills"
                return 0
                ;;
            2)
                SKILL_INSTALL_TARGET_DIR="$HOME/.claude/skills"
                return 0
                ;;
            q|Q|"")
                return 1
                ;;
            *)
                echo "  Invalid choice: $target_choice"
                ;;
        esac
    done
}

_collect_template_skill_updates() {
    local template_root="$1"
    template_skill_entries=()

    if [ ! -d "$template_root/skills" ]; then
        return 0
    fi

    if ! _resolve_skill_install_target_dir; then
        return 0
    fi

    while IFS= read -r template_skill_dir; do
        local skill_name installed_skill_dir skill_status
        skill_name="$(basename "$template_skill_dir")"
        installed_skill_dir="$SKILL_INSTALL_TARGET_DIR/$skill_name"

        if [ ! -d "$installed_skill_dir" ]; then
            skill_status="NEW"
        elif diff -qr "$template_skill_dir" "$installed_skill_dir" >/dev/null 2>&1; then
            continue
        else
            skill_status="UPDATE"
        fi

        template_skill_entries+=("$skill_status"$'\t'"$skill_name"$'\t'"$template_skill_dir")
    done < <(
        find "$template_root/skills" -mindepth 1 -maxdepth 1 -type d ! -name '.*' | sort
    )
}

_install_template_skills() {
    local template_root="$1"
    local selected_skill_names=()

    _collect_template_skill_updates "$template_root"

    if [ "${#template_skill_entries[@]}" -eq 0 ]; then
        return 0
    fi

    echo "Template skills with installable updates for $SKILL_INSTALL_TARGET_DIR:"
    local skill_entry_preview skill_status_preview skill_name_preview
    for skill_entry_preview in "${template_skill_entries[@]}"; do
        skill_status_preview="${skill_entry_preview%%$'\t'*}"
        skill_name_preview="${skill_entry_preview#*$'\t'}"
        skill_name_preview="${skill_name_preview%%$'\t'*}"
        printf "  - [%s] %s\n" "$skill_status_preview" "$skill_name_preview"
    done
    printf "Install or update template skills now? [y/N] "

    local install_choice
    read -r install_choice </dev/tty
    case "$install_choice" in
        y|Y) ;;
        *)
            echo "  Skipped template skill installation."
            return 0
            ;;
    esac

    local skill_list_lines=()
    local skill_entry skill_status skill_name template_skill_dir
    for skill_entry in "${template_skill_entries[@]}"; do
        skill_status="${skill_entry%%$'\t'*}"
        skill_name="${skill_entry#*$'\t'}"
        skill_name="${skill_name%%$'\t'*}"
        if [ "$skill_status" = "NEW" ]; then
            skill_list_lines+=("📄 NEW    	$skill_name")
        else
            skill_list_lines+=("🛠 UPDATE 	$skill_name")
        fi
    done

    if _ensure_fzf; then
        local selected_skill_lines=()
        while IFS= read -r selected_skill_line; do
            selected_skill_lines+=("$selected_skill_line")
        done < <(
            printf '%s\n' "${skill_list_lines[@]}" \
            | fzf \
                --multi \
                --ansi \
                --delimiter=$'\t' \
                --with-nth=1,2 \
                --preview="skill_name=\$(echo {} | cut -f2); skill_dir=\"$template_root/skills/\$skill_name\"; find \"\$skill_dir\" -maxdepth 2 -type f | sort; echo; echo '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'; echo; sed -n '1,160p' \"\$skill_dir/SKILL.md\"" \
                --preview-window=right:60%:wrap \
                --header=$'TAB: toggle select  ENTER: install selected  ESC: skip skill install\n' \
                --bind='tab:toggle+down' \
                --prompt='Select skills > ' \
            || true
        )

        local selected_skill_line
        for selected_skill_line in "${selected_skill_lines[@]}"; do
            selected_skill_names+=("$(echo "$selected_skill_line" | cut -f2)")
        done
    else
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        local idx=1
        local -a skill_map
        for skill_entry in "${template_skill_entries[@]}"; do
            skill_status="${skill_entry%%$'\t'*}"
            skill_name="${skill_entry#*$'\t'}"
            skill_name="${skill_name%%$'\t'*}"
            if [ "$skill_status" = "NEW" ]; then
                printf "  [%2d] 📄 NEW      %s\n" "$idx" "$skill_name"
            else
                printf "  [%2d] 🛠 UPDATE   %s\n" "$idx" "$skill_name"
            fi
            skill_map[$idx]="$skill_name"
            ((idx++))
        done

        echo ""
        echo "Enter skill numbers to install (e.g. 1 3), 'all', or 'q' to skip."
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

        while true; do
            local input
            printf "Your choice: "
            read -r input </dev/tty
            case "$input" in
                q|Q|"") echo "  Skipped template skill installation."; return 0 ;;
                all|ALL)
                    local skill_index
                    for skill_index in "${!skill_map[@]}"; do
                        selected_skill_names+=("${skill_map[$skill_index]}")
                    done
                    break
                    ;;
                *)
                    local valid=true
                    local -a nums=()
                    local num
                    for num in $input; do
                        if [ -n "${skill_map[$num]+_}" ]; then
                            nums+=("$num")
                        else
                            echo "  Invalid number: $num"
                            valid=false
                        fi
                    done
                    if $valid; then
                        for num in "${nums[@]}"; do
                            selected_skill_names+=("${skill_map[$num]}")
                        done
                        break
                    fi
                    ;;
            esac
        done
    fi

    if [ "${#selected_skill_names[@]}" -eq 0 ]; then
        echo "  No skills selected for installation."
        return 0
    fi

    mkdir -p "$SKILL_INSTALL_TARGET_DIR"

    local installed_count=0
    for skill_name in "${selected_skill_names[@]}"; do
        template_skill_dir="$template_root/skills/$skill_name"
        rsync -a --delete "$template_skill_dir/" "$SKILL_INSTALL_TARGET_DIR/$skill_name/"
        echo "  ✅ Installed: $SKILL_INSTALL_TARGET_DIR/$skill_name"
        ((installed_count++)) || true
    done

    skill_install_count=$installed_count
}

# ──────────────────────────────────────────────────────────────
# Justfile recipe-level helper (written once to $TEMP_DIR)
# ──────────────────────────────────────────────────────────────
JF_HELPER="$TEMP_DIR/jf.py"
cat > "$JF_HELPER" << 'PYEOF'
#!/usr/bin/env python3
"""Justfile recipe parser used by sync_template.sh.

Commands:
  names  <file>              print all recipe names, one per line
  block  <file> <name>       print the full block for a recipe (with preceding comments)
  append <file>              append stdin content to file
  replace <file> <name>      replace a recipe block with stdin content
"""
import sys, re

RECIPE_RE = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_-]*)[^\S\n]*[^:\n]*:(?![=])')
ASSIGN_RE = re.compile(r'^[a-zA-Z_]\w*\s*(:=|::=)')


def read_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return f.readlines()


def find_headers(lines: list[str]) -> list[tuple[int, str]]:
    """Return [(line_idx, recipe_name), ...] for every recipe header."""
    headers = []
    for i, line in enumerate(lines):
        m = RECIPE_RE.match(line)
        if m and not ASSIGN_RE.match(line):
            headers.append((i, m.group(1)))
    return headers


def block_range(lines: list[str], headers: list[tuple[int, str]], pos: int) -> tuple[int, int]:
    """Return (start, end) line indices for the recipe block at headers[pos].

    'start' walks back to include preceding comment/blank lines.
    'end' is the line just before the next recipe's comment block.
    """
    line_idx = headers[pos][0]
    start = line_idx
    while start > 0:
        prev = lines[start - 1]
        if prev.startswith("#") or prev.strip() == "":
            start -= 1
        else:
            break
    if pos + 1 < len(headers):
        end = headers[pos + 1][0]
        while end > line_idx + 1 and lines[end - 1].strip() == "":
            end -= 1
    else:
        end = len(lines)
    return start, end


if __name__ == "__main__":
    cmd = sys.argv[1]

    if cmd == "names":
        lines = read_lines(sys.argv[2])
        for _, name in find_headers(lines):
            print(name)

    elif cmd == "block":
        lines = read_lines(sys.argv[2])
        headers = find_headers(lines)
        for pos, (_, name) in enumerate(headers):
            if name == sys.argv[3]:
                s, e = block_range(lines, headers, pos)
                sys.stdout.write("".join(lines[s:e]))
                sys.exit(0)
        sys.exit(1)  # recipe not found

    elif cmd == "append":
        content = sys.stdin.read()
        with open(sys.argv[2], "a", encoding="utf-8") as f:
            if content and not content.startswith("\n"):
                f.write("\n")
            f.write(content)

    elif cmd == "replace":
        content = sys.stdin.read()
        lines = read_lines(sys.argv[2])
        headers = find_headers(lines)
        for pos, (_, name) in enumerate(headers):
            if name == sys.argv[3]:
                s, e = block_range(lines, headers, pos)
                with open(sys.argv[2], "w", encoding="utf-8") as f:
                    f.writelines(lines[:s])
                    f.write(content)
                    f.writelines(lines[e:])
                sys.exit(0)
        sys.exit(1)
PYEOF

# ──────────────────────────────────────────────────────────────
# Detect diff color support
# ──────────────────────────────────────────────────────────────
DIFF_COLOR_FLAG=""
diff --color=always /dev/null /dev/null 2>/dev/null && DIFF_COLOR_FLAG="--color=always"

_load_configured_project_paths

# ──────────────────────────────────────────────────────────────
# Clone template
# ──────────────────────────────────────────────────────────────
echo "🔍 Fetching template from $TEMPLATE_REPO ..."
git clone --depth=1 --quiet "$TEMPLATE_REPO" "$TEMP_DIR/template"
TEMPLATE_ROOT="$TEMP_DIR/template"
echo "✅ Template fetched."
echo ""

# ──────────────────────────────────────────────────────────────
# Phase 1: Scan — collect changed / new entries
#
# Normal files   → "changed" or "new" entry with the rel path
# justfile       → expanded into per-recipe entries: "justfile::recipe-name"
#                  (new file → single "justfile" entry, no expansion needed)
# ──────────────────────────────────────────────────────────────
changed_entries=()  # may include "justfile::recipe" entries
new_entries=()

while IFS= read -r rel_path; do
    if _is_never_synced "$rel_path"; then
        continue
    fi

    if ! $SHOW_ALL && _is_skipped_by_default "$rel_path"; then
        continue
    fi

    local_file="$LOCAL_ROOT/$rel_path"
    tmpl_file="$TEMPLATE_ROOT/$rel_path"

    # ── New file ──────────────────────────────────────────────
    if [ ! -f "$local_file" ]; then
        new_entries+=("$rel_path")
        continue
    fi

    # ── Identical ─────────────────────────────────────────────
    if diff -q "$local_file" "$tmpl_file" > /dev/null 2>&1; then
        continue
    fi

    # ── Changed: justfile gets recipe-level expansion ─────────
    if [ "$rel_path" = "justfile" ]; then
        # macOS ships Bash 3.2, so avoid Bash 4-only mapfile/readarray here.
        local_recipes=()
        while IFS= read -r recipe_name; do
            local_recipes+=("$recipe_name")
        done < <(python3 "$JF_HELPER" names "$local_file" 2>/dev/null || true)

        tmpl_recipes=()
        while IFS= read -r recipe_name; do
            tmpl_recipes+=("$recipe_name")
        done < <(python3 "$JF_HELPER" names "$tmpl_file" 2>/dev/null || true)

        for recipe in "${tmpl_recipes[@]}"; do
            local_block=$(python3 "$JF_HELPER" block "$local_file" "$recipe" 2>/dev/null || true)
            tmpl_block=$(python3  "$JF_HELPER" block "$tmpl_file"  "$recipe" 2>/dev/null || true)

            if [ -z "$local_block" ]; then
                new_entries+=("justfile::$recipe")
            elif [ "$local_block" != "$tmpl_block" ]; then
                changed_entries+=("justfile::$recipe")
            fi
        done
        continue
    fi

    # ── Changed: normal file ──────────────────────────────────
    changed_entries+=("$rel_path")

done < <(
    find "$TEMPLATE_ROOT" -type f \
        ! -path '*/.git/*' \
        | sed "s|$TEMPLATE_ROOT/||" \
        | sort
)

total_found=$(( ${#changed_entries[@]} + ${#new_entries[@]} ))

template_skill_entries=()
if [ "$LIST_ONLY_MODE" = "1" ]; then
    pending_skill_updates=0
elif [ -d "$TEMPLATE_ROOT/skills" ]; then
    _collect_template_skill_updates "$TEMPLATE_ROOT"
    pending_skill_updates=${#template_skill_entries[@]}
else
    pending_skill_updates=0
fi

if [ "$total_found" -eq 0 ] && [ "$pending_skill_updates" -eq 0 ]; then
    echo "✨ Everything is up to date with the template."
    exit 0
fi

echo "Found ${#changed_entries[@]} changed + ${#new_entries[@]} new entry/entries."
if [ "$pending_skill_updates" -gt 0 ]; then
    echo "Found $pending_skill_updates template skill install/update candidate(s)."
fi
echo ""

if [ "$LIST_ONLY_MODE" = "1" ]; then
    for entry in "${changed_entries[@]}"; do
        printf 'CHANGED\t%s\n' "$entry"
    done
    for entry in "${new_entries[@]}"; do
        printf 'NEW\t%s\n' "$entry"
    done
    exit 0
fi

# ──────────────────────────────────────────────────────────────
# Phase 2: Select — fzf UI or numbered fallback
# ──────────────────────────────────────────────────────────────

# Build tab-separated display lines: "<icon> <label>\t<entry>"
file_list_lines=()
if [ "${#changed_entries[@]}" -gt 0 ]; then
    for e in "${changed_entries[@]}"; do
        if [[ "$e" == justfile::* ]]; then
            file_list_lines+=("📝 CHANGED	$e")
        else
            file_list_lines+=("📝 CHANGED	$e")
        fi
    done
fi
if [ "${#new_entries[@]}" -gt 0 ]; then
    for e in "${new_entries[@]}"; do
        file_list_lines+=("📄 NEW    	$e")
    done
fi

selected_entries=()

if [ "$total_found" -eq 0 ]; then
    echo "No template file diffs to apply."
elif _ensure_fzf; then
    # ── fzf interactive mode ──────────────────────────────────
    export FZF_SYNC_LOCAL="$LOCAL_ROOT"
    export FZF_SYNC_TMPL="$TEMPLATE_ROOT"
    export FZF_JF_HELPER="$JF_HELPER"

    preview_cmd='
        entry=$(echo {} | cut -f2)
        if [[ "$entry" == justfile::* ]]; then
            recipe="${entry#justfile::}"
            local_block=$(python3 "$FZF_JF_HELPER" block "$FZF_SYNC_LOCAL/justfile" "$recipe" 2>/dev/null || true)
            tmpl_block=$(python3  "$FZF_JF_HELPER" block "$FZF_SYNC_TMPL/justfile"  "$recipe" 2>/dev/null || true)
            if [ -z "$local_block" ]; then
                echo "(new recipe — template content:)"
                echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                echo "$tmpl_block"
            else
                diff '"$DIFF_COLOR_FLAG"' -u \
                    <(echo "$local_block") \
                    <(echo "$tmpl_block") || true
            fi
        else
            local_f="$FZF_SYNC_LOCAL/$entry"
            tmpl_f="$FZF_SYNC_TMPL/$entry"
            if [ ! -f "$local_f" ]; then
                echo "(new file — template content:)"
                echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                cat "$tmpl_f"
            else
                diff '"$DIFF_COLOR_FLAG"' -u "$local_f" "$tmpl_f" || true
            fi
        fi
    '

    selected_lines=()
    while IFS= read -r selected_line; do
        selected_lines+=("$selected_line")
    done < <(
        printf '%s\n' "${file_list_lines[@]}" \
        | fzf \
            --multi \
            --ansi \
            --delimiter=$'\t' \
            --with-nth=1,2 \
            --preview="$preview_cmd" \
            --preview-window=right:60%:wrap \
            --header=$'TAB: toggle select  ENTER: apply selected  ESC: quit\n' \
            --bind='tab:toggle+down' \
            --prompt='Select entries > ' \
        || true
    )

    if [ "${#selected_lines[@]}" -gt 0 ]; then
        for line in "${selected_lines[@]}"; do
            selected_entries+=("$(echo "$line" | cut -f2)")
        done
    fi

else
    # ── Numbered list fallback ────────────────────────────────
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    idx=1
    entry_map=()
    if [ "${#changed_entries[@]}" -gt 0 ]; then
        for e in "${changed_entries[@]}"; do
            if [[ "$e" == justfile::* ]]; then
                printf "  [%2d] 📝 CHANGED  %s\n" "$idx" "${e/justfile::/justfile (recipe: }"
            else
                printf "  [%2d] 📝 CHANGED  %s\n" "$idx" "$e"
            fi
            entry_map[$idx]="$e"
            ((idx++))
        done
    fi
    if [ "${#new_entries[@]}" -gt 0 ]; then
        for e in "${new_entries[@]}"; do
            if [[ "$e" == justfile::* ]]; then
                printf "  [%2d] 📄 NEW      %s\n" "$idx" "${e/justfile::/justfile (recipe: }"
            else
                printf "  [%2d] 📄 NEW      %s\n" "$idx" "$e"
            fi
            entry_map[$idx]="$e"
            ((idx++))
        done
    fi

    echo ""
    echo "Enter numbers to update (e.g. 1 3 5), 'all', or 'q' to quit."
    echo "Prefix with 'd' to preview diff (e.g. d2)."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    _show_entry_diff() {
        local entry="$1"
        if [[ "$entry" == justfile::* ]]; then
            local recipe="${entry#justfile::}"
            local local_block tmpl_block
            local_block=$(python3 "$JF_HELPER" block "$LOCAL_ROOT/justfile" "$recipe" 2>/dev/null || true)
            tmpl_block=$(python3  "$JF_HELPER" block "$TEMPLATE_ROOT/justfile" "$recipe" 2>/dev/null || true)
            if [ -z "$local_block" ]; then
                echo "(new recipe)"; echo "$tmpl_block"
            else
                # shellcheck disable=SC2086
                diff $DIFF_COLOR_FLAG -u <(echo "$local_block") <(echo "$tmpl_block") || true
            fi
        else
            local local_f="$LOCAL_ROOT/$entry" tmpl_f="$TEMPLATE_ROOT/$entry"
            if [ ! -f "$local_f" ]; then
                echo "(new file)"; cat "$tmpl_f"
            else
                # shellcheck disable=SC2086
                diff $DIFF_COLOR_FLAG -u "$local_f" "$tmpl_f" || true
            fi
        fi
    }

    while true; do
        printf "Your choice: "
        read -r input </dev/tty
        case "$input" in
            q|Q) echo "Aborted."; exit 0 ;;
            all|ALL)
                for key in "${!entry_map[@]}"; do selected_entries+=("${entry_map[$key]}"); done
                break
                ;;
            d\ *|d[0-9]*)
                num="${input#d }"; num="${num#d}"; num="${num// /}"
                if [ -n "${entry_map[$num]+_}" ]; then
                    echo ""; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    _show_entry_diff "${entry_map[$num]}"
                    echo ""
                else
                    echo "  Invalid number: $num"
                fi
                ;;
            "") echo "  Nothing selected." ;;
            *)
                valid=true; nums=()
                for num in $input; do
                    if [ -n "${entry_map[$num]+_}" ]; then nums+=("$num")
                    else echo "  Invalid number: $num"; valid=false; fi
                done
                if $valid; then
                    for num in "${nums[@]}"; do selected_entries+=("${entry_map[$num]}"); done
                    break
                fi
                ;;
        esac
    done
fi

# ──────────────────────────────────────────────────────────────
# Phase 3: Apply selected entries
# ──────────────────────────────────────────────────────────────
echo ""
count_accepted=0
skill_install_count=0

# Collect justfile recipe operations separately so we apply them in one pass
jf_new_recipes=()
jf_changed_recipes=()

if [ "${#selected_entries[@]}" -eq 0 ]; then
    echo "No template file entries selected."
else
    for entry in "${selected_entries[@]}"; do
        if [[ "$entry" == justfile::* ]]; then
            recipe="${entry#justfile::}"
            # Check if it's new or changed
            local_block=$(python3 "$JF_HELPER" block "$LOCAL_ROOT/justfile" "$recipe" 2>/dev/null || true)
            if [ -z "$local_block" ]; then
                jf_new_recipes+=("$recipe")
            else
                jf_changed_recipes+=("$recipe")
            fi
            continue
        fi

        # Normal file
        local_file="$LOCAL_ROOT/$entry"
        tmpl_file="$TEMPLATE_ROOT/$entry"
        mkdir -p "$(dirname "$local_file")"
        if [ ! -f "$local_file" ]; then
            cp "$tmpl_file" "$local_file"
            echo "  ✅ Added:   $entry"
        else
            cp "$tmpl_file" "$local_file"
            echo "  ✅ Updated: $entry"
        fi
        ((count_accepted++)) || true
    done

    # Apply justfile changed recipes (replace in-place)
    if [ "${#jf_changed_recipes[@]}" -gt 0 ]; then
        for recipe in "${jf_changed_recipes[@]}"; do
            python3 "$JF_HELPER" block "$TEMPLATE_ROOT/justfile" "$recipe" \
                | python3 "$JF_HELPER" replace "$LOCAL_ROOT/justfile" "$recipe"
            echo "  ✅ Updated: justfile (recipe: $recipe)"
            ((count_accepted++)) || true
        done
    fi

    # Apply justfile new recipes (append)
    if [ "${#jf_new_recipes[@]}" -gt 0 ]; then
        for recipe in "${jf_new_recipes[@]}"; do
            python3 "$JF_HELPER" block "$TEMPLATE_ROOT/justfile" "$recipe" \
                | python3 "$JF_HELPER" append "$LOCAL_ROOT/justfile"
            echo "  ✅ Added:   justfile (recipe: $recipe)"
            ((count_accepted++)) || true
        done
    fi
fi

_install_template_skills "$TEMPLATE_ROOT"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Done. $count_accepted template entry/entries applied, $(( total_found - count_accepted )) skipped."
echo "Template skills installed or updated: $skill_install_count."
