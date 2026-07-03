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
    guard_supervisor_action_for_pr_state,
    run_post_pr_supervisor_cycle,
)
from backend.core.use_cases.agent_runner_workflow import (
    find_latest_unconsumed_marker,
    transition_issue_workflow_state,
)
from backend.core.use_cases.agent_runner_git import (
    has_changes,
    pop_worktree_stash,
    stash_worktree_changes,
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
    # mark_failed 意味着上一轮 supervision 没有产出有效评审结论（例如 agent
    # 基础设施崩溃重试耗尽，或输出不可解析触发 fail-closed）。Issue 能再次
    # 进入评审队列说明人工已把 label 从 agent/failed 拨回，这本身就是明确的
    # 重试请求，不应被"上下文未变化"的去重机制拦下
    if last_marker.action == "mark_failed":
        return True
    if last_marker.head_sha != pr_context.head_sha:
        return True
    if base_sha_remote and last_marker.base_sha != base_sha_remote:
        return True
    if last_marker.checks_state is not None and last_marker.checks_state != pr_context.checks_state:
        return True
    if last_marker.mergeable is not None and last_marker.mergeable != pr_context.mergeable:
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
) -> str:
    """Run supervisor cycle for a single review candidate."""
    comments = github_client.list_issue_comments(issue.number)

    # Guard: if Issue is effectively in agent/running with a pending rework
    # marker, do not run review; let run consume the marker to avoid overwriting
    # the pending repair/rebase request with a stale approval.
    if config.labels.running in issue.labels:
        pending_rework = find_latest_unconsumed_marker(
            comments,
            phase="post_pr_rework_requested",
            completion_phases={
                "implementation_complete",
                "draft_pr_created",
                "publish_recovered",
                "rebase_repair_complete",
            },
        )
        if pending_rework is not None:
            _logger.info(
                "Issue #%d has pending rework marker; skipping review to let run process it.",
                issue.number,
            )
            return "skipped_pending_rework"

    last_marker = parse_latest_event_marker(comments)

    # Find the linked PR context
    pr_branch = _extract_pr_branch_from_comments(comments)
    if pr_branch is None:
        _logger.warning("Issue #%d has no identifiable PR branch; skipping.", issue.number)
        return "skipped_no_pr_branch"

    pr_context = github_client.get_pull_request_context(pr_branch)
    if pr_context is None:
        open_pr_url = github_client.find_open_pr_by_head(pr_branch)
        if open_pr_url:
            _logger.warning(
                "Issue #%d branch %s has an open PR but complete PR context is "
                "unavailable; deferring supervision to avoid unsafe approval.",
                issue.number,
                pr_branch,
            )
            return "deferred_pr_context_unavailable"
        else:
            _logger.warning(
                "Issue #%d branch %s has no open PR; skipping.",
                issue.number,
                pr_branch,
            )
            return "skipped_no_open_pr"

    pr_number_match = re.search(r"/pull/(\d+)", pr_context.pr_url)
    pr_comments: list[str] = []
    if pr_number_match:
        pr_comments = github_client.list_pr_comments(int(pr_number_match.group(1)))

    base_sha_remote = github_client.get_remote_base_sha(config.git.remote, config.git.base_branch)

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
        return "skipped_context_unchanged"

    # Move to supervising if currently in review, using shared transition helper
    # so other durable workflow labels (including stale running/failed) are cleaned.
    if config.labels.review in issue.labels:
        transition_issue_workflow_state(
            github_client, issue.number, config, config.labels.supervising
        )

    worktree_path = create_or_reuse_worktree(repo_path, issue, config, process_runner)
    supervisor_agent = choose_agent(issue, config, agent)
    cycle = (last_marker.cycle + 1) if last_marker else 1

    stashed = False
    if has_changes(worktree_path, process_runner):
        stashed = stash_worktree_changes(worktree_path, process_runner, cycle)
        if not stashed:
            github_client.comment_issue(
                issue.number,
                "## Agent Runner Review Blocked\n\n"
                "Worktree has uncommitted changes before read-only supervisor cycle. "
                "Could not auto-stash; moving to blocked.",
            )
            transition_issue_workflow_state(
                github_client, issue.number, config, config.labels.blocked
            )
            return "blocked_dirty_worktree_before_supervisor"

    try:
        action_result = run_post_pr_supervisor_cycle(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            github_client=github_client,
            process_runner=process_runner,
            pr_context=pr_context,
            supervisor_agent=supervisor_agent,
            cycle=cycle,
        )
    except Exception:
        # Supervisor cycle 异常时先恢复 stash，避免变更丢失在 worktree 里。
        if stashed:
            process_runner.run(
                ["git", "stash", "pop"],
                cwd=worktree_path,
                check=False,
            )
        raise
    action_result = guard_supervisor_action_for_pr_state(action_result, pr_context)

    if action_result.action == "approve_for_human_review":
        if stashed:
            pop_worktree_stash(worktree_path, process_runner)
        # 只读 supervisor cycle 后若留下未提交变更，不能 approve 进入 human review。
        if has_changes(worktree_path, process_runner):
            github_client.comment_issue(
                issue.number,
                "## Agent Runner Review Blocked\n\n"
                "Read-only supervisor left uncommitted changes. "
                "Moving to blocked.",
            )
            transition_issue_workflow_state(
                github_client, issue.number, config, config.labels.blocked
            )
            return "blocked_dirty_read_only_supervisor"
        transition_issue_workflow_state(github_client, issue.number, config, config.labels.review)
        return "approved_for_human_review"

    if action_result.action == "wait_for_checks":
        # PR checks are still pending; keep supervising and wait for a later
        # state change without consuming a repair attempt.
        # Stashed changes stay on the stash so the next cycle starts clean.
        _logger.info(
            "Issue #%d checks still pending; staying in %s.",
            issue.number,
            config.labels.supervising,
        )
        return "waiting_for_checks"

    if action_result.action in ("request_human_input",):
        if stashed:
            pop_worktree_stash(worktree_path, process_runner)
        transition_issue_workflow_state(github_client, issue.number, config, config.labels.blocked)
        return "blocked_human_input"

    if action_result.action == "mark_failed":
        if stashed:
            pop_worktree_stash(worktree_path, process_runner)
        transition_issue_workflow_state(github_client, issue.number, config, config.labels.failed)
        return "marked_failed"

    if action_result.action in (
        "repair_pr_branch",
        "rebase_pr_branch",
        "resolve_conflict",
    ):
        if stashed:
            pop_worktree_stash(worktree_path, process_runner)
        head_sha = get_head_sha(worktree_path, process_runner)
        github_client.comment_issue(
            issue.number,
            build_rework_intent_comment(
                action=action_result.action,
                pr_branch=pr_branch,
                head_sha=head_sha,
            ),
        )
        transition_issue_workflow_state(github_client, issue.number, config, config.labels.running)
        return f"queued_{action_result.action}"

    # Unknown action: block
    if stashed:
        pop_worktree_stash(worktree_path, process_runner)
    transition_issue_workflow_state(github_client, issue.number, config, config.labels.blocked)
    return "blocked_unknown_action"


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
            outcome = _process_review_candidate(
                issue=issue,
                repo_path=repo_path,
                config=config,
                agent=agent,
                github_client=github_client,
                process_runner=process_runner,
            )
            _logger.info(
                "Review outcome for Issue #%d: %s (%s)",
                issue.number,
                outcome,
                issue.title,
            )
        except Exception as exc:  # noqa: BLE001 - report queue failures and continue.
            exit_code = 1
            transition_issue_workflow_state(
                github_client, issue.number, config, config.labels.failed
            )
            github_client.comment_issue(
                issue.number,
                f"## Agent Runner Review Failed\n\n```text\n{exc}\n```\n",
            )
            _logger.error("Review failed for Issue #%d: %s", issue.number, exc)
    return exit_code
