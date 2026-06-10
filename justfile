# Default recipe (runs when you type 'just')
default: _check-completion
    @just --list

# Internal: check if just shell completion is installed
_check-completion:
    #!/usr/bin/env bash
    set -euo pipefail
    # Skip in CI or non-interactive environments
    if [ -n "${CI:-}" ] || [ -z "${TERM:-}" ] || [ "${TERM}" = "dumb" ]; then
        exit 0
    fi

    shell_name="$(basename "${SHELL:-}")"
    completion_installed="false"

    case "$shell_name" in
        zsh)
            [ -f "$HOME/.zsh/completions/_just" ] && completion_installed="true"
            ;;
        bash)
            [ -f "$HOME/.config/just/just_completion.bash" ] && completion_installed="true"
            ;;
    esac

    if [ "$completion_installed" = "false" ]; then
        echo ""
        printf '\033[1;33m+------------------------------------------------------------+\033[0m\n'
        printf '\033[1;33m|  WARNING: Just shell completion is NOT installed           |\033[0m\n'
        printf '\033[1;33m+------------------------------------------------------------+\033[0m\n'
        printf '\033[1;33m|  Tab-completion for just commands is not available.        |\033[0m\n'
        printf '\033[1;33m|  Run the following command to install it:                  |\033[0m\n'
        printf '\033[1;33m|                                                            |\033[0m\n'
        printf '\033[1;32m|       just sync all                                        |\033[0m\n'
        printf '\033[1;33m|                                                            |\033[0m\n'
        printf '\033[1;33m+------------------------------------------------------------+\033[0m\n'
        echo ""
    fi

# NOTE:
#   Run `just sync all` once on a local machine to install shell completion
#   for `just` in the current shell profile.

# Sync dependencies
# Usage:
#   just sync           # dev deps (default)
#   just sync prod      # production only, no dev
#   just sync all       # all extras + install just shell completion
#   just sync dev       # all extras + pre-commit hooks
sync mode="": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{mode}}" in
        prod)
            uv sync --no-dev
            ;;
        all)
            uv sync --all-extras
            if [ -z "${CI:-}" ]; then
                shell_name="$(basename "${SHELL:-}")"
                case "$shell_name" in
                    zsh)
                        completion_dir="$HOME/.zsh/completions"
                        completion_dst="$completion_dir/_just"
                        mkdir -p "$completion_dir"
                        just --completions zsh > "$completion_dst"
                        fpath_line="fpath=(\"$completion_dir\" \$fpath)"
                        autoload_line="autoload -Uz compinit && compinit"
                        if ! grep -Fqx "$fpath_line" "$HOME/.zshrc" 2>/dev/null; then
                            printf '\n%s\n' "$fpath_line" >> "$HOME/.zshrc"
                        fi
                        if ! grep -Fqx "$autoload_line" "$HOME/.zshrc" 2>/dev/null; then
                            printf '%s\n' "$autoload_line" >> "$HOME/.zshrc"
                        fi
                        echo "Installed zsh completion: $completion_dst"
                        echo "Reload your shell with: source ~/.zshrc"
                        echo "Or open a new terminal session to activate just completions."
                        ;;
                    bash)
                        completion_dir="$HOME/.config/just"
                        completion_dst="$completion_dir/just_completion.bash"
                        source_line="[ -f \"$completion_dst\" ] && source \"$completion_dst\""
                        mkdir -p "$completion_dir"
                        just --completions bash > "$completion_dst"
                        if ! grep -Fqx "$source_line" "$HOME/.bashrc" 2>/dev/null; then
                            printf '\n%s\n' "$source_line" >> "$HOME/.bashrc"
                        fi
                        echo "Installed bash completion: $completion_dst"
                        echo "Reload your shell with: source ~/.bashrc"
                        echo "Or open a new terminal session to activate just completions."
                        ;;
                    *)
                        echo "Skipped shell completion install for unsupported shell: $shell_name"
                        ;;
                esac
            fi
            ;;
        dev)
            uv sync --all-extras
            uv run pre-commit install
            ;;
        "")
            uv sync
            ;;
        *)
            echo "❌ Unknown mode: {{mode}}"
            echo "Usage: just sync [prod|all|dev]"
            exit 1
            ;;
    esac

# Reinstall the `iar` CLI tool globally via uv in editable mode
# Usage:
#   just reinstall-iar
reinstall-iar:
    uv tool install --reinstall --editable .

# Run the development entrypoint
# Usage:
#   just run                 # start backend + frontend
#   just run backend         # start backend only
#   just run frontend        # start frontend only
#   just run docker          # start with Docker Compose (one-click deploy)
#   just run all frontend_dir=web frontend_cmd="pnpm dev"
run target="all" frontend_dir="frontend" backend_cmd="uv run python -m backend.main" frontend_cmd="npm run dev": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail

    target="{{target}}"
    frontend_dir="{{frontend_dir}}"
    backend_cmd='{{backend_cmd}}'
    frontend_cmd='{{frontend_cmd}}'
    backend_pid=""
    frontend_pid=""

    run_backend() {
        echo "Starting backend: $backend_cmd"
        bash -lc "$backend_cmd"
    }

    run_frontend() {
        if [ ! -d "$frontend_dir" ]; then
            echo "❌ Frontend directory not found: $frontend_dir"
            echo "   Override it with: just run frontend frontend_dir=<path>"
            exit 1
        fi

        if [ ! -f "$frontend_dir/package.json" ]; then
            echo "❌ package.json not found in frontend directory: $frontend_dir"
            echo "   Override the directory or command, for example:"
            echo "   just run frontend frontend_dir=<path> frontend_cmd='pnpm dev'"
            exit 1
        fi

        echo "Starting frontend in $frontend_dir: $frontend_cmd"
        (
            cd "$frontend_dir"
            bash -lc "$frontend_cmd"
        )
    }

    cleanup_processes() {
        for process_pid in "$backend_pid" "$frontend_pid"; do
            if [ -n "$process_pid" ] && kill -0 "$process_pid" 2>/dev/null; then
                kill "$process_pid" 2>/dev/null || true
            fi
        done
        wait 2>/dev/null || true
    }

    wait_for_first_exit() {
        while true; do
            if [ -n "$backend_pid" ] && ! kill -0 "$backend_pid" 2>/dev/null; then
                wait "$backend_pid"
                return $?
            fi

            if [ -n "$frontend_pid" ] && ! kill -0 "$frontend_pid" 2>/dev/null; then
                wait "$frontend_pid"
                return $?
            fi

            sleep 1
        done
    }

    case "$target" in
        backend)
            run_backend
            ;;
        frontend)
            run_frontend
            ;;
        all)
            trap cleanup_processes EXIT INT TERM
            run_backend &
            backend_pid=$!
            run_frontend &
            frontend_pid=$!
            wait_for_first_exit
            ;;
        docker)
            echo "Starting services with Docker Compose..."
            has_remote="false"
            if [ -f ".env" ]; then
                db_url=$(grep "^DATABASE_URL=" .env | head -1 | cut -d'=' -f2- | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/^"//;s/"$//;s/^'"'"'//;s/'"'"'$//')
                if [ -n "$db_url" ]; then
                    case "$db_url" in
                        *@db:*|*@localhost*|*@127.0.0.1*)
                            has_remote="false"
                            ;;
                        *)
                            has_remote="true"
                            ;;
                    esac
                fi
            fi
            if [ "$has_remote" = "true" ]; then
                echo "Detected remote DATABASE_URL; backend will connect directly to remote database"
            else
                echo "Using local PostgreSQL database"
            fi
            docker compose up --build
            ;;
        *)
            echo "❌ Unknown run target: $target"
            echo "Usage: just run [backend|frontend|all|docker]"
            exit 1
            ;;
    esac

# Run lint checks via pre-commit.
# Usage:
#   just lint          # commit-aligned staged-files pre-commit
#   just lint --reuse  # duplicate/reuse/architecture diagnostics
#   just lint --full   # all-files pre-commit gate
#   just lint --repo   # full local repository gate
lint mode="": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail

    source ./scripts/hooks/quality_flag.sh

    print_lint_usage() {
        echo "Usage:"
        echo "  just lint          # commit-aligned staged-files pre-commit"
        echo "  just lint --reuse  # duplicate/reuse/architecture diagnostics"
        echo "  just lint --full   # all-files pre-commit gate"
        echo "  just lint --repo   # full local repository gate"
    }

    run_full_lint() {
        git_dir="$(quality_git_dir)"
        flag_file="$git_dir/.last_linted_commit"
        branch_name="$(quality_branch_name)"
        head_hash="$(quality_head_hash)"
        current_tree="$(quality_effective_tree working lint)"
        staged_archive_prd_transition=0
        if quality_has_staged_archive_prd_transition; then
            staged_archive_prd_transition=1
        fi

        if quality_skip_is_empty_or_only "check-test-flag" && [ "$staged_archive_prd_transition" -eq 0 ] && quality_flag_matches "$flag_file" "$branch_name" "$head_hash" "$current_tree"; then
            if ! quality_skip_contains "check-test-flag"; then
                bash ./scripts/hooks/check_test_flag.sh
            fi
            echo "✅ just lint --full flag valid: $branch_name @ ${head_hash:0:8}"
            return 0
        fi

        if [ "$staged_archive_prd_transition" -eq 1 ]; then
            echo "🔍 staged archive PRD detected; running full lint checks."
        fi

        uv run pre-commit run --all-files --show-diff-on-failure

        if quality_skip_is_empty_or_only "check-test-flag"; then
            current_tree="$(quality_effective_tree working lint)"
            quality_write_flag "$flag_file" "$branch_name" "$head_hash" "$current_tree"
            echo "✅ just lint --full flag updated: $branch_name @ ${head_hash:0:8}"
        fi
    }

    run_reuse_lint() {
        reuse_status=0
        manual_reuse_hooks=(
            jscpd
            pylint-duplicate-code
        )
        regular_reuse_hooks=(
            check-architecture
            check-guidelines-consistency
            check-max-file-lines
        )

        for hook_id in "${manual_reuse_hooks[@]}"; do
            echo "🔍 Running reuse hook: $hook_id"
            if ! uv run pre-commit run "$hook_id" --hook-stage manual --all-files --show-diff-on-failure; then
                reuse_status=1
            fi
        done

        for hook_id in "${regular_reuse_hooks[@]}"; do
            echo "🔍 Running reuse hook: $hook_id"
            if ! uv run pre-commit run "$hook_id" --all-files --show-diff-on-failure; then
                reuse_status=1
            fi
        done

        return "$reuse_status"
    }

    case "{{mode}}" in
        "")
            uv run pre-commit run --show-diff-on-failure
            ;;
        --full)
            run_full_lint
            ;;
        --reuse)
            run_reuse_lint
            ;;
        --repo)
            SKIP=check-test-flag just lint --full
            just lint --reuse
            just test
            uv run mkdocs build --strict
            just lint --full
            ;;
        *)
            echo "❌ Unknown lint mode: {{mode}}"
            print_lint_usage
            exit 1
            ;;
    esac

# Serve MkDocs site locally with live reload (configurable port, default 8000)
docs-serve port="8000": _check-completion
    WATCHDOG_USE_POLLING=1 uv run mkdocs serve -a 127.0.0.1:{{port}}

# Remove cache files and build artifacts
clean:
    @echo "Cleaning cache files..."
    @rm -rf .ruff_cache
    @rm -rf __pycache__
    @find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    @find . -type f -name "*.pyc" -delete 2>/dev/null || true
    @find . -type f -name "*.pyo" -delete 2>/dev/null || true
    @find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
    @echo "Clean complete!"

# Build a release zip (does NOT modify local workspace)
release:
    uv run python scripts/release.py

# Check the current terminal network environment for Claude access.
check-net:
    ./scripts/diagnostics/check_claude_code.sh

# Install or test macOS Shortcut notifications for Codex CLI.
# Usage:
#   just codex-notify install
#   just codex-notify install codex通知
#   just codex-notify test
codex-notify action="install" shortcut_name="codex通知":
    #!/usr/bin/env bash
    set -euo pipefail
    case "{{action}}" in
        install)
            ./scripts/codex/install_macos_notify.sh "{{shortcut_name}}"
            ;;
        test)
            CODEX_NOTIFY_VERBOSE=1 CODEX_NOTIFY_SHORTCUT_NAME="{{shortcut_name}}" ./scripts/codex/notify_shortcut.sh '{"type":"agent-turn-complete","last-assistant-message":"Codex notify manual test"}'
            ;;
        *)
            echo "❌ Unknown action: {{action}}"
            echo "Usage: just codex-notify [install|test] [shortcut_name]"
            exit 1
            ;;
    esac

staged_changes:
    git diff --cached > staged_changes.diff

# Git worktree helper (implemented in scripts/worktree/)
# Usage:
#   just worktree <branch>                            # create/enter worktree (auto-checkout matching remote branch)
#   just worktree <branch> --checkout [<src>]         # reuse existing local/remote branch
#   just worktree <branch> --new                      # force new branch (ignore same-name remote)
#   just worktree <branch> --cmd trae                 # open in editor
#   just worktree <branch> enter_shell=false          # no shell
#   just worktree -o <branch> [--cmd trae]            # open existing worktree
#   just worktree -d <branch>                         # delete worktree
#   just worktree -m [<feature>] [base=main] [flags]  # merge worktree (current branch if omitted)
#   just worktree --doctor [branch]                   # doctor / cleanup-check
worktree arg1 arg2="" arg3="" arg4="" arg5="":
    #!/usr/bin/env bash
    set -euo pipefail

    # -o: open existing worktree
    if [ "{{arg1}}" = "-o" ]; then
        if [ -z "{{arg2}}" ]; then
            echo "❌ Usage: just worktree -o <branch_name> [--cmd editor]"
            exit 1
        fi
        open_cmd=(./scripts/worktree/open.sh "{{arg2}}")
        for raw_arg in "{{arg3}}" "{{arg4}}" "{{arg5}}"; do
            if [ -n "$raw_arg" ]; then
                open_cmd+=("$raw_arg")
            fi
        done
        "${open_cmd[@]}"
        exit 0
    fi

    # -d: delete worktree
    if [ "{{arg1}}" = "-d" ]; then
        if [ -z "{{arg2}}" ]; then
            echo "❌ Usage: just worktree -d <branch_name>"
            exit 1
        fi
        ./scripts/worktree/merge.sh "{{arg2}}" -d
        exit 0
    fi

    # -D: force delete worktree (bypass dirty/unmerged checks)
    if [ "{{arg1}}" = "-D" ]; then
        if [ -z "{{arg2}}" ]; then
            echo "❌ Usage: just worktree -D <branch_name>"
            exit 1
        fi
        ./scripts/worktree/merge.sh "{{arg2}}" -D
        exit 0
    fi

    # -m: merge worktree
    if [ "{{arg1}}" = "-m" ]; then
        raw_arg2="{{arg2}}"
        declare -a merge_args=()

        if [ -z "$raw_arg2" ] || [[ "$raw_arg2" == -* ]]; then
            current_branch="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
            if [ -z "$current_branch" ]; then
                echo "❌ Could not determine current branch."
                echo "Usage: just worktree -m [<feature_branch>] [base_branch=main] [flags]"
                exit 1
            fi
            merge_args+=("$current_branch")
            if [ -n "$raw_arg2" ]; then
                merge_args+=("$raw_arg2")
            fi
            for raw_arg in "{{arg3}}" "{{arg4}}" "{{arg5}}"; do
                if [ -n "$raw_arg" ]; then
                    merge_args+=("$raw_arg")
                fi
            done
        else
            merge_args+=("$raw_arg2")
            for raw_arg in "{{arg3}}" "{{arg4}}" "{{arg5}}"; do
                if [ -n "$raw_arg" ]; then
                    merge_args+=("$raw_arg")
                fi
            done
        fi

        ./scripts/worktree/merge.sh "${merge_args[@]}"
        exit 0
    fi

    # --doctor: cleanup-check
    if [ "{{arg1}}" = "--doctor" ]; then
        if [ -n "{{arg2}}" ]; then
            ./scripts/worktree/merge.sh --doctor "{{arg2}}"
        else
            ./scripts/worktree/merge.sh --doctor
        fi
        exit 0
    fi

    # Default: create/enter worktree
    branch_name="{{arg1}}"
    worktree_command=(./scripts/worktree/create.sh "$branch_name")
    enter_shell_value="true"
    expect_code_command="false"
    expect_base_branch="false"
    expect_checkout_source="false"

    known_flag_pattern='--cmd|--cmd=*|--base|--base=*|--checkout|--checkout=*|--new|enter_shell=true|enter_shell=false'

    for raw_arg in "{{arg2}}" "{{arg3}}" "{{arg4}}" "{{arg5}}"; do
        if [ -z "$raw_arg" ]; then
            continue
        fi

        if [ "$expect_code_command" = "true" ]; then
            case "$raw_arg" in
                --cmd|--cmd=*|--base|--base=*|--checkout|--checkout=*|--new|enter_shell=true|enter_shell=false)
                    expect_code_command="false"
                    ;;
                *)
                    worktree_command+=("$raw_arg")
                    expect_code_command="false"
                    continue
                    ;;
            esac
        fi

        if [ "$expect_base_branch" = "true" ]; then
            case "$raw_arg" in
                --cmd|--cmd=*|--base|--base=*|--checkout|--checkout=*|--new|enter_shell=true|enter_shell=false)
                    expect_base_branch="false"
                    ;;
                *)
                    worktree_command+=("$raw_arg")
                    expect_base_branch="false"
                    continue
                    ;;
            esac
        fi

        if [ "$expect_checkout_source" = "true" ]; then
            case "$raw_arg" in
                --cmd|--cmd=*|--base|--base=*|--checkout|--checkout=*|--new|enter_shell=true|enter_shell=false)
                    expect_checkout_source="false"
                    ;;
                *)
                    worktree_command+=("$raw_arg")
                    expect_checkout_source="false"
                    continue
                    ;;
            esac
        fi

        case "$raw_arg" in
            --base)
                worktree_command+=(--base)
                expect_base_branch="true"
                ;;
            --base=*)
                worktree_command+=("$raw_arg")
                ;;
            --cmd)
                worktree_command+=(--cmd)
                expect_code_command="true"
                ;;
            --cmd=*)
                worktree_command+=("$raw_arg")
                ;;
            --checkout)
                worktree_command+=(--checkout)
                expect_checkout_source="true"
                ;;
            --checkout=*)
                worktree_command+=("$raw_arg")
                ;;
            --new)
                worktree_command+=(--new)
                ;;
            enter_shell=true)
                enter_shell_value="true"
                ;;
            enter_shell=false)
                enter_shell_value="false"
                ;;
            *)
                echo "❌ Invalid argument: $raw_arg"
                echo "Usage:"
                echo "  just worktree <branch> [--checkout [<src>] | --new] [--base branch] [--cmd [editor]] [enter_shell=false]"
                echo "  just worktree -o <branch> [--cmd editor]"
                echo "  just worktree -d <branch>"
                echo "  just worktree -m [<feature>] [base=main] [flags]"
                echo "  just worktree --doctor [branch]"
                exit 1
                ;;
        esac
    done

    "${worktree_command[@]}"

    if [ "$enter_shell_value" = "true" ]; then
        repo_root="$(git rev-parse --show-toplevel)"
        repo_parent="$(dirname "$repo_root")"
        repo_name="$(basename "$repo_root")"
        worktrees_base="$repo_parent/${repo_name}-worktrees"
        if [[ "$branch_name" == issue-* ]]; then
            target_worktree_path="$worktrees_base/tasks/$branch_name"
        else
            target_worktree_path="$worktrees_base/$branch_name"
        fi
        echo "Entering worktree shell: $target_worktree_path"
        echo "Run 'exit' to return to previous shell."
        cd "$target_worktree_path"
        if [ -n "${TERM:-}" ] && [ "${TERM}" != "dumb" ]; then
            printf '\033]0;%s\007' "wt:$branch_name"
        fi

        user_shell="$(basename "${SHELL:-bash}")"
        if [ "$user_shell" = "zsh" ]; then
            zdotdir="$(mktemp -d)"
            printf '%s\n' \
                'if [ -f "$HOME/.zshrc" ]; then' \
                '    source "$HOME/.zshrc"' \
                'fi' \
                'if [ -n "${WORKTREE_BRANCH_NAME:-}" ]; then' \
                '    PS1="(wt:${WORKTREE_BRANCH_NAME}) ${PS1:-%n@%m:%~%# }"' \
                'fi' \
                'if [ -n "${WORKTREE_ZDOTDIR:-}" ] && [ -d "${WORKTREE_ZDOTDIR}" ]; then' \
                '    rm -rf "${WORKTREE_ZDOTDIR}" 2>/dev/null || true' \
                '    unset WORKTREE_ZDOTDIR' \
                'fi' \
                > "$zdotdir/.zshrc"
            exec env WORKTREE_BRANCH_NAME="$branch_name" WORKTREE_ZDOTDIR="$zdotdir" ZDOTDIR="$zdotdir" zsh -i
        else
            worktree_shell_rcfile="$(mktemp)"
            printf '%s\n' \
                'if [ -f "$HOME/.bashrc" ]; then' \
                '    source "$HOME/.bashrc"' \
                'fi' \
                'if [ -n "${WORKTREE_BRANCH_NAME:-}" ]; then' \
                '    PS1="(wt:${WORKTREE_BRANCH_NAME}) ${PS1:-\u@\h:\w\$ }"' \
                'fi' \
                'if [ -n "${WORKTREE_SHELL_RCFILE:-}" ] && [ -f "${WORKTREE_SHELL_RCFILE}" ]; then' \
                '    rm -f "${WORKTREE_SHELL_RCFILE}" 2>/dev/null || true' \
                '    unset WORKTREE_SHELL_RCFILE' \
                'fi' \
                > "$worktree_shell_rcfile"
            exec env WORKTREE_BRANCH_NAME="$branch_name" WORKTREE_SHELL_RCFILE="$worktree_shell_rcfile" bash --rcfile "$worktree_shell_rcfile" -i
        fi
    fi


# Create worktree from a PRD and start AI-assisted implementation.
# Automatically derives branch name from the PRD filename.
# When prompt is omitted, a default prompt is auto-generated from the PRD name.
# Usage:
#   just implement <prd-file> <clauded|kim> ["<prompt>"]
# Examples:
#   just implement tasks/pending/20260518-feature-x.md clauded "请根据 PRD 实现该功能"
#   just implement tasks/pending/feature-x.md kim "请实现这个功能"
#   just implement tasks/pending/feature-x.md clauded  # uses default prompt
implement prd_file ai_tool prompt="":
    #!/usr/bin/env bash
    set -euo pipefail

    prd_file="{{prd_file}}"
    ai_tool="{{ai_tool}}"
    prompt_text="{{prompt}}"

    # Validate PRD file exists
    if [ ! -f "$prd_file" ]; then
        echo "PRD file not found: $prd_file"
        echo "Usage: just implement <prd-file> <clauded|kim> \"<prompt>\""
        exit 1
    fi

    # Validate AI tool
    if [ "$ai_tool" != "clauded" ] && [ "$ai_tool" != "kim" ]; then
        echo "Unsupported AI tool: $ai_tool"
        echo "Supported tools: clauded, kim"
        echo "Usage: just implement <prd-file> <clauded|kim> \"<prompt>\""
        exit 1
    fi

    # Auto-generate default prompt if not provided
    if [ -z "$prompt_text" ]; then
        filename=$(basename "$prd_file")
        prd_name="${filename%.md}"
        # Strip date prefix for display
        if [[ "$prd_name" =~ ^[0-9]{8}-[0-9]{6}-(.+)$ ]]; then
            prd_display="${BASH_REMATCH[1]}"
        elif [[ "$prd_name" =~ ^[0-9]{8}-(.+)$ ]]; then
            prd_display="${BASH_REMATCH[1]}"
        else
            prd_display="$prd_name"
        fi
        prompt_text="请根据 PRD《${prd_display}》的要求实现该功能，完成验收清单"
        echo "ℹ️  No prompt provided, using default: $prompt_text"
        echo ""
    fi

    # Extract branch name from PRD filename
    filename=$(basename "$prd_file")
    branch_name="${filename%.md}"
    # Strip date prefix: YYYYMMDD-HHMMSS- or YYYYMMDD-
    if [[ "$branch_name" =~ ^[0-9]{8}-[0-9]{6}-(.+)$ ]]; then
        branch_name="${BASH_REMATCH[1]}"
    elif [[ "$branch_name" =~ ^[0-9]{8}-(.+)$ ]]; then
        branch_name="${BASH_REMATCH[1]}"
    fi

    echo "PRD: $prd_file"
    echo "Branch: $branch_name"
    echo "Tool: $ai_tool"
    echo "Prompt: $prompt_text"
    echo ""

    # Create worktree under tasks/ subdirectory
    ./scripts/worktree/create.sh "$branch_name" --subdir tasks
    echo ""

    # Determine worktree path
    repo_root="$(git rev-parse --show-toplevel)"
    repo_name="$(basename "$repo_root")"
    worktree_path="$(dirname "$repo_root")/${repo_name}-worktrees/tasks/$branch_name"

    # Copy PRD file into worktree preserving relative path
    prd_abs="$(cd "$(dirname "$prd_file")" && pwd)/$(basename "$prd_file")"
    prd_rel="$(python3 -c "import os.path; print(os.path.relpath('$prd_abs', '$repo_root'))")"
    prd_dest="$worktree_path/$prd_rel"
    mkdir -p "$(dirname "$prd_dest")"
    cp "$prd_abs" "$prd_dest"
    echo "PRD copied to worktree: $prd_rel"
    echo ""

    # Run AI tool in worktree via user's interactive shell (needed for alias resolution)
    echo "Running $ai_tool in worktree..."
    cd "$worktree_path"
    if [ "$ai_tool" = "kim" ]; then
        # kimi uses --prompt flag instead of positional argument
        "${SHELL:-bash}" -i -c "$(printf '%s --prompt %q' "$ai_tool" "$prompt_text")" || true
    else
        # clauded and others accept prompt as positional argument
        "${SHELL:-bash}" -i -c "$(printf '%s %q' "$ai_tool" "$prompt_text")" || true
    fi

    echo ""

    # Open worktree in VS Code (fallback to Code - Insiders)
    if command -v code &>/dev/null; then
        code "$worktree_path"
        echo "🚀 Opened worktree in VS Code: $worktree_path"
    elif command -v code-insiders &>/dev/null; then
        code-insiders "$worktree_path"
        echo "🚀 Opened worktree in VS Code Insiders: $worktree_path"
    fi

    echo "AI tool finished. Entering worktree shell..."
    echo "Run 'exit' to return to previous shell."
    echo ""

    cd "$worktree_path"
    if [ -n "${TERM:-}" ] && [ "${TERM}" != "dumb" ]; then
        printf '\033]0;%s\007' "wt:${branch_name}"
    fi
    exec "${SHELL:-bash}" -i


# Sync files from the upstream template repository.
# Compares local files against the template and offers to apply updates.
# Usage:
#   just sync-template         # skip project-specific paths from config.toml [template_sync]
#   just sync-template --all   # include project-specific files too
sync-template flags="":
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{flags}}" ]; then
        ./scripts/sync_template.sh {{flags}}
    else
        ./scripts/sync_template.sh
    fi

# Run tests after `just lint --full` (usage: just test [all|local|real])
#   just test        - Run local tests (no API keys needed)
#   just test all    - Run all tests
#   just test real   - Run tests requiring API keys
@test type="local": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail

    echo "🔍 Running full lint checks..."
    if ! SKIP=check-test-flag PRE_COMMIT_COLOR=never just lint --full; then
        echo "❌ Lint failed. Fix lint errors before running tests."
        echo "   Run: just lint --full"
        exit 1
    fi
    source ./scripts/hooks/quality_flag.sh
    lint_tree_after_lint="$(quality_effective_tree working lint)"
    echo "✅ Lint passed. Proceeding to tests..."

    # Check Alembic migration heads if Alembic is installed
    if command -v alembic &>/dev/null; then
        alembic_heads="$(uv run alembic heads 2>/dev/null || true)"
        if [ -n "$alembic_heads" ]; then
            alembic_head_count="$(printf "%s\n" "$alembic_heads" | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')"
            if [ "$alembic_head_count" -gt 1 ]; then
                echo "ERROR: Alembic migration graph must have exactly one head; found $alembic_head_count."
                printf "%s\n" "$alembic_heads"
                exit 1
            fi
        fi
    fi

    if [ "{{type}}" = "all" ]; then
        uv run pytest tests/ -v
    elif [ "{{type}}" = "real" ]; then
        uv run pytest tests/ -v -k "expensive or not expensive"
    else
        uv run pytest tests/ -v
    fi

    # 测试通过后写入 flag，绑定分支、HEAD 和有效 tree
    # 有效 tree 只包含可能进入 test/lint 的文件，排除文档、图片等无关类型
    # 这样修改 .md 等文件不会导致代码提交时被要求重测
    git_dir="$(quality_git_dir)"
    branch_name="$(quality_branch_name)"
    head_hash="$(quality_head_hash)"
    test_tree="$(quality_effective_tree working test)"
    quality_write_flag "$git_dir/.last_tested_commit" "$branch_name" "$head_hash" "$test_tree"
    quality_write_flag "$git_dir/.last_linted_commit" "$branch_name" "$head_hash" "$lint_tree_after_lint"
    echo "✅ just test flag updated: $branch_name @ $head_hash"
    echo "✅ just lint --full flag updated: $branch_name @ $head_hash"


# Run Playwright e2e tests (requires: just e2e-install first)
# Usage:
#   just e2e            # all tests (excluding visual regression)
#   just e2e smoke      # smoke tests only
#   just e2e no-auth    # public-page tests (no login required)
#   just e2e report     # open HTML test report
e2e type="":
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/tests/playwright-e2e"
    case "{{type}}" in
        smoke)   npm run test:smoke ;;
        no-auth) npm run test:no-auth ;;
        report)  npm run report ;;
        "")      npm test ;;
        *)
            echo "❌ Unknown type: {{type}}"
            echo "Usage: just e2e [smoke|no-auth|report]"
            exit 1
            ;;
    esac

# Install e2e dependencies and Playwright browsers (run once before first just e2e)
e2e-install:
    cd "{{justfile_directory()}}/tests/playwright-e2e" && npm install && npx playwright install chromium


# ── Frontend ──────────────────────────────────────────────────────────────────

# Frontend helper
# Usage:
#   just frontend dev
#   just frontend build
#   just frontend install
frontend action="dev":
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}/frontend"
    case "{{action}}" in
        dev)
            npm run dev
            ;;
        build)
            npm run build
            ;;
        install)
            npm install
            ;;
        *)
            echo "❌ Unknown action: {{action}}"
            echo "Usage: just frontend [dev|build|install]"
            exit 1
            ;;
    esac

# Pack all gitignored .env* files into a password-protected encrypted zip.
# Output: ./<project_name>_secrets.zip  (one fixed file per project root)
# Password is prompted interactively at compression time; required again to extract.
export-env-encrypted:
    ./scripts/secrets/export_env_encrypted.sh
