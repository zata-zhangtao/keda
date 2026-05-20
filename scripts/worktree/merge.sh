#!/usr/bin/env bash
set -euo pipefail

# git_worktree_merge.sh
# Purpose:
#   Merge a feature branch into a base branch (default: main), then push.
#
# Parameters:
#   1) <feature_branch>            Required. Branch to be merged.
#   2) [base_branch]               Optional. Target branch. Default: main.
#   3) --remote <name>             Optional. Remote name. Default: zata.
#   4) -d, --delete, --delete-only Optional flag. Skip merge/push, only cleanup.
#   5) --cleanup                   Optional flag. After successful merge/push:
#                                  remove worktree and delete local feature branch.
#   6) --delete-remote             Optional flag. Delete remote feature branch.
#                                  Note: effective only when --cleanup is enabled.
#                                  Also works with -d/--delete/--delete-only.
#   7) --worktree-path <path>      Optional. Worktree path used by cleanup.
#                                  Default: auto-detect by <feature_branch>,
#                                           fallback: $(dirname repo_root)/<feature_branch>
#   8) -h, --help                  Show help and exit.
#
# Preconditions:
#   - Must run inside a Git repository.
#   - Merge mode requires relevant feature/base worktrees to be clean.
#   - Merge mode requires local feature/base branches to exist.
#
resolve_worktree_path_by_branch() {
    local target_branch="$1"
    local target_branch_ref="refs/heads/$target_branch"
    local resolved_worktree_path=""
    resolved_worktree_path="$(
        git worktree list --porcelain | awk -v target_branch_ref="$target_branch_ref" '
            $1 == "worktree" {
                current_worktree_path = $2
            }
            $1 == "branch" {
                if ($2 == target_branch_ref) {
                    print current_worktree_path
                    exit
                }
            }
        '
    )"
    printf '%s' "$resolved_worktree_path"
}

resolve_git_common_dir() {
    local resolved_git_common_dir=""
    resolved_git_common_dir="$(git rev-parse --git-common-dir)"
    if [[ "$resolved_git_common_dir" != /* ]]; then
        resolved_git_common_dir="$(git rev-parse --show-toplevel)/$resolved_git_common_dir"
    fi
    printf '%s' "$resolved_git_common_dir"
}

run_worktree_doctor() {
    # Worktree doctor / cleanup-check mode.
    # When called without arguments: scan all registered worktrees for obvious inconsistencies.
    # When called with a feature branch: focus on that branch's expected worktree path.
    local doctor_feature_branch="${1:-}"

    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "❌ Current directory is not inside a Git repository."
        exit 1
    fi

    local repo_root=""
    repo_root="$(git rev-parse --show-toplevel)"
    cd "$repo_root"

    local git_common_dir=""
    git_common_dir="$(resolve_git_common_dir)"

    local worktrees_dir="$git_common_dir/worktrees"

    echo "🩺 Worktree cleanup doctor"
    echo "   Repository root: $repo_root"
    echo "   Git common dir : $git_common_dir"
    echo

    if [[ -z "$doctor_feature_branch" ]]; then
        # Global scan: registered worktrees whose directories are missing.
        if [[ ! -d "$worktrees_dir" ]]; then
            echo "No '.git/worktrees' directory found. No registered worktrees to inspect."
            return 0
        fi

        echo "Scanning registered worktrees for missing directories..."

        local found_issue="false"
        local current_worktree_path=""
        local current_branch_name=""

        while IFS= read -r line; do
            case "$line" in
                worktree\ *)
                    current_worktree_path="${line#worktree }"
                    ;;
                branch\ refs/heads/*)
                    current_branch_name="${line#branch refs/heads/}"
                    if [[ -n "$current_worktree_path" ]]; then
                        if [[ ! -d "$current_worktree_path" ]]; then
                            found_issue="true"
                            echo "⚠️ Metadata present but directory missing for branch '$current_branch_name':"
                            echo "   - registered worktree path: $current_worktree_path"
                        fi
                    fi
                    current_worktree_path=""
                    current_branch_name=""
                    ;;
            esac
        done < <(git worktree list --porcelain)

        if [[ "$found_issue" == "false" ]]; then
            echo "✅ No obvious stale worktrees with missing directories were found."
        else
            echo
            echo "You can clean up metadata-only entries using:"
            echo "  git worktree prune"
        fi
    else
        # Branch-specific scan: examine the expected worktree path and metadata state.
        local resolved_cleanup_worktree_path=""
        resolved_cleanup_worktree_path="$(resolve_worktree_path_by_branch "$doctor_feature_branch")"
        if [[ -z "$resolved_cleanup_worktree_path" ]]; then
            resolved_cleanup_worktree_path="$(dirname "$repo_root")/$doctor_feature_branch"
        fi

        echo "Checking worktree for feature branch '$doctor_feature_branch'..."
        echo "   Expected worktree path: $resolved_cleanup_worktree_path"

        local dir_exists="false"
        if [[ -d "$resolved_cleanup_worktree_path" ]]; then
            dir_exists="true"
            echo "   - Worktree directory exists on disk."
        else
            echo "   - Worktree directory does NOT exist on disk."
        fi

        local has_metadata="false"
        local metadata_dir_for_branch=""
        if [[ -d "$worktrees_dir" ]]; then
            local entry_path=""
            for metadata_dir_for_branch in "$worktrees_dir"/*; do
                [[ -d "$metadata_dir_for_branch" ]] || continue
                if [[ -f "$metadata_dir_for_branch/gitdir" ]]; then
                    entry_path="$(dirname "$(cat "$metadata_dir_for_branch/gitdir" 2>/dev/null || echo "")")"
                    if [[ "$entry_path" == "$resolved_cleanup_worktree_path" ]]; then
                        has_metadata="true"
                        break
                    fi
                fi
            done
        fi

        if [[ "$has_metadata" == "true" ]]; then
            echo "   - Metadata entry exists under: $metadata_dir_for_branch"
        else
            echo "   - No metadata entry under '.git/worktrees' for this path."
        fi

        echo
        echo "Diagnosis:"
        if [[ "$dir_exists" == "true" && "$has_metadata" == "false" ]]; then
            echo "   ➜ Residual worktree directory detected (metadata already removed)."
            echo "     You can remove it manually if you're sure it's safe:"
            echo "       rm -rf \"$resolved_cleanup_worktree_path\""
        elif [[ "$dir_exists" == "false" && "$has_metadata" == "true" ]]; then
            echo "   ➜ Metadata exists but directory is missing."
            echo "     You can clean it up with:"
            echo "       git worktree prune"
        elif [[ "$dir_exists" == "true" && "$has_metadata" == "true" ]]; then
            echo "   ➜ Worktree appears consistent (directory + metadata present)."
            echo "     If 'git worktree remove' fails, rerun this doctor to inspect the state."
        else
            echo "   ➜ No worktree directory or metadata found for this branch/path."
        fi
    fi
}

usage() {
    cat <<'EOF'
Usage:
  git_worktree_merge.sh <feature_branch> [base_branch] [--remote <name>] [-d|--delete|--delete-only] [--cleanup] [--delete-remote] [--worktree-path <path>]
  git_worktree_merge.sh --doctor [<feature_branch>]

Arguments:
  <feature_branch>       Required. The feature branch to merge.
  [base_branch]          Optional. The target branch. Defaults to main.

Options:
  --remote <name>       Remote name to pull/push. Default: zata.
  -d, --delete, --delete-only
                        Skip merge/push and only run cleanup for the feature branch.
  -D, --force-delete     Force delete: skip merge/push, force-remove worktree and force-delete
                        local branch (bypasses dirty/unmerged checks).
  --cleanup              Remove worktree and delete local feature branch after merge succeeds.
  --delete-remote        Delete <remote>/<feature_branch> (works with --cleanup/-d/--delete/--delete-only).
  --worktree-path <path> Explicit worktree path to remove during cleanup.
                         Default: auto-detect by <feature_branch>, fallback parent_of_repo_root/<feature_branch>
  --doctor               Doctor / cleanup-check mode. Without arguments, scans all registered worktrees
                         for missing directories. With <feature_branch>, inspects the state of the
                         expected worktree path and its metadata under .git/worktrees.
  -h, --help             Show this help message.

Checks before merge:
  - Current directory must be in a Git repository.
  - Relevant feature/base worktrees must be clean.
  - Local <feature_branch> and [base_branch] must exist.
  - If [base_branch] is already checked out in another worktree, that worktree is reused.

Examples:
  ./scripts/worktree/merge.sh feature-login
  ./scripts/worktree/merge.sh feature-login main --cleanup
  ./scripts/worktree/merge.sh feature-login -d
  ./scripts/worktree/merge.sh feature-login --delete
  ./scripts/worktree/merge.sh feature-login main --remote zata --cleanup
  ./scripts/worktree/merge.sh feature-login main --cleanup --delete-remote
  ./scripts/worktree/merge.sh --doctor
  ./scripts/worktree/merge.sh --doctor feature-login
EOF
}

if [[ $# -ge 1 && ( "$1" == "-h" || "$1" == "--help" ) ]]; then
    usage
    exit 0
fi

if [[ $# -ge 1 && ( "$1" == "--doctor" || "$1" == "--cleanup-check" ) ]]; then
    shift
    run_worktree_doctor "${1:-}"
    exit 0
fi

if [[ $# -lt 1 ]]; then
    usage
    exit 1
fi

feature_branch="$1"
shift
base_branch="main"
remote_name="zata"
delete_only_mode="false"
force_delete_mode="false"
cleanup_mode="false"
delete_remote_branch="false"
worktree_path=""

if [[ $# -gt 0 && "$1" != -* ]]; then
    base_branch="$1"
    shift
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cleanup)
            cleanup_mode="true"
            ;;
        -d|--delete|--delete-only)
            delete_only_mode="true"
            cleanup_mode="true"
            ;;
        -D|--force-delete)
            force_delete_mode="true"
            delete_only_mode="true"
            cleanup_mode="true"
            ;;
        --remote)
            if [[ $# -lt 2 ]]; then
                echo "❌ --remote requires a remote name."
                exit 1
            fi
            remote_name="$2"
            shift
            ;;
        --delete-remote)
            delete_remote_branch="true"
            ;;
        --worktree-path)
            if [[ $# -lt 2 ]]; then
                echo "❌ --worktree-path requires a path value."
                exit 1
            fi
            worktree_path="$2"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "❌ Unknown option: $1"
            usage
            exit 1
            ;;
    esac
    shift
done

if [[ "$feature_branch" == "$base_branch" ]]; then
    echo "❌ feature_branch and base_branch cannot be the same: $feature_branch"
    exit 1
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "❌ Current directory is not inside a Git repository."
    exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

preflight_check_worktree_permissions() {
    # Scan a worktree directory for potential permission problems before attempting removal.
    local target_worktree_path="$1"

    if [[ -z "$target_worktree_path" || ! -d "$target_worktree_path" ]]; then
        return 0
    fi

    local current_user_name=""
    current_user_name="$(id -un 2>/dev/null || whoami)"

    echo "🔎 Preflight: scanning worktree for files not owned by '$current_user_name' or not user-writable:"
    echo "   $target_worktree_path"

    # First, check if there is at least one suspicious path.
    if find "$target_worktree_path" \( ! -user "$current_user_name" -o ! -perm -u+w \) -print -quit 2>/dev/null | grep -q .; then
        echo "⚠️ Found paths that may cause 'Permission denied' during removal:"
        # Re-run to list all problematic paths, indented for readability.
        find "$target_worktree_path" \( ! -user "$current_user_name" -o ! -perm -u+w \) -print 2>/dev/null | sed 's/^/    /'
        echo "   Consider fixing ownership/permissions (chown/chmod) before retrying removal."
    else
        echo "✅ No obvious ownership/permission issues detected in worktree."
    fi
}

diagnose_failed_worktree_remove() {
    # After a failed 'git worktree remove', check .git/worktrees metadata vs the on-disk directory.
    local target_worktree_path="$1"

    local git_common_dir=""
    git_common_dir="$(resolve_git_common_dir)"

    local worktrees_dir="$git_common_dir/worktrees"
    local dir_exists="false"

    if [[ -d "$target_worktree_path" ]]; then
        dir_exists="true"
    fi

    local has_metadata="false"
    local metadata_dir_for_branch=""

    if [[ -d "$worktrees_dir" ]]; then
        local entry_path=""
        for metadata_dir_for_branch in "$worktrees_dir"/*; do
            [[ -d "$metadata_dir_for_branch" ]] || continue
            if [[ -f "$metadata_dir_for_branch/gitdir" ]]; then
                entry_path="$(dirname "$(cat "$metadata_dir_for_branch/gitdir" 2>/dev/null || echo "")")"
                if [[ "$entry_path" == "$target_worktree_path" ]]; then
                    has_metadata="true"
                    break
                fi
            fi
        done
    fi

    echo "🔍 Post-failure diagnostics for worktree: $target_worktree_path"

    if [[ "$dir_exists" == "true" && "$has_metadata" == "false" ]]; then
        echo "   ➜ It looks like .git/worktrees metadata has already been removed, but the directory remains."
        echo "     You can remove the residual directory manually if you're sure it's safe:"
        echo "       rm -rf \"$target_worktree_path\""
    elif [[ "$dir_exists" == "false" && "$has_metadata" == "true" ]]; then
        echo "   ➜ Metadata exists under '.git/worktrees', but the worktree directory is gone."
        echo "     You can clean up metadata-only entries using:"
        echo "       git worktree prune"
    elif [[ "$dir_exists" == "true" && "$has_metadata" == "true" ]]; then
        echo "   ➜ Both the worktree directory and metadata still exist."
        echo "     A typical next step is to fix filesystem permissions in:"
        echo "       $target_worktree_path"
        echo "     and then retry:"
        echo "       git worktree remove \"$target_worktree_path\""
    else
        echo "   ➜ No worktree directory or metadata found for this path."
        echo "     Nothing to clean up, but the previous failure may have been unrelated to worktree state."
    fi
}

ensure_worktree_clean() {
    local target_worktree_path="$1"
    local worktree_label="$2"
    local status_output=""

    if [[ -z "$target_worktree_path" || ! -d "$target_worktree_path" ]]; then
        echo "❌ Worktree path does not exist for $worktree_label:"
        echo "   $target_worktree_path"
        exit 1
    fi

    status_output="$(git -C "$target_worktree_path" status --porcelain)"
    if [[ -n "$status_output" ]]; then
        echo "❌ $worktree_label working tree is not clean:"
        echo "   $target_worktree_path"
        printf '%s\n' "$status_output" | sed 's/^/   /'
        exit 1
    fi
}

enter_base_worktree_for_merge() {
    local target_base_worktree_path="$1"
    local current_checked_out_branch=""

    current_checked_out_branch="$(git symbolic-ref --short -q HEAD || true)"
    if [[ "$current_checked_out_branch" == "$base_branch" ]]; then
        echo "🚀 Updating $base_branch from $remote_name..."
        return 0
    fi

    if [[ -n "$target_base_worktree_path" ]]; then
        if [[ ! -d "$target_base_worktree_path" ]]; then
            echo "❌ Base branch worktree metadata exists, but the directory is missing:"
            echo "   $target_base_worktree_path"
            echo "   Try: git worktree prune"
            exit 1
        fi
        echo "🚀 Using existing $base_branch worktree and updating from $remote_name:"
        echo "   $target_base_worktree_path"
        cd "$target_base_worktree_path"
    else
        echo "🚀 Switching to $base_branch and updating from $remote_name..."
        git checkout "$base_branch"
    fi

    current_checked_out_branch="$(git symbolic-ref --short -q HEAD || true)"
    if [[ "$current_checked_out_branch" != "$base_branch" ]]; then
        echo "❌ Expected to be on '$base_branch', but current branch is '${current_checked_out_branch:-detached HEAD}'."
        exit 1
    fi
}

leave_feature_worktree_for_cleanup() {
    local current_checked_out_branch=""
    local existing_base_worktree_path=""

    current_checked_out_branch="$(git symbolic-ref --short -q HEAD || true)"
    if [[ "$current_checked_out_branch" != "$feature_branch" ]]; then
        return 0
    fi

    existing_base_worktree_path="$(resolve_worktree_path_by_branch "$base_branch")"
    if [[ -n "$existing_base_worktree_path" ]]; then
        if [[ ! -d "$existing_base_worktree_path" ]]; then
            echo "❌ Base branch worktree metadata exists, but the directory is missing:"
            echo "   $existing_base_worktree_path"
            echo "   Try: git worktree prune"
            exit 1
        fi
        echo "↪ Leaving current feature worktree via existing $base_branch worktree:"
        echo "   $existing_base_worktree_path"
        cd "$existing_base_worktree_path"
        return 0
    fi

    if git show-ref --verify --quiet "refs/heads/$base_branch"; then
        git checkout "$base_branch"
    else
        echo "❌ Cannot cleanup checked-out branch '$feature_branch' because base branch '$base_branch' does not exist."
        exit 1
    fi
}

cleanup_feature_branch() {
    local resolved_cleanup_worktree_path="$worktree_path"
    local cleanup_worktree_branch=""

    if [[ -z "$resolved_cleanup_worktree_path" ]]; then
        resolved_cleanup_worktree_path="$(resolve_worktree_path_by_branch "$feature_branch")"
        if [[ -z "$resolved_cleanup_worktree_path" ]]; then
            resolved_cleanup_worktree_path="$(dirname "$repo_root")/$feature_branch"
        fi
    fi

    leave_feature_worktree_for_cleanup

    echo "🧹 Cleanup enabled."
    local branch_delete_flag="-d"
    if [[ "$force_delete_mode" == "true" ]]; then
        branch_delete_flag="-D"
        echo "💪 Force mode enabled."
    fi

    if git worktree list --porcelain | grep -Fq "worktree $resolved_cleanup_worktree_path"; then
        if [[ ! -d "$resolved_cleanup_worktree_path" ]]; then
            echo "❌ Registered worktree path is missing on disk:"
            echo "   $resolved_cleanup_worktree_path"
            diagnose_failed_worktree_remove "$resolved_cleanup_worktree_path"
            return 1
        fi
        cleanup_worktree_branch="$(git -C "$resolved_cleanup_worktree_path" symbolic-ref --short -q HEAD || true)"
        if [[ "$cleanup_worktree_branch" != "$feature_branch" ]]; then
            echo "⚠️ Worktree path is not checked out on '$feature_branch', skipped:"
            echo "   $resolved_cleanup_worktree_path"
        else
            if [[ "$force_delete_mode" != "true" ]]; then
                preflight_check_worktree_permissions "$resolved_cleanup_worktree_path"
            fi
            if [[ "$force_delete_mode" == "true" ]]; then
                if ! git worktree remove --force "$resolved_cleanup_worktree_path"; then
                    echo "❌ git worktree remove failed for: $resolved_cleanup_worktree_path"
                    diagnose_failed_worktree_remove "$resolved_cleanup_worktree_path"
                    echo "❌ Aborting cleanup because worktree removal failed."
                    return 1
                fi
            else
                if ! git worktree remove "$resolved_cleanup_worktree_path"; then
                    echo "❌ git worktree remove failed for: $resolved_cleanup_worktree_path"
                    diagnose_failed_worktree_remove "$resolved_cleanup_worktree_path"
                    echo "❌ Aborting cleanup because worktree removal failed."
                    return 1
                fi
            fi
            echo "✅ Removed worktree: $resolved_cleanup_worktree_path"
        fi
    else
        echo "⚠️ Worktree not found, skipped: $resolved_cleanup_worktree_path"
    fi

    if git show-ref --verify --quiet "refs/heads/$feature_branch"; then
        git branch "$branch_delete_flag" "$feature_branch"
        echo "✅ Deleted local branch: $feature_branch"
    else
        echo "⚠️ Local branch not found, skipped: $feature_branch"
    fi

    if [[ "$delete_remote_branch" == "true" ]]; then
        if ! git remote get-url "$remote_name" >/dev/null 2>&1; then
            echo "❌ Remote not found: $remote_name"
            exit 1
        fi

        if git push "$remote_name" --delete "$feature_branch"; then
            echo "✅ Deleted remote branch: $feature_branch"
        else
            echo "⚠️ Remote branch delete failed or branch not found on remote: $feature_branch"
        fi
    fi
}

if [[ "$delete_only_mode" == "true" ]]; then
    echo "🗑️ Delete-only mode enabled. Skipping merge and push."
    cleanup_feature_branch
    echo "✅ Delete-only flow completed successfully."
    exit 0
fi

if ! git show-ref --verify --quiet "refs/heads/$feature_branch"; then
    echo "❌ Local feature branch not found: $feature_branch"
    exit 1
fi

if ! git show-ref --verify --quiet "refs/heads/$base_branch"; then
    echo "❌ Local base branch not found: $base_branch"
    exit 1
fi

if ! git remote get-url "$remote_name" >/dev/null 2>&1; then
    echo "❌ Remote not found: $remote_name"
    exit 1
fi

feature_worktree_path="$(resolve_worktree_path_by_branch "$feature_branch")"
base_worktree_path="$(resolve_worktree_path_by_branch "$base_branch")"
current_checked_out_branch="$(git symbolic-ref --short -q HEAD || true)"

if [[ -n "$feature_worktree_path" ]]; then
    ensure_worktree_clean "$feature_worktree_path" "Feature branch '$feature_branch'"
fi

if [[ -n "$base_worktree_path" ]]; then
    ensure_worktree_clean "$base_worktree_path" "Base branch '$base_branch'"
elif [[ "$current_checked_out_branch" != "$feature_branch" && "$current_checked_out_branch" != "$base_branch" ]]; then
    ensure_worktree_clean "$repo_root" "Current worktree"
fi

enter_base_worktree_for_merge "$base_worktree_path"
ensure_worktree_clean "$(pwd)" "Base branch '$base_branch'"
git pull --ff-only "$remote_name" "$base_branch"

echo "🔀 Merging $feature_branch into $base_branch..."
git merge "$feature_branch"

echo "📤 Pushing $base_branch to $remote_name..."
git push "$remote_name" "$base_branch"

if [[ "$cleanup_mode" == "true" ]]; then
    cleanup_feature_branch
fi

echo "✅ Merge flow completed successfully."
