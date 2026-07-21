"""Agent Runner 的单轮队列调度实现。"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from backend.core.shared.interfaces.runner_live_view import NoOpRunnerLiveView
from backend.core.use_cases.agent_runner_blocked_claim import BlockedWorktreeClaimedError
from backend.core.use_cases.agent_runner_dependencies import (
    clear_dependency_waiting,
    evaluate_dependencies,
    mark_dependency_waiting,
    parse_dependency_marker,
)
from backend.core.use_cases.agent_runner_failure_marking import (
    _mark_issue_blocked,
    _mark_issue_failed,
)
from backend.core.use_cases.agent_runner_orchestrate import (
    AppConfig,
    AttemptResult,
    ForbiddenBlockedError,
    IContentGenerator,
    IGitHubClient,
    IProcessRunner,
    IRunHistoryStore,
    IRunnerLiveView,
    IssueSummary,
    _READY_DISCOVERY_LIMIT,
    _append_run_record_locked,
    _guard_blocked_issue_has_resolution,
    _guard_running_issue_is_rework,
    _logger,
    _persist_attempt_result,
    _process_blocked_resolution,
    _process_ready_issue,
    _process_running_publish_recovery,
    _process_running_rework,
    choose_agent,
    create_or_reuse_worktree,
    run_issue_with_agent_fallback,
)
from backend.core.use_cases.agent_runner_output_routing import (
    _OutputRoutedProcessRunner,
    issue_output_routing,
)
from backend.core.use_cases.agent_runner_validation_gate import process_validation_gate
from backend.core.use_cases.agent_runner_workflow import claim_blocked_issue
from backend.core.use_cases.agent_runner_worktree_probe import (
    _has_existing_local_commit_ready_for_publish,
    _worktree_needs_rebase_recovery,
)
from backend.core.use_cases.create_prd_from_issue import (
    CreatePrdFromIssueRequest,
    create_prd_from_issue,
)

RUNTIME_DEPENDENCY_NAMES = (
    "_process_ready_issue",
    "_process_running_rework",
    "_process_blocked_resolution",
    "_process_running_publish_recovery",
    "choose_agent",
    "create_or_reuse_worktree",
    "_worktree_needs_rebase_recovery",
    "_has_existing_local_commit_ready_for_publish",
)


@dataclass(frozen=True)
class PrdReworkRequest:
    """一轮 PRD rework Issue 处理请求。"""

    repo_path: Path
    config: AppConfig
    github_client: IGitHubClient
    process_runner: IProcessRunner
    content_generator: IContentGenerator | None = None
    max_issues: int = 1


def process_prd_rework_issues(request: PrdReworkRequest) -> None:
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
    repo_path = request.repo_path
    config = request.config
    github_client = request.github_client
    process_runner = request.process_runner
    content_generator = request.content_generator
    issues = github_client.list_rework_prd_issues(
        config.labels.rework_prd,
        limit=request.max_issues,
    )
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


@dataclass(frozen=True)
class RunOnceRequest:
    """Agent Runner 单轮队列调度请求。"""

    repo_path: Path
    config: AppConfig
    dry_run: bool
    agent: str
    max_issues: int
    github_client: IGitHubClient
    process_runner: IProcessRunner
    content_generator: IContentGenerator | None = None
    run_history_store: IRunHistoryStore | None = None
    run_trigger: str = "cli_run"
    repo_id: str | None = None
    concurrency: int = 1
    output_view: IRunnerLiveView | None = None


def run_once(request: RunOnceRequest) -> int:
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

    repo_path = request.repo_path
    config = request.config
    dry_run = request.dry_run
    agent = request.agent
    max_issues = request.max_issues
    github_client = request.github_client
    process_runner = request.process_runner
    content_generator = request.content_generator
    run_history_store = request.run_history_store
    run_trigger = request.run_trigger
    repo_id = request.repo_id
    concurrency = request.concurrency
    output_view = request.output_view
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
