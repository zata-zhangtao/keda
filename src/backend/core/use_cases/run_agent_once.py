"""Local Issue queue runner — single polling pass."""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from collections.abc import Callable
from fnmatch import fnmatch
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
)
from backend.core.use_cases.agent_runner_feedback import (
    PrdDeliveryError,
    VerificationFailedError,
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
from backend.core.use_cases.generated_content import (
    build_pr_context,
    generate_pr_content,
)

_logger = logging.getLogger(__name__)

_COMMIT_REQUEST_RELATIVE_PATH = Path(".agent-runner/commit-request.json")
_MAX_COMMIT_MESSAGE_LENGTH = 200

__all__ = [
    "AgentRunnerAttemptError",
    "MaxRetriesExceededError",
    "PrdDeliveryError",
    "UnrecoverableError",
    "VerificationFailedError",
    "build_prompt",
    "build_recovery_prompt",
    "choose_agent",
    "classify_failure",
    "commit_requested_changes",
    "create_or_reuse_worktree",
    "ensure_prd_delivery_ready",
    "ensure_verification_passed",
    "extract_agent_response_text",
    "extract_prd_path",
    "failed_verification_results",
    "format_agent_execution_failure",
    "format_attempt_history",
    "format_command",
    "format_failure_comment",
    "format_prd_delivery_failure",
    "format_recovery_failure_summary",
    "format_result_for_recovery",
    "format_verification_failure",
    "get_current_branch",
    "get_head_sha",
    "has_changes",
    "list_changed_paths",
    "list_git_remotes",
    "publish_changes",
    "read_commit_request",
    "remove_commit_request",
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


def format_command(template: str, *, issue_number: int) -> list[str]:
    """Format a configured command template for an Issue."""
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
    """Create or reuse a worktree for the Issue."""
    create_result = process_runner.run(
        format_command(config.worktree.create_command, issue_number=issue.number),
        cwd=repo_path,
        check=False,
    )
    if create_result.return_code != 0:
        process_runner.run(
            format_command(config.worktree.reuse_command, issue_number=issue.number),
            cwd=repo_path,
        )
    path_result = process_runner.run(
        format_command(config.worktree.path_command, issue_number=issue.number),
        cwd=repo_path,
    )
    return Path(path_result.stdout.strip()).resolve()


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


def run_agent(
    agent_name: str,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> CommandResult:
    """Run Codex or Claude Code in non-interactive mode."""
    prompt = build_prompt(issue, worktree_path, config.prompts, phase="execution")
    return run_agent_with_prompt(agent_name, prompt, worktree_path, process_runner)


def run_agent_with_prompt(
    agent_name: str,
    prompt: str,
    worktree_path: Path,
    process_runner: IProcessRunner,
    *,
    capture_output: bool = False,
) -> CommandResult:
    """Run Codex or Claude Code with a prepared prompt."""
    builder = _AGENT_COMMAND_BUILDERS.get(agent_name)
    if builder is not None:
        command = builder(prompt, worktree_path)
    else:
        command = _build_codex_command(prompt, worktree_path)
    return process_runner.run(
        command,
        cwd=worktree_path,
        capture_output=capture_output,
    )


def extract_agent_response_text(result: CommandResult) -> str:
    """Return assistant response text from direct stdout or Claude stream-json."""
    if not result.stdout:
        return ""
    command_name = result.command[0] if result.command else ""
    if command_name != "claude" or "stream-json" not in result.command:
        return result.stdout

    stream_text_parts: list[str] = []
    assistant_text_parts: list[str] = []
    result_parts: list[str] = []
    for output_line in result.stdout.splitlines():
        try:
            event_payload = json.loads(output_line)
        except json.JSONDecodeError:
            stream_text_parts.append(output_line)
            continue
        if not isinstance(event_payload, dict):
            continue
        event_type = event_payload.get("type")
        if event_type == "stream_event":
            _append_claude_stream_event_text(event_payload, stream_text_parts)
        elif event_type == "assistant":
            _append_claude_assistant_text(event_payload, assistant_text_parts)
        elif event_type == "result":
            result_text = str(event_payload.get("result") or "").strip()
            if result_text:
                result_parts.append(result_text)

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


def get_head_sha(worktree_path: Path, process_runner: IProcessRunner) -> str:
    """Return the full SHA of the current HEAD commit."""
    result = process_runner.run(["git", "rev-parse", "HEAD"], cwd=worktree_path)
    return result.stdout.strip()


def get_current_branch(worktree_path: Path, process_runner: IProcessRunner) -> str:
    """Return the current branch name for a worktree."""
    result = process_runner.run(["git", "branch", "--show-current"], cwd=worktree_path)
    return result.stdout.strip()


def run_verification(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> list[CommandResult]:
    """Run configured verification commands."""
    verification_results: list[CommandResult] = []
    for command in config.runner.verification_commands:
        result = process_runner.run(
            shlex.split(command),
            cwd=worktree_path,
            check=False,
        )
        verification_results.append(result)
        if result.return_code != 0:
            break
    return verification_results


def default_commit_message(issue: IssueSummary) -> str:
    """Build the fallback commit message for an Issue."""
    return f"[Agent] Issue #{issue.number}: {issue.title}"


def sanitize_commit_message(raw_message: object, issue: IssueSummary) -> str:
    """Return a single-line commit message safe to pass to Git."""
    if not isinstance(raw_message, str):
        return default_commit_message(issue)
    message = " ".join(raw_message.split())
    if not message:
        return default_commit_message(issue)
    return message[:_MAX_COMMIT_MESSAGE_LENGTH]


def read_commit_request(worktree_path: Path, issue: IssueSummary) -> str:
    """Read the agent's restricted commit request file."""
    request_path = worktree_path / _COMMIT_REQUEST_RELATIVE_PATH
    if not request_path.is_file():
        raise RuntimeError("Agent left uncommitted changes without a commit request.")
    with request_path.open("r", encoding="utf-8") as request_file:
        try:
            request_payload = json.load(request_file)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Commit request must be valid JSON.") from exc
    if not isinstance(request_payload, dict):
        raise RuntimeError("Commit request must be a JSON object.")
    return sanitize_commit_message(request_payload.get("commit_message"), issue)


def remove_commit_request(worktree_path: Path) -> None:
    """Remove the transient agent commit request file from the worktree."""
    request_path = worktree_path / _COMMIT_REQUEST_RELATIVE_PATH
    if request_path.exists():
        request_path.unlink()
    request_directory = request_path.parent
    try:
        request_directory.rmdir()
    except OSError:
        pass


def commit_requested_changes(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    *,
    expected_branch: str,
) -> list[CommandResult]:
    """Commit agent changes through the runner's restricted commit proxy."""
    current_branch = get_current_branch(worktree_path, process_runner)
    if current_branch != expected_branch:
        raise RuntimeError(f"Refusing to commit on unexpected branch: {current_branch}")
    commit_message = read_commit_request(worktree_path, issue)
    remove_commit_request(worktree_path)
    if not has_changes(worktree_path, process_runner):
        raise RuntimeError("Agent requested a commit but produced no file changes.")
    validate_safe_changes(worktree_path, config, process_runner)
    process_runner.run(["git", "add", "-A"], cwd=worktree_path)
    verification_results = run_verification(worktree_path, config, process_runner)
    ensure_verification_passed(verification_results)
    process_runner.run(["git", "commit", "-m", commit_message], cwd=worktree_path)
    return verification_results


def unstage_changes(worktree_path: Path, process_runner: IProcessRunner) -> None:
    """Reset the Git index after a staged verification failure."""
    process_runner.run(["git", "reset", "--mixed"], cwd=worktree_path)


def is_recoverable_commit_request_error(exc: RuntimeError) -> bool:
    """Return whether the agent can repair a commit request protocol error."""
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
                return FailureType.UNRECOVERABLE
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
        detail = result.detail.replace("\n", " ")[:100]
        recovered = "Yes" if result.recovered else "No"
        lines.append(
            f"| {result.attempt_number} | {result.failure_type.value} | {recovered} | {detail} |"
        )
    return "\n".join(lines)


def format_failure_comment(
    exc: BaseException,
    attempt_results: list[AttemptResult] | None = None,
) -> str:
    """Build a failure comment with optional attempt history."""
    lines = ["## Agent Runner Failed", ""]

    if attempt_results:
        lines.append(format_attempt_history(attempt_results))
        lines.append("")

    lines.extend(["```text", str(exc)])
    if exc.__cause__ is not None:
        lines.append(str(exc.__cause__))
    lines.extend(["```", ""])
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
) -> AgentCommitResult:
    """Run the agent, recover failed verification, and return final checks."""
    max_recovery_attempts = max(0, config.runner.max_recovery_attempts)
    recovery_retry_delay_seconds = max(0, config.runner.recovery_retry_delay_seconds)
    recovery_failure_summary = ""
    final_verification_results: list[CommandResult] = []
    attempt_results: list[AttemptResult] = []

    for attempt_index in range(max_recovery_attempts + 1):
        if attempt_index > 0:
            wait_before_recovery_attempt(
                issue.number,
                recovery_attempt=attempt_index,
                max_recovery_attempts=max_recovery_attempts,
                delay_seconds=recovery_retry_delay_seconds,
            )
        try:
            if attempt_index == 0:
                run_agent(selected_agent, issue, worktree_path, config, process_runner)
            else:
                recovery_prompt = build_recovery_prompt(
                    issue,
                    worktree_path,
                    recovery_attempt=attempt_index,
                    max_recovery_attempts=max_recovery_attempts,
                    failure_summary=recovery_failure_summary,
                )
                run_agent_with_prompt(
                    selected_agent, recovery_prompt, worktree_path, process_runner
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
            except RuntimeError as exc:
                after_sha = get_head_sha(worktree_path, process_runner)
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


def has_changes(worktree_path: Path, process_runner: IProcessRunner) -> bool:
    """Return whether the worktree has uncommitted changes."""
    result = process_runner.run(["git", "status", "--porcelain"], cwd=worktree_path)
    return bool(result.stdout.strip())


def list_changed_paths(
    worktree_path: Path, process_runner: IProcessRunner
) -> list[str]:
    """List changed paths in a worktree."""
    status_result = process_runner.run(
        ["git", "status", "--porcelain"], cwd=worktree_path
    )
    changed_paths: list[str] = []
    for status_line in status_result.stdout.splitlines():
        if not status_line:
            continue
        raw_path_text = status_line[3:]
        if " -> " in raw_path_text:
            changed_paths.extend(raw_path_text.split(" -> ", maxsplit=1))
        else:
            changed_paths.append(raw_path_text)
    return changed_paths


def validate_safe_changes(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Refuse to publish changes to configured forbidden paths."""
    blocked_paths: list[str] = []
    for changed_path_text in list_changed_paths(worktree_path, process_runner):
        changed_path_name = Path(changed_path_text).name
        for forbidden_pattern in config.safety.forbidden_path_patterns:
            if fnmatch(changed_path_text, forbidden_pattern) or fnmatch(
                changed_path_name,
                forbidden_pattern,
            ):
                blocked_paths.append(changed_path_text)
                break
    if blocked_paths:
        blocked_paths_text = ", ".join(sorted(set(blocked_paths)))
        raise RuntimeError(f"Refusing to publish forbidden paths: {blocked_paths_text}")


def list_git_remotes(worktree_path: Path, process_runner: IProcessRunner) -> list[str]:
    """Return configured Git remote names for the worktree."""
    remote_result = process_runner.run(["git", "remote"], cwd=worktree_path)
    remote_names = []
    for remote_line in remote_result.stdout.splitlines():
        remote_name = remote_line.strip()
        if remote_name and remote_name not in remote_names:
            remote_names.append(remote_name)
    return remote_names


def validate_publish_remote(
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> str:
    """Return the configured publish remote after confirming it exists."""
    remote_names = list_git_remotes(worktree_path, process_runner)
    configured_remote_name = config.git.remote
    if configured_remote_name in remote_names:
        return configured_remote_name

    available_remotes_text = ", ".join(remote_names) if remote_names else "(none)"
    raise RuntimeError(
        "Configured git remote "
        f"'{configured_remote_name}' does not exist. "
        f"Available remotes: {available_remotes_text}. "
        "Update [agent_runner.git].remote in .iar.toml or config.toml before publishing."
    )


def run_preflight_checks(
    repo_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> None:
    """Validate runner configuration before claiming any Issue."""
    validate_publish_remote(repo_path, config, process_runner)


def publish_changes(
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
    *,
    expected_branch: str | None = None,
    content_generator: IContentGenerator | None = None,
) -> tuple[str, str]:
    """Push and create a draft PR. Assumes the agent has already committed."""
    branch = get_current_branch(worktree_path, process_runner)
    if expected_branch is not None and branch != expected_branch:
        raise RuntimeError(
            f"Refusing to publish from unexpected branch: {branch} "
            f"(expected {expected_branch})"
        )
    validate_safe_changes(worktree_path, config, process_runner)
    publish_remote_name = validate_publish_remote(worktree_path, config, process_runner)
    process_runner.run(
        ["git", "push", "-u", publish_remote_name, branch], cwd=worktree_path
    )

    fallback_title = f"[Agent] {issue.title}"
    fallback_body = f"Closes #{issue.number}\n\nGenerated by issue-agent-runner.\n"

    gc_config = config.generated_content
    pr_title = fallback_title
    pr_body = fallback_body
    if gc_config.enabled:
        gc_context = build_pr_context(
            issue=issue,
            branch=branch,
            base_branch=config.git.base_branch,
            worktree_path=worktree_path,
            process_runner=process_runner,
            target_config=gc_config.draft_pr,
        )
        generated = generate_pr_content(
            config=gc_config,
            context=gc_context,
            fallback_title=fallback_title,
            fallback_body=fallback_body,
            generator=content_generator,
            cwd=worktree_path,
        )
        pr_title = generated.title
        pr_body = generated.body

    pr_url = github_client.create_draft_pr(
        title=pr_title,
        body=pr_body,
        base_branch=config.git.base_branch,
        cwd=worktree_path,
    )
    return branch, pr_url


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
