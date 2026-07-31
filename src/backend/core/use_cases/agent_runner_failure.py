"""Failure classification and formatting for the agent runner."""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from backend.core.shared.models.agent_runner import (
    AttemptResult,
    CommandResult,
    FailureType,
    PublishFailureCategory,
)
from backend.core.use_cases.agent_runner_feedback import (
    failed_verification_results,
    format_result_for_recovery,
    truncate_recovery_output,
)

__all__ = [
    "AgentRunnerAttemptError",
    "AgentUnavailableError",
    "ForbiddenBlockedError",
    "MaxRetriesExceededError",
    "ProviderCapacityError",
    "PublishFailureError",
    "UnrecoverableError",
    "build_publish_failure_comment_body",
    "classify_failure",
    "detect_usage_limit_root_cause",
    "format_agent_execution_failure",
    "format_attempt_history",
    "format_blocked_failure_comment",
    "format_failure_comment",
    "format_minimal_failure_comment",
    "format_publish_failure_comment",
    "format_recovery_failure_summary",
    "is_provider_capacity_failure",
    "is_recoverable_commit_request_error",
    "is_transient_failure",
]


class AgentRunnerAttemptError(RuntimeError):
    """Base error that carries attempt history."""

    def __init__(
        self,
        message: str,
        attempt_results: list[AttemptResult],
    ) -> None:
        super().__init__(message)
        self.attempt_results = attempt_results


class MaxRetriesExceededError(AgentRunnerAttemptError):
    """Raised when all recovery attempts are exhausted."""

    def __init__(self, attempt_results: list[AttemptResult]) -> None:
        super().__init__(
            f"Failed after {len(attempt_results)} attempts.",
            attempt_results,
        )


class UnrecoverableError(AgentRunnerAttemptError):
    """Raised when an unrecoverable failure is encountered."""

    def __init__(
        self,
        message: str,
        attempt_results: list[AttemptResult],
    ) -> None:
        super().__init__(message, attempt_results)


class ForbiddenBlockedError(AgentRunnerAttemptError):
    """Raised when forbidden paths block the agent and require human intervention."""

    def __init__(
        self,
        message: str,
        attempt_results: list[AttemptResult],
    ) -> None:
        super().__init__(message, attempt_results)


class ProviderCapacityError(AgentRunnerAttemptError):
    """Raised when the agent's model provider is at capacity or rate limited.

    Capacity failures (429 usage limit, 529 overloaded) keep failing on the
    same agent until the provider's usage window resets, so the escalation
    ladder switches to a different agent instead of retrying in place.
    """


class AgentUnavailableError(AgentRunnerAttemptError):
    """Raised when an agent CLI cannot be launched (command not found).

    Treated as agent-specific rather than a business failure: the escalation
    ladder skips the unavailable agent and tries the next one in the
    configured fallback order.
    """

    def __init__(
        self,
        agent: str,
        attempt_results: list[AttemptResult] | None = None,
    ) -> None:
        super().__init__(
            f"Agent '{agent}' is unavailable (command not found).",
            attempt_results or [],
        )
        self.agent = agent


class PublishFailureError(RuntimeError):
    """Raised when the publish phase fails after a local commit exists."""

    def __init__(
        self,
        message: str,
        *,
        worktree_path: Path | None = None,
        failure_category: PublishFailureCategory = PublishFailureCategory.UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.worktree_path = worktree_path
        self.failure_category = failure_category


def is_recoverable_commit_request_error(exc: BaseException) -> bool:
    """Return whether the agent can repair a commit request protocol error.

    CalledProcessError（如 pre-commit hook 失败、git commit 被拒绝）也视为可恢复，
    因为 agent 通常可以通过修改代码来修复这类问题。
    """
    # subprocess 命令失败（pre-commit、git 错误等）通常可由 agent 修复代码后重试
    if isinstance(exc, subprocess.CalledProcessError):
        return True
    message = str(exc)
    return message.startswith(
        (
            "Agent left uncommitted changes without a commit request.",
            "Commit request must be valid JSON.",
            "Commit request must be a JSON object.",
            "Agent requested a commit but produced no file changes.",
        )
    )


def classify_failure(
    *,
    before_sha: str,
    after_sha: str,
    has_uncommitted: bool,
    agent_result: CommandResult,
    verification_results: list[CommandResult],
    exc: BaseException | None = None,
    detect_provider_errors: bool = False,
) -> FailureType:
    """Classify the failure type of an agent execution attempt.

    Priority:
    1. UNRECOVERABLE (security/branch violations)
    2. UNCOMMITTED_CHANGES
    3. PROVIDER_CAPACITY / TRANSIENT (only when ``detect_provider_errors``)
    4. NO_COMMITS
    5. VERIFICATION_FAILED
    6. AGENT_ERROR
    7. SUCCESS

    Args:
        before_sha: SHA before the agent run.
        after_sha: SHA after the agent run.
        has_uncommitted: Whether the worktree has uncommitted changes.
        agent_result: Result of the agent CLI invocation.
        verification_results: Results of verification commands.
        exc: Optional exception raised during the attempt.
        detect_provider_errors: When ``True``, inspect ``exc`` for provider
            capacity / transient network signatures. Only the agent-invocation
            call site sets this; commit and verification failures must not be
            reclassified as transient just because their output mentions a
            network word.

    Returns:
        The classified failure type.
    """
    if exc is not None:
        if isinstance(exc, RuntimeError):
            exc_message = str(exc)
            if "Refusing to publish forbidden paths" in exc_message:
                return FailureType.FORBIDDEN_BLOCKED
            if "Refusing to commit on unexpected branch" in exc_message:
                return FailureType.UNRECOVERABLE
            if is_recoverable_commit_request_error(exc):
                return FailureType.UNCOMMITTED_CHANGES
        if detect_provider_errors:
            if is_provider_capacity_failure(exc):
                return FailureType.PROVIDER_CAPACITY
            if is_transient_failure(exc):
                return FailureType.TRANSIENT
        return FailureType.AGENT_ERROR

    if has_uncommitted:
        return FailureType.UNCOMMITTED_CHANGES

    if before_sha == after_sha:
        return FailureType.NO_COMMITS

    if any(result.return_code != 0 for result in verification_results):
        return FailureType.VERIFICATION_FAILED

    if agent_result.return_code != 0:
        return FailureType.AGENT_ERROR

    return FailureType.SUCCESS


_ATTEMPT_DETAIL_SUMMARY_MAX_LENGTH = 200
_ATTEMPT_DETAIL_SCAFFOLD_LINES = frozenset({"```", "```text", "stdout:", "stderr:"})
_ATTEMPT_START_DISPLAY_FORMAT = "%Y-%m-%d %H:%MZ"
_ATTEMPT_START_MISSING_DISPLAY = "-"
_ATTEMPT_NUMBER_SCOPE_NOTE = (
    "_`Attempt` counts within one agent run: it restarts at 1 on every agent "
    "switch and every re-claim, so use `Started` to tell consecutive runs apart. "
    "Rows are ordered oldest-first._"
)

_USAGE_LIMIT_HINT_PATTERN = re.compile(
    r"usage limit (?:exceeded|reached)|request rejected \(429\)",
    re.IGNORECASE,
)
_USAGE_LIMIT_RESET_AT_PATTERN = re.compile(r"resets at (\S+)")

#: Provider capacity / rate-limit signatures. A superset of the usage-limit
#: hint above: these mean the same agent will keep failing until the provider
#: window resets, so the runner should switch agents rather than retry.
_PROVIDER_CAPACITY_HINT_PATTERN = re.compile(
    r"usage limit (?:exceeded|reached)"
    r"|request rejected \(429\)"
    r"|\b429\b"
    r"|too many requests"
    r"|rate.?limit"
    r"|overloaded"
    r"|\b529\b",
    re.IGNORECASE,
)

#: Transient network / transport signatures. These are worth retrying with the
#: same agent because re-issuing the request frequently succeeds.
_TRANSIENT_HINT_PATTERN = re.compile(
    r"socket connection (?:was )?closed"
    r"|connection reset"
    r"|connection closed"
    r"|connection refused"
    r"|connection error"
    r"|econnreset"
    r"|broken pipe"
    r"|bad gateway"
    r"|service unavailable"
    r"|gateway time-?out"
    r"|temporarily unavailable"
    r"|\b50[234]\b"
    r"|\b400\b"
    r"|invalid params"
    r"|invalid parameter"
    r"|invalid request"
    r"|bad request"
    r"|malformed request"
    r"|input should be a valid dictionary"
    r"|timed out"
    r"|read timeout"
    r"|network error",
    re.IGNORECASE,
)


def _failure_text(exc: BaseException) -> str:
    """Return searchable text for an exception, including subprocess output.

    ``CommandFailedError`` (and other ``CalledProcessError`` subclasses) carry
    the agent's captured stdout/stderr on ``output`` / ``stderr``; the failure
    signature usually lives there rather than in ``str(exc)``.
    """
    parts = [str(exc)]
    for attribute_name in ("output", "stderr"):
        attribute_value = getattr(exc, attribute_name, None)
        if attribute_value:
            parts.append(str(attribute_value))
    return "\n".join(parts)


def is_provider_capacity_failure(exc: BaseException) -> bool:
    """Return whether ``exc`` indicates the provider is at capacity.

    Capacity failures (429 usage limit, 529 overloaded, rate limiting) keep
    failing on the same agent until the provider window resets. The escalation
    ladder treats this as a signal to switch agents.

    Args:
        exc: The exception raised by an agent invocation.

    Returns:
        ``True`` when the exception text matches a provider-capacity signature.
    """
    return _PROVIDER_CAPACITY_HINT_PATTERN.search(_failure_text(exc)) is not None


def is_transient_failure(exc: BaseException) -> bool:
    """Return whether ``exc`` looks like a transient network/transport error.

    Transient failures (dropped sockets, connection resets, gateway timeouts,
    5xx, and occasional provider-side 400 / invalid-parameter responses) often
    succeed on a simple retry with the same agent. Provider-capacity failures
    are intentionally excluded so capacity errors escalate instead of being
    retried in place.

    Args:
        exc: The exception raised by an agent invocation.

    Returns:
        ``True`` when the exception text matches a transient signature and is
        not a provider-capacity failure.
    """
    if is_provider_capacity_failure(exc):
        return False
    return _TRANSIENT_HINT_PATTERN.search(_failure_text(exc)) is not None


#: Workflow labels that represent a completed implementation phase. When a
#: failure occurs while transitioning to one of these labels, the agent's local
#: work is already done; recovery should retry the transition rather than
#: re-run the agent from ``agent/ready``.
_COMPLETION_WORKFLOW_LABELS = frozenset({"agent/supervising", "agent/review"})

#: Matches ``gh issue edit <number> ... --add-label <label>`` and captures the
#: target label, regardless of option order or extra flags.
_GH_ISSUE_EDIT_ADD_LABEL_PATTERN = re.compile(
    r"gh issue edit\s+\d+\s+.*--add-label\s+(\S+)",
    re.IGNORECASE,
)


def _detect_transition_target_label(exc: BaseException) -> str | None:
    """Return the target workflow label if ``exc`` is a failed transition.

    The GitHub CLI labels the transition command as
    ``gh issue edit <n> --add-label <target> --remove-label <current>``.
    When this command fails after the agent has already finished its work,
    the operator should retry the transition instead of re-running the agent.

    Args:
        exc: The exception that caused the failure.

    Returns:
        The target label string if one can be parsed, otherwise ``None``.
    """
    if not isinstance(exc, subprocess.CalledProcessError):
        return None
    cmd = getattr(exc, "cmd", None)
    if cmd is None:
        return None
    if isinstance(cmd, (list, tuple)):
        cmd_str = " ".join(str(part) for part in cmd)
    else:
        cmd_str = str(cmd)
    match = _GH_ISSUE_EDIT_ADD_LABEL_PATTERN.search(cmd_str)
    if match is None:
        return None
    return match.group(1)


def _is_completion_workflow_label(label: str) -> bool:
    """Return whether ``label`` represents a post-implementation workflow state."""
    return label in _COMPLETION_WORKFLOW_LABELS


def detect_usage_limit_root_cause(failure_text: str) -> str | None:
    """Return a human-readable root-cause line for API usage limit failures.

    Args:
        failure_text: Combined exception messages and attempt details to inspect.

    Returns:
        A Markdown summary line with the reset time when available,
        or None when no usage-limit signature is found.
    """
    if _USAGE_LIMIT_HINT_PATTERN.search(failure_text) is None:
        return None
    reset_match = _USAGE_LIMIT_RESET_AT_PATTERN.search(failure_text)
    if reset_match is not None:
        reset_at = reset_match.group(1).rstrip(".,;)")
        return (
            "**Root cause:** Claude API usage limit reached (429). "
            f"The limit resets at `{reset_at}`; retries before then will fail the same way."
        )
    return (
        "**Root cause:** Claude API usage limit reached (429). "
        "Retries will keep failing until the usage window resets."
    )


def _summarize_attempt_detail(detail: str) -> str:
    """Pick the most informative single line of an attempt detail.

    Attempt details are multi-line reports whose actual error almost always
    sits on the last content line (command output tail, exception message),
    so the summary keeps that line instead of head-truncating boilerplate.
    """
    stripped_lines = (line.strip() for line in detail.splitlines())
    informative_lines = [
        line for line in stripped_lines if line and line not in _ATTEMPT_DETAIL_SCAFFOLD_LINES
    ]
    if not informative_lines:
        return ""
    summary = informative_lines[-1].replace("|", "\\|")
    if len(summary) > _ATTEMPT_DETAIL_SUMMARY_MAX_LENGTH:
        return summary[: _ATTEMPT_DETAIL_SUMMARY_MAX_LENGTH - 1] + "…"
    return summary


def _format_attempt_start(started_at: str) -> str:
    """Render an attempt's ISO-8601 UTC start time for the history table.

    表格里的 ``Attempt`` 编号只在"本轮 agent 运行"内递增，跨 agent fallback 和
    重新 claim 都会从 1 重新开始，而实时评论把同一个 Issue 的全部轨迹渲染进同一
    张表，因此必须给每行一个时间锚点，读者才能分辨相邻两行属于同一轮还是两轮。

    历史行可能没有 ``started_at``（旧库记录，或没有配置 SQLite 时的内存兜底），
    空值渲染为 ``-``，无法解析的值原样回显，不让整张表因为一个时间戳失败。
    不带时区的取值按 UTC 解释，与 ``AttemptResult.started_at`` 的约定一致。
    """
    if not started_at:
        return _ATTEMPT_START_MISSING_DISPLAY
    try:
        started_datetime = datetime.fromisoformat(started_at)
    except ValueError:
        return started_at
    if started_datetime.tzinfo is None:
        started_datetime = started_datetime.replace(tzinfo=timezone.utc)
    return started_datetime.astimezone(timezone.utc).strftime(_ATTEMPT_START_DISPLAY_FORMAT)


_MAX_RENDERED_PHASES = 3
_PHASE_RENDER_FLOOR_SECONDS = 0.5


def format_attempt_duration(result: AttemptResult) -> str:
    """把 attempt 总时长渲染成"总时长（耗时最大的几个阶段）"。

    只显示一个总时长时，"卡了很久"无法定位——几千秒可能是 agent 自己在想，也可能
    是 ``just test`` 挂住或某条 RV 命令一路超时。这里把耗时最大的几个阶段直接摊在
    表格里，一眼能看出时间去哪了；阶段明细缺失（旧记录）时退回纯总时长。

    Args:
        result: 要渲染的 attempt 记录。

    Returns:
        形如 ``7153.9s (agent 7100.2s · verification 40.1s)`` 的单元格文本。
    """
    total_duration = f"{result.duration_seconds:.1f}s"
    rendered_phases = [
        f"{phase.name} {phase.seconds:.1f}s"
        for phase in result.phase_durations[:_MAX_RENDERED_PHASES]
        if phase.seconds >= _PHASE_RENDER_FLOOR_SECONDS
    ]
    if not rendered_phases:
        return total_duration
    return f"{total_duration} ({' · '.join(rendered_phases)})"


def format_attempt_history(
    attempt_results: list[AttemptResult],
    *,
    include_title: bool = True,
) -> str:
    """Format attempt results as a Markdown table.

    Args:
        attempt_results: Attempts to render, oldest first.
        include_title: Whether to prefix the table with its own heading. The
            live attempt-history comment already carries one, so it renders the
            table headless instead of repeating the title.
    """
    if not attempt_results:
        return ""

    lines = ["### Attempt History", ""] if include_title else []
    lines.extend(
        [
            "| Attempt | Started (UTC) | Agent | Failure Type | Recovered | Duration | Detail |",
            "|---------|---------------|-------|-------------|-----------|----------|--------|",
        ]
    )
    for result in attempt_results:
        detail = _summarize_attempt_detail(result.detail)
        recovered = "Yes" if result.recovered else "No"
        agent = result.agent or "-"
        duration = format_attempt_duration(result)
        lines.append(
            f"| {result.attempt_number} | {_format_attempt_start(result.started_at)} | {agent} | "
            f"{result.failure_type.value} | {recovered} | {duration} | {detail} |"
        )
    lines.extend(["", _ATTEMPT_NUMBER_SCOPE_NOTE])
    return "\n".join(lines)


def format_failure_comment(
    exc: BaseException,
    attempt_results: list[AttemptResult] | None = None,
    *,
    issue_number: int | None = None,
) -> str:
    """Build a failure comment with root-cause summary and attempt history.

    Known failure signatures (currently API usage limits) are surfaced as a
    bolded root-cause line at the top. A ``CalledProcessError`` cause is
    rendered with the short command name and truncated output instead of
    ``str(exc)``, which would echo the entire agent prompt.

    Args:
        exc: The exception that caused the failure.
        attempt_results: Optional attempt history rendered as a table.
        issue_number: When provided, a recovery guidance section with a
            copy-pastable relabel command is appended to the comment. If the
            exception is a failed workflow-label transition to a completion
            state (e.g. ``agent/supervising``), the guidance retries that
            transition instead of falling back to ``agent/ready``.
    """
    cause = exc.__cause__
    if isinstance(cause, subprocess.CalledProcessError):
        cause_text = "\n".join([str(cause.output or ""), str(cause.stderr or "")])
    elif cause is not None:
        cause_text = str(cause)
    else:
        cause_text = ""
    searchable_failure_text = "\n".join(
        [str(exc), cause_text] + [result.detail for result in attempt_results or []]
    )

    lines = ["## Agent Runner Failed", ""]

    root_cause_summary = detect_usage_limit_root_cause(searchable_failure_text)
    if root_cause_summary is not None:
        lines.extend([root_cause_summary, ""])

    if attempt_results:
        lines.append(format_attempt_history(attempt_results))
        lines.append("")

    lines.extend(["```text", summarize_failure_exception(exc), "```", ""])
    if isinstance(cause, subprocess.CalledProcessError):
        lines.extend([format_agent_execution_failure(cause), ""])
    elif cause is not None:
        lines.extend(["```text", summarize_failure_exception(cause), "```", ""])
    if issue_number is not None:
        transition_label = _detect_transition_target_label(exc)
        if transition_label is not None and _is_completion_workflow_label(transition_label):
            lines.extend(
                [
                    "### How To Recover",
                    "",
                    "The agent finished its work, but the final workflow "
                    "label transition failed. You can retry the transition "
                    "without re-running the agent:",
                    "",
                    "```bash",
                    f"gh issue edit {issue_number} "
                    f"--add-label {transition_label} --remove-label agent/failed",
                    "```",
                    "",
                    "If the worktree is dirty, remove it first "
                    f"(`git worktree remove <repo>-worktrees/tasks/issue-{issue_number}`). "
                    "See the 失败重跑 section in `docs/guides/agent-runner.md` "
                    "for the full procedure.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "### How To Recover",
                    "",
                    "After fixing the root cause, relabel the Issue so the "
                    "runner picks it up on its next poll:",
                    "",
                    "```bash",
                    f"gh issue edit {issue_number} "
                    "--add-label agent/ready --remove-label agent/failed",
                    "```",
                    "",
                    "If the worktree is dirty, remove it first "
                    f"(`git worktree remove <repo>-worktrees/tasks/issue-{issue_number}`). "
                    "See the 失败重跑 section in `docs/guides/agent-runner.md` "
                    "for the full procedure.",
                    "",
                ]
            )
    return "\n".join(lines)


def format_minimal_failure_comment(exc: BaseException, *, issue_number: int | None = None) -> str:
    """Build a compact failure comment used as a posting fallback.

    The full failure report embeds agent command output, which GitHub can
    reject (oversized or control characters). When that happens the caller
    falls back to this short, structured summary so the operator still learns
    that the run failed and how to recover, instead of the reason being lost.

    Args:
        exc: The exception that caused the failure.
        issue_number: When provided, a copy-pastable relabel command is
            appended so the operator can re-queue the Issue. If the exception
            is a failed workflow-label transition to a completion state, the
            guidance retries that transition instead of ``agent/ready``.
    """
    exc_message = str(exc).strip()
    failure_summary = exc_message.splitlines()[0] if exc_message else type(exc).__name__
    lines = [
        "## Agent Runner Failed",
        "",
        "The full failure report could not be posted to GitHub; "
        "showing a summary instead. See the runner logs for full details.",
        "",
        f"- Failure: `{failure_summary}`",
    ]
    if issue_number is not None:
        transition_label = _detect_transition_target_label(exc)
        if transition_label is not None and _is_completion_workflow_label(transition_label):
            lines.extend(
                [
                    "",
                    "### How To Recover",
                    "",
                    "The agent finished its work, but the final workflow "
                    "label transition failed. You can retry the transition "
                    "without re-running the agent:",
                    "",
                    "```bash",
                    f"gh issue edit {issue_number} "
                    f"--add-label {transition_label} --remove-label agent/failed",
                    "```",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "### How To Recover",
                    "",
                    "After fixing the root cause, relabel the Issue so the "
                    "runner picks it up on its next poll:",
                    "",
                    "```bash",
                    f"gh issue edit {issue_number} "
                    "--add-label agent/ready --remove-label agent/failed",
                    "```",
                ]
            )
    return "\n".join(lines)


def format_publish_failure_comment(
    exc: BaseException,
    issue_number: int,
    *,
    worktree_path: Path | None = None,
    failure_category: PublishFailureCategory = PublishFailureCategory.UNKNOWN,
) -> str:
    """Build a failure comment for publish phase failures.

    Args:
        exc: The exception that caused the failure.
        issue_number: GitHub Issue number.
        worktree_path: Path to the worktree, if available.
        failure_category: Category of the publish failure.

    Returns:
        Markdown comment body.
    """
    return build_publish_failure_comment_body(
        header="## Agent Runner Publish Failed",
        intro="The agent produced a local commit but publishing failed.",
        action_intro="To resume publishing without re-running the agent:",
        issue_number=issue_number,
        failure_category=failure_category.value,
        worktree_path=worktree_path,
        exc=exc,
    )


def build_publish_failure_comment_body(
    *,
    header: str,
    intro: str,
    action_intro: str,
    issue_number: int,
    failure_category: str,
    worktree_path: Path | None,
    exc: BaseException,
) -> str:
    """Build the shared body for publish / publish-recovery failure comments.

    Both the publish phase and the ``iar recover`` flow render the same
    structure (failure category, optional worktree, error text + cause, and the
    ``iar recover`` retry hint), differing only in the heading, intro line, and
    action sentence.

    Args:
        header: Markdown heading line (e.g. ``"## Agent Runner Publish Failed"``).
        intro: One-line description shown under the heading.
        action_intro: Sentence introducing the ``iar recover`` command block.
        issue_number: GitHub Issue number used in the recover command.
        failure_category: Human-readable publish failure category.
        worktree_path: Worktree path to surface, if available.
        exc: The exception that caused the failure.

    Returns:
        Markdown comment body.
    """
    lines = [
        header,
        "",
        intro,
        "",
        f"- Failure category: `{failure_category}`",
    ]

    if worktree_path is not None:
        lines.append(f"- Worktree: `{worktree_path}`")

    lines.extend(
        [
            "",
            "```text",
            str(exc),
            "",
        ]
    )

    if exc.__cause__ is not None:
        lines.append(str(exc.__cause__))

    lines.extend(
        [
            "```",
            "",
            action_intro,
            "",
            "```bash",
            f"uv run iar recover --issue {issue_number}",
            "```",
        ]
    )

    return "\n".join(lines)


def format_blocked_failure_comment(
    exc: BaseException,
    attempt_results: list[AttemptResult] | None = None,
    *,
    issue_number: int | None = None,
) -> str:
    """Build a blocked failure comment for forbidden path interception.

    Args:
        exc: The exception that caused the blockage.
        attempt_results: Optional attempt history rendered as a table.
        issue_number: When provided, recovery guidance is appended.
    """
    exc_message = str(exc)
    blocked_paths: list[str] = []
    if "Refusing to publish forbidden paths:" in exc_message:
        paths_part = exc_message.split("Refusing to publish forbidden paths:", 1)[1]
        blocked_paths = [p.strip() for p in paths_part.split(",") if p.strip()]

    lines = [
        "## Agent Runner Blocked",
        "",
        "The agent was blocked because it attempted to modify forbidden paths.",
        "",
        "- Block type: `blocked_forbidden`",
    ]

    if blocked_paths:
        lines.append("- Blocked paths:")
        for path in blocked_paths:
            lines.append(f"  - `{path}`")

    lines.extend(
        [
            "",
            "### How to Resume",
            "",
            "1. Review the blocked files above.",
            "2. Resolve each file appropriately (commit, revert, or modify).",
            "3. Ensure the worktree is clean (`git status` shows no pending changes).",
            "4. Run the following command to continue:",
            "",
            "```bash",
            f"uv run iar blocked-continue --issue {issue_number}",
            "```",
            "",
        ]
    )

    if attempt_results:
        lines.append(format_attempt_history(attempt_results))
        lines.append("")

    lines.extend(["```text", exc_message, "```", ""])
    return "\n".join(lines)


def format_recovery_failure_summary(
    heading: str,
    verification_results: list[CommandResult],
) -> str:
    """Build the failure section for a verification recovery prompt."""
    failed_results = failed_verification_results(verification_results)
    if not failed_results:
        return heading
    result_sections = "\n\n".join(format_result_for_recovery(result) for result in failed_results)
    return "\n\n".join([heading, result_sections])


def format_agent_execution_failure(exc: BaseException) -> str:
    """Build the failure section for a failed agent CLI invocation."""
    lines = ["Agent command failed before runner verification could start."]
    if isinstance(exc, subprocess.CalledProcessError):
        lines.extend(
            [
                f"Command: `{_agent_command_name(exc.cmd)}`",
                f"Exit code: {exc.returncode}",
                "stdout:",
                "```text",
                truncate_recovery_output(str(exc.output or "")),
                "```",
                "stderr:",
                "```text",
                truncate_recovery_output(str(exc.stderr or "")),
                "```",
            ]
        )
        if not exc.output and not exc.stderr:
            lines.append("The command streamed its details to the terminal.")
    else:
        lines.extend(
            [
                f"Exception type: {type(exc).__name__}",
                "Exception:",
                "```text",
                truncate_recovery_output(summarize_failure_exception(exc)),
                "```",
            ]
        )
    return "\n".join(lines)


def summarize_failure_exception(exc: BaseException) -> str:
    """Render an exception for failure comments without echoing the agent prompt.

    ``subprocess.TimeoutExpired.__str__`` 把整条命令原样拼进消息里,而 agent 的
    命令里带着完整 prompt——一次 verifier 超时因此让 Issue 评论变成几千行 prompt,
    真正有用的信息(谁超时了、超时前输出了什么)反而被埋掉。``CalledProcessError``
    早就在这里做了短命令名 + 截断输出的处理,超时只是漏了同一条路。

    非超时异常按 ``str(exc)`` 原样返回,保持既有行为不变。
    """
    if not isinstance(exc, subprocess.TimeoutExpired):
        return str(exc)
    summary_lines = [
        f"Command `{_agent_command_name(exc.cmd)}` timed out after {exc.timeout}s "
        "and was terminated."
    ]
    for stream_label, stream_content in (("stdout", exc.output), ("stderr", exc.stderr)):
        if not stream_content:
            continue
        summary_lines.extend(
            [
                f"Partial {stream_label} captured before the kill:",
                truncate_recovery_output(str(stream_content)),
            ]
        )
    if not exc.output and not exc.stderr:
        summary_lines.append("No output was captured before the kill.")
    return "\n".join(summary_lines)


def _agent_command_name(command: object) -> str:
    """Return a short command name without echoing a potentially huge prompt."""
    if isinstance(command, (list, tuple)) and command:
        return str(command[0])
    return str(command)
