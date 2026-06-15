"""Failure classification and formatting for the agent runner."""

from __future__ import annotations

import re
import subprocess
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
    "ForbiddenBlockedError",
    "MaxRetriesExceededError",
    "PublishFailureError",
    "UnrecoverableError",
    "classify_failure",
    "detect_usage_limit_root_cause",
    "format_agent_execution_failure",
    "format_attempt_history",
    "format_blocked_failure_comment",
    "format_failure_comment",
    "format_minimal_failure_comment",
    "format_publish_failure_comment",
    "format_recovery_failure_summary",
    "is_recoverable_commit_request_error",
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
) -> FailureType:
    """Classify the failure type of an agent execution attempt.

    Priority:
    1. UNRECOVERABLE (security/branch violations)
    2. UNCOMMITTED_CHANGES
    3. NO_COMMITS
    4. VERIFICATION_FAILED
    5. AGENT_ERROR
    6. SUCCESS

    Args:
        before_sha: SHA before the agent run.
        after_sha: SHA after the agent run.
        has_uncommitted: Whether the worktree has uncommitted changes.
        agent_result: Result of the agent CLI invocation.
        verification_results: Results of verification commands.
        exc: Optional exception raised during the attempt.

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

_USAGE_LIMIT_HINT_PATTERN = re.compile(
    r"usage limit (?:exceeded|reached)|request rejected \(429\)",
    re.IGNORECASE,
)
_USAGE_LIMIT_RESET_AT_PATTERN = re.compile(r"resets at (\S+)")


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
        line
        for line in stripped_lines
        if line and line not in _ATTEMPT_DETAIL_SCAFFOLD_LINES
    ]
    if not informative_lines:
        return ""
    summary = informative_lines[-1].replace("|", "\\|")
    if len(summary) > _ATTEMPT_DETAIL_SUMMARY_MAX_LENGTH:
        return summary[: _ATTEMPT_DETAIL_SUMMARY_MAX_LENGTH - 1] + "…"
    return summary


def format_attempt_history(attempt_results: list[AttemptResult]) -> str:
    """Format attempt results as a Markdown table."""
    if not attempt_results:
        return ""

    lines = [
        "### Attempt History",
        "",
        "| Attempt | Failure Type | Recovered | Detail |",
        "|---------|-------------|-----------|--------|",
    ]
    for result in attempt_results:
        detail = _summarize_attempt_detail(result.detail)
        recovered = "Yes" if result.recovered else "No"
        lines.append(
            f"| {result.attempt_number} | {result.failure_type.value} | {recovered} | {detail} |"
        )
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
            copy-pastable relabel command is appended to the comment.
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

    lines.extend(["```text", str(exc), "```", ""])
    if isinstance(cause, subprocess.CalledProcessError):
        lines.extend([format_agent_execution_failure(cause), ""])
    elif cause is not None:
        lines.extend(["```text", str(cause), "```", ""])
    if issue_number is not None:
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


def format_minimal_failure_comment(
    exc: BaseException, *, issue_number: int | None = None
) -> str:
    """Build a compact failure comment used as a posting fallback.

    The full failure report embeds agent command output, which GitHub can
    reject (oversized or control characters). When that happens the caller
    falls back to this short, structured summary so the operator still learns
    that the run failed and how to recover, instead of the reason being lost.

    Args:
        exc: The exception that caused the failure.
        issue_number: When provided, a copy-pastable relabel command is
            appended so the operator can re-queue the Issue.
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
    lines = [
        "## Agent Runner Publish Failed",
        "",
        "The agent produced a local commit but publishing failed.",
        "",
        f"- Failure category: `{failure_category.value}`",
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
            "To resume publishing without re-running the agent:",
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
    result_sections = "\n\n".join(
        format_result_for_recovery(result) for result in failed_results
    )
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
                truncate_recovery_output(str(exc)),
                "```",
            ]
        )
    return "\n".join(lines)


def _agent_command_name(command: object) -> str:
    """Return a short command name without echoing a potentially huge prompt."""
    if isinstance(command, (list, tuple)) and command:
        return str(command[0])
    return str(command)
