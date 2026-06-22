#!/usr/bin/env bash

QUALITY_TEST_EXCLUDED_FILE_PATTERN='\.(md|rst|txt|log|png|jpg|jpeg|gif|svg|ico|drawio|pdf|docx|xlsx|zip|tar|gz|bz2|example)$'
QUALITY_ARCHIVED_PRD_PATH_PATTERN='^tasks/archive/([^/]+-prd-[^/]+|P[0-3]-[A-Z]+-[0-9]{8}-[0-9]{6}-[^/]+)\.md$'

quality_git_dir() {
    git rev-parse --git-dir 2>/dev/null || echo ".git"
}

quality_has_head() {
    git rev-parse --verify HEAD >/dev/null 2>&1
}

quality_empty_tree_hash() {
    git hash-object -t tree /dev/null
}

quality_branch_name() {
    local branch_name

    if branch_name="$(git symbolic-ref --quiet --short HEAD 2>/dev/null)"; then
        printf '%s\n' "$branch_name"
        return 0
    fi

    if quality_has_head; then
        echo "HEAD"
        return 0
    fi

    echo "unknown"
}

quality_head_hash() {
    local head_hash

    if head_hash="$(git rev-parse --verify HEAD 2>/dev/null)"; then
        printf '%s\n' "$head_hash"
        return 0
    fi

    echo "no-commit"
}

quality_staged_file_paths() {
    git diff --cached --name-only
}

quality_working_file_paths() {
    if quality_has_head; then
        {
            git diff --name-only HEAD
            git ls-files --others --exclude-standard
        } | awk 'NF && !seen[$0]++'
        return 0
    fi

    git ls-files --cached --others --exclude-standard
}

quality_effective_tree() {
    local source_kind="$1"
    local tree_scope="$2"
    local base_tree_hash
    local temp_index
    local path_source_cmd

    temp_index="$(mktemp)"
    base_tree_hash="$(quality_empty_tree_hash)"
    if quality_has_head; then
        base_tree_hash="HEAD"
    fi
    GIT_INDEX_FILE="$temp_index" git read-tree "$base_tree_hash"

    case "$source_kind" in
        staged)
            path_source_cmd=(quality_staged_file_paths)
            ;;
        working)
            path_source_cmd=(quality_working_file_paths)
            ;;
        *)
            echo "unknown"
            rm -f "$temp_index"
            return 1
            ;;
    esac

    while IFS= read -r file_path; do
        if [ -z "$file_path" ]; then
            continue
        fi
        if [ "$tree_scope" = "test" ] && [[ "$file_path" =~ $QUALITY_TEST_EXCLUDED_FILE_PATTERN ]]; then
            continue
        fi
        GIT_INDEX_FILE="$temp_index" git add -- "$file_path"
    done < <("${path_source_cmd[@]}")

    GIT_INDEX_FILE="$temp_index" git write-tree 2>/dev/null || echo "unknown"
    rm -f "$temp_index"
}

quality_write_flag() {
    local flag_file="$1"
    local branch_name="$2"
    local head_hash="$3"
    local tree_hash="$4"

    mkdir -p "$(dirname "$flag_file")"
    printf '%s\n%s\n%s\n' "$branch_name" "$head_hash" "$tree_hash" > "$flag_file"
}

quality_flag_matches() {
    local flag_file="$1"
    local branch_name="$2"
    local head_hash="$3"
    local tree_hash="$4"
    local flag_branch
    local flag_head
    local flag_tree

    if [ ! -f "$flag_file" ]; then
        return 1
    fi

    flag_branch="$(sed -n '1p' "$flag_file")"
    flag_head="$(sed -n '2p' "$flag_file")"
    flag_tree="$(sed -n '3p' "$flag_file")"

    [ "$branch_name" = "$flag_branch" ] && [ "$head_hash" = "$flag_head" ] && [ "$tree_hash" = "$flag_tree" ]
}

quality_skip_contains() {
    local hook_id="$1"
    local skip_value

    skip_value=",${SKIP:-},"
    skip_value="${skip_value//[[:space:]]/}"
    [[ "$skip_value" == *",$hook_id,"* ]]
}

quality_skip_is_empty_or_only() {
    local allowed_hook_id="$1"
    local skip_value

    skip_value="${SKIP:-}"
    skip_value="${skip_value//[[:space:]]/}"
    [ -z "$skip_value" ] || [ "$skip_value" = "$allowed_hook_id" ]
}

quality_has_staged_archive_prd_transition() {
    local staged_file_path

    while IFS= read -r staged_file_path; do
        if [[ "$staged_file_path" =~ $QUALITY_ARCHIVED_PRD_PATH_PATTERN ]]; then
            return 0
        fi
    done < <(git diff --cached --name-only --diff-filter=ACR -- tasks/archive)

    return 1
}
