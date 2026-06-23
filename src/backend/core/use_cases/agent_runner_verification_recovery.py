"""Verification failure recovery helpers for agent runner rebase/repair flows.

These helpers let the post-PR supervisor feed verification command output
back to the agent as a recovery prompt, instead of failing the issue
immediately.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
)
from backend.core.use_cases.agent_runner_commit import (
    commit_requested_changes,
)
from backend.core.use_cases.agent_runner_feedback import (
    VerificationFailedError,
    build_recovery_prompt,
    ensure_verification_passed,
)
from backend.core.use_cases.agent_runner_failure import (
    format_recovery_failure_summary,
)
from backend.core.use_cases.agent_runner_git import (
    has_changes,
    run_verification,
)
from backend.core.use_cases.run_agent_once import run_agent_with_prompt


def has_staged_changes(worktree_path: Path, process_runner: IProcessRunner) -> bool:
    """Return whether the worktree has staged changes."""
    result = process_runner.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=worktree_path,
        check=False,
    )
    return result.return_code != 0


def run_recovery_after_verification_failure(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    supervisor_agent: str,
    verification_results: list[CommandResult],
    pr_branch: str,
    recovery_attempt: int,
    max_recovery_attempts: int,
) -> list[CommandResult]:
    """Run a recovery agent to fix verification failures and commit the result.

    Should be called when verification fails during rebase/repair. The agent
    receives the verification output, inspects the worktree, and requests a
    commit. The runner then commits the changes and returns the new verification
    results (which may themselves fail, to be handled by the caller).
    """
    failure_summary = format_recovery_failure_summary(
        "Verification failed after rebase/repair.",
        verification_results,
    )
    recovery_prompt = build_recovery_prompt(
        issue,
        worktree_path,
        recovery_attempt=recovery_attempt,
        max_recovery_attempts=max_recovery_attempts,
        failure_summary=failure_summary,
    )
    run_agent_with_prompt(
        supervisor_agent,
        recovery_prompt,
        worktree_path,
        process_runner,
        issue=issue,
    )

    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    if request_path.is_file():
        return commit_requested_changes(
            issue,
            worktree_path,
            config,
            process_runner,
            expected_branch=pr_branch,
        )
    if has_changes(worktree_path, process_runner):
        raise RuntimeError(
            "Recovery agent changed files without writing "
            ".agent-runner/commit-request.json."
        )
    return run_verification(worktree_path, config, process_runner)


def ensure_verification_passed_with_recovery(
    *,
    issue: IssueSummary,
    worktree_path: Path,
    config: AppConfig,
    process_runner: IProcessRunner,
    supervisor_agent: str,
    pr_branch: str,
) -> list[CommandResult]:
    """Run verification and recover from failures up to max attempts.

    If a configured verification command fails, the failure output is fed to
    the supervisor agent as a recovery prompt. The agent may request a commit;
    the runner commits those changes and re-runs verification. This repeats
    until verification passes or the maximum number of repair attempts is
    exhausted.
    """
    max_attempts = max(1, config.post_pr_supervisor.max_repair_attempts)
    verification_results = run_verification(worktree_path, config, process_runner)

    for attempt in range(1, max_attempts + 1):
        try:
            ensure_verification_passed(verification_results)
            return verification_results
        except VerificationFailedError as exc:
            if attempt >= max_attempts:
                raise
            # Unstage any partially-staged changes so the agent can see and
            # edit its prior work cleanly in the working tree.
            if has_staged_changes(worktree_path, process_runner):
                process_runner.run(
                    ["git", "reset", "--mixed"],
                    cwd=worktree_path,
                    check=False,
                )
            verification_results = run_recovery_after_verification_failure(
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                process_runner=process_runner,
                supervisor_agent=supervisor_agent,
                verification_results=exc.verification_results,
                pr_branch=pr_branch,
                recovery_attempt=attempt,
                max_recovery_attempts=max_attempts,
            )

    # The loop always returns once verification passes or raises when attempts
    # are exhausted; this fallback should never be reached.
    raise RuntimeError("Verification recovery loop exited without a result")
