"""Agent runner orchestration — high-level issue processing flow.

本模块是 Agent Runner 的核心编排层，负责管理 Issue 的完整生命周期流程：

1. **轮询发现** — 从 GitHub 发现 ready/running 状态的 Issue
2. **工作树准备** — 为每个 Issue 创建或复用 git worktree
3. **Agent 执行** — 调用 AI Agent 实现 Issue（可选，视恢复路径而定）
4. **代码评审** — push 之后、PR 之前运行 pre-PR review
5. **发布** — 将代码推送到远程并创建 Draft PR
6. **事后监督** — 可选的 PR 后监督循环（修复冲突、重新构建等）

Issue 有三条处理路径：
- `_process_ready_issue`: 新 Issue → 完整 Agent 执行 → 评审 → 发布
- `_process_running_rework`: 已运行 Issue → 检测到 rework 标记 → 执行修复 → 评审
- `_process_running_publish_recovery`: 已运行 Issue → 有本地 commit → 直接评审 → 发布
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.interfaces.runner_console import (
    AttemptRecord,
    IRunHistoryStore,
)
from backend.core.shared.interfaces.runner_live_view import (
    IRunnerLiveView,
    NoOpRunnerLiveView,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    AttemptResult,
    CommandResult,
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
    worktree_claim_lock_path,
)
from backend.core.use_cases.agent_runner_git import (
    get_current_branch,
    has_changes,
)
from backend.core.use_cases.agent_runner_reclaim import format_claim_marker
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
from backend.core.use_cases.agent_runner_output_routing import (
    _OutputRoutedProcessRunner,
    issue_output_routing,
)
from backend.core.use_cases.agent_runner_run_history import append_run_record
from backend.core.use_cases.agent_runner_supervisor import (
    _run_supervisor_with_repair_loop,
)
from backend.core.use_cases.agent_runner_validation import (
    ValidationEvidenceError,
    process_validation_gate,
    publish_validation_evidence,
)
from backend.core.use_cases.pr_supervisor import (
    build_rebase_repair_complete_comment,
    execute_rebase,
    execute_repair,
)
from backend.core.use_cases.agent_runner_worktree_branch import (
    _ensure_worktree_branch,
)
from backend.core.use_cases.create_prd_from_issue import (
    CreatePrdFromIssueRequest,
    create_prd_from_issue,
)
from backend.core.use_cases.run_agent_once import (
    choose_agent,
    create_or_reuse_worktree,
    get_head_sha,
    resolve_agent_fallback_order,
)
from backend.core.use_cases.agent_runner_failure import (
    AgentUnavailableError,
    ForbiddenBlockedError,
    MaxRetriesExceededError,
    ProviderCapacityError,
    UnrecoverableError,
    format_attempt_history,
)
from backend.core.use_cases.agent_runner_failure_marking import (
    _mark_issue_blocked,
    _mark_issue_failed,
)
from backend.core.use_cases.agent_runner_worktree_probe import (
    _find_worktree_path_for_issue,
    _has_existing_local_commit_ready_for_publish,
    _worktree_needs_rebase_recovery,
)

_logger = logging.getLogger(__name__)

# Scan past dependency-blocked ready Issues without letting them consume the
# per-pass processing quota. ``max_issues`` still caps actual claims.
_READY_DISCOVERY_LIMIT = 100


def process_prd_rework_issues(
    *,
    repo_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None = None,
    max_issues: int = 1,
) -> None:
    """处理标记为 PRD rework 的 Issue。

    在正常的 ready Issue 执行之前调用：为每个 Issue 建/复用 ``issue-<N>``
    worktree，在 worktree 内生成或重写 PRD、commit 进 ``issue-<N>`` 分支并经
    draft PR 落地，随后更新 Issue body/labels/comments。主工作树保持干净。
    单个 Issue 失败时记录错误并继续处理后续 Issue，不让 PRD 生成阶段污染
    ready Issue 执行阶段。

    Args:
        repo_path: 目标仓库路径。
        config: 应用配置。
        github_client: GitHub 客户端。
        process_runner: git 命令执行器（建/复用 worktree、commit、push）。
        content_generator: 可选的 AI 内容生成器。
        max_issues: 本轮最多处理的 rework-prd Issue 数量。
    """
    issues = github_client.list_rework_prd_issues(config.labels.rework_prd, limit=max_issues)
    for issue in issues:
        _logger.info("Processing PRD rework for Issue #%d: %s", issue.number, issue.title)
        try:
            worktree_path = create_or_reuse_worktree(repo_path, issue, config, process_runner)
            create_prd_from_issue(
                request=CreatePrdFromIssueRequest(
                    repo_path=repo_path,
                    issue=issue,
                    config=config,
                    generated_content_config=config.generated_content,
                    content_generator=content_generator,
                    queue_ready=True,
                    worktree_path=worktree_path,
                    process_runner=process_runner,
                ),
                github_client=github_client,
            )
        except Exception as exc:  # noqa: BLE001 - isolate PRD rework failures.
            _logger.exception("PRD rework failed for Issue #%d", issue.number)
            try:
                github_client.edit_issue_labels(
                    issue.number,
                    add=[config.labels.failed],
                    remove=[config.labels.rework_prd],
                )
            except Exception as label_exc:  # noqa: BLE001 - best-effort label update.
                _logger.error(
                    "Failed to mark Issue #%d as %s: %s",
                    issue.number,
                    config.labels.failed,
                    label_exc,
                )
            try:
                github_client.comment_issue(
                    issue.number,
                    f"PRD generation failed: {exc}\n\n"
                    "Please review the error and re-add the "
                    f"`{config.labels.rework_prd}` label to retry.",
                )
            except Exception as comment_exc:  # noqa: BLE001 - best-effort comment.
                _logger.error(
                    "Failed to comment on Issue #%d PRD failure: %s",
                    issue.number,
                    comment_exc,
                )


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
    on_attempt_recorded: Callable[[AttemptResult, list[AttemptResult]], None] | None = None,
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
    worktree_path = _find_worktree_path_for_issue(repo_path, issue, config, process_runner)
    expected_branch = f"issue-{issue.number}"
    _ensure_worktree_branch(worktree_path, expected_branch, issue, config, process_runner)
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
    lock_path = worktree_claim_lock_path(worktree_path)
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
            on_attempt_recorded=on_attempt_recorded,
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
    on_attempt_recorded: Callable[[AttemptResult, list[AttemptResult]], None] | None = None,
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
        MaxRetriesExceededError,
        PrdDeliveryError,
        ProviderCapacityError,
        VerificationFailedError,
        build_progress_continuation_prompt,
        checkpoint_uncommitted_progress,
        run_agent_until_committed,
    )

    selected_agent = choose_agent(issue, config, agent)

    # 步骤 1: 声明 Issue
    transition_issue_workflow_state(github_client, issue.number, config, config.labels.running)
    claim_host = socket.gethostname()
    claim_pid = os.getpid()
    github_client.comment_issue(
        issue.number,
        "## Agent Runner Claimed\n\n"
        f"- Host: `{claim_host}`\n"
        f"- PID: `{claim_pid}`\n"
        f"- Agent: `{selected_agent}`\n\n"
        f"{format_claim_marker(claim_host, claim_pid)}",
    )

    # 步骤 2: 准备 worktree
    worktree_path = create_or_reuse_worktree(repo_path, issue, config, process_runner)
    before_sha = get_head_sha(worktree_path, process_runner)
    expected_branch = get_current_branch(worktree_path, process_runner)

    # 步骤 3: 检查恢复路径
    #
    # 已有本地提交分三种情况：
    # - 完全达到交付标准 → 直接发布，不调用 agent。
    # - 存在提交但门禁未过（上一次 claim 的 WIP checkpoint / 部分进度）→ 不硬失败，
    #   在已提交进度上重跑 agent 续作（continuation prompt）。
    # - 无本地提交 → 全新实现。
    continuation_prompt: str | None = None
    try:
        commit_result = _reuse_existing_local_commit(issue, worktree_path, config, process_runner)
    except (VerificationFailedError, PrdDeliveryError, ValidationEvidenceError) as exc:
        _logger.info(
            "Issue #%d has partial local commits not yet delivery-ready (%s); "
            "re-running agent to continue from committed progress.",
            issue.number,
            exc.__class__.__name__,
        )
        commit_result = None
        verification_results: list[CommandResult] | None = None
        failure_summary = str(exc)
        if isinstance(exc, VerificationFailedError):
            verification_results = exc.verification_results
        continuation_prompt = build_progress_continuation_prompt(
            issue,
            worktree_path,
            failure_summary=failure_summary,
            verification_results=verification_results,
        )

    if commit_result is not None:
        # 有已存在的本地 commit 且已达交付标准 → 恢复路径
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

    # 步骤 4: 无可发布的本地 commit → Agent 执行（首跑，或在 checkpoint 上续作）。
    #
    # 失败前把 agent 的在途进度提交成 WIP checkpoint，使其能被下一次 claim 复用、
    # 继续推进；否则体量较大的 PRD 会在每次 claim 从零开始、永远收敛不了。
    try:
        new_commit_result = run_agent_until_committed(
            selected_agent=selected_agent,
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=process_runner,
            before_sha=before_sha,
            expected_branch=expected_branch,
            prompt_override=continuation_prompt,
            on_attempt_recorded=on_attempt_recorded,
        )
    except (MaxRetriesExceededError, ProviderCapacityError, KeyboardInterrupt):
        # 切换 agent 前、或被 Ctrl-C / SIGINT 优雅打断时,先把在途进度 checkpoint：
        # 让 fallback 链上的下一个 agent、或重新 claim 时能在已提交进度上续作,而不是
        # 从零重来。KeyboardInterrupt 同样 checkpoint 后再抛出,让中断照常退出。
        # best-effort：checkpoint 自身异常不得掩盖原始失败/中断（禁改路径已被
        # checkpoint 内部隔离,不再整块放弃）。
        try:
            checkpoint_sha = checkpoint_uncommitted_progress(
                issue,
                worktree_path,
                config,
                process_runner,
                expected_branch=expected_branch,
            )
        except Exception as checkpoint_exc:  # noqa: BLE001 - 不能掩盖原始失败
            _logger.warning(
                "Failed to checkpoint in-progress work for Issue #%d: %s",
                issue.number,
                checkpoint_exc,
            )
        else:
            if checkpoint_sha is not None:
                _logger.info(
                    "Checkpointed in-progress work for Issue #%d at %s "
                    "for the next claim to continue.",
                    issue.number,
                    checkpoint_sha,
                )
        raise

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
    **kwargs: object,
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
        worktree_path = _find_worktree_path_for_issue(repo_path, issue, config, process_runner)
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
        transition_issue_workflow_state(github_client, issue.number, config, config.labels.blocked)
        return

    # worktree 可能因上一次 runner 在 rebase 中途中断而停在 detached HEAD；
    # 先治愈回目标分支再校验，避免对中断状态直接硬失败、把 Issue 打成 failed。
    _ensure_worktree_branch(worktree_path, pr_branch, issue, config, process_runner)

    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != pr_branch:
        raise RuntimeError(f"Rework aborted: on branch {current_branch}, expected {pr_branch}")

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
                verification_passed=all(result.return_code == 0 for result in verification_results),
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
                verification_passed=all(result.return_code == 0 for result in verification_results),
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
    transition_issue_workflow_state(github_client, issue.number, config, config.labels.supervising)

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
        transition_issue_workflow_state(github_client, issue.number, config, config.labels.review)


def _process_running_publish_recovery(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None = None,
    **kwargs: object,
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
    worktree_path = _find_worktree_path_for_issue(repo_path, issue, config, process_runner)
    expected_branch = f"issue-{issue.number}"

    # 原子锁：恢复路径会对 worktree 做 rebase 治愈与发布等写操作，必须与其他
    # runner（含 blocked 恢复）在同一 worktree 上互斥，否则并发 git 操作会互相
    # 破坏一个本就脆弱的 mid-rebase 工作区。锁被活进程持有时抛
    # BlockedWorktreeClaimedError，由 run_once 调度循环记日志后跳过。
    lock_path = worktree_claim_lock_path(worktree_path)
    _acquire_blocked_claim_lock(lock_path, issue.number)
    try:
        _ensure_worktree_branch(worktree_path, expected_branch, issue, config, process_runner)

        # 检查是否有可复用的本地 commit
        commit_result = _reuse_existing_local_commit(issue, worktree_path, config, process_runner)
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
    finally:
        _release_blocked_claim_lock(lock_path)


def _stamp_attempts_with_agent(
    attempts: list[AttemptResult],
    agent: str,
) -> list[AttemptResult]:
    """Return attempts labeled with the agent that produced them.

    Attempts recorded inside a single agent's run already carry the agent
    name; this helper remains for backward compatibility and cross-agent
    fallback merging. Attempts that already carry an agent label are left
    untouched.

    Args:
        attempts: Attempt history from one agent's run.
        agent: Agent name to stamp onto unlabeled attempts.

    Returns:
        A new list of attempts with the agent stamped.
    """
    return [attempt if attempt.agent else replace(attempt, agent=agent) for attempt in attempts]


_ATTEMPT_HISTORY_MARKER = "<!-- iar-attempt-history -->"
_ATTEMPT_HISTORY_TITLE = "### Attempt History"


def _build_attempt_history_comment(attempt_results: list[AttemptResult]) -> str:
    """Build a GitHub comment body that carries the attempt history table."""
    history_table = format_attempt_history(attempt_results)
    if not history_table:
        history_table = "_(No attempts recorded yet.)_"
    return "\n".join(
        [
            _ATTEMPT_HISTORY_MARKER,
            f"{_ATTEMPT_HISTORY_TITLE} (live)",
            "",
            history_table,
        ]
    )


def _persist_attempt_result(
    *,
    result: AttemptResult,
    attempt_results: list[AttemptResult],
    repo_id: str,
    issue_number: int,
    github_client: IGitHubClient,
    run_history_store: IRunHistoryStore | None,
) -> None:
    """Persist one attempt to SQLite and update the GitHub running comment.

    This is the incremental persistence callback wired into
    :func:`run_agent_until_committed`. Failures are logged and swallowed so the
    runner state machine is never blocked by the side-channel storage.
    """
    if run_history_store is not None:
        try:
            run_history_store.append_attempt(
                AttemptRecord(
                    repo_id=repo_id,
                    issue_number=issue_number,
                    agent=result.agent,
                    attempt_number=result.attempt_number,
                    failure_type=result.failure_type.value,
                    recovered=result.recovered,
                    detail=result.detail,
                    started_at=result.started_at,
                    finished_at=result.finished_at,
                    duration_seconds=result.duration_seconds,
                )
            )
        except Exception:  # noqa: BLE001 - side-channel must not break runs
            _logger.warning(
                "Failed to append attempt record for Issue #%d",
                issue_number,
                exc_info=True,
            )

    try:
        entries = github_client.list_issue_comment_entries(issue_number)
        comment_id: int | None = None
        for existing_id, body in entries:
            if _ATTEMPT_HISTORY_MARKER in body:
                comment_id = existing_id
                break
        comment_body = _build_attempt_history_comment(attempt_results)
        if comment_id is not None:
            github_client.edit_issue_comment(comment_id, comment_body)
        else:
            github_client.comment_issue(issue_number, comment_body)
    except Exception:  # noqa: BLE001 - side-channel must not break runs
        _logger.warning(
            "Failed to update GitHub attempt history for Issue #%d",
            issue_number,
            exc_info=True,
        )


def run_issue_with_agent_fallback(
    *,
    issue: IssueSummary,
    config: AppConfig,
    agent: str,
    process_for_agent: Callable[..., None],
    on_attempt_recorded: Callable[[AttemptResult, list[AttemptResult]], None] | None = None,
) -> str:
    """Process an Issue across the configured agent fallback chain.

    Level 2 of the escalation ladder. ``process_for_agent`` is invoked with a
    keyword ``agent`` argument for each candidate agent resolved by
    :func:`resolve_agent_fallback_order`, capped at ``max_agent_switches``
    switches. The chain advances to the next agent when an agent exhausts its
    recovery budget (:class:`MaxRetriesExceededError`) or hits a provider
    capacity limit (:class:`ProviderCapacityError`), and skips an agent whose
    CLI is unavailable (:class:`AgentUnavailableError`). Unrecoverable and
    forbidden-path failures are re-raised immediately because every agent would
    hit the same wall.

    When the chain is exhausted, the merged (agent-stamped) attempt history is
    raised as a :class:`MaxRetriesExceededError` so the failure comment shows
    every agent that was tried. With no fallback configured the chain contains
    only the primary agent, so behavior matches single-agent runs.

    Args:
        issue: Issue being processed.
        config: Agent Runner configuration.
        agent: The ``--agent`` override (``"auto"`` routes by label).
        process_for_agent: Callable accepting ``agent=<name>`` that runs the
            full implement → review → publish pipeline for one agent.

    Returns:
        The agent name that completed the Issue.

    Raises:
        UnrecoverableError: A security/branch violation that no agent can fix.
        ForbiddenBlockedError: Forbidden paths require human intervention.
        MaxRetriesExceededError: Every candidate agent failed.
        AgentUnavailableError: Every candidate agent's CLI was unavailable.
    """
    fallback_order = resolve_agent_fallback_order(issue, config, agent)
    max_switches = max(0, config.runner.max_agent_switches)
    candidate_agents = fallback_order[: max_switches + 1]
    combined_attempts: list[AttemptResult] = []
    last_switch_exc: Exception | None = None
    for candidate_index, candidate_agent in enumerate(candidate_agents):
        is_last_candidate = candidate_index == len(candidate_agents) - 1
        try:
            process_for_agent(
                agent=candidate_agent,
                on_attempt_recorded=on_attempt_recorded,
            )
            return candidate_agent
        except (UnrecoverableError, ForbiddenBlockedError):
            # Every agent would hit the same wall; do not switch.
            raise
        except AgentUnavailableError as exc:
            last_switch_exc = exc
            _logger.warning(
                "Issue #%d: agent '%s' is unavailable; trying next candidate.",
                issue.number,
                candidate_agent,
            )
            continue
        except (ProviderCapacityError, MaxRetriesExceededError) as exc:
            combined_attempts.extend(
                _stamp_attempts_with_agent(
                    getattr(exc, "attempt_results", None) or [],
                    candidate_agent,
                )
            )
            last_switch_exc = exc
            if is_last_candidate:
                break
            _logger.warning(
                "Issue #%d: agent '%s' failed with %s; switching to next agent.",
                issue.number,
                candidate_agent,
                type(exc).__name__,
            )
            continue

    if last_switch_exc is None:
        raise RuntimeError(f"No agent candidates available for Issue #{issue.number}.")
    if combined_attempts:
        # Carry the merged, agent-stamped attempt history while preserving the
        # last agent's root cause (e.g. the verification error) so the failure
        # comment still surfaces it instead of a duplicated wrapper message.
        raise MaxRetriesExceededError(combined_attempts) from last_switch_exc.__cause__
    raise last_switch_exc


_RUN_HISTORY_LOCK = threading.Lock()


def _append_run_record_locked(**kwargs: object) -> None:
    """Thread-safe ``append_run_record`` for the parallel processing path.

    Run-history storage (e.g. SQLite) is not safe for concurrent writers, so
    serialize appends. Uncontended in the default sequential path.
    """
    with _RUN_HISTORY_LOCK:
        append_run_record(**kwargs)


def _process_single_issue(
    issue: IssueSummary,
    issue_kind: str,
    *,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    content_generator: IContentGenerator | None,
    run_history_store: IRunHistoryStore | None,
    run_trigger: str,
    effective_repo_id: str,
    output_view: IRunnerLiveView,
) -> int:
    """Process one discovered Issue end-to-end.

    Extracted from :func:`run_once` so it can run either sequentially or inside
    a thread pool. All failures are caught and recorded here; the function never
    raises, so the caller treats the return value as this Issue's exit-code
    contribution.

    Returns:
        ``0`` on success or skip, ``1`` on a recorded failure/block.
    """
    selected_agent = choose_agent(issue, config, agent)
    output_view.register_issue(issue.number, selected_agent)
    run_started_at = datetime.now(timezone.utc)
    used_agent = selected_agent

    def _on_attempt_recorded(result: AttemptResult, attempt_results: list[AttemptResult]) -> None:
        _persist_attempt_result(
            result=result,
            attempt_results=attempt_results,
            repo_id=effective_repo_id,
            issue_number=issue.number,
            github_client=github_client,
            run_history_store=run_history_store,
        )

    try:
        if issue_kind == "ready":
            used_agent = run_issue_with_agent_fallback(
                issue=issue,
                config=config,
                agent=agent,
                process_for_agent=partial(
                    _process_ready_issue,
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
                ),
                on_attempt_recorded=_on_attempt_recorded,
            )
        elif issue_kind == "running_rework":
            _, marker = _guard_running_issue_is_rework(issue, config, github_client)
            if marker is None:
                output_view.update_status(issue.number, "skipped")
                return 0
            used_agent = run_issue_with_agent_fallback(
                issue=issue,
                config=config,
                agent=agent,
                process_for_agent=partial(
                    _process_running_rework,
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    github_client=github_client,
                    process_runner=process_runner,
                    marker=marker,
                ),
            )
        elif issue_kind == "blocked_resolution":
            marker = _guard_blocked_issue_has_resolution(issue, github_client)
            if marker is None:
                output_view.update_status(issue.number, "skipped")
                return 0
            claimed = claim_blocked_issue(github_client, issue.number, config)
            if not claimed:
                _logger.info(
                    "Issue #%d already claimed by another runner, skipping.",
                    issue.number,
                )
                output_view.update_status(issue.number, "skipped")
                return 0
            used_agent = run_issue_with_agent_fallback(
                issue=issue,
                config=config,
                agent=agent,
                process_for_agent=partial(
                    _process_blocked_resolution,
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
                    marker=marker,
                ),
                on_attempt_recorded=_on_attempt_recorded,
            )
        else:
            used_agent = run_issue_with_agent_fallback(
                issue=issue,
                config=config,
                agent=agent,
                process_for_agent=partial(
                    _process_running_publish_recovery,
                    issue=issue,
                    repo_path=repo_path,
                    config=config,
                    github_client=github_client,
                    process_runner=process_runner,
                    content_generator=content_generator,
                ),
            )
        _logger.info("Completed Issue #%d: %s", issue.number, issue.title)
        output_view.update_status(issue.number, "completed")
        _append_run_record_locked(
            run_history_store=run_history_store,
            repo_id=effective_repo_id,
            repo_path=repo_path,
            issue=issue,
            trigger=run_trigger,
            agent=used_agent,
            outcome="completed",
            error_summary=None,
            started_at=run_started_at,
        )
        return 0
    except ForbiddenBlockedError as exc:
        _mark_issue_blocked(
            issue=issue,
            config=config,
            github_client=github_client,
            exc=exc,
        )
        _logger.error("Blocked Issue #%d: %s", issue.number, exc)
        output_view.update_status(issue.number, "blocked")
        _append_run_record_locked(
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
        return 1
    except BlockedWorktreeClaimedError as exc:
        _logger.info(
            "Issue #%d worktree already claimed by another runner, skipping: %s",
            issue.number,
            exc,
        )
        output_view.update_status(issue.number, "skipped")
        return 0
    except Exception as exc:  # noqa: BLE001 - report queue failures and continue.
        _mark_issue_failed(
            issue=issue,
            config=config,
            github_client=github_client,
            exc=exc,
        )
        _logger.error("Failed Issue #%d: %s", issue.number, exc)
        output_view.update_status(issue.number, "failed")
        _append_run_record_locked(
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
        return 1


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
    concurrency: int = 1,
    output_view: IRunnerLiveView | None = None,
) -> int:
    """执行一次轮询处理。

    本函数是 Agent Runner 的入口点，在每次轮询间隔调用。
    发现并处理 ready 和 running 状态的 Issue。

    Issue 发现逻辑：
    1. 扫描 ready 标签的 Issue，跳过依赖未满足的条目后最多处理
       ``max(max_issues, concurrency)`` 个
    2. 对 remaining 配额，从 running 标签 Issue 中筛选候选：
       - 有 rework 标记 → running_rework
       - 有已就绪的本地 commit → running_publish_recovery
       - 否则跳过

    并发处理：``concurrency <= 1`` 时逐个串行处理（与历史行为逐字节一致）；
    ``concurrency > 1`` 时用线程池同一轮并行处理多个 Issue，每个 Issue 的
    agent 输出经 ``output_view`` 与每 Issue 日志文件分流，互不交错。

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
        concurrency: 单轮并行处理的 Issue 数量；``1`` 为串行（默认，零回归）。
            实际领取上限取 ``max(max_issues, concurrency)``。
        output_view: 并行时每 Issue 的实时输出视图；为 ``None`` 时不展示看板
            （仍写每 Issue 日志文件）。串行路径忽略该参数。

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

    # 发现 ready Issue。并行时单轮领取上限抬到 max(max_issues, concurrency)，
    # 使单独一个 --concurrency N 即可领到并跑 N 个，无需另调 --max-issues。
    effective_max_issues = max(max_issues, concurrency)
    ready_discovery_limit = max(effective_max_issues, _READY_DISCOVERY_LIMIT)
    ready_issues = github_client.list_ready_issues(config.labels.ready, ready_discovery_limit)
    processed_count = 0
    issues_to_process: list[tuple[IssueSummary, str]] = []

    for issue in ready_issues:
        if processed_count >= effective_max_issues:
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
    remaining = effective_max_issues - processed_count
    if remaining > 0:
        running_candidates = github_client.list_review_candidate_issues(
            [config.labels.running], remaining
        )
        for issue in running_candidates:
            is_rework, marker = _guard_running_issue_is_rework(issue, config, github_client)
            if is_rework and marker is not None:
                issues_to_process.append((issue, "running_rework"))
            elif _has_existing_local_commit_ready_for_publish(
                issue=issue,
                repo_path=repo_path,
                config=config,
                process_runner=process_runner,
            ) or _worktree_needs_rebase_recovery(
                issue=issue,
                repo_path=repo_path,
                config=config,
                process_runner=process_runner,
            ):
                issues_to_process.append((issue, "running_publish_recovery"))
            else:
                _logger.info(
                    "Skipping Issue #%d with label %s: no rework marker, no clean "
                    "local commit ready to publish, and no recoverable "
                    "rebase/detached worktree.",
                    issue.number,
                    config.labels.running,
                )

    # 发现 blocked Issue（使用剩余配额）
    remaining = effective_max_issues - len(issues_to_process)
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

    # DRY RUN：仅列出将处理的 Issue，不实际处理（串行、零副作用）。
    if dry_run:
        for issue, issue_kind in issues_to_process:
            selected_agent = choose_agent(issue, config, agent)
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
        return 0

    process_kwargs = {
        "repo_path": repo_path,
        "config": config,
        "agent": agent,
        "github_client": github_client,
        "content_generator": content_generator,
        "run_history_store": run_history_store,
        "run_trigger": run_trigger,
        "effective_repo_id": effective_repo_id,
    }

    # 串行路径：concurrency<=1 时逐个处理，与历史行为逐字节一致——无线程池、
    # 无每 Issue 日志文件、无实时看板（NoOp 视图把展示调用变为空操作）。
    if concurrency <= 1:
        noop_view = NoOpRunnerLiveView()
        exit_code = 0
        for issue, issue_kind in issues_to_process:
            exit_code |= _process_single_issue(
                issue,
                issue_kind,
                process_runner=process_runner,
                output_view=noop_view,
                **process_kwargs,
            )
        return exit_code

    # 并行路径：线程池同一轮并行处理多个 Issue。每个 Issue 的 agent 输出经
    # output_sink 路由到独立日志文件与（可选）独立看板列，互不交错。
    active_view = output_view or NoOpRunnerLiveView()
    log_base = repo_path / "logs"

    def _process_with_routing(item: tuple[IssueSummary, str]) -> int:
        issue, issue_kind = item
        try:
            with issue_output_routing(
                repo_id=effective_repo_id,
                issue_number=issue.number,
                log_base=log_base,
                output_view=active_view,
            ) as sink:
                scoped_runner = _OutputRoutedProcessRunner(process_runner, sink)
                return _process_single_issue(
                    issue,
                    issue_kind,
                    process_runner=scoped_runner,
                    output_view=active_view,
                    **process_kwargs,
                )
        except Exception as exc:  # noqa: BLE001 - one Issue's I/O must not kill the pass.
            _logger.error("Parallel routing failed for Issue #%d: %s", issue.number, exc)
            return 1

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(_process_with_routing, issues_to_process))
    finally:
        active_view.close()
    return 1 if any(results) else 0
