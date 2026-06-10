"""Agent Runner 事后监督循环。

本模块包含 PR 发布后的监督逻辑，用于在 Draft PR 创建后检查代码质量、
请求修复、执行 rebase 等操作。

主要函数：
- `_run_supervisor_with_repair_loop`: 主监督循环入口
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    PullRequestContext,
)
from backend.core.use_cases.pr_supervisor import (
    build_rebase_repair_complete_comment,
    build_rework_intent_comment,
    build_supervisor_result_comment,
    execute_rebase,
    execute_repair,
    run_post_pr_supervisor_cycle,
)
from backend.core.use_cases.agent_runner_git import has_changes
from backend.core.use_cases.run_agent_once import (
    get_head_sha,
)

_logger = logging.getLogger(__name__)


def _run_supervisor_with_repair_loop(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    pr_context: PullRequestContext,
    supervisor_agent: str,
) -> None:
    """运行 PR 事后监督修复循环。

    在 Draft PR 创建后运行，监督者（也是 AI Agent）检查 PR 内容，
    决定：
    - approve_for_human_review: 批准进入人工 review
    - request_human_input: 请求人工输入
    - repair_pr_branch: 执行代码修复
    - resolve_conflict: 解决冲突
    - rebase_pr_branch: 执行 rebase
    - mark_failed: 标记失败

    修复/rebase 操作受 max_repair_attempts 限制。

    Args:
        issue: Issue 对象
        worktree_path: worktree 目录
        config: 应用配置
        github_client: GitHub 客户端
        process_runner: 进程运行器
        pr_context: PR 上下文（URL、分支、SHA）
        supervisor_agent: 监督者 Agent
    """
    max_repair = max(0, config.post_pr_supervisor.max_repair_attempts)
    current_pr_context = pr_context

    # 循环：最多 max_repair + 1 次修复尝试
    for cycle in range(1, max_repair + 2):
        # 只读 supervisor cycle 前必须确认 worktree 干净；
        # 若不干净，视为协议违规并阻止继续。
        if has_changes(worktree_path, process_runner):
            github_client.comment_issue(
                issue.number,
                build_supervisor_result_comment(
                    action="dirty_worktree_before_supervisor",
                    supervisor=supervisor_agent,
                    summary="Worktree has uncommitted changes before read-only supervisor cycle. Moving to blocked.",
                    findings_counts={},
                    verification_status="",
                    head_sha=current_pr_context.head_sha,
                    cycle=cycle,
                ),
            )
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.blocked],
                remove=[config.labels.supervising],
            )
            return

        action_result = run_post_pr_supervisor_cycle(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            github_client=github_client,
            process_runner=process_runner,
            pr_context=current_pr_context,
            supervisor_agent=supervisor_agent,
            cycle=cycle,
        )

        # 分支：根据监督者动作决定后续行为
        if action_result.action == "approve_for_human_review":
            # 只读 supervisor cycle 后若留下未提交变更，不能 approve 进入 human review。
            if has_changes(worktree_path, process_runner):
                github_client.comment_issue(
                    issue.number,
                    build_supervisor_result_comment(
                        action="dirty_read_only_supervisor",
                        supervisor=supervisor_agent,
                        summary="Read-only supervisor left uncommitted changes. Moving to blocked.",
                        findings_counts={},
                        verification_status="",
                        head_sha=current_pr_context.head_sha,
                        cycle=cycle,
                    ),
                )
                github_client.edit_issue_labels(
                    issue.number,
                    add=[config.labels.blocked],
                    remove=[config.labels.supervising],
                )
                return
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.review],
                remove=[config.labels.supervising],
            )
            return

        if action_result.action in ("request_human_input",):
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.blocked],
                remove=[config.labels.supervising],
            )
            return

        if action_result.action == "mark_failed":
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.failed],
                remove=[config.labels.supervising],
            )
            return

        # 修复/冲突解决
        if action_result.action in ("repair_pr_branch", "resolve_conflict"):
            if cycle > max_repair:
                # 超过最大修复次数，标记为 blocked
                github_client.comment_issue(
                    issue.number,
                    build_supervisor_result_comment(
                        action="max_repair_exceeded",
                        supervisor=supervisor_agent,
                        summary="Max repair attempts exceeded; moving to blocked.",
                        findings_counts={},
                        verification_status="",
                        head_sha=current_pr_context.head_sha,
                        cycle=cycle,
                    ),
                )
                github_client.edit_issue_labels(
                    issue.number,
                    add=[config.labels.blocked],
                    remove=[config.labels.supervising],
                )
                return

            github_client.comment_issue(
                issue.number,
                build_rework_intent_comment(
                    action=action_result.action,
                    pr_branch=current_pr_context.branch,
                    head_sha=current_pr_context.head_sha,
                ),
            )
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.running],
                remove=[config.labels.supervising],
            )
            verification_results = execute_repair(
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                process_runner=process_runner,
                pr_branch=current_pr_context.branch,
                expected_head=current_pr_context.head_sha,
                supervisor_agent=supervisor_agent,
            )
            repair_sha = get_head_sha(worktree_path, process_runner)
            github_client.comment_issue(
                issue.number,
                build_rebase_repair_complete_comment(
                    action=action_result.action,
                    head_sha=repair_sha,
                    verification_passed=all(
                        result.return_code == 0 for result in verification_results
                    ),
                ),
            )
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.supervising],
                remove=[config.labels.running],
            )
            # 更新完整 PR 上下文后再继续循环，避免未知 mergeability 被批准。
            refreshed_pr_context = github_client.get_pull_request_context(
                current_pr_context.branch
            )
            if refreshed_pr_context is None:
                _logger.warning(
                    "Deferring post-repair supervisor for Issue #%d branch %s: "
                    "complete PR context is unavailable.",
                    issue.number,
                    current_pr_context.branch,
                )
                return
            current_pr_context = refreshed_pr_context
            continue

        # Rebase
        if action_result.action == "rebase_pr_branch":
            if cycle > max_repair:
                github_client.comment_issue(
                    issue.number,
                    build_supervisor_result_comment(
                        action="max_rebase_exceeded",
                        supervisor=supervisor_agent,
                        summary="Max rebase attempts exceeded; moving to blocked.",
                        findings_counts={},
                        verification_status="",
                        head_sha=current_pr_context.head_sha,
                        cycle=cycle,
                    ),
                )
                github_client.edit_issue_labels(
                    issue.number,
                    add=[config.labels.blocked],
                    remove=[config.labels.supervising],
                )
                return

            github_client.comment_issue(
                issue.number,
                build_rework_intent_comment(
                    action=action_result.action,
                    pr_branch=current_pr_context.branch,
                    head_sha=current_pr_context.head_sha,
                ),
            )
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.running],
                remove=[config.labels.supervising],
            )
            verification_results = execute_rebase(
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                process_runner=process_runner,
                pr_branch=current_pr_context.branch,
                expected_head=current_pr_context.head_sha,
                supervisor_agent=supervisor_agent,
            )
            rebase_sha = get_head_sha(worktree_path, process_runner)
            github_client.comment_issue(
                issue.number,
                build_rebase_repair_complete_comment(
                    action=action_result.action,
                    head_sha=rebase_sha,
                    verification_passed=all(
                        result.return_code == 0 for result in verification_results
                    ),
                ),
            )
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.supervising],
                remove=[config.labels.running],
            )
            refreshed_pr_context = github_client.get_pull_request_context(
                current_pr_context.branch
            )
            if refreshed_pr_context is None:
                _logger.warning(
                    "Deferring post-rebase supervisor for Issue #%d branch %s: "
                    "complete PR context is unavailable.",
                    issue.number,
                    current_pr_context.branch,
                )
                return
            current_pr_context = refreshed_pr_context
            continue

        # 未知动作：标记为 blocked
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.blocked],
            remove=[config.labels.supervising],
        )
        return

    # 循环耗尽仍未批准：标记为 blocked
    github_client.edit_issue_labels(
        issue.number,
        add=[config.labels.blocked],
        remove=[config.labels.supervising],
    )
