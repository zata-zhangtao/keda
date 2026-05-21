#!/usr/bin/env bash

# 将此函数放入 .zshrc 或 .bashrc
# 用法:
#   source ./scripts/worktree/open.sh && ai_open <分支名> [--cmd [code_cmd]]
#   或直接执行:
#   ./scripts/worktree/open.sh <分支名> [--cmd [code_cmd]]

ai_open_usage() {
    cat <<'EOF'
Usage:
  ai_open <branch_name> [--cmd [code_cmd]]

Options:
  --cmd [code_cmd]  使用指定命令打开 worktree 目录。
                    不传 code_cmd 时默认使用: code-insiders
  -h, --help        显示帮助

Examples:
  ai_open feature-login
  ai_open feature-login --cmd
  ai_open feature-login --cmd code
  ./scripts/worktree/open.sh feature-login
  ./scripts/worktree/open.sh feature-login --cmd code
EOF
}

resolve_worktree_path_by_branch() {
    local branch_name="$1"
    local repo_root_path="$2"
    local repo_parent_path="$3"
    local worktree_path=""

    # 优先通过 git worktree list 查找分支对应的 worktree 路径
    worktree_path="$(git -C "$repo_root_path" worktree list --porcelain 2>/dev/null \
        | awk -v branch="$branch_name" '
            /^worktree / { path = substr($0, 11) }
            /^branch / {
                sub(/^refs\/heads\//, "", $2)
                if ($2 == branch) { print path; exit }
            }
        '
    )"

    if [ -n "$worktree_path" ] && [ -d "$worktree_path" ]; then
        printf '%s\n' "$worktree_path"
        return 0
    fi

    # 回退到 create.sh 的约定路径: 仓库根目录上级的同名文件夹
    worktree_path="$repo_parent_path/$branch_name"
    if [ -d "$worktree_path" ]; then
        printf '%s\n' "$worktree_path"
        return 0
    fi

    return 1
}

function ai_open() {
    local branch_name=""
    local vscode_command_name="code-insiders"
    local repo_root_path=""
    local repo_parent_path=""
    local worktree_path=""

    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                ai_open_usage
                return 0
                ;;
            --cmd)
                if [ "$#" -gt 1 ] && [[ "$2" != -* ]]; then
                    vscode_command_name="$2"
                    shift
                fi
                ;;
            --cmd=*)
                vscode_command_name="${1#--cmd=}"
                if [ -z "$vscode_command_name" ]; then
                    echo "❌ --cmd= 后需要提供命令名，例如: --cmd=code"
                    return 1
                fi
                ;;
            -*)
                echo "❌ 未知参数: $1"
                ai_open_usage
                return 1
                ;;
            *)
                if [ -z "$branch_name" ]; then
                    branch_name="$1"
                else
                    echo "❌ 只允许一个分支名参数，收到多余参数: $1"
                    ai_open_usage
                    return 1
                fi
                ;;
        esac
        shift
    done

    if [ -z "$branch_name" ]; then
        echo "请提供分支名称！例如: ai_open feature-login"
        ai_open_usage
        return 1
    fi

    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "❌ 当前目录不是 Git 仓库，无法定位 worktree。"
        return 1
    fi

    repo_root_path="$(git rev-parse --show-toplevel)"
    repo_parent_path="$(dirname "$repo_root_path")"

    worktree_path="$(resolve_worktree_path_by_branch "$branch_name" "$repo_root_path" "$repo_parent_path")"

    if [ -z "$worktree_path" ]; then
        echo "❌ 未找到分支 '$branch_name' 对应的 worktree 目录。"
        echo "   已尝试查找 git worktree list 及约定路径: $repo_parent_path/$branch_name"
        return 1
    fi

    if ! command -v "$vscode_command_name" >/dev/null 2>&1; then
        echo "❌ 未找到命令: $vscode_command_name"
        echo "   请确认该 CLI 已安装并在 PATH 中。"
        return 1
    fi

    echo "🚀 正在使用 $vscode_command_name 打开: $worktree_path ..."
    if ! "$vscode_command_name" "$worktree_path"; then
        echo "❌ 执行失败: $vscode_command_name \"$worktree_path\""
        return 1
    fi

    echo "✅ 已打开 worktree: $worktree_path"
}

# If executed directly with bash, run ai_open with all CLI args.
# If sourced in shell profile, only function definitions are loaded.
if [ -n "${BASH_VERSION:-}" ] && [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    ai_open "$@"
fi
