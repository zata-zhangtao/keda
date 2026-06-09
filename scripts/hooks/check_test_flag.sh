#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/hooks/quality_flag.sh
source "$script_dir/quality_flag.sh"

# 如果所有变更均为非代码文件，直接跳过 just test 检查
all_changes_are_excluded() {
    local files
    files="$(git diff --cached --name-only)"
    if [ -z "$files" ]; then
        files="$(quality_working_file_paths)"
    fi
    if [ -z "$files" ]; then
        return 0
    fi
    while IFS= read -r file_path; do
        if [ -z "$file_path" ]; then
            continue
        fi
        if [[ ! "$file_path" =~ $QUALITY_TEST_EXCLUDED_FILE_PATTERN ]]; then
            return 1
        fi
    done <<< "$files"
    return 0
}

if all_changes_are_excluded; then
    echo "✅ 所有变更均为非代码文件，跳过 just test 检查。"
    exit 0
fi

git_dir="$(quality_git_dir)"
FLAG_FILE="$git_dir/.last_tested_commit"

if [ ! -f "$FLAG_FILE" ]; then
    echo "❌ 当前代码尚未执行过 just test。请先运行 just test 后再提交。"
    echo "   (如需跳过检查: git commit --no-verify)"
    exit 1
fi

current_branch="$(quality_branch_name)"
current_head="$(quality_head_hash)"
# 计算当前相关文件的有效 tree，与 just test 时的过滤规则保持一致。
# commit 阶段优先检查 staged tree；manual just lint 无 staged 文件时回退到
# working tree，这样刚运行过 just test 的本地工作区也能通过 lint。
# 排除文档、图片等不进入 test/lint 的文件类型。
staged_files="$(git diff --cached --name-only)"
if [ -n "$staged_files" ]; then
    current_tree="$(quality_effective_tree staged test)"
else
    current_tree="$(quality_effective_tree working test)"
fi

flag_branch="$(sed -n '1p' "$FLAG_FILE")"
flag_head="$(sed -n '2p' "$FLAG_FILE")"
flag_tree="$(sed -n '3p' "$FLAG_FILE")"

if [ "$current_branch" != "$flag_branch" ] || [ "$current_head" != "$flag_head" ] || [ "$current_tree" != "$flag_tree" ]; then
    echo "❌ just test 标记已过期。分支、HEAD 或提交内容已变更，请重新运行 just test。"
    echo "   当前: $current_branch @ ${current_head:0:8} (tree: ${current_tree:0:8})"
    echo "   标记: $flag_branch @ ${flag_head:0:8} (tree: ${flag_tree:0:8})"
    exit 1
fi

echo "✅ just test 标记有效 (${current_branch} @ ${current_head:0:8})，允许提交。"
