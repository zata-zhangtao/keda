# ───────────────────────────────────────────────────────────────────────────────
# justfile — project-specific recipes.
#
# Common recipes (default, sync, lint, test, release, worktree, implement,
# check, ai, codex-notify, sync-template, e2e, e2e-install, docs-serve, clean,
# staged_changes, export-env-encrypted) are imported from `justfile.shared`
# (synced from the upstream template). Keda-specific recipes that have no
# shared equivalent remain here.
# ───────────────────────────────────────────────────────────────────────────────

set allow-duplicate-recipes := true

import "justfile.shared"

# Reinstall the `iar` CLI tool globally via uv in editable mode
# Usage:
#   just reinstall-iar
reinstall-iar:
    uv tool install --force --reinstall --editable .

# Run the development entrypoint
# Usage:
#   just run                 # start backend + frontend
#   just run backend         # start backend only
#   just run frontend        # start frontend only
#   just run docker          # start with Docker Compose (one-click deploy)
#   just run backend_port=8010 frontend_port=5178
#   just run all frontend_dir=web frontend_cmd="pnpm dev"
run arg1="" arg2="" arg3="" arg4="" arg5="" arg6="": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail

    target="all"
    frontend_dir="frontend"
    backend_port=""
    frontend_port=""
    backend_cmd="uv run python -m backend.main"
    frontend_cmd="npm run dev"
    backend_pid=""
    frontend_pid=""
    run_state_file="$(git rev-parse --git-path vanta-run.env)"
    positional_index=0

    parse_run_arg() {
        cli_arg="$1"
        if [ -z "$cli_arg" ]; then
            return 0
        fi

        case "$cli_arg" in
            target=*)
                target="${cli_arg#target=}"
                ;;
            frontend_dir=*)
                frontend_dir="${cli_arg#frontend_dir=}"
                ;;
            backend_port=*)
                backend_port="${cli_arg#backend_port=}"
                ;;
            frontend_port=*)
                frontend_port="${cli_arg#frontend_port=}"
                ;;
            backend_cmd=*)
                backend_cmd="${cli_arg#backend_cmd=}"
                ;;
            frontend_cmd=*)
                frontend_cmd="${cli_arg#frontend_cmd=}"
                ;;
            *)
                case "$positional_index" in
                    0)
                        target="$cli_arg"
                        ;;
                    1)
                        frontend_dir="$cli_arg"
                        ;;
                    2)
                        backend_cmd="$cli_arg"
                        ;;
                    3)
                        frontend_cmd="$cli_arg"
                        ;;
                    *)
                        echo "ERROR: Unexpected run argument: $cli_arg"
                        echo "Usage: just run [backend|frontend|all|docker] [backend_port=<port>] [frontend_port=<port>]"
                        exit 1
                        ;;
                esac
                positional_index=$((positional_index + 1))
                ;;
        esac
    }

    for cli_arg in {{quote(arg1)}} {{quote(arg2)}} {{quote(arg3)}} {{quote(arg4)}} {{quote(arg5)}} {{quote(arg6)}}; do
        parse_run_arg "$cli_arg"
    done

    load_run_ports() {
        if [ -f "$run_state_file" ]; then
            # shellcheck disable=SC1090
            source "$run_state_file"
        fi

        backend_port="${backend_port:-${BACKEND_PORT:-8000}}"
        frontend_port="${frontend_port:-${FRONTEND_PORT:-5173}}"
    }

    save_run_ports() {
        mkdir -p "$(dirname "$run_state_file")"
        {
            printf 'BACKEND_PORT=%s\n' "$backend_port"
            printf 'FRONTEND_PORT=%s\n' "$frontend_port"
        } > "$run_state_file"
    }

    check_port() {
        port_label="$1"
        port_value="$2"
        occupying_pids="$(lsof -nP -iTCP:"$port_value" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $1 !~ /^(com\.docker|docker|vpnkit|hyperkit)/ {print $2}' | sort -u || true)"

        if [ -n "$occupying_pids" ]; then
            echo ""
            echo "⚠️  $port_label port $port_value is already in use by process(es): $occupying_pids"
            echo ""
            echo "   You can switch to a different port:"
            echo "      just run backend_port=8010 frontend_port=5178"
            echo ""
            echo "   Or stop the existing process:"
            echo "      just down backend_port=$backend_port frontend_port=$backend_port"
            echo ""
            exit 1
        fi
    }

    run_backend() {
        echo "Starting backend on port $backend_port: $backend_cmd"
        PORT="$backend_port" bash -lc "$backend_cmd"
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

        echo "Starting frontend in $frontend_dir on port $frontend_port: $frontend_cmd"
        (
            cd "$frontend_dir"
            BACKEND_PORT="$backend_port" FRONTEND_PORT="$frontend_port" bash -lc "$frontend_cmd"
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

    load_run_ports
    save_run_ports
    echo "Saved run ports to $run_state_file"

    case "$target" in
        backend)
            check_port "Backend" "$backend_port"
            run_backend
            ;;
        frontend)
            check_port "Frontend" "$frontend_port"
            run_frontend
            ;;
        all)
            check_port "Backend" "$backend_port"
            check_port "Frontend" "$frontend_port"
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

# Stop local development services by remembered or provided ports.
# Usage:
#   just down
#   just down backend
#   just down frontend
#   just down backend_port=8010 frontend_port=5178
#   just down docker
down arg1="" arg2="" arg3="": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail

    target="all"
    backend_port=""
    frontend_port=""
    run_state_file="$(git rev-parse --git-path vanta-run.env)"
    positional_index=0

    parse_down_arg() {
        cli_arg="$1"
        if [ -z "$cli_arg" ]; then
            return 0
        fi

        case "$cli_arg" in
            target=*)
                target="${cli_arg#target=}"
                ;;
            backend_port=*)
                backend_port="${cli_arg#backend_port=}"
                ;;
            frontend_port=*)
                frontend_port="${cli_arg#frontend_port=}"
                ;;
            *)
                if [ "$positional_index" -eq 0 ]; then
                    target="$cli_arg"
                    positional_index=1
                else
                    echo "ERROR: Unexpected down argument: $cli_arg"
                    echo "Usage: just down [backend|frontend|all|docker] [backend_port=<port>] [frontend_port=<port>]"
                    exit 1
                fi
                ;;
        esac
    }

    for cli_arg in {{quote(arg1)}} {{quote(arg2)}} {{quote(arg3)}}; do
        parse_down_arg "$cli_arg"
    done

    load_run_ports() {
        if [ -f "$run_state_file" ]; then
            # shellcheck disable=SC1090
            source "$run_state_file"
        fi

        backend_port="${backend_port:-${BACKEND_PORT:-8000}}"
        frontend_port="${frontend_port:-${FRONTEND_PORT:-5173}}"
    }

    stop_port() {
        port_label="$1"
        port_value="$2"
        # Exclude Docker Desktop / dockerd processes so just down does not kill the Docker daemon
        process_ids="$(lsof -nP -iTCP:"$port_value" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $1 !~ /^(com\.docker|docker|vpnkit|hyperkit)/ {print $2}' | sort -u || true)"

        if [ -z "$process_ids" ]; then
            echo "No $port_label process listening on port $port_value"
            return 0
        fi

        echo "Stopping $port_label process(es) on port $port_value: $process_ids"
        kill $process_ids 2>/dev/null || true
        sleep 1

        remaining_process_ids="$(lsof -nP -iTCP:"$port_value" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $1 !~ /^(com\.docker|docker|vpnkit|hyperkit)/ {print $2}' | sort -u || true)"
        if [ -n "$remaining_process_ids" ]; then
            echo "Force stopping $port_label process(es) on port $port_value: $remaining_process_ids"
            kill -9 $remaining_process_ids 2>/dev/null || true
        fi
    }

    load_run_ports

    case "$target" in
        backend)
            stop_port backend "$backend_port"
            ;;
        frontend)
            stop_port frontend "$frontend_port"
            ;;
        all)
            stop_port backend "$backend_port"
            stop_port frontend "$frontend_port"
            ;;
        docker)
            docker compose down
            ;;
        *)
            echo "❌ Unknown down target: $target"
            echo "Usage: just down [backend|frontend|all|docker]"
            exit 1
            ;;
    esac

# Check that bundled workflow templates are byte-identical to their source
# files, and that the README field table matches PreviewSettings.model_fields.
# Exits non-zero on any drift.
check-template-drift:
    #!/usr/bin/env bash
    set -euo pipefail

    template_root="src/backend/engines/agent_runner/templates/preview"

    # 1. Byte-level diff between template copies and source files.
    diff_pairs=(
        ".github/workflows/deploy-preview.yml|.github/workflows/deploy-preview.yml"
        "deploy/vps-traefik/README.md|deploy/vps-traefik/README.md"
        "deploy/vps-traefik/docker-compose.preview.yml|deploy/vps-traefik/docker-compose.preview.yml"
        "deploy/vps-traefik/deploy-preview.sh|deploy/vps-traefik/deploy-preview.sh"
        "deploy/vps-traefik/preview.env.example|deploy/vps-traefik/preview.env.example"
        "scripts/preview_env.py|scripts/preview_env.py"
        "scripts/provision_preview_server.py|scripts/provision_preview_server.py"
    )

    drift_detected=0
    for pair in "${diff_pairs[@]}"; do
        template_rel="${pair%%|*}"
        source_rel="${pair##*|}"
        if ! diff -q "${template_root}/${template_rel}" "${source_rel}" > /dev/null; then
            echo "❌ Template drift: ${template_root}/${template_rel} != ${source_rel}"
            drift_detected=1
        fi
    done

    # 2. README field table must match PreviewSettings.model_fields.
    if command -v uv > /dev/null 2>&1; then
        preview_field_names="$(uv run python -c "from backend.infrastructure.config.settings import PreviewSettings; import sys; [sys.stdout.write(name + \"\n\") for name in PreviewSettings.model_fields.keys()]")"
        readme_field_names="$(awk '/^Non-sensitive structure is configured in `config.toml \[preview\]`:/{flag=1; next} flag && /^- `/ {gsub(/^- `/, ""); gsub(/`$/, ""); print}' deploy/vps-traefik/README.md)"
        if [ -n "$readme_field_names" ]; then
            if [ "$(printf '%s\n' "$preview_field_names" | sort)" != "$(printf '%s\n' "$readme_field_names" | sort)" ]; then
                echo "❌ deploy/vps-traefik/README.md field table does not match PreviewSettings.model_fields"
                echo "  Expected: $(printf '%s ' $preview_field_names)"
                echo "  Found:    $(printf '%s ' $readme_field_names)"
                drift_detected=1
            fi
        else
            echo "⚠️  Could not extract README field table; skipping field check."
        fi
    fi

    if [ "$drift_detected" -ne 0 ]; then
        echo "❌ Template drift detected; sync source files into ${template_root}."
        exit 1
    fi
    echo "✅ Templates in sync."

# ── Frontend ──────────────────────────────────────────────────────────────────

# Run tests after `just lint --full` (usage: just test [local|all|real])
#   just test        - Run local tests change-aware via pytest-testmon
#   just test all    - Run all tests (ignores .testmondata)
#   just test real   - Run tests requiring API keys (ignores .testmondata)
#
# This override shadows the shared @test recipe because keda does not include
# pytest-xdist in dev dependencies (-n auto would fail), and because all/real
# must force a full run regardless of .testmondata state.
@test type="local": _check-completion
    #!/usr/bin/env bash
    set -euo pipefail

    source ./scripts/shared/hooks/quality_flag.sh

    git_dir="$(quality_git_dir)"
    flag_file="$git_dir/.last_tested_commit"
    branch_name="$(quality_branch_name)"
    head_hash="$(quality_head_hash)"
    test_tree="$(quality_effective_tree working test)"

    if [ "{{type}}" = "local" ] && quality_flag_matches "$flag_file" "$branch_name" "$head_hash" "$test_tree"; then
        echo "✅ just test flag valid: $branch_name @ ${head_hash:0:8} (tree: ${test_tree:0:8}); skipping tests."
        exit 0
    fi

    echo "🔍 Running full lint checks..."
    if ! SKIP=check-test-flag just lint --full >/dev/null 2>&1; then
        echo "ERROR: Lint failed. Fix lint errors before running tests."
        echo "   Run: just lint --full"
        exit 1
    fi
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

    pytest_exit_code=0
    if [ "{{type}}" = "all" ]; then
        uv run pytest tests/ -v -m '' --no-testmon || pytest_exit_code=$?
    elif [ "{{type}}" = "real" ]; then
        uv run pytest tests/ -v -m 'real_api' --no-testmon || pytest_exit_code=$?
        if [ "$pytest_exit_code" -eq 5 ]; then
            echo "ℹ️  No real_api tests collected; treating as success."
            pytest_exit_code=0
        fi
    else
        # Local mode: --testmon is picked up from pyproject.toml addopts.
        # pytest-xdist is intentionally not used; keda does not declare it.
        uv run pytest tests/ -v || pytest_exit_code=$?
    fi

    if [ "$pytest_exit_code" -ne 0 ]; then
        exit "$pytest_exit_code"
    fi

    # Write flag after tests pass, binding branch, HEAD and effective tree.
    branch_name="$(quality_branch_name)"
    head_hash="$(quality_head_hash)"
    test_tree="$(quality_effective_tree working test)"
    quality_write_flag "$git_dir/.last_tested_commit" "$branch_name" "$head_hash" "$test_tree"
    quality_write_flag "$git_dir/.last_linted_commit" "$branch_name" "$head_hash" "$lint_tree_after_lint"
    echo "✅ just test flag updated: $branch_name @ $head_hash"
    echo "✅ just lint --full flag updated: $branch_name @ $head_hash"

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
