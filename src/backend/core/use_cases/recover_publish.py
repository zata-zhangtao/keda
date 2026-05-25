"""Publish recovery use case for resuming failed publish operations."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import (
    IGitHubClient,
    IProcessRunner,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    PublishRecoveryRequest,
    PublishRecoveryResult,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "PublishRecoveryError",
    "resolve_existing_worktree",
    "validate_worktree_clean",
    "validate_branch_safety",
    "recover_publish_issue",
    "build_recovery_success_comment",
]


class PublishRecoveryError(RuntimeError):
    """Base error for publish recovery failures."""

    pass


def resolve_existing_worktree(
    repo_path: Path,
    issue_number: int,
    config: AppConfig,
    process_runner: IProcessRunner,
) -> Path:
    """Resolve the path to an existing issue worktree without creating one.

    Args:
        repo_path: Path to the main repository.
        issue_number: GitHub Issue number.
        config: Application configuration.
        process_runner: Process runner for executing commands.

    Returns:
        Resolved absolute path to the worktree.

    Raises:
        PublishRecoveryError: If the worktree path cannot be resolved or does not exist.
    """
    from backend.core.use_cases.run_agent_once import format_command

    path_result = process_runner.run(
        format_command(config.worktree.path_command, issue_number=issue_number),
        cwd=repo_path,
    )
    worktree_path = Path(path_result.stdout.strip()).resolve()

    if not worktree_path.exists():
        raise PublishRecoveryError(
            f"Issue worktree does not exist: {worktree_path}. "
            f"Recovery requires an existing worktree with a local commit."
        )

    # Verify it is a valid git worktree
    git_dir_result = process_runner.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=worktree_path,
        check=False,
    )
    if git_dir_result.return_code != 0:
        raise PublishRecoveryError(f"Path is not a valid git worktree: {worktree_path}")

    return worktree_path


def validate_worktree_clean(
    worktree_path: Path,
    process_runner: IProcessRunner,
) -> None:
    """Validate that the worktree has no uncommitted changes.

    Args:
        worktree_path: Path to the worktree.
        process_runner: Process runner for executing commands.

    Raises:
        PublishRecoveryError: If the worktree has uncommitted changes.
    """
    status_result = process_runner.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
    )
    if status_result.stdout.strip():
        raise PublishRecoveryError(
            f"Worktree has uncommitted changes. "
            f"Recovery requires a clean worktree with an existing commit. "
            f"Path: {worktree_path}"
        )


def validate_branch_safety(
    *,
    worktree_path: Path,
    issue_number: int,
    config: AppConfig,
    process_runner: IProcessRunner,
    expected_branch: str | None = None,
) -> str:
    """Validate branch safety for publish recovery.

    Args:
        worktree_path: Path to the worktree.
        issue_number: GitHub Issue number.
        config: Application configuration.
        process_runner: Process runner for executing commands.
        expected_branch: Optional explicit branch name to expect.

    Returns:
        The validated current branch name.

    Raises:
        PublishRecoveryError: If the branch is not safe for recovery.
    """
    branch_result = process_runner.run(
        ["git", "branch", "--show-current"],
        cwd=worktree_path,
    )
    current_branch = branch_result.stdout.strip()

    if not current_branch:
        raise PublishRecoveryError(
            "Cannot recover from detached HEAD state. "
            "Checkout a valid branch first."
        )

    if current_branch == config.git.base_branch:
        raise PublishRecoveryError(
            f"Refusing to publish from base branch '{config.git.base_branch}'. "
            f"Switch to the issue branch and retry."
        )

    # If explicit branch provided, current branch must match exactly
    if expected_branch is not None:
        if current_branch != expected_branch:
            raise PublishRecoveryError(
                f"Current branch '{current_branch}' does not match "
                f"expected branch '{expected_branch}'. "
                f"Use --branch to confirm the current branch."
            )
        return current_branch

    # Without explicit branch, validate branch references issue number
    issue_ref_patterns = [
        rf"issue[-_]?{issue_number}",
        rf"tasks[-_/]issue[-_]?{issue_number}",
        rf"issue[-_]?{issue_number}[-_/]",
    ]
    branch_matches_issue = any(
        re.search(pattern, current_branch, re.IGNORECASE)
        for pattern in issue_ref_patterns
    )

    if not branch_matches_issue:
        raise PublishRecoveryError(
            f"Branch '{current_branch}' does not appear to reference "
            f"Issue #{issue_number}. "
            f"Use --branch to explicitly confirm the current branch."
        )

    return current_branch


def recover_publish_issue(
    *,
    request: PublishRecoveryRequest,
    repo_path: Path,
    config: AppConfig,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> PublishRecoveryResult:
    """Recover a failed publish operation for an Issue.

    This function safely resumes publish for tasks that already have a local commit.
    It does not run the agent, create commits, or modify the worktree.

    Args:
        request: Recovery request with issue number and optional branch.
        repo_path: Path to the main repository.
        config: Application configuration.
        github_client: GitHub client for API operations.
        process_runner: Process runner for Git commands.

    Returns:
        PublishRecoveryResult with branch, SHA, PR URL, and reuse status.

    Raises:
        PublishRecoveryError: If recovery cannot proceed safely.
    """
    from backend.core.use_cases.run_agent_once import (
        get_head_sha,
        list_git_remotes,
    )

    issue_number = request.issue_number

    # Resolve existing worktree
    worktree_path = resolve_existing_worktree(
        repo_path, issue_number, config, process_runner
    )

    # Validate worktree is clean
    validate_worktree_clean(worktree_path, process_runner)

    # Validate branch safety
    branch = validate_branch_safety(
        worktree_path=worktree_path,
        issue_number=issue_number,
        config=config,
        process_runner=process_runner,
        expected_branch=request.expected_branch,
    )

    # Get HEAD SHA before any operations
    head_sha = get_head_sha(worktree_path, process_runner)

    # Validate configured remote exists
    remote_names = list_git_remotes(worktree_path, process_runner)
    configured_remote = config.git.remote
    if configured_remote not in remote_names:
        available_text = ", ".join(remote_names) if remote_names else "(none)"
        raise PublishRecoveryError(
            f"Configured git remote '{configured_remote}' does not exist. "
            f"Available remotes: {available_text}. "
            f"Update [agent_runner.git].remote in config.toml."
        )

    # Push to configured remote
    _logger.info(
        "Pushing branch '%s' to remote '%s' for Issue #%d",
        branch,
        configured_remote,
        issue_number,
    )
    push_result = process_runner.run(
        ["git", "push", "-u", configured_remote, branch],
        cwd=worktree_path,
        check=False,
    )
    if push_result.return_code != 0:
        raise PublishRecoveryError(
            f"Failed to push branch '{branch}' to remote '{configured_remote}'. "
            f"Exit code: {push_result.return_code}. "
            f"Stderr: {push_result.stderr}"
        )

    # Check for existing open PR
    existing_pr_url = github_client.find_open_pr_by_head(branch)
    pr_reused = existing_pr_url is not None

    if existing_pr_url:
        pr_url = existing_pr_url
        _logger.info(
            "Reusing existing PR for Issue #%d: %s",
            issue_number,
            pr_url,
        )
    else:
        # Create new draft PR
        pr_title = f"[Agent] Issue #{issue_number}"
        pr_body = f"Closes #{issue_number}\n\nRecovered by issue-agent-runner.\n"

        _logger.info("Creating draft PR for Issue #%d", issue_number)
        pr_url = github_client.create_draft_pr(
            title=pr_title,
            body=pr_body,
            base_branch=config.git.base_branch,
            cwd=worktree_path,
        )

    # Update labels - only after successful push and PR
    labels_to_remove = [
        config.labels.failed,
        config.labels.running,
        config.labels.ready,
    ]
    github_client.edit_issue_labels(
        issue_number,
        add=[config.labels.review],
        remove=labels_to_remove,
    )

    # Comment on Issue with recovery summary
    github_client.comment_issue(
        issue_number,
        build_recovery_success_comment(
            branch=branch,
            head_sha=head_sha,
            pr_url=pr_url,
            pr_reused=pr_reused,
        ),
    )

    _logger.info(
        "Publish recovery complete for Issue #%d: branch=%s, pr=%s, reused=%s",
        issue_number,
        branch,
        pr_url,
        pr_reused,
    )

    return PublishRecoveryResult(
        issue_number=issue_number,
        branch=branch,
        head_sha=head_sha,
        pr_url=pr_url,
        pr_reused=pr_reused,
    )


def build_recovery_success_comment(
    *,
    branch: str,
    head_sha: str,
    pr_url: str,
    pr_reused: bool,
) -> str:
    """Build the Issue comment for successful publish recovery.

    Args:
        branch: Branch name that was pushed.
        head_sha: HEAD SHA of the commit.
        pr_url: URL of the PR (new or reused).
        pr_reused: Whether an existing PR was reused.

    Returns:
        Markdown comment body.
    """
    reuse_status = "reused" if pr_reused else "created"
    return "\n".join(
        [
            "## Agent Runner Publish Recovered",
            "",
            f"- Branch: `{branch}`",
            f"- HEAD SHA: `{head_sha}`",
            f"- Draft PR ({reuse_status}): {pr_url}",
        ]
    )
