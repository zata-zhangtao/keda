"""Agent Runner 发布流程。

本模块包含 Issue 实现完成后的发布逻辑，包括：
- 评论构建函数
- 发布完成处理（新实现路径和恢复路径）
- 本地 commit 复用逻辑

主要函数：
- `build_implementation_complete_comment`: 构建实现完成评论
- `build_draft_pr_created_comment`: 构建 Draft PR 创建评论
- `_reuse_existing_local_commit`: 复用已有的本地 commit
- `_finish_implementation_publication`: 完成新实现的发布流程
- `_finish_existing_commit_publication`: 完成已存在 commit 的恢复发布流程
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    AgentCommitResult,
    AppConfig,
    AttemptResult,
    CommandResult,
    FailureType,
    IssueSummary,
    PublishFailureCategory,
)
from backend.core.use_cases.agent_review import run_pre_push_review
from backend.core.use_cases.agent_runner_events import format_event_marker
from backend.core.use_cases.agent_runner_failure import PublishFailureError
from backend.core.use_cases.run_agent_once import (
    ensure_prd_delivery_ready,
    ensure_verification_passed,
    format_attempt_history,
    get_head_sha,
    has_changes,
    publish_changes,
    run_verification,
)

_logger = logging.getLogger(__name__)


def build_implementation_complete_comment(
    *,
    agent: str,
    branch: str,
    head_sha: str,
    verification_results: list[CommandResult],
    attempt_results: list[AttemptResult] | None = None,
) -> str:
    """构建 Agent 实现完成后的 Issue 评论。

    Args:
        agent: 执行的 AI Agent 名称（codex/claude/auto）
        branch: 功能分支名
        head_sha: 实现完成后的 HEAD commit SHA
        verification_results: 验证命令的执行结果列表
        attempt_results: Agent 尝试历史（用于恢复路径显示）

    Returns:
        格式化的 Markdown 评论文本，包含事件标记和验证结果摘要
    """

    marker = format_event_marker(
        phase="implementation_complete",
        cycle=1,
        head_sha=head_sha,
    )
    verification_lines = "\n".join(
        f"- `{' '.join(result.command)}`: exit {result.return_code}"
        for result in verification_results
    )
    lines = [
        marker,
        "",
        "## Agent Runner Implementation Complete",
        "",
        f"- Agent: `{agent}`",
        f"- Branch: `{branch}`",
        f"- Head SHA: `{head_sha}`",
        "",
        "Verification:",
        verification_lines,
    ]
    if attempt_results:
        lines.append("")
        lines.append(format_attempt_history(attempt_results))
    return "\n".join(lines)


def build_draft_pr_created_comment(
    *,
    pr_url: str,
    branch: str,
    head_sha: str,
) -> str:
    """构建 Draft PR 创建后的 Issue 评论。

    Args:
        pr_url: 生成的 Draft PR 链接
        branch: 功能分支名
        head_sha: PR 头部的 commit SHA

    Returns:
        格式化的 Markdown 评论文本
    """
    marker = format_event_marker(
        phase="draft_pr_created",
        cycle=1,
        head_sha=head_sha,
        pr_branch=branch,
    )
    return "\n".join(
        [
            marker,
            "",
            "## Agent Runner Draft PR Created",
            "",
            f"- Branch: `{branch}`",
            f"- Draft PR: {pr_url}",
            f"- Head SHA: `{head_sha}`",
        ]
    )


def _workflow_state_labels(config: AppConfig) -> list[str]:
    """获取工作流状态标签列表。

    这些标签代表 Issue 的生命周期状态，发布时会从这些标签中移除。
    注意：不包含 agent routing 标签（如 assigned）。

    Args:
        config: 应用配置

    Returns:
        工作流状态标签列表
    """
    return [
        config.labels.ready,
        config.labels.running,
        config.labels.supervising,
        config.labels.review,
        config.labels.blocked,
    ]


def _publish_changes_with_recovery_context(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    expected_branch: str,
    content_generator: IContentGenerator | None,
) -> tuple[str, str]:
    """Publish changes and preserve recovery context on publish failures."""
    try:
        return publish_changes(
            issue,
            worktree_path,
            config,
            github_client,
            process_runner,
            expected_branch=expected_branch,
            content_generator=content_generator,
        )
    except (RuntimeError, OSError) as exc:
        raise PublishFailureError(
            str(exc),
            worktree_path=worktree_path,
            failure_category=PublishFailureCategory.PUSH,
        ) from exc


def _edit_issue_labels_after_publish(
    *,
    issue_number: int,
    add_labels: list[str],
    remove_labels: list[str],
    worktree_path: Path,
    github_client: IGitHubClient,
) -> None:
    """Update labels and preserve recovery context on GitHub failures."""
    try:
        github_client.edit_issue_labels(
            issue_number,
            add=add_labels,
            remove=remove_labels,
        )
    except Exception as exc:  # noqa: BLE001 - surface category for recovery.
        raise PublishFailureError(
            f"Failed to update labels after publish: {exc}",
            worktree_path=worktree_path,
            failure_category=PublishFailureCategory.LABEL_UPDATE,
        ) from exc


def _comment_issue_after_publish(
    *,
    issue_number: int,
    comment_body: str,
    worktree_path: Path,
    github_client: IGitHubClient,
) -> None:
    """Post a publish comment and preserve recovery context on GitHub failures."""
    try:
        github_client.comment_issue(issue_number, comment_body)
    except Exception as exc:  # noqa: BLE001 - surface category for recovery.
        raise PublishFailureError(
            f"Failed to post draft PR comment: {exc}",
            worktree_path=worktree_path,
            failure_category=PublishFailureCategory.COMMENT_UPDATE,
        ) from exc


def _count_local_commits_since_base(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> int:
    """计算 worktree 相对于远程 base 分支的本地提交数量。

    使用 git rev-list --count 计算 base..HEAD 之间的提交数。

    Args:
        worktree_path: worktree 目录
        config: 应用配置
        process_runner: 进程运行器

    Returns:
        本地提交数量
    """
    base_ref_name = f"{config.git.remote}/{config.git.base_branch}"
    ahead_result = process_runner.run(
        ["git", "rev-list", "--count", f"{base_ref_name}..HEAD"],
        cwd=worktree_path,
        check=False,
    )
    if ahead_result.return_code != 0:
        return 0
    try:
        return int(ahead_result.stdout.strip() or "0")
    except ValueError:
        return 0


def _reuse_existing_local_commit(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> AgentCommitResult | None:
    """检查并复用 worktree 中已有的本地提交。

    用于恢复路径：当 worktree 已存在干净的本地 commit 时，
    直接复用而不重新调用 Agent。

    复用条件：
    1. 有超过 base 分支的本地提交
    2. 工作区没有未提交的变更
    3. 验证通过
    4. PRD 交付物就绪

    Args:
        issue: Issue 对象
        worktree_path: worktree 目录
        config: 应用配置
        process_runner: 进程运行器

    Returns:
        包含验证结果的 AgentCommitResult，或 None（不满足复用条件）
    """
    local_commit_count = _count_local_commits_since_base(
        worktree_path, config, process_runner
    )
    if local_commit_count <= 0 or has_changes(worktree_path, process_runner):
        return None

    # 验证步骤：运行配置的验证命令
    verification_results = run_verification(worktree_path, config, process_runner)
    ensure_verification_passed(verification_results)

    # PRD 交付物检查：确保必要文件存在
    ensure_prd_delivery_ready(issue, worktree_path, process_runner)

    # 二次检查：验证后可能有新变更
    if has_changes(worktree_path, process_runner):
        return None

    base_ref_name = f"{config.git.remote}/{config.git.base_branch}"
    head_sha = get_head_sha(worktree_path, process_runner)
    _logger.info(
        "Reusing %d existing local commit(s) for Issue #%d at %s.",
        local_commit_count,
        issue.number,
        head_sha,
    )
    return AgentCommitResult(
        verification_results=verification_results,
        attempt_results=[
            AttemptResult(
                attempt_number=1,
                failure_type=FailureType.SUCCESS,
                recovered=True,
                detail=(
                    f"Reused {local_commit_count} existing local commit(s) "
                    f"already ahead of {base_ref_name}; agent was not invoked."
                ),
            )
        ],
    )


def _finish_implementation_publication(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    selected_agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    expected_branch: str,
    commit_result: AgentCommitResult,
    content_generator: IContentGenerator | None = None,
) -> None:
    """完成新实现的发布流程（完整路径）。

    处理流程：
    1. 评论实现完成信息和验证结果
    2. 运行 pre-push code review（可修改代码）
    3. 发布 changes 到远程并创建 Draft PR
    4. 启动 PR 后监督循环（或直接进入 review 标签）

    Args:
        issue: Issue 对象
        worktree_path: worktree 目录
        config: 应用配置
        selected_agent: 选定的 AI Agent
        github_client: GitHub 客户端
        process_runner: 进程运行器
        expected_branch: 预期的分支名
        commit_result: Agent 提交结果
        content_generator: 可选的 AI 内容生成器（用于 PR description）
    """
    # 导入监督循环（避免循环导入）
    from backend.core.use_cases.agent_runner_supervisor import (
        _run_supervisor_with_repair_loop,
    )

    verification_results = commit_result.verification_results
    after_sha = get_head_sha(worktree_path, process_runner)

    # 步骤 1: 评论实现完成信息
    github_client.comment_issue(
        issue.number,
        build_implementation_complete_comment(
            agent=selected_agent,
            branch=expected_branch,
            head_sha=after_sha,
            verification_results=verification_results,
            attempt_results=commit_result.attempt_results,
        ),
    )

    # 步骤 2: 运行 pre-push code review（可能修改代码并产生新 commit）
    final_sha, _final_verification_results = run_pre_push_review(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=github_client,
        process_runner=process_runner,
        selected_agent=selected_agent,
        head_sha_before=after_sha,
        expected_branch=expected_branch,
        verification_results=verification_results,
    )

    # 步骤 3: 发布到远程并创建 Draft PR
    branch, pr_url = _publish_changes_with_recovery_context(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=github_client,
        process_runner=process_runner,
        expected_branch=expected_branch,
        content_generator=content_generator,
    )

    # 切换标签：running → supervising
    _edit_issue_labels_after_publish(
        issue_number=issue.number,
        add_labels=[config.labels.supervising],
        remove_labels=[config.labels.running],
        worktree_path=worktree_path,
        github_client=github_client,
    )

    publish_sha = get_head_sha(worktree_path, process_runner)
    _comment_issue_after_publish(
        issue_number=issue.number,
        comment_body=build_draft_pr_created_comment(
            pr_url=pr_url,
            branch=branch,
            head_sha=publish_sha,
        ),
        worktree_path=worktree_path,
        github_client=github_client,
    )

    # 步骤 4: PR 后监督（可选）
    supervisor_config = config.post_pr_supervisor
    if supervisor_config.enabled:
        # 获取 PR 上下文（如果已存在）
        pr_context = github_client.get_pull_request_context(branch)
        if pr_context is None:
            _logger.warning(
                "Deferring post-PR supervisor for Issue #%d branch %s: "
                "complete PR context is unavailable.",
                issue.number,
                branch,
            )
        else:
            supervisor_agent = (
                selected_agent
                if supervisor_config.supervisor_agent == "auto"
                else supervisor_config.supervisor_agent
            )
            # 启动监督修复循环
            _run_supervisor_with_repair_loop(
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                github_client=github_client,
                process_runner=process_runner,
                pr_context=pr_context,
                supervisor_agent=supervisor_agent,
            )
    else:
        # 未启用监督时直接进入 review 标签
        _edit_issue_labels_after_publish(
            issue_number=issue.number,
            add_labels=[config.labels.review],
            remove_labels=[config.labels.supervising],
            worktree_path=worktree_path,
            github_client=github_client,
        )

    _logger.info(
        "Published Issue #%d from %s at %s after implementation head %s.",
        issue.number,
        branch,
        final_sha,
        after_sha,
    )


def _finish_existing_commit_publication(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    selected_agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    expected_branch: str,
    commit_result: AgentCommitResult,
    content_generator: IContentGenerator | None = None,
) -> None:
    """完成已存在本地 commit 的恢复发布流程。

    与 _finish_implementation_publication 类似，但不调用 Agent。
    用于 runner 重启后发现 worktree 已有干净的本地提交的情况。

    处理流程：
    1. 评论实现完成信息（标记为 recovered）
    2. 运行 pre-push code review（确保代码质量）
    3. 发布 changes 到远程并创建 Draft PR
    4. 启动 PR 后监督循环（或直接进入 review 标签）

    Args:
        issue: Issue 对象
        worktree_path: worktree 目录
        config: 应用配置
        selected_agent: 选定的 AI Agent
        github_client: GitHub 客户端
        process_runner: 进程运行器
        expected_branch: 预期的分支名
        commit_result: 已存在的提交结果
        content_generator: 可选的 AI 内容生成器
    """
    # 导入监督循环（避免循环导入）
    from backend.core.use_cases.agent_runner_supervisor import (
        _run_supervisor_with_repair_loop,
    )

    verification_results = commit_result.verification_results
    head_sha = get_head_sha(worktree_path, process_runner)

    # 步骤 1: 评论实现完成信息（attempt_results 会显示 recovered=True）
    github_client.comment_issue(
        issue.number,
        build_implementation_complete_comment(
            agent=selected_agent,
            branch=expected_branch,
            head_sha=head_sha,
            verification_results=verification_results,
            attempt_results=commit_result.attempt_results,
        ),
    )

    # 步骤 2: 运行 pre-push code review（重要：确保复用的代码也经过评审）
    final_sha, _final_verification_results = run_pre_push_review(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=github_client,
        process_runner=process_runner,
        selected_agent=selected_agent,
        head_sha_before=head_sha,
        expected_branch=expected_branch,
        verification_results=verification_results,
    )

    # 步骤 3: 发布到远程并创建 Draft PR
    branch, pr_url = _publish_changes_with_recovery_context(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=github_client,
        process_runner=process_runner,
        expected_branch=expected_branch,
        content_generator=content_generator,
    )
    publish_sha = get_head_sha(worktree_path, process_runner)

    # 切换标签：从 workflow state labels → supervising
    _edit_issue_labels_after_publish(
        issue_number=issue.number,
        add_labels=[config.labels.supervising],
        remove_labels=_workflow_state_labels(config),
        worktree_path=worktree_path,
        github_client=github_client,
    )
    _comment_issue_after_publish(
        issue_number=issue.number,
        comment_body=build_draft_pr_created_comment(
            pr_url=pr_url,
            branch=branch,
            head_sha=publish_sha,
        ),
        worktree_path=worktree_path,
        github_client=github_client,
    )

    # 步骤 4: PR 后监督（可选）
    supervisor_config = config.post_pr_supervisor
    if supervisor_config.enabled:
        pr_context = github_client.get_pull_request_context(branch)
        if pr_context is None:
            _logger.warning(
                "Deferring post-PR supervisor for Issue #%d branch %s: "
                "complete PR context is unavailable.",
                issue.number,
                branch,
            )
        else:
            supervisor_agent = (
                selected_agent
                if supervisor_config.supervisor_agent == "auto"
                else supervisor_config.supervisor_agent
            )
            _run_supervisor_with_repair_loop(
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                github_client=github_client,
                process_runner=process_runner,
                pr_context=pr_context,
                supervisor_agent=supervisor_agent,
            )
    else:
        _edit_issue_labels_after_publish(
            issue_number=issue.number,
            add_labels=[config.labels.review],
            remove_labels=[config.labels.supervising],
            worktree_path=worktree_path,
            github_client=github_client,
        )

    _logger.info(
        "Recovered publication for Issue #%d from %s at %s.",
        issue.number,
        branch,
        final_sha,
    )
