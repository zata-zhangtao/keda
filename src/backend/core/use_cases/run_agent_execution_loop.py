"""Agent 执行、验证与 recovery 状态机。"""

from __future__ import annotations
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import (
    AgentCommitResult,
    AppConfig,
    AttemptResult,
    CommandResult,
    FailureType,
    IssueSummary,
)
from backend.core.use_cases.run_agent_once import (
    AgentUnavailableError,
    MaxRetriesExceededError,
    PrdDeliveryError,
    ProviderCapacityError,
    UnrecoverableError,
    _append_attempt_and_notify,
    _logger,
    _make_attempt_result,
    _persist_short_term_memory,
    _resolve_memory_stores,
    _resolve_repo_id,
    build_recovery_prompt,
    classify_failure,
    commit_requested_changes,
    ensure_prd_delivery_ready,
    ensure_verification_passed,
    extract_prd_path,
    failed_verification_results,
    format_agent_execution_failure,
    format_recovery_failure_summary,
    get_head_sha,
    has_changes,
    run_agent,
    run_agent_with_prompt_resilient,
    run_fix_agent,
    run_verification,
    unstage_changes,
    wait_before_recovery_attempt,
)
from backend.core.use_cases.agent_runner_failure import (
    ForbiddenBlockedError,
    is_recoverable_commit_request_error,
)
from backend.core.use_cases.agent_runner_feedback import (
    VerificationFailedError,
    format_prd_delivery_detail,
    format_prd_delivery_failure,
)
from backend.core.use_cases.agent_runner_structured_evidence import ValidationEvidenceError
from backend.core.use_cases.agent_runner_validation import (
    ensure_no_misplaced_evidence_helpers,
    ensure_validation_commands_pass,
    ensure_validation_evidence_ready,
    format_validation_evidence_detail,
    format_validation_evidence_failure,
)


@dataclass(frozen=True)
class AgentExecutionRequest:
    """Agent recovery 状态机的一次执行请求。"""

    selected_agent: str
    issue: IssueSummary
    worktree_path: Path
    config: AppConfig
    process_runner: IProcessRunner
    before_sha: str
    expected_branch: str
    prompt_override: str | None = None
    on_attempt_recorded: Callable[[AttemptResult, list[AttemptResult]], None] | None = None


def run_agent_until_committed(request: AgentExecutionRequest) -> AgentCommitResult:
    """Run the agent, recover failed verification, and return final checks.

    这是一个带 recovery 重试的状态机循环。每次尝试包含以下阶段：
    1. 运行 agent（首次）或发送 recovery prompt（重试）
    2. 运行验证命令（lint / test）
    3. 检查 PRD 交付状态（归档 pending PRD）
    4. 通过 commit proxy 提交变更

    任意阶段失败后，如果还有剩余重试次数，会构造 recovery prompt
    让 agent 在下一次尝试中修复问题。所有尝试记录都写入 attempt_results，
    最终随失败评论一起发布到 GitHub Issue。

    Args:
        selected_agent: 使用的 AI agent 名称（claude / kimi / codex）。
        issue: 当前处理的 Issue。
        worktree_path: agent 工作的 git worktree 路径。
        config: Agent Runner 配置。
        process_runner: 命令执行器。
        before_sha: 循环开始前的 HEAD SHA，用于检测是否有新提交。
        expected_branch: 期望的分支名，防止 agent 切换分支。

    Returns:
        AgentCommitResult，包含最终验证结果和尝试历史。

    Raises:
        UnrecoverableError: 遇到安全违规（forbidden paths、分支异常）不可恢复。
        MaxRetriesExceededError: 所有重试次数耗尽仍未成功。
    """
    selected_agent = request.selected_agent
    issue = request.issue
    worktree_path = request.worktree_path
    config = request.config
    process_runner = request.process_runner
    before_sha = request.before_sha
    expected_branch = request.expected_branch
    prompt_override = request.prompt_override
    on_attempt_recorded = request.on_attempt_recorded
    max_recovery_attempts = max(0, config.runner.max_recovery_attempts)
    recovery_retry_delay_seconds = max(0, config.runner.recovery_retry_delay_seconds)
    prd_relative_path = extract_prd_path(issue.body)
    prd_baseline_content = (
        (worktree_path / prd_relative_path).read_text(encoding="utf-8")
        if prd_relative_path is not None and (worktree_path / prd_relative_path).exists()
        else None
    )
    recovery_failure_summary = ""
    recovery_failure_type: str = "verification_failed"
    final_verification_results: list[CommandResult] = []
    attempt_results: list[AttemptResult] = []
    verifier_verdict = None  # set by Phase 3.6 when the independent verifier runs

    # Recovery 重试循环：第 0 次是正常执行，后续是 recovery
    for attempt_index in range(max_recovery_attempts + 1):
        if attempt_index > 0:
            wait_before_recovery_attempt(
                issue.number,
                recovery_attempt=attempt_index,
                max_recovery_attempts=max_recovery_attempts,
                delay_seconds=recovery_retry_delay_seconds,
            )

        attempt_started_mono = time.monotonic()
        attempt_started_iso = datetime.now(timezone.utc).isoformat()
        repo_id = _resolve_repo_id(issue, worktree_path)

        # Phase 1: 运行 agent 或 recovery prompt
        try:
            if attempt_index == 0:
                if prompt_override is not None:
                    run_agent_with_prompt_resilient(
                        selected_agent,
                        prompt_override,
                        worktree_path,
                        process_runner,
                        issue=issue,
                        transient_retry_attempts=(config.runner.transient_retry_attempts),
                        transient_retry_delay_seconds=(config.runner.transient_retry_delay_seconds),
                        timeout_seconds=config.runner.timeout_seconds,
                        inactivity_timeout_seconds=config.runner.inactivity_timeout_seconds,
                    )
                else:
                    run_agent(
                        selected_agent,
                        issue,
                        worktree_path,
                        config,
                        process_runner,
                        timeout_seconds=config.runner.timeout_seconds,
                        inactivity_timeout_seconds=config.runner.inactivity_timeout_seconds,
                    )
            else:
                long_term_store, skill_store = _resolve_memory_stores(worktree_path, config.memory)
                recovery_prompt = build_recovery_prompt(
                    issue,
                    worktree_path,
                    recovery_attempt=attempt_index,
                    max_recovery_attempts=max_recovery_attempts,
                    failure_summary=recovery_failure_summary,
                    verification_results=final_verification_results,
                    memory_config=config.memory,
                    failure_type=recovery_failure_type,
                    long_term_store=long_term_store,
                    skill_store=skill_store,
                )
                recovery_timeout = (
                    config.runner.recovery_timeout_seconds or config.runner.timeout_seconds
                )
                run_agent_with_prompt_resilient(
                    selected_agent,
                    recovery_prompt,
                    worktree_path,
                    process_runner,
                    issue=issue,
                    transient_retry_attempts=config.runner.transient_retry_attempts,
                    transient_retry_delay_seconds=(config.runner.transient_retry_delay_seconds),
                    timeout_seconds=recovery_timeout,
                    inactivity_timeout_seconds=config.runner.inactivity_timeout_seconds,
                )
        except AgentUnavailableError:
            # The agent CLI could not be launched; let the cross-agent fallback
            # skip to the next candidate instead of burning recovery attempts.
            raise
        except (
            RuntimeError,
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            failure_type = classify_failure(
                before_sha=before_sha,
                after_sha=before_sha,
                has_uncommitted=False,
                agent_result=CommandResult(("",), 0, "", ""),
                verification_results=[],
                exc=exc,
                detect_provider_errors=True,
            )
            (
                _append_attempt_and_notify(
                    attempt_results,
                    _make_attempt_result(
                        attempt_number=attempt_index + 1,
                        failure_type=failure_type,
                        recovered=False,
                        detail=format_agent_execution_failure(exc),
                        agent=selected_agent,
                        started_mono=attempt_started_mono,
                        started_iso=attempt_started_iso,
                    ),
                    on_attempt_recorded,
                ),
            )

            _persist_short_term_memory(
                config=config,
                issue=issue,
                worktree_path=worktree_path,
                attempt=attempt_results[-1],
                repo_id=repo_id,
            )
            if failure_type == FailureType.UNRECOVERABLE:
                raise UnrecoverableError(str(exc), attempt_results) from exc
            if failure_type == FailureType.FORBIDDEN_BLOCKED:
                raise ForbiddenBlockedError(str(exc), attempt_results) from exc
            if failure_type == FailureType.PROVIDER_CAPACITY:
                # The same provider will keep failing until its window resets;
                # escalate so the fallback chain can switch to another agent.
                raise ProviderCapacityError(str(exc), attempt_results) from exc
            if attempt_index >= max_recovery_attempts:
                raise MaxRetriesExceededError(attempt_results) from exc
            recovery_failure_summary = format_agent_execution_failure(exc)
            recovery_failure_type = failure_type.value
            recovery_failure_summary = format_agent_execution_failure(exc)
            _logger.warning(
                "Agent command failed for Issue #%d; asking agent to recover (%d/%d).",
                issue.number,
                attempt_index + 1,
                max_recovery_attempts,
            )
            continue

        # Phase 2: 验证 agent 产出的代码（staging 之前）
        verification_results = run_verification(worktree_path, config, process_runner)
        final_verification_results = verification_results
        try:
            ensure_verification_passed(verification_results)
        except VerificationFailedError as exc:
            after_sha = get_head_sha(worktree_path, process_runner)
            failure_type = classify_failure(
                before_sha=before_sha,
                after_sha=after_sha,
                has_uncommitted=False,
                agent_result=CommandResult(("",), 0, "", ""),
                verification_results=exc.verification_results,
                exc=None,
            )
            (
                _append_attempt_and_notify(
                    attempt_results,
                    _make_attempt_result(
                        attempt_number=attempt_index + 1,
                        failure_type=failure_type,
                        recovered=False,
                        detail=format_recovery_failure_summary(
                            "Verification before staging failed.",
                            exc.verification_results,
                        ),
                        agent=selected_agent,
                        started_mono=attempt_started_mono,
                        started_iso=attempt_started_iso,
                    ),
                    on_attempt_recorded,
                ),
            )

            _persist_short_term_memory(
                config=config,
                issue=issue,
                worktree_path=worktree_path,
                attempt=attempt_results[-1],
                repo_id=repo_id,
            )
            if attempt_index >= max_recovery_attempts:
                raise MaxRetriesExceededError(attempt_results) from exc
            recovery_failure_type = failure_type.value
            recovery_failure_summary = format_recovery_failure_summary(
                "Verification before staging failed.",
                exc.verification_results,
            )
            _logger.warning(
                "Verification failed for Issue #%d; asking agent to recover (%d/%d).",
                issue.number,
                attempt_index + 1,
                max_recovery_attempts,
            )
            continue

        # Phase 3: 检查 PRD 交付（归档已完成 PRD）
        try:
            ensure_prd_delivery_ready(
                issue,
                worktree_path,
                process_runner,
                prd_baseline_content=prd_baseline_content,
            )
        except PrdDeliveryError as exc:
            after_sha = get_head_sha(worktree_path, process_runner)
            failure_type = classify_failure(
                before_sha=before_sha,
                after_sha=after_sha,
                has_uncommitted=False,
                agent_result=CommandResult(("",), 0, "", ""),
                verification_results=verification_results,
                exc=exc,
            )
            (
                _append_attempt_and_notify(
                    attempt_results,
                    _make_attempt_result(
                        attempt_number=attempt_index + 1,
                        failure_type=failure_type,
                        recovered=False,
                        detail=format_prd_delivery_detail(str(exc)),
                        agent=selected_agent,
                        started_mono=attempt_started_mono,
                        started_iso=attempt_started_iso,
                    ),
                    on_attempt_recorded,
                ),
            )

            _persist_short_term_memory(
                config=config,
                issue=issue,
                worktree_path=worktree_path,
                attempt=attempt_results[-1],
                repo_id=repo_id,
            )
            if attempt_index >= max_recovery_attempts:
                raise MaxRetriesExceededError(attempt_results) from exc
            recovery_failure_summary = format_prd_delivery_failure(str(exc))
            recovery_failure_type = failure_type.value
            recovery_failure_summary = format_prd_delivery_failure(str(exc))
            _logger.warning(
                "PRD delivery check failed for Issue #%d; asking agent to recover (%d/%d).",
                issue.number,
                attempt_index + 1,
                max_recovery_attempts,
            )
            continue

        # Phase 3.5: Realistic Validation 证据门禁（要求验证且无豁免时）
        try:
            ensure_validation_evidence_ready(issue, worktree_path, config, process_runner)
            ensure_no_misplaced_evidence_helpers(worktree_path, process_runner)
            ensure_validation_commands_pass(issue, worktree_path, config, process_runner)
            # Phase 3.6: independent verifier (pre-PR; red -> this same recovery
            # loop auto-repairs, bounded; escalates to a human only on exhaustion).
            # Local import breaks the run_agent_once <-> run_verifier_agent cycle.
            from backend.core.use_cases.run_verifier_agent import run_verifier_gate

            verifier_verdict = run_verifier_gate(
                issue, worktree_path, config, process_runner, selected_agent
            )
        except ValidationEvidenceError as exc:
            after_sha = get_head_sha(worktree_path, process_runner)
            failure_type = classify_failure(
                before_sha=before_sha,
                after_sha=after_sha,
                has_uncommitted=False,
                agent_result=CommandResult(("",), 0, "", ""),
                verification_results=verification_results,
                exc=exc,
            )
            (
                _append_attempt_and_notify(
                    attempt_results,
                    _make_attempt_result(
                        attempt_number=attempt_index + 1,
                        failure_type=failure_type,
                        recovered=False,
                        detail=format_validation_evidence_detail(str(exc)),
                        agent=selected_agent,
                        started_mono=attempt_started_mono,
                        started_iso=attempt_started_iso,
                    ),
                    on_attempt_recorded,
                ),
            )

            _persist_short_term_memory(
                config=config,
                issue=issue,
                worktree_path=worktree_path,
                attempt=attempt_results[-1],
                repo_id=repo_id,
            )
            if attempt_index >= max_recovery_attempts:
                raise MaxRetriesExceededError(attempt_results) from exc
            recovery_failure_summary = format_validation_evidence_failure(
                str(exc), config.validation.evidence_dir
            )
            recovery_failure_type = failure_type.value
            _logger.warning(
                "Validation evidence check failed for Issue #%d; asking agent to recover (%d/%d).",
                issue.number,
                attempt_index + 1,
                max_recovery_attempts,
            )
            continue

        # Phase 4: Commit proxy — agent 通过 commit-request 文件请求提交
        if has_changes(worktree_path, process_runner):
            _logger.warning(
                "Agent left uncommitted changes for Issue #%d; runner processing commit request.",
                issue.number,
            )
            try:
                final_verification_results = commit_requested_changes(
                    issue,
                    worktree_path,
                    config,
                    process_runner,
                    expected_branch=expected_branch,
                )
            except VerificationFailedError as exc:
                # staging 后验证失败：runner autofix 已在 commit_requested_changes
                # 内部尝试过。先 unstage，再交给 Fix Agent 处理简单局部失败。
                unstage_changes(worktree_path, process_runner)
                fix_succeeded = False
                if not config.runner.fix_agent_enabled:
                    _logger.info(
                        "Fix Agent disabled for Issue #%d; escalating staged "
                        "verification failure to full recovery.",
                        issue.number,
                    )
                else:
                    try:
                        fix_agent_result = run_fix_agent(
                            selected_agent,
                            issue,
                            worktree_path,
                            config,
                            process_runner,
                            verification_results=exc.verification_results,
                        )
                        if fix_agent_result.return_code != 0:
                            raise RuntimeError(
                                f"Fix Agent exited with code {fix_agent_result.return_code}"
                            )
                        post_fix_verification = run_verification(
                            worktree_path, config, process_runner
                        )
                        if failed_verification_results(post_fix_verification):
                            raise VerificationFailedError(post_fix_verification)
                        final_verification_results = commit_requested_changes(
                            issue,
                            worktree_path,
                            config,
                            process_runner,
                            expected_branch=expected_branch,
                        )
                        fix_succeeded = True
                    except (
                        RuntimeError,
                        subprocess.CalledProcessError,
                        VerificationFailedError,
                    ) as fix_exc:
                        _logger.warning(
                            "Fix Agent failed for Issue #%d: %s",
                            issue.number,
                            fix_exc,
                        )
                if fix_succeeded:
                    # Fix Agent repaired the failure and the runner committed it.
                    # Fall through to Phase 5 to record success.
                    _logger.info(
                        "Fix Agent repaired staged verification failure for "
                        "Issue #%d; runner committed the fix.",
                        issue.number,
                    )
                else:
                    after_sha = get_head_sha(worktree_path, process_runner)
                    failure_type = classify_failure(
                        before_sha=before_sha,
                        after_sha=after_sha,
                        has_uncommitted=False,
                        agent_result=CommandResult(("",), 0, "", ""),
                        verification_results=exc.verification_results,
                        exc=None,
                    )
                    (
                        _append_attempt_and_notify(
                            attempt_results,
                            _make_attempt_result(
                                attempt_number=attempt_index + 1,
                                failure_type=failure_type,
                                recovered=False,
                                detail=format_recovery_failure_summary(
                                    "Verification after runner staged changes with git add -A failed.",
                                    exc.verification_results,
                                ),
                                agent=selected_agent,
                                started_mono=attempt_started_mono,
                                started_iso=attempt_started_iso,
                            ),
                            on_attempt_recorded,
                        ),
                    )

                    _persist_short_term_memory(
                        config=config,
                        issue=issue,
                        worktree_path=worktree_path,
                        attempt=attempt_results[-1],
                        repo_id=repo_id,
                    )
                    if attempt_index >= max_recovery_attempts:
                        raise MaxRetriesExceededError(attempt_results) from exc
                    recovery_failure_type = failure_type.value
                    recovery_failure_summary = format_recovery_failure_summary(
                        "Verification after runner staged changes with git add -A failed.",
                        exc.verification_results,
                    )
                    _logger.warning(
                        "Staged verification failed for Issue #%d; "
                        "asking agent to recover (%d/%d).",
                        issue.number,
                        attempt_index + 1,
                        max_recovery_attempts,
                    )
                    continue
            except (RuntimeError, subprocess.CalledProcessError) as exc:
                after_sha = get_head_sha(worktree_path, process_runner)
                # 对于不可恢复的 commit 错误（如分支切换、无 commit request），
                # 或者已耗尽重试次数，直接失败。
                # CalledProcessError（如 pre-commit hook 失败）则视为可恢复。
                if (
                    attempt_index >= max_recovery_attempts
                    or not is_recoverable_commit_request_error(exc)
                ):
                    failure_type = classify_failure(
                        before_sha=before_sha,
                        after_sha=after_sha,
                        has_uncommitted=True,
                        agent_result=CommandResult(("",), 0, "", ""),
                        verification_results=final_verification_results,
                        exc=exc,
                    )
                    (
                        _append_attempt_and_notify(
                            attempt_results,
                            _make_attempt_result(
                                attempt_number=attempt_index + 1,
                                failure_type=failure_type,
                                recovered=False,
                                detail=str(exc),
                                agent=selected_agent,
                                started_mono=attempt_started_mono,
                                started_iso=attempt_started_iso,
                            ),
                            on_attempt_recorded,
                        ),
                    )

                    _persist_short_term_memory(
                        config=config,
                        issue=issue,
                        worktree_path=worktree_path,
                        attempt=attempt_results[-1],
                        repo_id=repo_id,
                    )
                    if failure_type == FailureType.UNRECOVERABLE:
                        raise UnrecoverableError(str(exc), attempt_results) from exc
                    if failure_type == FailureType.FORBIDDEN_BLOCKED:
                        raise ForbiddenBlockedError(str(exc), attempt_results) from exc
                    if attempt_index >= max_recovery_attempts:
                        raise MaxRetriesExceededError(attempt_results) from exc
                    raise
                failure_type = classify_failure(
                    before_sha=before_sha,
                    after_sha=after_sha,
                    has_uncommitted=True,
                    agent_result=CommandResult(("",), 0, "", ""),
                    verification_results=final_verification_results,
                    exc=None,
                )
                (
                    _append_attempt_and_notify(
                        attempt_results,
                        _make_attempt_result(
                            attempt_number=attempt_index + 1,
                            failure_type=failure_type,
                            recovered=False,
                            detail=f"The runner could not process the commit request.\n{exc}",
                            agent=selected_agent,
                            started_mono=attempt_started_mono,
                            started_iso=attempt_started_iso,
                        ),
                        on_attempt_recorded,
                    ),
                )

                _persist_short_term_memory(
                    config=config,
                    issue=issue,
                    worktree_path=worktree_path,
                    attempt=attempt_results[-1],
                    repo_id=repo_id,
                )
                if attempt_index >= max_recovery_attempts:
                    raise MaxRetriesExceededError(attempt_results) from exc
                recovery_failure_type = failure_type.value
                recovery_failure_summary = "\n".join(
                    [
                        "The runner could not process the commit request.",
                        str(exc),
                        "Fix the worktree and write a valid commit request JSON.",
                    ]
                )
                _logger.warning(
                    "Commit request failed for Issue #%d; asking agent to recover (%d/%d).",
                    issue.number,
                    attempt_index + 1,
                    max_recovery_attempts,
                )
                continue

        # Phase 5: 检查 agent 是否实际产生了 commit
        after_sha = get_head_sha(worktree_path, process_runner)
        if before_sha != after_sha:
            success_attempt = _make_attempt_result(
                attempt_number=attempt_index + 1,
                failure_type=FailureType.SUCCESS,
                recovered=attempt_index > 0,
                detail="Agent produced commits and passed verification.",
                agent=selected_agent,
                started_mono=attempt_started_mono,
                started_iso=attempt_started_iso,
            )
            (
                _append_attempt_and_notify(
                    attempt_results,
                    success_attempt,
                    on_attempt_recorded,
                ),
            )

            _persist_short_term_memory(
                config=config,
                issue=issue,
                worktree_path=worktree_path,
                attempt=attempt_results[-1],
                repo_id=repo_id,
            )
            _persist_short_term_memory(
                config=config,
                issue=issue,
                worktree_path=worktree_path,
                attempt=success_attempt,
                repo_id=repo_id,
            )
            return AgentCommitResult(final_verification_results, attempt_results, verifier_verdict)

        # Agent 没有产生任何变更：进入 recovery 要求实际修改代码
        has_uncommitted = has_changes(worktree_path, process_runner)
        failure_type = classify_failure(
            before_sha=before_sha,
            after_sha=after_sha,
            has_uncommitted=has_uncommitted,
            agent_result=CommandResult(("",), 0, "", ""),
            verification_results=verification_results,
            exc=None,
        )
        (
            _append_attempt_and_notify(
                attempt_results,
                _make_attempt_result(
                    attempt_number=attempt_index + 1,
                    failure_type=failure_type,
                    recovered=False,
                    detail="Agent produced no git commits.",
                    agent=selected_agent,
                    started_mono=attempt_started_mono,
                    started_iso=attempt_started_iso,
                ),
                on_attempt_recorded,
            ),
        )

        _persist_short_term_memory(
            config=config,
            issue=issue,
            worktree_path=worktree_path,
            attempt=attempt_results[-1],
            repo_id=repo_id,
        )
        if attempt_index >= max_recovery_attempts:
            raise MaxRetriesExceededError(attempt_results)
        recovery_failure_type = failure_type.value
        recovery_failure_summary = "\n".join(
            [
                "The previous attempt produced no git commits.",
                "Make the requested code changes and write a valid commit request JSON.",
            ]
        )
        _logger.warning(
            "Agent produced no git commits for Issue #%d; asking agent to recover (%d/%d).",
            issue.number,
            attempt_index + 1,
            max_recovery_attempts,
        )

    raise MaxRetriesExceededError(attempt_results)
