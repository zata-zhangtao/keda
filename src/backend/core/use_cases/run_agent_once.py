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
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from backend.core.agent.memory import (
    save_short_term_memory,
)
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
    AgentUnavailableError,
    MaxRetriesExceededError,
    ProviderCapacityError,
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
    is_transient_failure,
)
from backend.core.use_cases.agent_runner_feedback import (
    PrdDeliveryError,
    VerificationFailedError,
    build_fix_prompt,
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
    build_validation_prompt_line,
    ensure_evidence_dir_excluded,
)
from backend.core.use_cases.agent_runner_worktree_branch import (
    _ensure_worktree_branch,
    _reconcile_worktree_with_remote_branch,
)
from backend.core.use_cases.worktree_env import copy_missing_env_files
from backend.core.use_cases.worktree_database import (
    WorktreeDatabaseProvisionRequest,
    provision_worktree_database,
)
from backend.core.use_cases.worktree_frontend import (
    ensure_frontend_node_modules,
    exclude_frontend_node_modules_from_git,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "AgentRunnerAttemptError",
    "AgentUnavailableError",
    "MaxRetriesExceededError",
    "PrdDeliveryError",
    "ProviderCapacityError",
    "PublishFailureError",
    "EmptyCommitRequestError",
    "UnrecoverableError",
    "VerificationFailedError",
    "_ensure_worktree_branch",
    "_reconcile_worktree_with_remote_branch",
    "build_blocked_continuation_prompt",
    "build_fix_prompt",
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
    "resolve_agent_fallback_order",
    "resolve_prd_archive_path",
    "run_agent",
    "run_agent_until_committed",
    "run_agent_with_prompt",
    "run_agent_with_prompt_resilient",
    "run_fix_agent",
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
                "Command template references {base_branch} but no base_branch was provided."
            )
        return shlex.split(template.format(issue_number=issue_number, base_branch=base_branch))
    return shlex.split(template.format(issue_number=issue_number))


def choose_agent(issue: IssueSummary, config: AppConfig, override_agent: str) -> str:
    """Choose an AI agent for the Issue."""
    if override_agent != "auto":
        return override_agent
    for agent_name, label in config.labels.agent_labels.items():
        if label in issue.labels:
            return agent_name
    return config.runner.default_agent if config.runner.default_agent != "auto" else "claude"


def resolve_agent_fallback_order(
    issue: IssueSummary,
    config: AppConfig,
    override_agent: str,
) -> list[str]:
    """Return the ordered list of agents to try for an Issue.

    The first entry is the primary agent resolved by :func:`choose_agent`.
    Subsequent entries come from ``config.runner.agent_fallback_order`` with the
    primary agent and duplicates removed, preserving configured order. When no
    fallback order is configured the list contains only the primary agent, so
    the escalation ladder behaves exactly like single-agent runs.

    Args:
        issue: Issue being processed.
        config: Agent Runner configuration.
        override_agent: The ``--agent`` override (``"auto"`` routes by label).

    Returns:
        Ordered, de-duplicated agent names to attempt.
    """
    primary_agent = choose_agent(issue, config, override_agent)
    fallback_order = [primary_agent]
    for candidate_agent in config.runner.agent_fallback_order:
        normalized_agent = candidate_agent.strip()
        if normalized_agent and normalized_agent not in fallback_order:
            fallback_order.append(normalized_agent)
    return fallback_order


# Serializes the shared-repository git mutation in worktree creation across
# parallel Issue workers (``iar daemon --concurrency``). ``git worktree add``
# writes the main repo's ``.git/worktrees`` and refs, which can race when
# several issues are set up at once. Uncontended (no overhead) in the default
# sequential path. The long agent run happens after this lock is released.
_SHARED_GIT_LOCK = threading.Lock()


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
    # Hold the shared-git lock only for the worktree-add writes; the agent run
    # (the long pole) happens later in the caller, outside this lock.
    with _SHARED_GIT_LOCK:
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
    if config.worktree.provision_database:
        provision_worktree_database(
            WorktreeDatabaseProvisionRequest(
                repository_path=repo_path,
                worktree_path=worktree_path,
                issue_number=issue.number,
            ),
            process_runner,
        )
    # node_modules is gitignored, so `git worktree add` never materializes it.
    # Install deps directly in the worktree when a lockfile is present; this is
    # the only form guaranteed to work with every frontend toolchain (including
    # Next.js/Turbopack). If no lockfile exists or the install fails, fall back
    # to symlinking from the main checkout. Reused worktrees are healed the same
    # way; existing node_modules are left untouched.
    installed_frontend_paths, linked_frontend_paths = ensure_frontend_node_modules(
        repo_path, worktree_path, process_runner
    )
    if linked_frontend_paths:
        exclude_frontend_node_modules_from_git(worktree_path, linked_frontend_paths, process_runner)
    handled_frontend_paths = installed_frontend_paths + linked_frontend_paths
    if handled_frontend_paths:
        _logger.info(
            "Prepared node_modules for %d frontend project(s) in worktree %s: "
            "installed=%s, linked=%s",
            len(handled_frontend_paths),
            worktree_path,
            ", ".join(str(frontend_path) for frontend_path in installed_frontend_paths),
            ", ".join(str(frontend_path) for frontend_path in linked_frontend_paths),
        )
    expected_branch = f"issue-{issue.number}"
    _ensure_worktree_branch(worktree_path, expected_branch, issue, config, process_runner)
    _reconcile_worktree_with_remote_branch(worktree_path, config, process_runner)
    return worktree_path


def _resolve_repo_id(issue: IssueSummary, worktree_path: Path) -> str:
    """Derive a stable per-repository identifier for short-term memory paths.

    Uses the worktree directory name as a stable stand-in when no registry
    lookup is available. Kept dependency-light on purpose: this function
    lives in the ``core/`` layer and must not reach into ``engines/`` or
    ``infrastructure/`` to read the registry.
    """
    try:
        return worktree_path.resolve().name or "default"
    except OSError:
        return "default"


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


def _build_verification_commands_summary(
    config: AppConfig,
) -> str:
    """Return a human-readable list of configured verification commands."""
    commands = config.runner.verification_commands
    if not commands:
        return "No verification commands configured."
    return "\n".join(f"- `{command}`" for command in commands)


def run_agent(
    agent_name: str,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    *,
    timeout_seconds: int | None = None,
    inactivity_timeout_seconds: int | None = None,
) -> CommandResult:
    """Run Codex or Claude Code in non-interactive mode."""
    long_term_store, skill_store = _resolve_memory_stores(worktree_path, config.memory)
    prompt = build_prompt(
        issue,
        worktree_path,
        config.prompts,
        phase="execution",
        validation_line=build_validation_prompt_line(issue, config),
        verification_commands_summary=_build_verification_commands_summary(config),
        memory_config=config.memory,
        long_term_store=long_term_store,
        skill_store=skill_store,
    )
    return run_agent_with_prompt_resilient(
        agent_name,
        prompt,
        worktree_path,
        process_runner,
        issue=issue,
        transient_retry_attempts=config.runner.transient_retry_attempts,
        transient_retry_delay_seconds=config.runner.transient_retry_delay_seconds,
        timeout_seconds=timeout_seconds,
        inactivity_timeout_seconds=inactivity_timeout_seconds,
    )


def run_fix_agent(
    agent_name: str,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    verification_results: list[CommandResult],
) -> CommandResult:
    """Run a focused Fix Agent for simple local verification failures.

    The Fix Agent prompt only contains the current verification failure and
    constraints; it does not ask the agent to update PRD checklists, evidence,
    or other global deliverables.

    Args:
        agent_name: Agent to invoke.
        issue: Current Issue.
        worktree_path: Agent worktree path.
        config: Agent Runner configuration.
        process_runner: Command executor.
        verification_results: Failed verification results to repair.

    Returns:
        The Fix Agent command result.
    """
    prompt = build_fix_prompt(
        issue,
        worktree_path,
        verification_results=verification_results,
        verification_commands_summary=_build_verification_commands_summary(config),
    )
    fix_timeout = config.runner.fix_timeout_seconds or config.runner.timeout_seconds
    _logger.info(
        "Starting Fix Agent for Issue #%d (timeout=%ss).",
        issue.number,
        fix_timeout,
    )
    return run_agent_with_prompt_resilient(
        agent_name,
        prompt,
        worktree_path,
        process_runner,
        issue=issue,
        transient_retry_attempts=config.runner.transient_retry_attempts,
        transient_retry_delay_seconds=config.runner.transient_retry_delay_seconds,
        timeout_seconds=fix_timeout,
        inactivity_timeout_seconds=config.runner.inactivity_timeout_seconds,
    )


def run_agent_with_prompt(
    agent_name: str,
    prompt: str,
    worktree_path: Path,
    process_runner: IProcessRunner,
    *,
    capture_output: bool = False,
    timeout_seconds: int | None = None,
    inactivity_timeout_seconds: int | None = None,
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
    run_kwargs: dict[str, object] = {
        "command": command,
        "cwd": worktree_path,
        "capture_output": capture_output,
        "timeout": timeout_seconds,
        "label": label,
    }
    if inactivity_timeout_seconds is not None:
        run_kwargs["inactivity_timeout"] = inactivity_timeout_seconds
    result = process_runner.run(**run_kwargs)
    if issue is not None:
        _logger.info(
            "Agent finished for Issue #%d: %s (exit_code=%d)",
            issue.number,
            issue.url,
            result.return_code,
        )
    return result


def run_agent_with_prompt_resilient(
    agent_name: str,
    prompt: str,
    worktree_path: Path,
    process_runner: IProcessRunner,
    *,
    capture_output: bool = False,
    timeout_seconds: int | None = None,
    inactivity_timeout_seconds: int | None = None,
    issue: IssueSummary | None = None,
    transient_retry_attempts: int = 2,
    transient_retry_delay_seconds: int = 10,
) -> CommandResult:
    """Run an agent, retrying transient network/transport failures in place.

    Level 1 of the escalation ladder. Only :func:`is_transient_failure` errors
    (dropped sockets, connection resets, gateway timeouts, 5xx) are retried with
    the same agent, because re-issuing the request usually succeeds. A missing
    agent CLI is surfaced as :class:`AgentUnavailableError` so the orchestration
    layer can skip to the next agent; every other error propagates unchanged so
    the recovery loop or the cross-agent fallback can handle it.

    Args:
        agent_name: Agent to invoke (claude / codex / kimi).
        prompt: Prepared prompt text.
        worktree_path: Worktree the agent runs in.
        process_runner: Command executor.
        capture_output: Whether to capture stdout/stderr.
        timeout_seconds: Optional per-invocation timeout.
        inactivity_timeout_seconds: Optional no-output timeout.
        issue: Optional Issue for logging context.
        transient_retry_attempts: Extra retries granted to transient failures.
        transient_retry_delay_seconds: Backoff between transient retries.

    Returns:
        The successful :class:`CommandResult`.

    Raises:
        AgentUnavailableError: The agent CLI could not be launched.
        Exception: The original error when it is not transient or retries are
            exhausted.
    """
    max_retries = max(0, transient_retry_attempts)
    issue_number = issue.number if issue is not None else 0
    for retry_index in range(max_retries + 1):
        try:
            return run_agent_with_prompt(
                agent_name,
                prompt,
                worktree_path,
                process_runner,
                capture_output=capture_output,
                timeout_seconds=timeout_seconds,
                inactivity_timeout_seconds=inactivity_timeout_seconds,
                issue=issue,
            )
        except FileNotFoundError as exc:
            raise AgentUnavailableError(agent_name) from exc
        except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
            if retry_index >= max_retries or not is_transient_failure(exc):
                raise
            _logger.warning(
                "Transient error from agent '%s' for Issue #%d; retrying (%d/%d): %s",
                agent_name,
                issue_number,
                retry_index + 1,
                max_retries,
                exc,
            )
            wait_before_recovery_attempt(
                issue_number,
                recovery_attempt=retry_index + 1,
                max_recovery_attempts=max_retries,
                delay_seconds=transient_retry_delay_seconds,
            )
    raise RuntimeError("unreachable: resilient agent retry loop exited")


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


def _make_attempt_result(
    *,
    attempt_number: int,
    failure_type: FailureType,
    recovered: bool,
    detail: str,
    agent: str,
    started_mono: float,
    started_iso: str,
) -> AttemptResult:
    """Build an ``AttemptResult`` with wall-clock timing filled in now."""
    finished_mono = time.monotonic()
    finished_iso = datetime.now(timezone.utc).isoformat()
    return AttemptResult(
        attempt_number=attempt_number,
        failure_type=failure_type,
        recovered=recovered,
        detail=detail,
        agent=agent,
        started_at=started_iso,
        finished_at=finished_iso,
        duration_seconds=round(finished_mono - started_mono, 3),
    )


def _append_attempt_and_notify(
    attempt_results: list[AttemptResult],
    result: AttemptResult,
    on_attempt_recorded: Callable[[AttemptResult, list[AttemptResult]], None] | None,
) -> None:
    """Append a result and notify the incremental persistence callback."""
    attempt_results.append(result)
    if on_attempt_recorded is not None:
        try:
            on_attempt_recorded(result, list(attempt_results))
        except Exception:  # noqa: BLE001 - persistence side-channel must not break runs
            _logger.warning(
                "Attempt persistence callback failed for attempt %d; continuing.",
                result.attempt_number,
                exc_info=True,
            )


def _resolve_memory_stores(worktree_path: Path, memory_config):
    """Construct the long-term + skill stores for prompt injection.

    Returns ``(None, None)`` when memory is disabled so callers can fall
    back to non-injecting behaviour without sprinkling the same guard
    everywhere. The actual composition lives in
    ``core/agent/memory/_composition.py`` which dynamically loads the
    ``infrastructure/`` implementations, preserving the strict
    ``core -> infrastructure`` ban.
    """
    from backend.core.agent.memory._composition import (
        build_default_memory_services,
    )

    services = build_default_memory_services(worktree_path, memory_config)
    return services.long_term, services.skill


def _persist_short_term_memory(
    *,
    config: AppConfig,
    issue: IssueSummary,
    worktree_path: Path,
    attempt: AttemptResult,
    repo_id: str,
) -> None:
    """Best-effort save of a single attempt into the short-term memory store."""
    if not config.memory.enabled:
        return
    try:
        from backend.core.agent.memory._composition import (
            build_default_memory_services,
        )

        services = build_default_memory_services(worktree_path, config.memory)
        if services.short_term is None:
            return
        save_short_term_memory(
            repo_id=repo_id,
            issue=issue,
            attempt_result=attempt,
            worktree_path=worktree_path,
            memory_config=config.memory,
            store=services.short_term,
        )
    except Exception as exc:  # noqa: BLE001 - memory side-channel must not break runner.
        _logger.warning(
            "Failed to record short-term memory for Issue #%d attempt %d: %s",
            issue.number,
            attempt.attempt_number,
            exc,
        )


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
    on_attempt_recorded: Callable[[AttemptResult, list[AttemptResult]], None] | None = None,
) -> AgentCommitResult:
    """运行 Agent recovery 状态机并返回最终提交结果。"""
    from backend.core.use_cases.run_agent_execution_loop import (
        AgentExecutionRequest,
        run_agent_until_committed as run_execution_loop,
    )

    return run_execution_loop(
        AgentExecutionRequest(
            selected_agent=selected_agent,
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=process_runner,
            before_sha=before_sha,
            expected_branch=expected_branch,
            prompt_override=prompt_override,
            on_attempt_recorded=on_attempt_recorded,
        )
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
