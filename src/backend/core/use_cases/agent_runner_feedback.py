"""Prompt, recovery, and PRD delivery helpers for the agent runner."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import (
    CommandResult,
    IssueSummary,
    PromptConfig,
)
from backend.core.shared.prd_checklist import parse_prd_checklist

_MAX_RECOVERY_OUTPUT_LENGTH = 12000


class VerificationFailedError(RuntimeError):
    """Raised when configured verification commands do not pass."""

    def __init__(self, verification_results: list[CommandResult]) -> None:
        self.verification_results = verification_results
        super().__init__(format_verification_failure(verification_results))


class PrdDeliveryError(RuntimeError):
    """Raised when the canonical PRD is not ready for delivery."""


def extract_prd_path(issue_body: str) -> str | None:
    """Extract a PRD path from an Issue body."""
    match = re.search(r"PRD path:\s*`([^`]+)`", issue_body)
    return match.group(1) if match else None


_DEFAULT_EXECUTION_TEMPLATE = "\n".join(
    [
        "Complete GitHub Issue #{issue_number}: {issue_title}",
        "",
        "Issue URL: {issue_url}",
        "Worktree: {worktree_path}",
        "{prd_line}",
        "",
        "Issue body:",
        "{issue_body}",
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


def _build_prd_line(issue: IssueSummary) -> str:
    """Build the PRD reference line for a prompt template."""
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
        return (
            f"Also read the canonical PRD at `{prd_path}`. "
            "Before requesting a commit, update the PRD's Acceptance Checklist "
            f"to reflect completed work.{move_instruction}"
        )
    return "If the Issue references a PRD, read it before editing."


def build_prompt(
    issue: IssueSummary,
    worktree_path: Path,
    prompt_config: PromptConfig,
    phase: str = "execution",
) -> str:
    """Build the prompt sent to the local AI agent from a template."""
    template = prompt_config.phases.get(phase, _DEFAULT_EXECUTION_TEMPLATE)
    prd_line = _build_prd_line(issue)
    return template.format(
        issue_number=issue.number,
        issue_title=issue.title,
        issue_url=issue.url,
        worktree_path=worktree_path,
        issue_body=issue.body,
        prd_line=prd_line,
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
                    "Archive directory does not exist: "
                    f"{archive_dir.relative_to(worktree_path).as_posix()}"
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
