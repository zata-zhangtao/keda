#!/usr/bin/env bash

# Bash completion extension for just worktree recipes.
# Supports branch name completion for:
#   just worktree-delete <feature_branch>
#   just worktree-merge <feature_branch> [base_branch] [flags]

_just_worktree_branch_candidates() {
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        return 0
    fi

    git for-each-ref --format='%(refname:short)' refs/heads 2>/dev/null
}

_just_worktree_remote_candidates() {
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        return 0
    fi

    git remote 2>/dev/null
}

_just_worktree_completion() {
    local cur prev recipe_name
    local branch_candidates option_candidates remote_candidates

    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    recipe_name="${COMP_WORDS[1]:-}"
    branch_candidates="$(_just_worktree_branch_candidates)"
    option_candidates="-d --delete --delete-only --cleanup --delete-remote --remote --worktree-path"
    remote_candidates="$(_just_worktree_remote_candidates)"

    case "$recipe_name" in
        worktree-delete)
            if [[ "$COMP_CWORD" -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "$branch_candidates" -- "$cur") )
                return 0
            fi
            ;;
        worktree-merge)
            if [[ "$prev" == "--remote" ]]; then
                COMPREPLY=( $(compgen -W "$remote_candidates" -- "$cur") )
                return 0
            fi

            if [[ "$prev" == "--worktree-path" ]]; then
                COMPREPLY=( $(compgen -d -- "$cur") )
                return 0
            fi

            if [[ "$COMP_CWORD" -eq 2 ]]; then
                COMPREPLY=( $(compgen -W "$branch_candidates" -- "$cur") )
                return 0
            fi

            if [[ "$COMP_CWORD" -eq 3 && "${COMP_WORDS[2]}" != -* ]]; then
                COMPREPLY=( $(compgen -W "$branch_candidates" -- "$cur") )
                return 0
            fi

            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "$option_candidates" -- "$cur") )
                return 0
            fi
            ;;
    esac

    if declare -F _just >/dev/null 2>&1; then
        _just "$@"
        return $?
    fi

    return 0
}

if ! declare -F _just >/dev/null 2>&1; then
    eval "$(just --completions bash)"
fi

complete -F _just_worktree_completion -o bashdefault -o default just
