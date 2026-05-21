#!/usr/bin/env bash

# 将此函数放入 .zshrc 或 .bashrc
# 用法:
#   source ./scripts/worktree/create.sh && ai_worktree <新分支名> [--base <base_branch>] [--cmd [code_cmd]]
#   或直接执行:
#   ./scripts/worktree/create.sh <新分支名> [--base <base_branch>] [--cmd [code_cmd]]

ai_worktree_usage() {
    cat <<'EOF'
Usage:
  ai_worktree <new_branch_name> [--base <base_branch>] [--cmd [code_cmd]]

Options:
  --base <base_branch>
                    从指定本地分支创建 worktree。默认使用: main
  --cmd [code_cmd]  创建完成后自动执行: <code_cmd> --add <worktree_path>
                    不传 code_cmd 时默认使用: code
  --subdir <dir>    在 <repo>-worktrees/<dir>/ 下创建 worktree，
                    而非直接放在 <repo>-worktrees/
  -h, --help        显示帮助

Behavior:
  所有 worktree 统一集中到 <repo_parent>/<repo-name>-worktrees/ 下。
  issue-* 分支在未指定 --subdir 时，默认归入 tasks/ 子目录。

Examples:
  ai_worktree feature-login
  ai_worktree feature-login --base develop
  ai_worktree feature-login --cmd
  ai_worktree feature-login --cmd code-insiders
  ai_worktree issue-3
  ai_worktree issue-3 --subdir foo
  ./scripts/worktree/create.sh feature-login
  ./scripts/worktree/create.sh feature-login --base develop
  ./scripts/worktree/create.sh feature-login --cmd
  ./scripts/worktree/create.sh feature-login --cmd code-insiders
EOF
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

discover_frontend_project_directories() {
    local search_root_path="$1"

    if [ -z "$search_root_path" ] || [ ! -d "$search_root_path" ]; then
        return 0
    fi

    if [ -f "$search_root_path/package.json" ]; then
        printf '%s\n' "$search_root_path"
    fi

    while IFS= read -r nested_package_json_path; do
        dirname "$nested_package_json_path"
    done < <(
        find "$search_root_path" \
            \( -type d \( -name ".git" -o -name ".venv" -o -name "node_modules" -o -name "site" \) -prune \) -o \
            -mindepth 2 -type f -name "package.json" -print
    )
}

resolve_frontend_dependency_strategy() {
    # WORKTREE_FRONTEND_STRATEGY 可选值:
    #   - install-per-worktree: 在新 worktree 中执行一次前端依赖安装（默认行为）
    #   - symlink-from-main: 复用源仓库前端目录中的 node_modules（符号链接）
    local configured_strategy="${WORKTREE_FRONTEND_STRATEGY:-}"

    if [ -z "$configured_strategy" ]; then
        echo "install-per-worktree"
        return 0
    fi

    case "$configured_strategy" in
        install-per-worktree|symlink-from-main)
            echo "$configured_strategy"
            ;;
        *)
            echo "⚠️ 未知的 WORKTREE_FRONTEND_STRATEGY: $configured_strategy，回退为 install-per-worktree。" >&2
            echo "install-per-worktree"
            ;;
    esac
}

setup_frontend_node_modules_symlinks() {
    # 在新 worktree 中为前端工程目录创建 node_modules 符号链接，复用源仓库的依赖目录。
    # 参数:
    #   $1: 源仓库根目录（包含已安装依赖的 worktree）
    #   $2: 新建 worktree 的根目录
    local source_root_path="$1"
    local target_root_path="$2"

    if [ -z "$source_root_path" ] || [ -z "$target_root_path" ]; then
        return 0
    fi

    if [ ! -d "$source_root_path" ] || [ ! -d "$target_root_path" ]; then
        return 0
    fi

    local linked_project_count=0

    while IFS= read -r source_frontend_dir; do
        local relative_frontend_path="${source_frontend_dir#"$source_root_path"/}"
        local target_frontend_dir="$target_root_path"
        local frontend_display_path="."

        if [ "$source_frontend_dir" != "$source_root_path" ]; then
            target_frontend_dir="$target_root_path/$relative_frontend_path"
            frontend_display_path="$relative_frontend_path"
        fi

        local source_node_modules_path="$source_frontend_dir/node_modules"
        local target_node_modules_path="$target_frontend_dir/node_modules"

        if [ ! -d "$source_node_modules_path" ]; then
            echo "ℹ️ 源前端目录缺少 node_modules，跳过符号链接: $frontend_display_path"
            continue
        fi

        if [ -e "$target_node_modules_path" ]; then
            # 目标目录已存在 node_modules（目录或链接）时不覆盖，避免误删本地安装。
            continue
        fi

        if [ ! -d "$target_frontend_dir" ]; then
            mkdir -p "$target_frontend_dir"
        fi

        if ln -s "$source_node_modules_path" "$target_node_modules_path"; then
            echo "🔗 已为前端目录创建 node_modules 符号链接: $frontend_display_path"
            echo "   $target_node_modules_path -> $source_node_modules_path"
            linked_project_count=$((linked_project_count + 1))
        else
            echo "⚠️ 创建符号链接失败: $target_node_modules_path" >&2
        fi
    done < <(discover_frontend_project_directories "$source_root_path")

    if [ "$linked_project_count" -eq 0 ]; then
        echo "ℹ️ 未在源仓库中找到适合创建符号链接的前端项目（package.json + node_modules）。"
    fi
}

install_frontend_dependencies_in_current_directory() {
    # Priority: lock-file driven install for reproducible frontend environments.
    if [ -f pnpm-lock.yaml ]; then
        if ! command_exists pnpm; then
            echo "⚠️ 检测到 pnpm-lock.yaml，但未找到 pnpm，跳过前端依赖安装。"
            return 0
        fi
        echo "📦 检测到 pnpm-lock.yaml，正在执行 pnpm install --ignore-scripts ..."
        if ! pnpm install --ignore-scripts; then
            echo "❌ pnpm install 失败。"
            return 1
        fi
        return 0
    fi

    if [ -f package-lock.json ]; then
        if ! command_exists npm; then
            echo "⚠️ 检测到 package-lock.json，但未找到 npm，跳过前端依赖安装。"
            return 0
        fi
        echo "📦 检测到 package-lock.json，正在执行 npm ci --ignore-scripts ..."
        if ! npm ci --ignore-scripts; then
            echo "❌ npm ci 失败。"
            return 1
        fi
        return 0
    fi

    if [ -f yarn.lock ]; then
        if ! command_exists yarn; then
            echo "⚠️ 检测到 yarn.lock，但未找到 yarn，跳过前端依赖安装。"
            return 0
        fi
        echo "📦 检测到 yarn.lock，正在执行 yarn install --ignore-scripts ..."
        if ! yarn install --ignore-scripts; then
            echo "❌ yarn install 失败。"
            return 1
        fi
        return 0
    fi

    if [ -f bun.lock ] || [ -f bun.lockb ]; then
        if ! command_exists bun; then
            echo "⚠️ 检测到 bun lock 文件，但未找到 bun，跳过前端依赖安装。"
            return 0
        fi
        echo "📦 检测到 bun lock 文件，正在执行 bun install --ignore-scripts ..."
        if ! bun install --ignore-scripts; then
            echo "❌ bun install 失败。"
            return 1
        fi
        return 0
    fi

    if [ -f package.json ]; then
        if ! command_exists npm; then
            echo "⚠️ 检测到 package.json，但未找到 npm，跳过前端依赖安装。"
            return 0
        fi
        echo "📦 检测到 package.json（无 lock 文件），正在执行 npm install --ignore-scripts ..."
        if ! npm install --ignore-scripts; then
            echo "❌ npm install 失败。"
            return 1
        fi
    fi

    return 0
}

install_frontend_dependencies_in_directory() {
    local frontend_project_path="$1"
    local frontend_display_path="$2"

    if [ -z "$frontend_project_path" ] || [ ! -d "$frontend_project_path" ]; then
        return 0
    fi

    if [ ! -f "$frontend_project_path/package.json" ]; then
        return 0
    fi

    echo "🧩 正在处理前端目录: $frontend_display_path"
    if ! (
        cd "$frontend_project_path" &&
        install_frontend_dependencies_in_current_directory
    ); then
        echo "❌ 前端依赖安装失败: $frontend_display_path"
        return 1
    fi

    return 0
}

install_frontend_dependencies_for_worktree() {
    local worktree_root_path="$1"
    local discovered_project_count=0
    local frontend_project_path=""
    local relative_frontend_path=""
    local frontend_display_path=""

    while IFS= read -r frontend_project_path; do
        if [ -z "$frontend_project_path" ]; then
            continue
        fi

        discovered_project_count=$((discovered_project_count + 1))
        relative_frontend_path="${frontend_project_path#"$worktree_root_path"/}"
        frontend_display_path="."
        if [ "$frontend_project_path" != "$worktree_root_path" ]; then
            frontend_display_path="$relative_frontend_path"
        fi

        if ! install_frontend_dependencies_in_directory "$frontend_project_path" "$frontend_display_path"; then
            return 1
        fi
    done < <(discover_frontend_project_directories "$worktree_root_path")

    if [ "$discovered_project_count" -eq 0 ]; then
        echo "ℹ️ 未检测到 package.json，跳过前端依赖安装。"
    fi

    return 0
}

install_python_dependencies() {
    if [ ! -f pyproject.toml ]; then
        return 0
    fi

    if ! command_exists uv; then
        echo "⚠️ 检测到 pyproject.toml，但未找到 uv，跳过 Python 依赖安装。"
        return 0
    fi

    echo "📦 检测到 pyproject.toml，正在执行 uv sync --all-extras ..."
    if ! uv sync --all-extras; then
        echo "❌ uv sync 失败。"
        return 1
    fi
    return 0
}

function ai_worktree() {
    local branch_name=""
    local base_branch_name="${KODA_WORKTREE_BASE_BRANCH:-main}"
    local enable_vscode_add="false"
    local vscode_command_name="code"
    local subdir_name=""
    local repo_root_path=""
    local repo_parent_path=""
    local target_abs_path=""
    local source_env_example_path=""
    local copied_env_file_count=0
    local source_env_file_path=""
    local relative_env_file_path=""
    local target_env_file_path=""

    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                ai_worktree_usage
                return 0
                ;;
            --cmd)
                enable_vscode_add="true"
                if [ "$#" -gt 1 ] && [[ "$2" != -* ]]; then
                    vscode_command_name="$2"
                    shift
                fi
                ;;
            --cmd=*)
                enable_vscode_add="true"
                vscode_command_name="${1#--cmd=}"
                if [ -z "$vscode_command_name" ]; then
                    echo "❌ --cmd= 后需要提供命令名，例如: --cmd=code-insiders"
                    return 1
                fi
                ;;
            --base)
                if [ "$#" -le 1 ] || [[ "$2" == -* ]]; then
                    echo "❌ --base 后需要提供基底分支名，例如: --base develop"
                    return 1
                fi
                base_branch_name="$2"
                shift
                ;;
            --base=*)
                base_branch_name="${1#--base=}"
                if [ -z "$base_branch_name" ]; then
                    echo "❌ --base= 后需要提供基底分支名，例如: --base=develop"
                    return 1
                fi
                ;;
            --subdir)
                if [ "$#" -le 1 ] || [[ "$2" == -* ]]; then
                    echo "❌ --subdir 后需要提供子目录名，例如: --subdir tasks"
                    return 1
                fi
                subdir_name="$2"
                shift
                ;;
            --subdir=*)
                subdir_name="${1#--subdir=}"
                ;;
            -*)
                echo "❌ 未知参数: $1"
                ai_worktree_usage
                return 1
                ;;
            *)
                if [ -z "$branch_name" ]; then
                    branch_name="$1"
                else
                    echo "❌ 只允许一个分支名参数，收到多余参数: $1"
                    ai_worktree_usage
                    return 1
                fi
                ;;
        esac
        shift
    done

    # Fallback from code to code-insiders if code is not installed
    if [ "$vscode_command_name" = "code" ] && ! command -v code &>/dev/null && command -v code-insiders &>/dev/null; then
        vscode_command_name="code-insiders"
    fi

    if [ -z "$branch_name" ]; then
        echo "请提供分支名称！例如: ai_worktree feature-login"
        ai_worktree_usage
        return 1
    fi

    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "❌ 当前目录不是 Git 仓库，无法创建 worktree。"
        return 1
    fi

    repo_root_path="$(git rev-parse --show-toplevel)"
    repo_parent_path="$(dirname "$repo_root_path")"
    # 1. 约定 worktree 统一集中到 <repo_parent>/<repo-name>-worktrees/
    #    issue-* 分支在未指定 --subdir 时默认归入 tasks/ 子目录
    if [ -z "$subdir_name" ] && [[ "$branch_name" == issue-* ]]; then
        subdir_name="tasks"
    fi

    local repo_name=""
    repo_name="$(basename "$repo_root_path")"
    local worktrees_base_path=""
    worktrees_base_path="$repo_parent_path/${repo_name}-worktrees"

    if [ -n "$subdir_name" ]; then
        target_abs_path="$worktrees_base_path/$subdir_name/$branch_name"
    else
        target_abs_path="$worktrees_base_path/$branch_name"
    fi
    if [ -e "$target_abs_path" ]; then
        echo "❌ 目标目录已存在: $target_abs_path"
        return 1
    fi

    if ! git -C "$repo_root_path" show-ref --verify --quiet "refs/heads/$base_branch_name"; then
        echo "❌ 基底分支不存在: $base_branch_name"
        return 1
    fi

    echo "🚀 正在创建 Git Worktree: $target_abs_path ..."
    if ! git -C "$repo_root_path" worktree add -b "$branch_name" "$target_abs_path" "$base_branch_name"; then
        echo "❌ Git worktree 创建失败。"
        return 1
    fi

    echo "🔗 正在处理 .env ..."
    # 2. 复制仓库中所有以 .env 结尾的文件（保持相对路径），避免子目录环境文件丢失
    copied_env_file_count=0
    while IFS= read -r source_env_file_path; do
        relative_env_file_path="${source_env_file_path#"$repo_root_path"/}"
        target_env_file_path="$target_abs_path/$relative_env_file_path"
        mkdir -p "$(dirname "$target_env_file_path")"
        cp "$source_env_file_path" "$target_env_file_path"
        copied_env_file_count=$((copied_env_file_count + 1))
    done < <(
        find "$repo_root_path" -type f -name ".env*" \
            -not -path "$repo_root_path/.git/*" \
            -not -path "$repo_root_path/.venv/*" \
            -not -path "$repo_root_path/.uv-cache/*" \
            -not -path "$repo_root_path/site/*"
    )

    source_env_example_path="$repo_root_path/.env.example"
    if [ "$copied_env_file_count" -gt 0 ]; then
        echo "✅ 已复制 $copied_env_file_count 个 .env 文件到新 worktree。"
    elif [ -f "$source_env_example_path" ]; then
        cp "$source_env_example_path" "$target_abs_path/.env"
        echo "⚠️ 仓库根目录没有 .env，已使用 .env.example 创建 .env。"
    else
        echo "⚠️ 仓库根目录未找到 .env/.env.example，跳过。"
    fi

    # 3. 自动安装依赖 / 或复用前端依赖 (极速模式)
    echo "📦 正在使用全局缓存安装依赖 ..."
    if ! cd "$target_abs_path"; then
        echo "❌ 无法进入目录: $target_abs_path"
        return 1
    fi

    local frontend_dependency_strategy
    frontend_dependency_strategy="$(resolve_frontend_dependency_strategy)"
    echo "🧩 前端依赖策略: $frontend_dependency_strategy"

    if [ "$frontend_dependency_strategy" = "symlink-from-main" ]; then
        # 复用源仓库前端依赖目录（优先避免重复安装）
        setup_frontend_node_modules_symlinks "$repo_root_path" "$target_abs_path"
    else
        # install-per-worktree: 保持现有“极速安装”行为，可通过环境变量禁用
        if [ "${WORKTREE_SKIP_FRONTEND_INSTALL:-false}" = "true" ]; then
            echo "⚠️ 已设置 WORKTREE_SKIP_FRONTEND_INSTALL=true，跳过前端依赖安装。"
        else
            if ! install_frontend_dependencies_for_worktree "$target_abs_path"; then
                return 1
            fi
        fi
    fi

    if ! install_python_dependencies; then
        return 1
    fi

    if [ "$enable_vscode_add" = "true" ]; then
        if ! command -v "$vscode_command_name" >/dev/null 2>&1; then
            echo "❌ 未找到命令: $vscode_command_name"
            echo "   请确认该 CLI 已安装并在 PATH 中。"
            return 1
        fi
        if ! "$vscode_command_name" "$target_abs_path"; then
            echo "❌ 执行失败: $vscode_command_name \"$target_abs_path\""
            return 1
        fi
        echo "🧩 已打开 worktree: $target_abs_path"
    fi

    echo "✅ 准备完毕！AI 可以开始在 $target_abs_path 愉快地写代码了。"
}

# If executed directly with bash, run ai_worktree with all CLI args.
# If sourced in shell profile, only function definitions are loaded.
if [ -n "${BASH_VERSION:-}" ] && [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    ai_worktree "$@"
fi
