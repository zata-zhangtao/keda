"""Post-PR review polling — single pass across supervising and review issues."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    PullRequestContext,
)
from backend.core.use_cases.agent_runner_events import (
    parse_latest_event_marker,
)
from backend.core.use_cases.pr_supervisor import (
    build_rework_intent_comment,
    run_post_pr_supervisor_cycle,
)
from backend.core.use_cases.run_agent_once import (
    choose_agent,
    create_or_reuse_worktree,
    get_head_sha,
)

_logger = logging.getLogger(__name__)


def _context_changed_wide(
    pr_context: PullRequestContext,
    last_marker,
    base_sha_remote: str,
    issue_comments_count: int,
    pr_comments_count: int,
) -> bool:
    """Return whether PR context has changed since the last supervisor event."""
    if last_marker is None:
        return True
    if last_marker.head_sha != pr_context.head_sha:
        return True
    if last_marker.base_sha != base_sha_remote:
        return True
    if (
        last_marker.checks_state is not None
        and last_marker.checks_state != pr_context.checks_state
    ):
        return True
    if (
        last_marker.mergeable is not None
        and last_marker.mergeable != pr_context.mergeable
    ):
        return True
    if (
        last_marker.issue_comments_count is not None
        and last_marker.issue_comments_count != issue_comments_count
    ):
        return True
    if (
        last_marker.pr_comments_count is not None
        and last_marker.pr_comments_count != pr_comments_count
    ):
        return True
    return False


def _extract_pr_branch_from_comments(comments: list[str]) -> str | None:
    """Extract the latest known PR branch from Issue comments."""
    for comment_body in reversed(comments):
        marker = parse_latest_event_marker([comment_body])
        if marker is not None and marker.pr_branch:
            return marker.pr_branch

        branch_patterns = (
            r"PR Branch:\s*`([^`]+)`",
            r"Branch:\s*`([^`]+)`",
        )
        for branch_pattern in branch_patterns:
            branch_match = re.search(branch_pattern, comment_body)
            if branch_match:
                return branch_match.group(1)
    return None


def _process_review_candidate(
    *,
    issue: IssueSummary,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> None:
    """Run supervisor cycle for a single review candidate."""
    comments = github_client.list_issue_comments(issue.number)
    last_marker = parse_latest_event_marker(comments)

    # Find the linked PR context
    pr_branch = _extract_pr_branch_from_comments(comments)
    if pr_branch is None:
        _logger.warning(
            "Issue #%d has no identifiable PR branch; skipping.", issue.number
        )
        return

    pr_context = github_client.get_pull_request_context(pr_branch)
    if pr_context is None:
        open_pr_url = github_client.find_open_pr_by_head(pr_branch)
        if open_pr_url:
            pr_context = PullRequestContext(
                pr_url=open_pr_url,
                branch=pr_branch,
                head_sha=last_marker.head_sha if last_marker else "",
                base_sha="",
            )
        else:
            _logger.warning(
                "Issue #%d branch %s has no open PR; skipping.",
                issue.number,
                pr_branch,
            )
            return

    pr_number_match = re.search(r"/pull/(\d+)", pr_context.pr_url)
    pr_comments: list[str] = []
    if pr_number_match:
        pr_comments = github_client.list_pr_comments(int(pr_number_match.group(1)))

    base_sha_remote = github_client.get_remote_base_sha(
        config.git.remote, config.git.base_branch
    )

    if not _context_changed_wide(
        pr_context,
        last_marker,
        base_sha_remote,
        issue_comments_count=len(comments),
        pr_comments_count=len(pr_comments),
    ):
        _logger.info(
            "Issue #%d context unchanged since last supervisor event; skipping.",
            issue.number,
        )
        return

    # Move to supervising if currently in review
    if config.labels.review in issue.labels:
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.supervising],
            remove=[config.labels.review],
        )

    worktree_path = create_or_reuse_worktree(repo_path, issue, config, process_runner)
    supervisor_agent = choose_agent(issue, config, agent)

    action_result = run_post_pr_supervisor_cycle(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=github_client,
        process_runner=process_runner,
        pr_context=pr_context,
        supervisor_agent=supervisor_agent,
        cycle=(last_marker.cycle + 1) if last_marker else 1,
    )

    if action_result.action == "approve_for_human_review":
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.review],
            remove=[config.labels.supervising],
        )
        return

    if action_result.action in ("request_human_input",):
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.blocked],
            remove=[config.labels.supervising],
        )
        return

    if action_result.action == "mark_failed":
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.failed],
            remove=[config.labels.supervising],
        )
        return

    if action_result.action in (
        "repair_pr_branch",
        "rebase_pr_branch",
        "resolve_conflict",
    ):
        head_sha = get_head_sha(worktree_path, process_runner)
        github_client.comment_issue(
            issue.number,
            build_rework_intent_comment(
                action=action_result.action,
                pr_branch=pr_branch,
                head_sha=head_sha,
            ),
        )
        github_client.edit_issue_labels(
            issue.number,
            add=[config.labels.running],
            remove=[config.labels.supervising],
        )
        return

    # Unknown action: block
    github_client.edit_issue_labels(
        issue.number,
        add=[config.labels.blocked],
        remove=[config.labels.supervising],
    )


def review_once(
    *,
    repo_path: Path,
    config: AppConfig,
    dry_run: bool,
    agent: str,
    max_issues: int,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> int:
    """Run one review polling pass.

    Args:
        repo_path: Target repository path.
        config: Application configuration.
        dry_run: If True, only list candidates without processing.
        agent: Agent override (auto, codex, claude).
        max_issues: Maximum issues to process.
        github_client: Client for interacting with GitHub.
        process_runner: Runner for executing subprocess commands.

    Returns:
        Exit code (0 on success, 1 if any issue failed).
    """
    candidates = github_client.list_review_candidate_issues(
        [config.labels.supervising, config.labels.review], max_issues
    )
    if not candidates:
        _logger.info(
            "No open Issues found with labels %s or %s.",
            config.labels.supervising,
            config.labels.review,
        )
        return 0

    exit_code = 0
    for issue in candidates:
        if dry_run:
            _logger.info(
                "DRY RUN: would review Issue #%d: %s",
                issue.number,
                issue.title,
            )
            continue
        try:
            _process_review_candidate(
                issue=issue,
                repo_path=repo_path,
                config=config,
                agent=agent,
                github_client=github_client,
                process_runner=process_runner,
            )
            _logger.info("Reviewed Issue #%d: %s", issue.number, issue.title)
        except Exception as exc:  # noqa: BLE001 - report queue failures and continue.
            exit_code = 1
            current_labels = [
                label for label in issue.labels if label.startswith("agent/")
            ]
            remove_labels = [
                label
                for label in current_labels
                if label not in (config.labels.failed,)
            ]
            github_client.edit_issue_labels(
                issue.number,
                add=[config.labels.failed],
                remove=remove_labels,
            )
            github_client.comment_issue(
                issue.number,
                f"## Agent Runner Review Failed\n\n```text\n{exc}\n```\n",
            )
            _logger.error("Review failed for Issue #%d: %s", issue.number, exc)
    return exit_code
