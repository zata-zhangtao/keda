"""Local Issue queue runner — single polling pass."""

from __future__ import annotations

import json
import logging
import re
import shlex
import socket
import subprocess
import time
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
)
from backend.core.shared.prd_checklist import parse_prd_checklist

_logger = logging.getLogger(__name__)

_COMMIT_REQUEST_RELATIVE_PATH = Path(".agent-runner/commit-request.json")
_MAX_COMMIT_MESSAGE_LENGTH = 200
_MAX_RECOVERY_OUTPUT_LENGTH = 4000


class VerificationFailedError(RuntimeError):
    """Raised when configured verification commands do not pass."""

    def __init__(self, verification_results: list[CommandResult]) -> None:
        self.verification_results = verification_results
        super().__init__(format_verification_failure(verification_results))


class PrdDeliveryError(RuntimeError):
    """Raised when the canonical PRD is not ready for delivery."""


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
        else "codex"
    )


def extract_prd_path(issue_body: str) -> str | None:
    """Extract a PRD path from an Issue body."""
    match = re.search(r"PRD path:\s*`([^`]+)`", issue_body)
    return match.group(1) if match else None


def build_prompt(issue: IssueSummary, worktree_path: Path) -> str:
    """Build the prompt sent to the local AI agent."""
    prd_path = extract_prd_path(issue.body)
    if prd_path:
        prd_path_obj = Path(prd_path)
        move_instruction = ""
        if (
            len(prd_path_obj.parts) >= 2
            and prd_path_obj.parts[0] == "tasks"
            and prd_path_obj.parts[1] == "pending"
        ):
            move_instruction = (
                " If all checklist items are complete, move the PRD from "
                "`tasks/pending/` to `tasks/archive/`."
            )
        prd_line = (
            f"Also read the canonical PRD at `{prd_path}`. "
            "Before requesting a commit, update the PRD's Acceptance Checklist "
            f"to reflect completed work.{move_instruction}"
        )
    else:
        prd_line = "If the Issue references a PRD, read it before editing."
    return "\n".join(
        [
            f"Complete GitHub Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"Worktree: {worktree_path}",
            prd_line,
            "",
            "Issue body:",
            issue.body,
            "",
            "Execution rules:",
            "- Read AGENTS.md and follow repository instructions.",
            "- Only modify files inside the current worktree.",
            "- Do not merge main, delete branches, push, or create PRs; "
            "the runner handles publishing.",
            "- Do not run `git add` or `git commit`; the runner exposes "
            "a restricted commit proxy.",
            "- After finishing your changes, request a commit by writing "
            "`.agent-runner/commit-request.json` as JSON with `commit_message`.",
            "- Do not touch production systems or real business data.",
            "- Implement the requested task with focused tests and docs updates.",
            "- Finish with a concise summary, tests run, and remaining risk.",
        ]
    )


def truncate_recovery_output(output_text: str) -> str:
    """Limit command output included in recovery prompts and failure comments."""
    if len(output_text) <= _MAX_RECOVERY_OUTPUT_LENGTH:
        return output_text
    return "\n".join(
        [
            "[output truncated; showing tail]",
            output_text[-_MAX_RECOVERY_OUTPUT_LENGTH:],
        ]
    )


def format_result_for_recovery(result: CommandResult) -> str:
    """Format one command result for a recovery prompt."""
    return "\n".join(
        [
            f"Command: `{shlex.join(result.command)}`",
            f"Exit code: {result.return_code}",
            "stdout:",
            "```text",
            truncate_recovery_output(result.stdout),
            "```",
            "stderr:",
            "```text",
            truncate_recovery_output(result.stderr),
            "```",
        ]
    )


def failed_verification_results(
    verification_results: list[CommandResult],
) -> list[CommandResult]:
    """Return failed command results from a verification run."""
    return [result for result in verification_results if result.return_code != 0]


def format_verification_failure(verification_results: list[CommandResult]) -> str:
    """Format configured verification failures for logs and Issue comments."""
    failed_results = failed_verification_results(verification_results)
    if not failed_results:
        return "Verification failed without a captured failing command."
    first_failed_result = failed_results[0]
    return "\n".join(
        [
            f"Command failed: {shlex.join(first_failed_result.command)}",
            f"Exit code: {first_failed_result.return_code}",
            "stdout:",
            truncate_recovery_output(first_failed_result.stdout),
            "stderr:",
            truncate_recovery_output(first_failed_result.stderr),
        ]
    )


def resolve_prd_archive_path(prd_relative_path: str) -> str | None:
    """Convert a pending PRD path to its archive counterpart.

    Returns None when the path is not under ``tasks/pending/``.
    """
    path = Path(prd_relative_path)
    if len(path.parts) >= 2 and path.parts[0] == "tasks" and path.parts[1] == "pending":
        return str(Path("tasks") / "archive" / path.name)
    return None


def _format_unchecked_items(
    unchecked_items: list[tuple[int, str]],
) -> str:
    """Format unchecked checklist items for error messages."""
    return "\n".join(f"  - L{line}: {text}" for line, text in unchecked_items)


def ensure_prd_delivery_ready(
    issue: IssueSummary,
    worktree_path: Path,
    process_runner: IProcessRunner,
) -> None:
    """Validate canonical PRD state and auto-archive if complete.

    Raises:
        PrdDeliveryError: When the PRD is not ready for delivery.
    """
    prd_relative_path = extract_prd_path(issue.body)
    if not prd_relative_path:
        return

    prd_path = worktree_path / prd_relative_path
    if prd_path.exists():
        file_content = prd_path.read_text(encoding="utf-8")
        checklist_result = parse_prd_checklist(file_content)

        if not checklist_result.section_found:
            raise PrdDeliveryError(
                f"Acceptance Checklist section missing in {prd_relative_path}"
            )

        if checklist_result.unchecked_items:
            unchecked_summary = _format_unchecked_items(
                checklist_result.unchecked_items
            )
            raise PrdDeliveryError(
                f"Acceptance Checklist has unchecked items in {prd_relative_path}:\n"
                f"{unchecked_summary}"
            )

        archive_relative_path = resolve_prd_archive_path(prd_relative_path)
        if archive_relative_path:
            archive_path = worktree_path / archive_relative_path
            archive_dir = archive_path.parent
            if not archive_dir.exists():
                raise PrdDeliveryError(
                    f"Archive directory does not exist: {archive_dir.relative_to(worktree_path).as_posix()}"
                )
            process_runner.run(
                [
                    "git",
                    "mv",
                    str(prd_relative_path),
                    str(archive_relative_path),
                ],
                cwd=worktree_path,
            )
        return

    # PRD not found at the claimed path; check if already archived.
    archive_relative_path = resolve_prd_archive_path(prd_relative_path)
    if archive_relative_path:
        archive_path = worktree_path / archive_relative_path
        if archive_path.exists():
            file_content = archive_path.read_text(encoding="utf-8")
            checklist_result = parse_prd_checklist(file_content)
            if not checklist_result.section_found:
                raise PrdDeliveryError(
                    f"Acceptance Checklist section missing in {archive_relative_path}"
                )
            if checklist_result.unchecked_items:
                unchecked_summary = _format_unchecked_items(
                    checklist_result.unchecked_items
                )
                raise PrdDeliveryError(
                    f"Acceptance Checklist has unchecked items in {archive_relative_path}:\n"
                    f"{unchecked_summary}"
                )
            return

    raise PrdDeliveryError(f"Canonical PRD not found: {prd_relative_path}")


def format_prd_delivery_failure(message: str) -> str:
    """Build the failure section for a PRD delivery recovery prompt."""
    return "\n".join(
        [
            "PRD delivery check failed.",
            message,
            "Update the canonical PRD: ensure all Acceptance Checklist items are checked, "
            "and move the PRD from tasks/pending/ to tasks/archive/ if complete.",
        ]
    )


def ensure_verification_passed(verification_results: list[CommandResult]) -> None:
    """Raise when any configured verification command failed."""
    if failed_verification_results(verification_results):
        raise VerificationFailedError(verification_results)


def build_recovery_prompt(
    issue: IssueSummary,
    worktree_path: Path,
    *,
    recovery_attempt: int,
    max_recovery_attempts: int,
    failure_summary: str,
) -> str:
    """Build a prompt that asks the agent to repair a failed attempt."""
    prd_path = extract_prd_path(issue.body)
    if prd_path:
        prd_line = (
            f"Also re-check the canonical PRD at `{prd_path}` if it affects the fix. "
            "Ensure the Acceptance Checklist is updated and the PRD is archived if complete."
        )
    else:
        prd_line = "If the Issue references a PRD, re-check it if it affects the fix."
    return "\n".join(
        [
            f"Repair GitHub Issue #{issue.number}: {issue.title}",
            "",
            f"Issue URL: {issue.url}",
            f"Worktree: {worktree_path}",
            f"Recovery attempt: {recovery_attempt}/{max_recovery_attempts}",
            prd_line,
            "",
            "The runner could not finish the previous attempt:",
            failure_summary,
            "",
            "Recovery rules:",
            "- Inspect the current worktree and fix the failure.",
            "- Only modify files inside the current worktree.",
            "- Do not switch branches, merge main, push, or create PRs.",
            "- Do not run `git add` or `git commit`; the runner handles commits.",
            "- After fixing the issue, write or update "
            "`.agent-runner/commit-request.json` as JSON with `commit_message`.",
            "- Finish with a concise summary, tests run, and remaining risk.",
        ]
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
    process_runner: IProcessRunner,
) -> CommandResult:
    """Run Codex or Claude Code in non-interactive mode."""
    prompt = build_prompt(issue, worktree_path)
    return run_agent_with_prompt(agent_name, prompt, worktree_path, process_runner)


def run_agent_with_prompt(
    agent_name: str,
    prompt: str,
    worktree_path: Path,
    process_runner: IProcessRunner,
) -> CommandResult:
    """Run Codex or Claude Code with a prepared prompt."""
    builder = _AGENT_COMMAND_BUILDERS.get(agent_name)
    if builder is not None:
        command = builder(prompt, worktree_path)
    else:
        command = _build_codex_command(prompt, worktree_path)
    return process_runner.run(command, cwd=worktree_path, capture_output=False)


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
) -> list[CommandResult]:
    """Run the agent, recover failed verification, and return final checks."""
    max_recovery_attempts = max(0, config.runner.max_recovery_attempts)
    recovery_retry_delay_seconds = max(0, config.runner.recovery_retry_delay_seconds)
    recovery_failure_summary = ""
    final_verification_results: list[CommandResult] = []

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
                run_agent(selected_agent, issue, worktree_path, process_runner)
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
            if attempt_index >= max_recovery_attempts:
                raise
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
            if attempt_index >= max_recovery_attempts:
                raise
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
            if attempt_index >= max_recovery_attempts:
                raise
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
                if attempt_index >= max_recovery_attempts:
                    raise
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
                if (
                    attempt_index >= max_recovery_attempts
                    or not is_recoverable_commit_request_error(exc)
                ):
                    raise
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
            return final_verification_results

        if attempt_index >= max_recovery_attempts:
            raise RuntimeError("Agent produced no git commits.")
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

    raise RuntimeError("Agent produced no git commits.")


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
        "Update [agent_runner.git].remote in config.toml before publishing."
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
    pr_body = f"Closes #{issue.number}\n\nGenerated by issue-agent-runner.\n"
    pr_url = github_client.create_draft_pr(
        title=f"[Agent] {issue.title}",
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
    """Run one polling pass.

    Args:
        repo_path: Target repository path.
        config: Application configuration.
        dry_run: If True, only list ready issues without processing.
        agent: Agent override (auto, codex, claude).
        max_issues: Maximum issues to process.
        github_client: Client for interacting with GitHub.
        process_runner: Runner for executing subprocess commands.

    Returns:
        Exit code (0 on success, 1 if any issue failed).
    """
    if not dry_run:
        try:
            run_preflight_checks(repo_path, config, process_runner)
        except Exception as exc:  # noqa: BLE001 - report preflight failure cleanly.
            _logger.error("Agent runner preflight failed: %s", exc)
            return 1

    issues = github_client.list_ready_issues(config.labels.ready, max_issues)
    if not issues:
        _logger.info("No open Issues found with label %s.", config.labels.ready)
        return 0

    exit_code = 0
    for issue in issues:
        selected_agent = choose_agent(issue, config, agent)
        if dry_run:
            _logger.info(
                "DRY RUN: would process Issue #%d with %s: %s",
                issue.number,
                selected_agent,
                issue.title,
            )
            continue
        try:
            github_client.edit_issue_labels(
                issue.number, add=[config.labels.running], remove=[config.labels.ready]
            )
            github_client.comment_issue(
                issue.number,
                "## Agent Runner Claimed\n\n"
                f"- Host: `{socket.gethostname()}`\n"
                f"- Agent: `{selected_agent}`\n",
            )
            worktree_path = create_or_reuse_worktree(
                repo_path, issue, config, process_runner
            )
            before_sha = get_head_sha(worktree_path, process_runner)
            expected_branch = get_current_branch(worktree_path, process_runner)
            verification_results = run_agent_until_committed(
                selected_agent=selected_agent,
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                process_runner=process_runner,
                before_sha=before_sha,
                expected_branch=expected_branch,
            )
            branch, pr_url = publish_changes(
                issue,
                worktree_path,
                config,
                github_client,
                process_runner,
                expected_branch=expected_branch,
            )
            github_client.edit_issue_labels(
                issue.number, add=[config.labels.review], remove=[config.labels.running]
            )
            verification_lines = "\n".join(
                f"- `{' '.join(result.command)}`: exit {result.return_code}"
                for result in verification_results
            )
            github_client.comment_issue(
                issue.number,
                "\n".join(
                    [
                        "## Agent Runner Result",
                        "",
                        f"- Branch: `{branch}`",
                        f"- Draft PR: {pr_url}",
                        "",
                        "Verification:",
                        verification_lines,
                    ]
                ),
            )
            _logger.info("Completed Issue #%d: %s", issue.number, issue.title)
        except Exception as exc:  # noqa: BLE001 - report queue failures and continue.
            exit_code = 1
            github_client.edit_issue_labels(
                issue.number, add=[config.labels.failed], remove=[config.labels.running]
            )
            github_client.comment_issue(
                issue.number, f"## Agent Runner Failed\n\n```text\n{exc}\n```\n"
            )
            _logger.error("Failed Issue #%d: %s", issue.number, exc)
    return exit_code
