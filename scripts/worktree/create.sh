#!/usr/bin/env bash

# 将此函数放入 .zshrc 或 .bashrc
# 用法:
#   source ./scripts/worktree/create.sh && ai_worktree <新分支名> [--base <base_branch>] [--cmd [code_cmd]]
#   或直接执行:
#   ./scripts/worktree/create.sh <新分支名> [--base <base_branch>] [--cmd [code_cmd]]

ai_worktree_usage() {
    cat <<'EOF'
Usage:
  ai_worktree <branch_name> [--checkout [<source_ref>] | --new]
                            [--base <base_branch>] [--cmd [code_cmd]]
                            [--subdir <dir>]

Options:
  --checkout [<source>]
                    复用已有分支而非新建。<source> 可省略，默认等于 <branch_name>。
                    <source> 支持本地分支名、远程 tracking 形式 (例如 zata/issue-15)。
                    不传 --checkout 时，若检测到同名远程分支会自动进入 checkout 模式。
  --new             显式强制新建分支；同名远程分支即使存在也忽略。
                    与 --checkout 互斥。
  --base <base_branch>
                    指定 base branch 名称。默认使用: main
                    仅在新建分支时生效；checkout 模式下会被忽略。
  --cmd [code_cmd]  创建完成后自动执行: <code_cmd> --add <worktree_path>
                    不传 code_cmd 时默认使用: code
  --subdir <dir>    在 <repo>-worktrees/<dir>/ 下创建 worktree，
                    而非直接放在 <repo>-worktrees/
  -h, --help        显示帮助

Behavior:
  所有 worktree 统一集中到 <repo_parent>/<repo-name>-worktrees/ 下。
  issue-* 分支在未指定 --subdir 时，默认归入 tasks/ 子目录。
  默认会同步 base/源远程的 tracking ref 作为新 worktree 起点。
  可通过环境变量控制远程同步行为:
    KEDA_WORKTREE_SYNC_BASE      默认 true，设为 false 关闭远程同步
    KEDA_WORKTREE_BASE_REMOTE    覆盖默认 remote 名
    KODA_WORKTREE_BASE_BRANCH    覆盖默认 base branch

  Mode resolution:
    --new                -> 强制新建；本地同名分支已存在则报错。
    --checkout [<src>]   -> 显式 checkout；<src> 默认为 <branch_name>。
    (neither)            -> 本地存在 -> 报错并提示用 --checkout；
                          远程唯一匹配 -> 自动 checkout；
                          均无 -> 新建分支。

Examples:
  ai_worktree feature-login
  ai_worktree feature-login --base develop
  ai_worktree feature-login --cmd
  ai_worktree feature-login --cmd code-insiders
  ai_worktree issue-3
  ai_worktree issue-3 --subdir foo
  ai_worktree issue-15 --checkout zata/issue-15
  ai_worktree issue-15 --checkout
  ai_worktree feature-x --new
  KEDA_WORKTREE_SYNC_BASE=false ai_worktree feature-login
  ./scripts/worktree/create.sh feature-login
  ./scripts/worktree/create.sh issue-15 --checkout zata/issue-15
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

resolve_base_remote_name() {
    local repo_root_path="$1"
    local base_branch_name="$2"
    local configured_remote="${KEDA_WORKTREE_BASE_REMOTE:-}"

    if [ -n "$configured_remote" ]; then
        echo "$configured_remote"
        return 0
    fi

    local branch_remote=""
    branch_remote="$(git -C "$repo_root_path" config --get "branch.${base_branch_name}.remote" 2>/dev/null || true)"

    if [ -n "$branch_remote" ]; then
        echo "$branch_remote"
    else
        echo "origin"
    fi
}

remote_exists() {
    local repo_root_path="$1"
    local remote_name="$2"

    git -C "$repo_root_path" remote get-url "$remote_name" >/dev/null 2>&1
}

resolve_worktree_start_point() {
    local repo_root_path="$1"
    local base_branch_name="$2"
    local sync_enabled="$3"

    if [ "$sync_enabled" != "true" ]; then
        if ! git -C "$repo_root_path" show-ref --verify --quiet "refs/heads/$base_branch_name" 2>/dev/null; then
            echo "❌ 基底分支不存在: $base_branch_name" >&2
            return 1
        fi
        echo "$base_branch_name"
        return 0
    fi

    local base_remote_name=""
    base_remote_name="$(resolve_base_remote_name "$repo_root_path" "$base_branch_name")"

    if ! remote_exists "$repo_root_path" "$base_remote_name"; then
        echo "ℹ️ 未找到 remote '$base_remote_name'，回退到本地 base branch: $base_branch_name" >&2
        if ! git -C "$repo_root_path" show-ref --verify --quiet "refs/heads/$base_branch_name" 2>/dev/null; then
            echo "❌ 基底分支不存在: $base_branch_name" >&2
            return 1
        fi
        echo "$base_branch_name"
        return 0
    fi

    echo "🌐 正在从 remote '$base_remote_name' 同步 base branch '$base_branch_name' ..." >&2
    if ! git -C "$repo_root_path" fetch --prune "$base_remote_name" \
        "+refs/heads/${base_branch_name}:refs/remotes/${base_remote_name}/${base_branch_name}" 2>/dev/null; then
        echo "❌ 从 remote '$base_remote_name' 获取 '$base_branch_name' 失败。" >&2
        echo "   请检查网络连接、remote URL 和分支名称。" >&2
        echo "   或者设置 KEDA_WORKTREE_SYNC_BASE=false 以使用本地 base branch。" >&2
        return 1
    fi

    local remote_ref="${base_remote_name}/${base_branch_name}"
    local fetched_sha=""
    fetched_sha="$(git -C "$repo_root_path" rev-parse "$remote_ref" 2>/dev/null || true)"

    if [ -z "$fetched_sha" ]; then
        echo "❌ 无法解析 remote tracking ref: $remote_ref" >&2
        return 1
    fi

    echo "✅ 已同步远程 base: $remote_ref @ ${fetched_sha:0:8}" >&2
    echo "$remote_ref"
}

local_branch_exists() {
    local repo_root_path="$1"
    local branch_name="$2"
    git -C "$repo_root_path" show-ref --verify --quiet "refs/heads/$branch_name"
}

# Echo configured remote name whose prefix matches the candidate ("<remote>/<rest>"),
# preferring the longest match if multiple remotes share prefixes.
parse_remote_prefix() {
    local repo_root_path="$1"
    local candidate="$2"
    local best_remote=""
    local best_len=0
    local remote_name=""
    local remote_len=0

    while IFS= read -r remote_name; do
        [ -z "$remote_name" ] && continue
        if [[ "$candidate" == "$remote_name/"* ]]; then
            remote_len=${#remote_name}
            if [ "$remote_len" -gt "$best_len" ]; then
                best_remote="$remote_name"
                best_len="$remote_len"
            fi
        fi
    done < <(git -C "$repo_root_path" remote)

    if [ -n "$best_remote" ]; then
        echo "$best_remote"
        return 0
    fi
    return 1
}

# Echo the unique remote name that has refs/remotes/<remote>/<branch_name>.
# Return non-zero if 0 or >1 remotes match (ambiguous).
find_unique_remote_for_branch() {
    local repo_root_path="$1"
    local branch_name="$2"
    local remote_name=""
    local match_remote=""
    local match_count=0

    while IFS= read -r remote_name; do
        [ -z "$remote_name" ] && continue
        if git -C "$repo_root_path" show-ref --verify --quiet "refs/remotes/$remote_name/$branch_name" 2>/dev/null; then
            match_remote="$remote_name"
            match_count=$((match_count + 1))
        fi
    done < <(git -C "$repo_root_path" remote)

    if [ "$match_count" -eq 1 ]; then
        echo "$match_remote"
        return 0
    fi
    return 1
}

# Targeted fetch of a single remote branch into its tracking ref. Non-fatal on failure.
fetch_remote_branch_ref() {
    local repo_root_path="$1"
    local remote_name="$2"
    local remote_branch_name="$3"

    echo "🌐 正在同步 ${remote_name}/${remote_branch_name} ..." >&2
    if ! git -C "$repo_root_path" fetch --prune "$remote_name" \
        "+refs/heads/${remote_branch_name}:refs/remotes/${remote_name}/${remote_branch_name}" 2>/dev/null; then
        echo "⚠️ 同步远程分支失败 (${remote_name}/${remote_branch_name})，将使用本地已有的 tracking ref。" >&2
        return 1
    fi
    return 0
}

# Resolve the checkout source for explicit --checkout mode. Sets WORKTREE_PLAN_* globals.
_resolve_checkout_source() {
    local repo_root_path="$1"
    local local_branch_name="$2"
    local source_name="$3"
    local sync_enabled="$4"
    local remote_prefix=""
    local remote_branch=""
    local detected_remote=""

    # Case 1: source matches "<remote>/<rest>" form with a configured remote.
    if remote_prefix="$(parse_remote_prefix "$repo_root_path" "$source_name")"; then
        remote_branch="${source_name#"$remote_prefix/"}"
        if [ "$sync_enabled" = "true" ]; then
            fetch_remote_branch_ref "$repo_root_path" "$remote_prefix" "$remote_branch" || true
        fi
        if ! git -C "$repo_root_path" show-ref --verify --quiet "refs/remotes/$remote_prefix/$remote_branch"; then
            echo "❌ 远程分支不存在: $remote_prefix/$remote_branch" >&2
            return 1
        fi
        if local_branch_exists "$repo_root_path" "$local_branch_name"; then
            echo "❌ 本地分支 $local_branch_name 已存在，无法用 -b 重新创建。" >&2
            echo "   若想复用本地分支，传 --checkout $local_branch_name 即可。" >&2
            return 1
        fi
        WORKTREE_PLAN_MODE="CHECKOUT_REMOTE"
        WORKTREE_PLAN_LOCAL_BRANCH="$local_branch_name"
        WORKTREE_PLAN_START_POINT="$remote_prefix/$remote_branch"
        return 0
    fi

    # Case 2a: source matches a local branch.
    if local_branch_exists "$repo_root_path" "$source_name"; then
        if [ "$local_branch_name" != "$source_name" ]; then
            echo "❌ 本地分支 $source_name 已存在；要复用它，新 worktree 的分支名应与之一致。" >&2
            echo "   请把第一个位置参数也写成 $source_name，或者改用 --new 创建另一个分支。" >&2
            return 1
        fi
        WORKTREE_PLAN_MODE="CHECKOUT_LOCAL"
        WORKTREE_PLAN_LOCAL_BRANCH="$source_name"
        WORKTREE_PLAN_START_POINT="$source_name"
        return 0
    fi

    # Case 2b: source matches a remote-tracking branch on exactly one remote.
    if detected_remote="$(find_unique_remote_for_branch "$repo_root_path" "$source_name")"; then
        if [ "$sync_enabled" = "true" ]; then
            fetch_remote_branch_ref "$repo_root_path" "$detected_remote" "$source_name" || true
        fi
        if local_branch_exists "$repo_root_path" "$local_branch_name"; then
            echo "❌ 本地分支 $local_branch_name 已存在，无法用 -b 重新创建。" >&2
            return 1
        fi
        WORKTREE_PLAN_MODE="CHECKOUT_REMOTE"
        WORKTREE_PLAN_LOCAL_BRANCH="$local_branch_name"
        WORKTREE_PLAN_START_POINT="$detected_remote/$source_name"
        return 0
    fi

    echo "❌ 找不到可 checkout 的分支: $source_name" >&2
    echo "   尝试过: 本地 refs/heads/$source_name、各 remote 下的 $source_name、显式 <remote>/<branch> 形式。" >&2
    echo "   先运行 \`git fetch\` 或检查分支名拼写。" >&2
    return 1
}

# Compute creation plan. Sets globals:
#   WORKTREE_PLAN_MODE          NEW | CHECKOUT_REMOTE | CHECKOUT_LOCAL
#   WORKTREE_PLAN_LOCAL_BRANCH  local branch name to live in the new worktree
#   WORKTREE_PLAN_START_POINT   start ref (empty for NEW; caller resolves base separately)
resolve_worktree_creation_plan() {
    local repo_root_path="$1"
    local local_branch_arg="$2"
    local explicit_mode="$3"        # "" | "new" | "checkout"
    local checkout_source_arg="$4"  # may be empty even in checkout mode
    local sync_enabled="$5"
    local detected_remote=""

    WORKTREE_PLAN_MODE=""
    WORKTREE_PLAN_LOCAL_BRANCH=""
    WORKTREE_PLAN_START_POINT=""

    case "$explicit_mode" in
        new)
            if local_branch_exists "$repo_root_path" "$local_branch_arg"; then
                echo "❌ 本地分支已存在: $local_branch_arg" >&2
                echo "   --new 拒绝复用已有分支。请换个名字，或改用 --checkout 复用。" >&2
                return 1
            fi
            WORKTREE_PLAN_MODE="NEW"
            WORKTREE_PLAN_LOCAL_BRANCH="$local_branch_arg"
            return 0
            ;;
        checkout)
            local effective_source="${checkout_source_arg:-$local_branch_arg}"
            _resolve_checkout_source "$repo_root_path" "$local_branch_arg" "$effective_source" "$sync_enabled"
            return $?
            ;;
        "")
            # Auto-detect.
            if local_branch_exists "$repo_root_path" "$local_branch_arg"; then
                echo "❌ 本地分支已存在: $local_branch_arg" >&2
                echo "   请使用 --checkout 复用已有分支，或换个分支名。" >&2
                echo "   (git 不允许同一分支被多个 worktree 同时占用，新建同名会失败。)" >&2
                return 1
            fi
            if detected_remote="$(find_unique_remote_for_branch "$repo_root_path" "$local_branch_arg")"; then
                echo "🔍 自动检测到远程分支: $detected_remote/$local_branch_arg" >&2
                echo "   将进入 checkout 模式；如需强制新建同名本地分支请加 --new。" >&2
                if [ "$sync_enabled" = "true" ]; then
                    fetch_remote_branch_ref "$repo_root_path" "$detected_remote" "$local_branch_arg" || true
                fi
                WORKTREE_PLAN_MODE="CHECKOUT_REMOTE"
                WORKTREE_PLAN_LOCAL_BRANCH="$local_branch_arg"
                WORKTREE_PLAN_START_POINT="$detected_remote/$local_branch_arg"
                return 0
            fi
            WORKTREE_PLAN_MODE="NEW"
            WORKTREE_PLAN_LOCAL_BRANCH="$local_branch_arg"
            return 0
            ;;
        *)
            echo "❌ 内部错误: 未知 explicit_mode '$explicit_mode'" >&2
            return 1
            ;;
    esac
}

function ai_worktree() {
    local branch_name=""
    local base_branch_name="${KODA_WORKTREE_BASE_BRANCH:-main}"
    local base_branch_user_set="false"
    local enable_vscode_add="false"
    local vscode_command_name="code"
    local subdir_name=""
    local creation_mode=""
    local checkout_source=""
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
                base_branch_user_set="true"
                shift
                ;;
            --base=*)
                base_branch_name="${1#--base=}"
                if [ -z "$base_branch_name" ]; then
                    echo "❌ --base= 后需要提供基底分支名，例如: --base=develop"
                    return 1
                fi
                base_branch_user_set="true"
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
            --new)
                if [ "$creation_mode" = "checkout" ]; then
                    echo "❌ --new 与 --checkout 互斥，不能同时使用。"
                    return 1
                fi
                creation_mode="new"
                ;;
            --checkout)
                if [ "$creation_mode" = "new" ]; then
                    echo "❌ --new 与 --checkout 互斥，不能同时使用。"
                    return 1
                fi
                creation_mode="checkout"
                # Only consume the next token as source if the positional branch
                # name is already set — otherwise it likely IS the positional.
                if [ -n "$branch_name" ] && [ "$#" -gt 1 ] && [[ "$2" != -* ]]; then
                    checkout_source="$2"
                    shift
                fi
                ;;
            --checkout=*)
                if [ "$creation_mode" = "new" ]; then
                    echo "❌ --new 与 --checkout 互斥，不能同时使用。"
                    return 1
                fi
                creation_mode="checkout"
                checkout_source="${1#--checkout=}"
                if [ -z "$checkout_source" ]; then
                    echo "❌ --checkout= 后需要提供 source，例如: --checkout=zata/issue-15"
                    return 1
                fi
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

    local sync_enabled="${KEDA_WORKTREE_SYNC_BASE:-true}"
    local worktree_start_point=""
    local start_sha=""
    local plan_mode=""
    local local_branch_name=""

    if ! resolve_worktree_creation_plan \
            "$repo_root_path" "$branch_name" "$creation_mode" "$checkout_source" "$sync_enabled"; then
        return 1
    fi
    plan_mode="$WORKTREE_PLAN_MODE"
    local_branch_name="$WORKTREE_PLAN_LOCAL_BRANCH"
    worktree_start_point="$WORKTREE_PLAN_START_POINT"

    if [ "$plan_mode" = "NEW" ]; then
        worktree_start_point="$(resolve_worktree_start_point "$repo_root_path" "$base_branch_name" "$sync_enabled")"
        if [ $? -ne 0 ] || [ -z "$worktree_start_point" ]; then
            return 1
        fi
    elif [ "$base_branch_user_set" = "true" ]; then
        echo "⚠️ checkout 模式下 --base $base_branch_name 不生效，已忽略。" >&2
    fi

    start_sha="$(git -C "$repo_root_path" rev-parse "$worktree_start_point" 2>/dev/null || true)"
    if [ -z "$start_sha" ]; then
        echo "❌ 无法解析起点: $worktree_start_point"
        return 1
    fi

    echo "🚀 正在创建 Git Worktree: $target_abs_path ..."
    case "$plan_mode" in
        NEW)
            echo "   模式: 新建分支 $local_branch_name"
            echo "   起点: $worktree_start_point @ ${start_sha:0:8}"
            if ! git -C "$repo_root_path" worktree add -b "$local_branch_name" "$target_abs_path" "$worktree_start_point"; then
                echo "❌ Git worktree 创建失败。"
                return 1
            fi
            ;;
        CHECKOUT_REMOTE)
            echo "   模式: checkout 远程分支 → 本地 $local_branch_name 跟踪 $worktree_start_point"
            echo "   起点: $worktree_start_point @ ${start_sha:0:8}"
            if ! git -C "$repo_root_path" worktree add -b "$local_branch_name" "$target_abs_path" "$worktree_start_point"; then
                echo "❌ Git worktree 创建失败。"
                return 1
            fi
            ;;
        CHECKOUT_LOCAL)
            echo "   模式: 复用已有本地分支 $local_branch_name"
            echo "   起点: $worktree_start_point @ ${start_sha:0:8}"
            if ! git -C "$repo_root_path" worktree add "$target_abs_path" "$local_branch_name"; then
                echo "❌ Git worktree 创建失败。"
                return 1
            fi
            ;;
        *)
            echo "❌ 内部错误: 未知 plan_mode '$plan_mode'"
            return 1
            ;;
    esac

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
