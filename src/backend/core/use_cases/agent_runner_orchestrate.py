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
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.interfaces.runner_console import IRunHistoryStore
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    ReviewEventMarker,
)
from backend.core.use_cases.agent_runner_dependencies import (
    clear_dependency_waiting,
    evaluate_dependencies,
    mark_dependency_waiting,
    parse_dependency_marker,
)
from backend.core.use_cases.agent_runner_events import (
    parse_latest_pending_rework_marker,
)
from backend.core.use_cases.agent_runner_blocked_claim import (
    BlockedWorktreeClaimedError,
    _acquire_blocked_claim_lock,
    _release_blocked_claim_lock,
)
from backend.core.use_cases.agent_runner_git import has_changes
from backend.core.use_cases.agent_runner_workflow import (
    claim_blocked_issue,
    find_latest_unconsumed_marker,
    transition_issue_workflow_state,
)
from backend.core.use_cases.agent_runner_publication import (
    _finish_existing_commit_publication,
    _finish_implementation_publication,
    _reuse_existing_local_commit,
)
from backend.core.use_cases.agent_runner_rework import build_missing_worktree_comment
from backend.core.use_cases.agent_runner_run_history import append_run_record
from backend.core.use_cases.agent_runner_supervisor import (
    _run_supervisor_with_repair_loop,
)
from backend.core.use_cases.agent_runner_validation import (
    process_validation_gate,
    publish_validation_evidence,
)
from backend.core.use_cases.pr_supervisor import (
    build_rebase_repair_complete_comment,
    execute_rebase,
    execute_repair,
)
from backend.core.use_cases.run_agent_once import (
    _ensure_worktree_branch,
    choose_agent,
    create_or_reuse_worktree,
    format_command,
    get_current_branch,
    get_head_sha,
)

_logger = logging.getLogger(__name__)

# Scan past dependency-blocked ready Issues without letting them consume the
# per-pass processing quota. ``max_issues`` still caps actual claims.
_READY_DISCOVERY_LIMIT = 100


def _has_rework_intent(
    issue: IssueSummary,
    github_client: IGitHubClient,
) -> tuple[bool, ReviewEventMarker | None]:
    """检测 Issue 是否包含事后修复请求标记。

    通过解析 Issue 的评论列表，查找尚未被完成事件消费的
    post_pr_rework_requested 事件标记。该标记在监督者请求修复时写入，
    后续 supervisor 观察类 marker 不能掩盖仍待执行的 repair/rebase。

    Args:
        issue: Issue 对象
        github_client: GitHub 客户端

    Returns:
        (是否存在 rework 意图, 事件标记对象)
    """
    comments = github_client.list_issue_comments(issue.number)
    marker = parse_latest_pending_rework_marker(comments)
    if marker is not None:
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
    4. 标记的 head_sha 与 open PR 当前 head 一致（避免修错 head）

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
    pr_context = github_client.get_pull_request_context(pr_branch)
    if pr_context is None:
        return False, None
    if marker.head_sha and marker.head_sha != pr_context.head_sha:
        _logger.warning(
            "Issue #%d rework marker head %s does not match open PR head %s; "
            "ignoring stale marker.",
            issue.number,
            marker.head_sha,
            pr_context.head_sha,
        )
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
    # path_command runs with cwd=repo_path, so a relative output must be
    # anchored there too — bare resolve() would anchor it to the daemon
    # process cwd instead.
    worktree_path_output = Path(path_result.stdout.strip())
    if not worktree_path_output.is_absolute():
        worktree_path_output = repo_path / worktree_path_output
    worktree_path = worktree_path_output.resolve()
    if not worktree_path.exists():
        raise FileNotFoundError(
            "worktree path does not exist (path_command output): "
            f"{worktree_path}. path_command return_code={path_result.return_code}, "
            f"stdout={path_result.stdout!r}."
        )
    return worktree_path


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
    try:
        transition_issue_workflow_state(
            github_client, issue.number, config, config.labels.failed
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
        comment_body = format_failure_comment(
            exc, attempt_results, issue_number=issue.number
        )
    else:
        comment_body = format_failure_comment(exc, issue_number=issue.number)
    try:
        github_client.comment_issue(issue.number, comment_body)
    except Exception as comment_exc:  # noqa: BLE001 - preserve original failure.
        _logger.error(
            "Failed to comment on Issue #%d failure: %s",
            issue.number,
            comment_exc,
        )


def _mark_issue_blocked(
    *,
    issue: IssueSummary,
    config: AppConfig,
    github_client: IGitHubClient,
    exc: Exception,
) -> None:
    """将 Issue 标记为 blocked 状态（forbidden path 拦截）。

    Args:
        issue: Issue 对象
        config: 应用配置
        github_client: GitHub 客户端
        exc: 捕获的异常对象
    """
    from backend.core.use_cases.agent_runner_failure import (
        ForbiddenBlockedError,
        format_blocked_failure_comment,
    )

    try:
        transition_issue_workflow_state(
            github_client, issue.number, config, config.labels.blocked
        )
    except Exception as label_exc:  # noqa: BLE001 - preserve original failure.
        _logger.error(
            "Failed to mark Issue #%d as %s: %s",
            issue.number,
            config.labels.blocked,
            label_exc,
        )

    attempt_results = getattr(exc, "attempt_results", None)
    if isinstance(exc, ForbiddenBlockedError) and attempt_results is not None:
        comment_body = format_blocked_failure_comment(
            exc, attempt_results, issue_number=issue.number
        )
    else:
        comment_body = format_blocked_failure_comment(exc, issue_number=issue.number)
    try:
        github_client.comment_issue(issue.number, comment_body)
    except Exception as comment_exc:  # noqa: BLE001 - preserve original failure.
        _logger.error(
            "Failed to comment on Issue #%d blocked: %s",
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


_BLOCKED_RESOLUTION_COMPLETION_PHASES = {
    "implementation_complete",
    "draft_pr_created",
    "publish_recovered",
    "rebase_repair_complete",
    "blocked_resolution_complete",
}


def _guard_blocked_issue_has_resolution(
    issue: IssueSummary,
    github_client: IGitHubClient,
) -> ReviewEventMarker | None:
    """检测 blocked Issue 是否包含未消费的 blocked_resolution_requested marker。

    Args:
        issue: Issue 对象
        github_client: GitHub 客户端

    Returns:
        未消费的 blocked_resolution marker，或 None
    """
    comments = github_client.list_issue_comments(issue.number)
    return find_latest_unconsumed_marker(
        comments,
        phase="blocked_resolution_requested",
        completion_phases=_BLOCKED_RESOLUTION_COMPLETION_PHASES,
    )


def _process_blocked_resolution(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None = None,
    marker: ReviewEventMarker,
) -> None:
    """处理带 blocked_resolution marker 的 blocked Issue。

    在现有 worktree 上发送 continuation prompt，让 Agent 继续完成剩余任务。

    Args:
        issue: Issue 对象
        repo_path: 仓库根目录
        config: 应用配置
        agent: Agent 覆盖
        github_client: GitHub 客户端
        process_runner: 进程运行器
        content_generator: 可选的 AI 内容生成器
        marker: blocked_resolution_requested 事件标记
    """
    from backend.core.use_cases.agent_runner_publish import validate_safe_changes
    from backend.core.use_cases.run_agent_once import (
        build_blocked_continuation_prompt,
        run_agent_until_committed,
    )

    selected_agent = choose_agent(issue, config, agent)

    # 定位 worktree 并确认分支
    worktree_path = _find_worktree_path_for_issue(
        repo_path, issue, config, process_runner
    )
    expected_branch = f"issue-{issue.number}"
    _ensure_worktree_branch(worktree_path, expected_branch, process_runner)
    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != expected_branch:
        raise RuntimeError(
            f"Blocked resolution aborted: on branch {current_branch}, "
            f"expected {expected_branch}"
        )

    # worktree 必须是 clean 的
    if has_changes(worktree_path, process_runner):
        raise RuntimeError(
            "Blocked resolution aborted: worktree has uncommitted changes. "
            "Please commit or stash them before continuing."
        )

    # 再次检查无 forbidden paths
    validate_safe_changes(worktree_path, config, process_runner)

    # 原子锁：防止多个 runner 同时处理同一个 blocked Issue 的 worktree
    lock_path = worktree_path / ".agent-runner" / "blocked-claim.lock"
    _acquire_blocked_claim_lock(lock_path, issue.number)
    try:
        # 构建并发送 continuation prompt
        continuation_prompt = build_blocked_continuation_prompt(
            issue, worktree_path, marker.blocked_paths
        )
        before_sha = get_head_sha(worktree_path, process_runner)

        commit_result = run_agent_until_committed(
            selected_agent=selected_agent,
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=process_runner,
            before_sha=before_sha,
            expected_branch=current_branch,
            prompt_override=continuation_prompt,
        )

        # 完成发布流程
        _finish_implementation_publication(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            selected_agent=selected_agent,
            github_client=github_client,
            process_runner=process_runner,
            expected_branch=current_branch,
            commit_result=commit_result,
            content_generator=content_generator,
        )
    finally:
        _release_blocked_claim_lock(lock_path)


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
    transition_issue_workflow_state(
        github_client, issue.number, config, config.labels.running
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

    # 定位 worktree；缺失时进入 blocked 并给出可操作的恢复说明。
    try:
        worktree_path = _find_worktree_path_for_issue(
            repo_path, issue, config, process_runner
        )
    except FileNotFoundError as exc:
        message = str(exc)
        prefix = "(path_command output): "
        suffix = ". path_command return_code="
        expected_path = message
        if prefix in message and suffix in message:
            expected_path = message.split(prefix, 1)[1].split(suffix, 1)[0]
        github_client.comment_issue(
            issue.number,
            build_missing_worktree_comment(
                issue=issue,
                pr_branch=pr_branch,
                expected_path=expected_path,
            ),
        )
        transition_issue_workflow_state(
            github_client, issue.number, config, config.labels.blocked
        )
        return

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
        verification_results = execute_rebase(
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
            build_rebase_repair_complete_comment(
                action=action,
                head_sha=rebase_sha,
                verification_passed=all(
                    result.return_code == 0 for result in verification_results
                ),
            ),
        )
    else:
        verification_results = execute_repair(
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
            build_rebase_repair_complete_comment(
                action=action,
                head_sha=repair_sha,
                verification_passed=all(
                    result.return_code == 0 for result in verification_results
                ),
            ),
        )

    # 修复后刷新验证证据：新 head 需要新证据与新一轮人工签收
    rework_pr_url = github_client.find_open_pr_by_head(pr_branch)
    if rework_pr_url is not None:
        try:
            publish_validation_evidence(
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                github_client=github_client,
                process_runner=process_runner,
                pr_url=rework_pr_url,
                head_sha=get_head_sha(worktree_path, process_runner),
            )
        except Exception as evidence_exc:  # noqa: BLE001 - refresh is best effort.
            _logger.warning(
                "Failed to refresh validation evidence for Issue #%d: %s",
                issue.number,
                evidence_exc,
            )

    # 标记为 supervising 并获取 PR 上下文
    transition_issue_workflow_state(
        github_client, issue.number, config, config.labels.supervising
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
        transition_issue_workflow_state(
            github_client, issue.number, config, config.labels.review
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
    expected_branch = f"issue-{issue.number}"
    _ensure_worktree_branch(worktree_path, expected_branch, process_runner)

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
    run_history_store: IRunHistoryStore | None = None,
    run_trigger: str = "cli_run",
    repo_id: str | None = None,
) -> int:
    """执行一次轮询处理。

    本函数是 Agent Runner 的入口点，在每次轮询间隔调用。
    发现并处理 ready 和 running 状态的 Issue。

    Issue 发现逻辑：
    1. 扫描 ready 标签的 Issue，跳过依赖未满足的条目后最多处理 max_issues 个
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
        run_history_store: 可选的运行历史旁路存储；为 ``None`` 时零行为变化
        run_trigger: 写入运行记录的触发来源（如 cli_run / console_daemon）
        repo_id: 写入运行记录的仓库 ID；缺省取 ``repo_path.name``

    Returns:
        退出码（0 成功，1 有 Issue 处理失败）
    """
    from backend.core.use_cases.run_agent_once import run_preflight_checks

    effective_repo_id = repo_id or repo_path.name

    # 前置检查
    if not dry_run:
        try:
            run_preflight_checks(repo_path, config, process_runner)
        except Exception as exc:  # noqa: BLE001 - report preflight failure cleanly.
            _logger.error("Agent runner preflight failed: %s", exc)
            return 1

    # Realistic Validation 软门禁：维护 review 阶段 Issue 的勾选状态
    # label、重置过期签收并清理已关闭 Issue 的证据分支。
    # 与 Issue 领取相互独立，失败不影响本轮处理。
    if not dry_run:
        try:
            process_validation_gate(
                repo_path=repo_path,
                config=config,
                github_client=github_client,
                process_runner=process_runner,
            )
        except Exception as gate_exc:  # noqa: BLE001 - gate must not break polling.
            _logger.error("Validation gate pass failed: %s", gate_exc)

    # 发现 ready Issue
    ready_discovery_limit = max(max_issues, _READY_DISCOVERY_LIMIT)
    ready_issues = github_client.list_ready_issues(
        config.labels.ready, ready_discovery_limit
    )
    processed_count = 0
    issues_to_process: list[tuple[IssueSummary, str]] = []

    for issue in ready_issues:
        if processed_count >= max_issues:
            break
        declaration = parse_dependency_marker(issue.body)
        if declaration is not None:
            verdict = evaluate_dependencies(declaration, github_client, config.labels)
            if not verdict.satisfied:
                mark_dependency_waiting(
                    issue=issue,
                    verdict=verdict,
                    github_client=github_client,
                    labels_config=config.labels,
                    dry_run=dry_run,
                )
                if dry_run:
                    _logger.info(
                        "DRY RUN: Issue #%d blocked by dependencies: %s",
                        issue.number,
                        ", ".join(
                            f"{b.blocker_type}:{b.target}({b.current_state})"
                            for b in verdict.blockers
                        ),
                    )
                continue
            clear_dependency_waiting(
                issue=issue,
                github_client=github_client,
                labels_config=config.labels,
                dry_run=dry_run,
            )
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

    # 发现 blocked Issue（使用剩余配额）
    remaining = max_issues - len(issues_to_process)
    if remaining > 0:
        blocked_candidates = github_client.list_review_candidate_issues(
            [config.labels.blocked], remaining
        )
        for issue in blocked_candidates:
            marker = _guard_blocked_issue_has_resolution(issue, github_client)
            if marker is not None:
                issues_to_process.append((issue, "blocked_resolution"))
            else:
                _logger.info(
                    "Skipping Issue #%d with label %s: no blocked_resolution_requested marker.",
                    issue.number,
                    config.labels.blocked,
                )

    if not issues_to_process:
        _logger.info(
            "No open Issues found with label %s, eligible running rework, or blocked resolution.",
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
            if issue_kind == "blocked_resolution":
                marker = _guard_blocked_issue_has_resolution(issue, github_client)
                if marker is None:
                    _logger.info(
                        "DRY RUN: Issue #%d blocked_resolution marker not found, skipping.",
                        issue.number,
                    )
                    continue
            continue
        from backend.core.use_cases.agent_runner_failure import ForbiddenBlockedError

        run_started_at = datetime.now(timezone.utc)
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
            elif issue_kind == "blocked_resolution":
                marker = _guard_blocked_issue_has_resolution(issue, github_client)
                if marker is None:
                    continue
                claimed = claim_blocked_issue(github_client, issue.number, config)
                if not claimed:
                    _logger.info(
                        "Issue #%d already claimed by another runner, skipping.",
                        issue.number,
                    )
                    continue
                _process_blocked_resolution(
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    agent=agent,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
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
            append_run_record(
                run_history_store=run_history_store,
                repo_id=effective_repo_id,
                repo_path=repo_path,
                issue=issue,
                trigger=run_trigger,
                agent=selected_agent,
                outcome="completed",
                error_summary=None,
                started_at=run_started_at,
            )
        except ForbiddenBlockedError as exc:
            exit_code = 1
            _mark_issue_blocked(
                issue=issue,
                config=config,
                github_client=github_client,
                exc=exc,
            )
            _logger.error("Blocked Issue #%d: %s", issue.number, exc)
            append_run_record(
                run_history_store=run_history_store,
                repo_id=effective_repo_id,
                repo_path=repo_path,
                issue=issue,
                trigger=run_trigger,
                agent=selected_agent,
                outcome="blocked",
                error_summary=str(exc),
                started_at=run_started_at,
            )
        except BlockedWorktreeClaimedError as exc:
            _logger.info(
                "Issue #%d worktree already claimed by another runner, skipping: %s",
                issue.number,
                exc,
            )
        except Exception as exc:  # noqa: BLE001 - report queue failures and continue.
            exit_code = 1
            _mark_issue_failed(
                issue=issue,
                config=config,
                github_client=github_client,
                exc=exc,
            )
            _logger.error("Failed Issue #%d: %s", issue.number, exc)
            append_run_record(
                run_history_store=run_history_store,
                repo_id=effective_repo_id,
                repo_path=repo_path,
                issue=issue,
                trigger=run_trigger,
                agent=selected_agent,
                outcome="failed",
                error_summary=str(exc),
                started_at=run_started_at,
            )
    return exit_code
