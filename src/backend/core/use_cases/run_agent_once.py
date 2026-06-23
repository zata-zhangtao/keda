"""Local Issue queue runner — single polling pass.

本模块是 Agent Runner 的核心执行层，负责：
1. 为单个 Issue 创建或复用 git worktree
2. 调用 AI Agent（Claude / Kimi / Codex）执行代码变更
3. 运行验证命令（lint / test）
4. 通过受限 commit proxy 将 agent 变更提交到本地分支
5. 管理 recovery 重试循环：当验证或 commit 失败时，给 agent 发送 recovery prompt

Commit Proxy 机制：
agent 不直接执行 `git commit`，而是将 commit message 写入
`.agent-runner/commit-request.json`。runner 读取该文件后执行 commit，
这样可以确保在 commit 前运行验证、检查 forbidden paths、控制分支安全。
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
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
)
from backend.core.use_cases.agent_runner_commit import (
    EmptyCommitRequestError,
    checkpoint_uncommitted_progress,
    commit_requested_changes,
    sanitize_commit_message,
    unstage_changes,
)
from backend.core.use_cases.agent_runner_failure import (
    AgentRunnerAttemptError,
    ForbiddenBlockedError,
    MaxRetriesExceededError,
    PublishFailureError,
    UnrecoverableError,
    classify_failure,
    detect_usage_limit_root_cause,
    format_agent_execution_failure,
    format_attempt_history,
    format_failure_comment,
    format_minimal_failure_comment,
    format_publish_failure_comment,
    format_recovery_failure_summary,
    is_recoverable_commit_request_error,
)
from backend.core.use_cases.agent_runner_feedback import (
    PrdDeliveryError,
    VerificationFailedError,
    build_progress_continuation_prompt,
    build_prompt,
    build_recovery_prompt,
    ensure_prd_delivery_ready,
    ensure_verification_passed,
    extract_prd_path,
    failed_verification_results,
    format_prd_delivery_failure,
    format_result_for_recovery,
    format_verification_failure,
    resolve_prd_archive_path,
    truncate_recovery_output,
)
from backend.core.use_cases.agent_runner_git import (
    get_active_rebase_target,
    get_current_branch,
    get_head_sha,
    has_changes,
    is_detached_head,
    list_changed_paths,
    list_git_remotes,
    run_verification,
)
from backend.core.use_cases.agent_runner_publish import (
    publish_changes,
    run_preflight_checks,
    validate_publish_remote,
    validate_safe_changes,
)
from backend.core.use_cases.agent_runner_validation import (
    ValidationEvidenceError,
    build_validation_prompt_line,
    ensure_evidence_dir_excluded,
    ensure_validation_evidence_ready,
    format_validation_evidence_failure,
)
from backend.core.use_cases.agent_runner_worktree_branch import (
    _ensure_worktree_branch,
    _reconcile_worktree_with_remote_branch,
)
from backend.core.use_cases.worktree_env import copy_missing_env_files
from backend.core.use_cases.worktree_frontend import (
    exclude_frontend_node_modules_from_git,
    link_frontend_node_modules,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "AgentRunnerAttemptError",
    "MaxRetriesExceededError",
    "PrdDeliveryError",
    "PublishFailureError",
    "EmptyCommitRequestError",
    "UnrecoverableError",
    "VerificationFailedError",
    "_ensure_worktree_branch",
    "_reconcile_worktree_with_remote_branch",
    "build_blocked_continuation_prompt",
    "build_progress_continuation_prompt",
    "build_prompt",
    "build_recovery_prompt",
    "checkpoint_uncommitted_progress",
    "choose_agent",
    "classify_failure",
    "commit_requested_changes",
    "create_or_reuse_worktree",
    "detect_usage_limit_root_cause",
    "ensure_prd_delivery_ready",
    "ensure_verification_passed",
    "extract_agent_response_text",
    "extract_prd_path",
    "failed_verification_results",
    "format_agent_execution_failure",
    "format_attempt_history",
    "format_command",
    "format_failure_comment",
    "format_minimal_failure_comment",
    "format_prd_delivery_failure",
    "format_publish_failure_comment",
    "format_recovery_failure_summary",
    "format_result_for_recovery",
    "format_verification_failure",
    "get_active_rebase_target",
    "get_current_branch",
    "get_head_sha",
    "has_changes",
    "is_detached_head",
    "list_changed_paths",
    "list_git_remotes",
    "publish_changes",
    "resolve_prd_archive_path",
    "run_agent",
    "run_agent_until_committed",
    "run_agent_with_prompt",
    "run_once",
    "run_preflight_checks",
    "run_verification",
    "sanitize_commit_message",
    "truncate_recovery_output",
    "unstage_changes",
    "validate_publish_remote",
    "validate_safe_changes",
    "wait_before_recovery_attempt",
]


def format_command(
    template: str,
    *,
    issue_number: int,
    base_branch: str | None = None,
) -> list[str]:
    """Format a configured command template for an Issue.

    Args:
        template: Command template string. May reference ``{issue_number}``
            and optionally ``{base_branch}`` placeholders.
        issue_number: GitHub issue number the runner is processing.
        base_branch: Repository base branch. Required when ``template``
            contains the ``{base_branch}`` placeholder; ignored otherwise.

    Returns:
        Tokenized command list ready for subprocess execution.
    """
    if "{base_branch}" in template:
        if base_branch is None:
            raise ValueError(
                "Command template references {base_branch} but no base_branch "
                "was provided."
            )
        return shlex.split(
            template.format(issue_number=issue_number, base_branch=base_branch)
        )
    return shlex.split(template.format(issue_number=issue_number))


def choose_agent(issue: IssueSummary, config: AppConfig, override_agent: str) -> str:
    """Choose an AI agent for the Issue."""
    if override_agent != "auto":
        return override_agent
    for agent_name, label in config.labels.agent_labels.items():
        if label in issue.labels:
            return agent_name
    return (
        config.runner.default_agent
        if config.runner.default_agent != "auto"
        else "claude"
    )


def create_or_reuse_worktree(
    repo_path: Path,
    issue: IssueSummary,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> Path:
    """Create or reuse a worktree for the Issue.

    The three configured commands are run in sequence:

    1. ``create_command`` (best effort, ``check=False``) attempts to create
       the worktree. Failures are tolerated here because the next command
       may be able to recover.
    2. ``reuse_command`` runs only when ``create_command`` failed. It
       usually re-resolves the worktree path and is a no-op on disk.
    3. ``path_command`` always runs to obtain the canonical absolute path.

    After the three commands complete, the returned path is verified to
    exist. If it does not, a :class:`FileNotFoundError` is raised that
    carries the three commands' return codes and stdout excerpts so the
    next engineer can see exactly which step went wrong.

    Finally, gitignored artifacts that ``git worktree add`` never
    materializes are restored from the main checkout: missing ``.env*``
    files are copied (so agent commands see the same configuration), and
    each frontend project's ``node_modules`` is symlinked from the main
    checkout (so worktree builds like ``vite`` work without a per-worktree
    install). Reused worktrees are healed the same way; existing files and
    ``node_modules`` are not touched.
    """
    create_result = process_runner.run(
        format_command(
            config.worktree.create_command,
            issue_number=issue.number,
            base_branch=config.worktree.base_branch,
        ),
        cwd=repo_path,
        check=False,
    )
    if create_result.return_code != 0:
        reuse_result = process_runner.run(
            format_command(
                config.worktree.reuse_command,
                issue_number=issue.number,
            ),
            cwd=repo_path,
            check=False,
        )
    else:
        reuse_result = None
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
            "worktree path does not exist after create/reuse/path pipeline: "
            f"{worktree_path}. "
            f"create_command return_code={create_result.return_code}, "
            f"reuse_command return_code="
            f"{reuse_result.return_code if reuse_result is not None else 'skipped'}, "
            f"path_command return_code={path_result.return_code}, "
            f"path_command stdout={path_result.stdout!r}."
        )
    # 证据目录本地排除：截图/输出证据永远不进代码 diff。
    ensure_evidence_dir_excluded(worktree_path, config, process_runner)
    copied_env_paths = copy_missing_env_files(repo_path, worktree_path)
    if copied_env_paths:
        _logger.info(
            "Copied %d missing env file(s) into worktree %s: %s",
            len(copied_env_paths),
            worktree_path,
            ", ".join(str(env_path) for env_path in copied_env_paths),
        )
    # node_modules is gitignored, so `git worktree add` never materializes it;
    # symlink each frontend project's deps from the main checkout so worktree
    # builds (vite, etc.) work without a per-worktree install. Reused worktrees
    # are healed the same way; existing node_modules are left untouched.
    linked_frontend_paths = link_frontend_node_modules(repo_path, worktree_path)
    if linked_frontend_paths:
        exclude_frontend_node_modules_from_git(
            worktree_path, linked_frontend_paths, process_runner
        )
        _logger.info(
            "Linked node_modules for %d frontend project(s) into worktree %s: %s",
            len(linked_frontend_paths),
            worktree_path,
            ", ".join(str(frontend_path) for frontend_path in linked_frontend_paths),
        )
    expected_branch = f"issue-{issue.number}"
    _ensure_worktree_branch(
        worktree_path, expected_branch, issue, config, process_runner
    )
    _reconcile_worktree_with_remote_branch(worktree_path, config, process_runner)
    return worktree_path


def _build_claude_command(prompt: str, worktree_path: Path) -> list[str]:  # noqa: ARG001
    return [
        "claude",
        "--dangerously-skip-permissions",
        "--verbose",
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        prompt,
    ]


def _build_kimi_command(prompt: str, worktree_path: Path) -> list[str]:  # noqa: ARG001
    return ["kimi", "--prompt", prompt]


def _build_codex_command(prompt: str, worktree_path: Path) -> list[str]:
    return [
        "codex",
        "--cd",
        str(worktree_path),
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "exec",
        prompt,
    ]


_AGENT_COMMAND_BUILDERS: dict[str, Callable[[str, Path], list[str]]] = {
    "claude": _build_claude_command,
    "kimi": _build_kimi_command,
}


def build_blocked_continuation_prompt(
    issue: IssueSummary,
    worktree_path: Path,
    blocked_paths: tuple[str, ...],
) -> str:
    """Build a continuation prompt for a blocked Issue that has been resolved.

    Args:
        issue: Issue being processed.
        worktree_path: Path to the agent worktree.
        blocked_paths: The forbidden paths that were previously blocked.

    Returns:
        A prompt instructing the agent to continue the remaining work.
    """
    lines = [
        f"Continue working on Issue #{issue.number}: {issue.title}",
        f"Issue URL: {issue.url}",
        f"Worktree: {worktree_path}",
        "",
        "The following files were previously blocked by forbidden-path rules and have now been resolved by a human operator.",
        "Please continue to complete the remaining tasks without modifying these files again unless explicitly required:",
        "",
    ]
    for path in blocked_paths:
        lines.append(f"- {path}")
    lines.extend(
        [
            "",
            "Proceed with the remaining implementation, verification, and commit as normal.",
        ]
    )
    return "\n".join(lines)


def run_agent(
    agent_name: str,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> CommandResult:
    """Run Codex or Claude Code in non-interactive mode."""
    prompt = build_prompt(
        issue,
        worktree_path,
        config.prompts,
        phase="execution",
        validation_line=build_validation_prompt_line(issue, config),
    )
    return run_agent_with_prompt(
        agent_name, prompt, worktree_path, process_runner, issue=issue
    )


def run_agent_with_prompt(
    agent_name: str,
    prompt: str,
    worktree_path: Path,
    process_runner: IProcessRunner,
    *,
    capture_output: bool = False,
    timeout_seconds: int | None = None,
    issue: IssueSummary | None = None,
) -> CommandResult:
    """Run Codex or Claude Code with a prepared prompt."""
    if issue is not None:
        _logger.info(
            "Starting agent for Issue #%d: %s",
            issue.number,
            issue.url,
        )
    builder = _AGENT_COMMAND_BUILDERS.get(agent_name)
    if builder is not None:
        command = builder(prompt, worktree_path)
    else:
        command = _build_codex_command(prompt, worktree_path)
    label = f"Issue #{issue.number}: {issue.url}" if issue is not None else None
    result = process_runner.run(
        command,
        cwd=worktree_path,
        capture_output=capture_output,
        timeout=timeout_seconds,
        label=label,
    )
    if issue is not None:
        _logger.info(
            "Agent finished for Issue #%d: %s (exit_code=%d)",
            issue.number,
            issue.url,
            result.return_code,
        )
    return result


def extract_agent_response_text(result: CommandResult) -> str:
    """Return assistant response text from direct stdout or Claude stream-json.

    Claude 使用 `--output-format stream-json` 时，每行输出是一个 JSON 事件，
    包含 stream_event（文本增量）、assistant（完整消息）或 result（最终结果）。
    本函数按优先级提取有效文本，非 stream-json 命令则直接返回原始 stdout。

    注意：process runner 对 stream-json 命令会先把事件流渲染成纯文本再返回，
    此时 stdout 已不是原始事件流；若仍逐行重解析，恰好构成合法 JSON 标量的行
    （如数组末尾不带逗号的字符串元素）会被静默丢弃，破坏其中的 JSON 内容。
    因此只有确实识别到 stream-json 事件时才走事件提取，否则原样返回。
    """
    if not result.stdout:
        return ""
    command_name = result.command[0] if result.command else ""
    if command_name != "claude" or "stream-json" not in result.command:
        return result.stdout

    stream_text_parts: list[str] = []
    assistant_text_parts: list[str] = []
    result_parts: list[str] = []
    saw_stream_json_event = False
    for output_line in result.stdout.splitlines():
        try:
            event_payload = json.loads(output_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event_payload, dict):
            continue
        event_type = event_payload.get("type")
        if event_type == "stream_event":
            saw_stream_json_event = True
            _append_claude_stream_event_text(event_payload, stream_text_parts)
        elif event_type == "assistant":
            saw_stream_json_event = True
            _append_claude_assistant_text(event_payload, assistant_text_parts)
        elif event_type == "result":
            saw_stream_json_event = True
            result_text = str(event_payload.get("result") or "").strip()
            if result_text:
                result_parts.append(result_text)

    if not saw_stream_json_event:
        return result.stdout
    if stream_text_parts:
        return "".join(stream_text_parts)
    if assistant_text_parts:
        return "".join(assistant_text_parts)
    if result_parts:
        return "\n".join(result_parts)
    return result.stdout


def _append_claude_stream_event_text(
    event_payload: dict[str, object],
    text_parts: list[str],
) -> None:
    event = event_payload.get("event")
    if not isinstance(event, dict):
        return
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return
    if delta.get("type") == "text_delta":
        text_parts.append(str(delta.get("text", "")))


def _append_claude_assistant_text(
    event_payload: dict[str, object],
    text_parts: list[str],
) -> None:
    message = event_payload.get("message")
    if not isinstance(message, dict):
        return
    content_blocks = message.get("content", [])
    if not isinstance(content_blocks, list):
        return
    for content_block in content_blocks:
        if not isinstance(content_block, dict):
            continue
        if content_block.get("type") == "text":
            text_parts.append(str(content_block.get("text", "")))


def wait_before_recovery_attempt(
    issue_number: int,
    *,
    recovery_attempt: int,
    max_recovery_attempts: int,
    delay_seconds: int,
) -> None:
    """Wait before a recovery attempt when retry delay is configured."""
    if delay_seconds <= 0:
        return
    _logger.info(
        "Waiting %d seconds before recovery attempt %d/%d for Issue #%d.",
        delay_seconds,
        recovery_attempt,
        max_recovery_attempts,
        issue_number,
    )
    time.sleep(delay_seconds)


def run_agent_until_committed(
    *,
    selected_agent: str,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    before_sha: str,
    expected_branch: str,
    prompt_override: str | None = None,
) -> AgentCommitResult:
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
    max_recovery_attempts = max(0, config.runner.max_recovery_attempts)
    recovery_retry_delay_seconds = max(0, config.runner.recovery_retry_delay_seconds)
    recovery_failure_summary = ""
    final_verification_results: list[CommandResult] = []
    attempt_results: list[AttemptResult] = []

    # Recovery 重试循环：第 0 次是正常执行，后续是 recovery
    for attempt_index in range(max_recovery_attempts + 1):
        if attempt_index > 0:
            wait_before_recovery_attempt(
                issue.number,
                recovery_attempt=attempt_index,
                max_recovery_attempts=max_recovery_attempts,
                delay_seconds=recovery_retry_delay_seconds,
            )

        # Phase 1: 运行 agent 或 recovery prompt
        try:
            if attempt_index == 0:
                if prompt_override is not None:
                    run_agent_with_prompt(
                        selected_agent,
                        prompt_override,
                        worktree_path,
                        process_runner,
                        issue=issue,
                    )
                else:
                    run_agent(
                        selected_agent, issue, worktree_path, config, process_runner
                    )
            else:
                recovery_prompt = build_recovery_prompt(
                    issue,
                    worktree_path,
                    recovery_attempt=attempt_index,
                    max_recovery_attempts=max_recovery_attempts,
                    failure_summary=recovery_failure_summary,
                )
                run_agent_with_prompt(
                    selected_agent,
                    recovery_prompt,
                    worktree_path,
                    process_runner,
                    issue=issue,
                )
        except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
            failure_type = classify_failure(
                before_sha=before_sha,
                after_sha=before_sha,
                has_uncommitted=False,
                agent_result=CommandResult(("",), 0, "", ""),
                verification_results=[],
                exc=exc,
            )
            attempt_results.append(
                AttemptResult(
                    attempt_number=attempt_index + 1,
                    failure_type=failure_type,
                    recovered=False,
                    detail=format_agent_execution_failure(exc),
                )
            )
            if failure_type == FailureType.UNRECOVERABLE:
                raise UnrecoverableError(str(exc), attempt_results) from exc
            if failure_type == FailureType.FORBIDDEN_BLOCKED:
                raise ForbiddenBlockedError(str(exc), attempt_results) from exc
            if attempt_index >= max_recovery_attempts:
                raise MaxRetriesExceededError(attempt_results) from exc
            recovery_failure_summary = format_agent_execution_failure(exc)
            _logger.warning(
                "Agent command failed for Issue #%d; "
                "asking agent to recover (%d/%d).",
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
            attempt_results.append(
                AttemptResult(
                    attempt_number=attempt_index + 1,
                    failure_type=failure_type,
                    recovered=False,
                    detail=format_recovery_failure_summary(
                        "Verification before staging failed.",
                        exc.verification_results,
                    ),
                )
            )
            if attempt_index >= max_recovery_attempts:
                raise MaxRetriesExceededError(attempt_results) from exc
            recovery_failure_summary = format_recovery_failure_summary(
                "Verification before staging failed.",
                exc.verification_results,
            )
            _logger.warning(
                "Verification failed for Issue #%d; "
                "asking agent to recover (%d/%d).",
                issue.number,
                attempt_index + 1,
                max_recovery_attempts,
            )
            continue

        # Phase 3: 检查 PRD 交付（归档已完成 PRD）
        try:
            ensure_prd_delivery_ready(issue, worktree_path, process_runner)
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
            attempt_results.append(
                AttemptResult(
                    attempt_number=attempt_index + 1,
                    failure_type=failure_type,
                    recovered=False,
                    detail=format_prd_delivery_failure(str(exc)),
                )
            )
            if attempt_index >= max_recovery_attempts:
                raise MaxRetriesExceededError(attempt_results) from exc
            recovery_failure_summary = format_prd_delivery_failure(str(exc))
            _logger.warning(
                "PRD delivery check failed for Issue #%d; "
                "asking agent to recover (%d/%d).",
                issue.number,
                attempt_index + 1,
                max_recovery_attempts,
            )
            continue

        # Phase 3.5: Realistic Validation 证据门禁（要求验证且无豁免时）
        try:
            ensure_validation_evidence_ready(issue, worktree_path, config)
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
            attempt_results.append(
                AttemptResult(
                    attempt_number=attempt_index + 1,
                    failure_type=failure_type,
                    recovered=False,
                    detail=format_validation_evidence_failure(str(exc)),
                )
            )
            if attempt_index >= max_recovery_attempts:
                raise MaxRetriesExceededError(attempt_results) from exc
            recovery_failure_summary = format_validation_evidence_failure(str(exc))
            _logger.warning(
                "Validation evidence check failed for Issue #%d; "
                "asking agent to recover (%d/%d).",
                issue.number,
                attempt_index + 1,
                max_recovery_attempts,
            )
            continue

        # Phase 4: Commit proxy — agent 通过 commit-request 文件请求提交
        if has_changes(worktree_path, process_runner):
            _logger.warning(
                "Agent left uncommitted changes for Issue #%d; "
                "runner processing commit request.",
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
                # staging 后验证失败：unstage 并进入 recovery，让 agent 修复
                unstage_changes(worktree_path, process_runner)
                after_sha = get_head_sha(worktree_path, process_runner)
                failure_type = classify_failure(
                    before_sha=before_sha,
                    after_sha=after_sha,
                    has_uncommitted=False,
                    agent_result=CommandResult(("",), 0, "", ""),
                    verification_results=exc.verification_results,
                    exc=None,
                )
                attempt_results.append(
                    AttemptResult(
                        attempt_number=attempt_index + 1,
                        failure_type=failure_type,
                        recovered=False,
                        detail=format_recovery_failure_summary(
                            "Verification after runner staged changes with git add -A failed.",
                            exc.verification_results,
                        ),
                    )
                )
                if attempt_index >= max_recovery_attempts:
                    raise MaxRetriesExceededError(attempt_results) from exc
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
                    attempt_results.append(
                        AttemptResult(
                            attempt_number=attempt_index + 1,
                            failure_type=failure_type,
                            recovered=False,
                            detail=str(exc),
                        )
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
                attempt_results.append(
                    AttemptResult(
                        attempt_number=attempt_index + 1,
                        failure_type=failure_type,
                        recovered=False,
                        detail=f"The runner could not process the commit request.\n{exc}",
                    )
                )
                if attempt_index >= max_recovery_attempts:
                    raise MaxRetriesExceededError(attempt_results) from exc
                recovery_failure_summary = "\n".join(
                    [
                        "The runner could not process the commit request.",
                        str(exc),
                        "Fix the worktree and write a valid commit request JSON.",
                    ]
                )
                _logger.warning(
                    "Commit request failed for Issue #%d; "
                    "asking agent to recover (%d/%d).",
                    issue.number,
                    attempt_index + 1,
                    max_recovery_attempts,
                )
                continue

        # Phase 5: 检查 agent 是否实际产生了 commit
        after_sha = get_head_sha(worktree_path, process_runner)
        if before_sha != after_sha:
            attempt_results.append(
                AttemptResult(
                    attempt_number=attempt_index + 1,
                    failure_type=FailureType.SUCCESS,
                    recovered=attempt_index > 0,
                    detail="Agent produced commits and passed verification.",
                )
            )
            return AgentCommitResult(final_verification_results, attempt_results)

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
        attempt_results.append(
            AttemptResult(
                attempt_number=attempt_index + 1,
                failure_type=failure_type,
                recovered=False,
                detail="Agent produced no git commits.",
            )
        )
        if attempt_index >= max_recovery_attempts:
            raise MaxRetriesExceededError(attempt_results)
        recovery_failure_summary = "\n".join(
            [
                "The previous attempt produced no git commits.",
                "Make the requested code changes and write a valid commit request JSON.",
            ]
        )
        _logger.warning(
            "Agent produced no git commits for Issue #%d; "
            "asking agent to recover (%d/%d).",
            issue.number,
            attempt_index + 1,
            max_recovery_attempts,
        )

    raise MaxRetriesExceededError(attempt_results)


def run_once(
    *,
    repo_path: Path,
    config: AppConfig,
    dry_run: bool,
    agent: str,
    max_issues: int,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> int:
    """Compatibility entry point for the orchestrated single-pass runner."""
    from backend.core.use_cases.agent_runner_orchestrate import run_once as _run_once

    return _run_once(
        repo_path=repo_path,
        config=config,
        dry_run=dry_run,
        agent=agent,
        max_issues=max_issues,
        github_client=github_client,
        process_runner=process_runner,
    )
