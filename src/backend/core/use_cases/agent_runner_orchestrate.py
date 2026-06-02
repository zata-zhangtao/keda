"""Agent runner orchestration — high-level issue processing flow.

本模块是 Agent Runner 的核心编排层，负责管理 Issue 的完整生命周期流程：

1. **轮询发现** — 从 GitHub 发现 ready/running 状态的 Issue
2. **工作树准备** — 为每个 Issue 创建或复用 git worktree
3. **Agent 执行** — 调用 AI Agent 实现 Issue（可选，视恢复路径而定）
4. **代码评审** — 在 push 前运行 pre-push review
5. **发布** — 将代码推送到远程并创建 Draft PR
6. **事后监督** — 可选的 PR 后监督循环（修复冲突、重新构建等）

Issue 有三条处理路径：
- `_process_ready_issue`: 新 Issue → 完整 Agent 执行 → 评审 → 发布
- `_process_running_rework`: 已运行 Issue → 检测到 rework 标记 → 执行修复 → 评审
- `_process_running_publish_recovery`: 已运行 Issue → 有本地 commit → 直接评审 → 发布
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    ReviewEventMarker,
)
from backend.core.use_cases.agent_runner_events import (
    parse_latest_event_marker,
)
from backend.core.use_cases.agent_runner_git import has_changes
from backend.core.use_cases.agent_runner_publication import (
    _finish_existing_commit_publication,
    _finish_implementation_publication,
    _reuse_existing_local_commit,
)
from backend.core.use_cases.agent_runner_supervisor import (
    _run_supervisor_with_repair_loop,
)
from backend.core.use_cases.pr_supervisor import (
    build_rework_intent_comment,
    execute_rebase,
    execute_repair,
)
from backend.core.use_cases.run_agent_once import (
    choose_agent,
    create_or_reuse_worktree,
    format_command,
    get_current_branch,
    get_head_sha,
)

_logger = logging.getLogger(__name__)


def _has_rework_intent(
    issue: IssueSummary,
    github_client: IGitHubClient,
) -> tuple[bool, ReviewEventMarker | None]:
    """检测 Issue 是否包含事后修复请求标记。

    通过解析 Issue 的评论列表，查找 post_pr_rework_requested 事件标记。
    该标记在监督者请求修复时由监督循环写入。

    Args:
        issue: Issue 对象
        github_client: GitHub 客户端

    Returns:
        (是否存在 rework 意图, 事件标记对象)
    """
    comments = github_client.list_issue_comments(issue.number)
    marker = parse_latest_event_marker(comments)
    if marker is not None and marker.phase == "post_pr_rework_requested":
        return True, marker
    return False, None


def _guard_running_issue_is_rework(
    issue: IssueSummary,
    config: AppConfig,
    github_client: IGitHubClient,
) -> tuple[bool, ReviewEventMarker | None]:
    """判断一个 running 状态的 Issue 是否符合 rework 资格。

    资格条件：
    1. 有 post_pr_rework_requested 事件标记
    2. 标记中包含有效的 PR 分支名
    3. 该分支在 GitHub 上存在对应的 open PR

    Args:
        issue: Issue 对象
        config: 应用配置
        github_client: GitHub 客户端

    Returns:
        (是否符合 rework 资格, 事件标记对象)
    """
    has_rework, marker = _has_rework_intent(issue, github_client)
    if not has_rework or marker is None:
        return False, None
    pr_branch = marker.pr_branch
    if pr_branch is None:
        return False, None
    pr_url = github_client.find_open_pr_by_head(pr_branch)
    if pr_url is None:
        return False, None
    return True, marker


def _find_worktree_path_for_issue(
    repo_path: Path,
    issue: IssueSummary,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> Path:
    """根据 Issue 编号查找对应的 worktree 目录路径。

    通过执行配置的 path_command 获取 worktree 路径。
    path_command 通常是查找包含 issue 编号的 worktree 目录的脚本。

    Args:
        repo_path: 仓库根目录
        issue: Issue 对象
        config: 应用配置
        process_runner: 进程运行器

    Returns:
        worktree 的绝对路径
    """
    path_result = process_runner.run(
        format_command(config.worktree.path_command, issue_number=issue.number),
        cwd=repo_path,
    )
    return Path(path_result.stdout.strip()).resolve()


def _mark_issue_failed(
    *,
    issue: IssueSummary,
    config: AppConfig,
    github_client: IGitHubClient,
    exc: Exception,
) -> None:
    """将 Issue 标记为失败状态。

    最佳努力（best-effort）报告：即使标签或评论写入失败，
    也保留原始异常，不吞没错误。

    Args:
        issue: Issue 对象
        config: 应用配置
        github_client: GitHub 客户端
        exc: 捕获的异常对象
    """
    # 延迟导入避免循环依赖
    from backend.core.use_cases.agent_runner_publication import (
        _workflow_state_labels,
    )

    try:
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.failed],
            remove=_workflow_state_labels(config),
        )
    except Exception as label_exc:  # noqa: BLE001 - preserve original failure.
        _logger.error(
            "Failed to mark Issue #%d as %s: %s",
            issue.number,
            config.labels.failed,
            label_exc,
        )

    from backend.core.use_cases.run_agent_once import (
        PublishFailureError,
        format_failure_comment,
        format_publish_failure_comment,
    )

    # 尝试从异常中提取尝试历史并格式化失败评论
    attempt_results = getattr(exc, "attempt_results", None)
    if isinstance(exc, PublishFailureError):
        comment_body = format_publish_failure_comment(
            exc,
            issue.number,
            worktree_path=exc.worktree_path,
            failure_category=exc.failure_category,
        )
    elif attempt_results is not None:
        comment_body = format_failure_comment(exc, attempt_results)
    else:
        comment_body = f"## Agent Runner Failed\n\n```text\n{exc}\n```\n"
    try:
        github_client.comment_issue(issue.number, comment_body)
    except Exception as comment_exc:  # noqa: BLE001 - preserve original failure.
        _logger.error(
            "Failed to comment on Issue #%d failure: %s",
            issue.number,
            comment_exc,
        )


def _has_existing_local_commit_ready_for_publish(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> bool:
    """检查 Issue 是否有可发布的本地提交。

    用于 running 状态 Issue 的发布恢复检测。
    轮询时发现 running Issue 会调用此函数判断是否可恢复发布。

    检测条件：
    1. 存在 worktree 目录
    2. 有超过 base 分支的提交
    3. 工作区干净（无未提交变更）

    Args:
        issue: Issue 对象
        repo_path: 仓库根目录
        config: 应用配置
        process_runner: 进程运行器

    Returns:
        是否有可发布的本地 commit
    """
    try:
        worktree_path = _find_worktree_path_for_issue(
            repo_path, issue, config, process_runner
        )
        from backend.core.use_cases.agent_runner_publication import (
            _count_local_commits_since_base,
        )

        return _count_local_commits_since_base(
            worktree_path, config, process_runner
        ) > 0 and not has_changes(worktree_path, process_runner)
    except Exception as exc:  # noqa: BLE001 - candidate probing must not fail polling.
        _logger.info(
            "Skipping existing local commit probe for Issue #%d: %s",
            issue.number,
            exc,
        )
        return False


def _process_ready_issue(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None = None,
) -> None:
    """处理 ready 状态的 Issue（完整实现路径）。

    ready Issue 是新 claim 的 Issue，需要完整处理：
    1. 标记为 running 并评论声明
    2. 创建或复用 worktree
    3. 检查是否有已存在的本地 commit（恢复路径）
    4. 如无本地 commit 则运行 Agent 实现
    5. 完成发布流程

    Args:
        issue: Issue 对象
        repo_path: 仓库根目录
        config: 应用配置
        agent: Agent 覆盖（auto/codex/claude）
        github_client: GitHub 客户端
        process_runner: 进程运行器
        content_generator: 可选的 AI 内容生成器
    """
    from backend.core.use_cases.run_agent_once import (
        run_agent_until_committed,
    )

    selected_agent = choose_agent(issue, config, agent)

    # 步骤 1: 声明 Issue
    github_client.edit_issue_labels(
        issue.number, add=[config.labels.running], remove=[config.labels.ready]
    )
    github_client.comment_issue(
        issue.number,
        "## Agent Runner Claimed\n\n"
        f"- Host: `{socket.gethostname()}`\n"
        f"- Agent: `{selected_agent}`\n",
    )

    # 步骤 2: 准备 worktree
    worktree_path = create_or_reuse_worktree(repo_path, issue, config, process_runner)
    before_sha = get_head_sha(worktree_path, process_runner)
    expected_branch = get_current_branch(worktree_path, process_runner)

    # 步骤 3: 检查恢复路径
    commit_result = _reuse_existing_local_commit(
        issue, worktree_path, config, process_runner
    )
    if commit_result is not None:
        # 有已存在的本地 commit → 恢复路径
        _finish_existing_commit_publication(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            selected_agent=selected_agent,
            github_client=github_client,
            process_runner=process_runner,
            expected_branch=expected_branch,
            commit_result=commit_result,
            content_generator=content_generator,
        )
        return

    # 步骤 4: 无本地 commit → 完整 Agent 执行
    new_commit_result = run_agent_until_committed(
        selected_agent=selected_agent,
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        process_runner=process_runner,
        before_sha=before_sha,
        expected_branch=expected_branch,
    )

    # 步骤 5: 完成发布流程
    _finish_implementation_publication(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        selected_agent=selected_agent,
        github_client=github_client,
        process_runner=process_runner,
        expected_branch=expected_branch,
        commit_result=new_commit_result,
        content_generator=content_generator,
    )


def _process_running_rework(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    marker: ReviewEventMarker,
) -> None:
    """处理带 rework 标记的 running Issue。

    当 Issue 有 post_pr_rework_requested 事件标记时进入此路径。
    监督者之前已请求修复，现在执行修复操作。

    Args:
        issue: Issue 对象
        repo_path: 仓库根目录
        config: 应用配置
        agent: Agent 覆盖
        github_client: GitHub 客户端
        process_runner: 进程运行器
        marker: 事件标记（包含动作类型和分支信息）
    """
    pr_branch = marker.pr_branch
    if pr_branch is None:
        raise RuntimeError("Rework marker missing pr_branch")

    # 定位 worktree 并确认分支
    worktree_path = _find_worktree_path_for_issue(
        repo_path, issue, config, process_runner
    )
    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != pr_branch:
        raise RuntimeError(
            f"Rework aborted: on branch {current_branch}, expected {pr_branch}"
        )

    expected_head = marker.head_sha or get_head_sha(worktree_path, process_runner)
    action = marker.action or "repair_pr_branch"
    supervisor_agent = choose_agent(issue, config, agent)

    # 执行修复或 rebase
    if action == "rebase_pr_branch":
        execute_rebase(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=process_runner,
            pr_branch=pr_branch,
            expected_head=expected_head,
            supervisor_agent=supervisor_agent,
        )
        rebase_sha = get_head_sha(worktree_path, process_runner)
        github_client.comment_issue(
            issue.number,
            build_rework_intent_comment(
                action=action,
                pr_branch=pr_branch,
                head_sha=rebase_sha,
            ),
        )
    else:
        execute_repair(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=process_runner,
            pr_branch=pr_branch,
            expected_head=expected_head,
            supervisor_agent=supervisor_agent,
        )
        repair_sha = get_head_sha(worktree_path, process_runner)
        github_client.comment_issue(
            issue.number,
            build_rework_intent_comment(
                action=action,
                pr_branch=pr_branch,
                head_sha=repair_sha,
            ),
        )

    # 标记为 supervising 并获取 PR 上下文
    github_client.edit_issue_labels(
        issue.number,
        add=[config.labels.supervising],
        remove=[config.labels.running],
    )

    # 修复后再次运行监督循环
    if config.post_pr_supervisor.enabled:
        pr_context = github_client.get_pull_request_context(pr_branch)
        if pr_context is None:
            _logger.warning(
                "Deferring post-rework supervisor for Issue #%d branch %s: "
                "complete PR context is unavailable.",
                issue.number,
                pr_branch,
            )
            return
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
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.review],
            remove=[config.labels.running],
        )


def _process_running_publish_recovery(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None = None,
) -> None:
    """恢复 running Issue 的发布流程。

    用于 runner 重启后发现 running Issue 已有本地 commit 的情况。
    通过复用已有 commit 来完成发布，无需重新运行 Agent。

    Args:
        issue: Issue 对象
        repo_path: 仓库根目录
        config: 应用配置
        agent: Agent 覆盖
        github_client: GitHub 客户端
        process_runner: 进程运行器
        content_generator: 可选的 AI 内容生成器
    """
    selected_agent = choose_agent(issue, config, agent)

    # 定位 worktree 并确认分支
    worktree_path = _find_worktree_path_for_issue(
        repo_path, issue, config, process_runner
    )
    expected_branch = get_current_branch(worktree_path, process_runner)

    # 检查是否有可复用的本地 commit
    commit_result = _reuse_existing_local_commit(
        issue, worktree_path, config, process_runner
    )
    if commit_result is None:
        raise RuntimeError(
            f"Issue #{issue.number} has no clean local commit ready for publication."
        )

    # 完成发布流程（恢复路径）
    _finish_existing_commit_publication(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        selected_agent=selected_agent,
        github_client=github_client,
        process_runner=process_runner,
        expected_branch=expected_branch,
        commit_result=commit_result,
        content_generator=content_generator,
    )


def run_once(
    *,
    repo_path: Path,
    config: AppConfig,
    dry_run: bool,
    agent: str,
    max_issues: int,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None = None,
) -> int:
    """执行一次轮询处理。

    本函数是 Agent Runner 的入口点，在每次轮询间隔调用。
    发现并处理 ready 和 running 状态的 Issue。

    Issue 发现逻辑：
    1. 收集所有 ready 标签的 Issue（最多 max_issues 个）
    2. 对 remaining 配额，从 running 标签 Issue 中筛选候选：
       - 有 rework 标记 → running_rework
       - 有已就绪的本地 commit → running_publish_recovery
       - 否则跳过

    Args:
        repo_path: 目标仓库路径
        config: 应用配置
        dry_run: 若为 True，仅列出待处理 Issue 不实际处理
        agent: Agent 覆盖（auto/codex/claude）
        max_issues: 每次轮询最多处理的 Issue 数量
        github_client: GitHub 客户端
        process_runner: 进程运行器
        content_generator: 可选的 AI 内容生成器

    Returns:
        退出码（0 成功，1 有 Issue 处理失败）
    """
    from backend.core.use_cases.run_agent_once import run_preflight_checks

    # 前置检查
    if not dry_run:
        try:
            run_preflight_checks(repo_path, config, process_runner)
        except Exception as exc:  # noqa: BLE001 - report preflight failure cleanly.
            _logger.error("Agent runner preflight failed: %s", exc)
            return 1

    # 发现 ready Issue
    ready_issues = github_client.list_ready_issues(config.labels.ready, max_issues)
    processed_count = 0
    issues_to_process: list[tuple[IssueSummary, str]] = []

    for issue in ready_issues:
        issues_to_process.append((issue, "ready"))
        processed_count += 1

    # 发现 running Issue（使用剩余配额）
    remaining = max_issues - processed_count
    if remaining > 0:
        running_candidates = github_client.list_review_candidate_issues(
            [config.labels.running], remaining
        )
        for issue in running_candidates:
            is_rework, marker = _guard_running_issue_is_rework(
                issue, config, github_client
            )
            if is_rework and marker is not None:
                issues_to_process.append((issue, "running_rework"))
            elif _has_existing_local_commit_ready_for_publish(
                issue=issue,
                repo_path=repo_path,
                config=config,
                process_runner=process_runner,
            ):
                issues_to_process.append((issue, "running_publish_recovery"))
            else:
                _logger.info(
                    "Skipping Issue #%d with label %s: no rework marker, open PR, or clean local commit.",
                    issue.number,
                    config.labels.running,
                )

    if not issues_to_process:
        _logger.info(
            "No open Issues found with label %s or eligible running rework.",
            config.labels.ready,
        )
        return 0

    # 处理 Issue
    exit_code = 0
    for issue, issue_kind in issues_to_process:
        selected_agent = choose_agent(issue, config, agent)
        if dry_run:
            _logger.info(
                "DRY RUN: would process Issue #%d (%s) with %s: %s",
                issue.number,
                issue_kind,
                selected_agent,
                issue.title,
            )
            continue
        try:
            if issue_kind == "ready":
                _process_ready_issue(
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    agent=agent,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
                )
            elif issue_kind == "running_rework":
                _, marker = _guard_running_issue_is_rework(issue, config, github_client)
                if marker is None:
                    continue
                _process_running_rework(
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    agent=agent,
                    github_client=github_client,
                    process_runner=process_runner,
                    marker=marker,
                )
            else:
                _process_running_publish_recovery(
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    agent=agent,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
                )
            _logger.info("Completed Issue #%d: %s", issue.number, issue.title)
        except Exception as exc:  # noqa: BLE001 - report queue failures and continue.
            exit_code = 1
            _mark_issue_failed(
                issue=issue,
                config=config,
                github_client=github_client,
                exc=exc,
            )
            _logger.error("Failed Issue #%d: %s", issue.number, exc)
    return exit_code
