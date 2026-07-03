"""Blocked-continue CLI use case for forbidden path resolution."""

from __future__ import annotations

import logging
from pathlib import Path

from backend.core.shared.interfaces.agent_runner import IGitHubClient, IProcessRunner
from backend.core.shared.models.agent_runner import AppConfig, IssueSummary
from backend.core.use_cases.agent_runner_events import (
    _parse_event_marker,
    format_event_marker,
)
from backend.core.use_cases.agent_runner_git import get_current_branch, has_changes
from backend.core.use_cases.agent_runner_orchestrate import (
    BlockedWorktreeClaimedError,
    _find_worktree_path_for_issue,
    _process_blocked_resolution,
)
from backend.core.use_cases.agent_runner_publish import validate_safe_changes
from backend.core.use_cases.agent_runner_workflow import claim_blocked_issue

_logger = logging.getLogger(__name__)


class BlockedContinueError(RuntimeError):
    """Raised when blocked-continue validation fails."""

    pass


def blocked_continue_issue(
    *,
    issue_number: int,
    repo_path: Path,
    config: AppConfig,
    agent: str,
    github_client: IGitHubClient,
    process_runner: IProcessRunner,
) -> bool:
    """Resume a blocked Issue after the operator has resolved forbidden paths.

    Steps:
    1. Validate the worktree is clean and no forbidden paths remain.
    2. Write a ``blocked_resolution_requested`` marker comment.
    3. Attempt a CAS claim to move the Issue from ``agent/blocked`` to
       ``agent/running``.
    4. If claimed successfully, start ``_process_blocked_resolution``.

    Args:
        issue_number: GitHub Issue number.
        repo_path: Repository root path.
        config: Application configuration.
        agent: Agent override.
        github_client: GitHub client.
        process_runner: Process runner.

    Returns:
        True if the Issue was claimed and processing started, False if
        another runner already claimed it.

    Raises:
        BlockedContinueError: Worktree validation failed.
    """
    issue = github_client.get_issue(issue_number)
    if config.labels.blocked not in issue.labels:
        raise BlockedContinueError(
            f"Issue #{issue_number} does not have label {config.labels.blocked}."
        )

    # Validate worktree
    try:
        worktree_path = _find_worktree_path_for_issue(repo_path, issue, config, process_runner)
    except Exception as exc:
        raise BlockedContinueError(
            f"Could not locate worktree for Issue #{issue_number}: {exc}"
        ) from exc

    current_branch = get_current_branch(worktree_path, process_runner)
    expected_branch = f"issue-{issue_number}"
    if current_branch != expected_branch:
        raise BlockedContinueError(
            f"Worktree is on branch {current_branch}, expected {expected_branch}."
        )

    if has_changes(worktree_path, process_runner):
        raise BlockedContinueError(
            "Worktree has uncommitted changes. Please commit or stash them first."
        )

    try:
        validate_safe_changes(worktree_path, config, process_runner)
    except RuntimeError as exc:
        raise BlockedContinueError(
            f"Forbidden paths are still present in the worktree: {exc}"
        ) from exc

    # Extract blocked paths from the original blocked comment for the marker
    blocked_paths = _extract_blocked_paths_from_comments(issue, github_client)

    # Write blocked_resolution_requested marker
    marker = format_event_marker(
        phase="blocked_resolution_requested",
        cycle=1,
        blocked_paths=blocked_paths,
    )
    github_client.comment_issue(issue_number, marker)
    _logger.info("Wrote blocked_resolution_requested marker for Issue #%d.", issue_number)

    # CAS claim
    claimed = claim_blocked_issue(github_client, issue_number, config)
    if not claimed:
        _logger.info("Issue #%d already claimed by another runner.", issue_number)
        return False

    # Proceed with blocked resolution
    try:
        _process_blocked_resolution(
            issue=issue,
            repo_path=repo_path,
            config=config,
            agent=agent,
            github_client=github_client,
            process_runner=process_runner,
            marker=_build_marker_from_paths(blocked_paths),
        )
    except BlockedWorktreeClaimedError:
        _logger.info("Issue #%d worktree claimed by another runner.", issue_number)
        return False
    return True


def _extract_blocked_paths_from_comments(
    issue: IssueSummary,
    github_client: IGitHubClient,
) -> tuple[str, ...]:
    """Extract blocked paths from the latest blocked failure comment.

    优先从 ``iar:event`` marker 的 ``blocked_paths`` 字段提取结构化数据；
    若 marker 不存在，则降级为从 Markdown 正文中解析 ``Blocked paths:``
    列表。
    """
    comments = github_client.list_issue_comments(issue.number)
    # 优先：从最新的 event marker 提取结构化 blocked_paths
    for comment_body in reversed(comments):
        marker = _parse_event_marker(comment_body)
        if marker is not None and marker.blocked_paths:
            return marker.blocked_paths
    # 降级：从 Markdown 正文反向推断
    for comment_body in reversed(comments):
        if "Blocked paths:" in comment_body:
            paths: list[str] = []
            for line in comment_body.splitlines():
                stripped = line.strip()
                if stripped.startswith("- `") and stripped.endswith("`"):
                    path = stripped[3:-1]
                    if path:
                        paths.append(path)
            return tuple(paths)
    return ()


def _build_marker_from_paths(blocked_paths: tuple[str, ...]):
    """Build a minimal ReviewEventMarker for blocked resolution."""
    from backend.core.shared.models.agent_runner import ReviewEventMarker

    return ReviewEventMarker(
        version=1,
        phase="blocked_resolution_requested",
        cycle=1,
        blocked_paths=blocked_paths,
    )
