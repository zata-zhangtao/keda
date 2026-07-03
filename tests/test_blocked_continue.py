"""Tests for blocked-continue use case."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
)
from backend.core.use_cases.agent_runner_failure import format_blocked_failure_comment
from backend.core.use_cases.blocked_continue import (
    BlockedContinueError,
    blocked_continue_issue,
    _extract_blocked_paths_from_comments,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def _make_blocked_issue(number: int) -> IssueSummary:
    return IssueSummary(
        number=number,
        title=f"Issue #{number}",
        url=f"https://github.com/example/repo/issues/{number}",
        body="",
        labels=("agent/blocked",),
    )


def _setup_worktree_runner() -> FakeProcessRunner:
    responses = {
        ("git", "status", "--porcelain"): CommandResult(
            ("git", "status", "--porcelain"), 0, "", ""
        ),
        ("git", "branch", "--show-current"): CommandResult(
            ("git", "branch", "--show-current"), 0, "issue-99\n", ""
        ),
        ("git", "rev-parse", "HEAD"): CommandResult(
            ("git", "rev-parse", "HEAD"), 0, "abc123\n", ""
        ),
        ("git", "status", "--short"): CommandResult(("git", "status", "--short"), 0, "", ""),
    }
    return FakeProcessRunner(responses=responses)


def test_blocked_continue_missing_blocked_label_fails() -> None:
    fake_client = FakeGitHubClient()
    fake_client.get_issue = lambda n: IssueSummary(
        number=n,
        title=f"Issue #{n}",
        url=f"https://github.com/example/repo/issues/{n}",
        body="",
        labels=("agent/running",),
    )
    runner = FakeProcessRunner()

    with pytest.raises(BlockedContinueError, match="does not have label"):
        blocked_continue_issue(
            issue_number=1,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=fake_client,
            process_runner=runner,
        )


def test_blocked_continue_dirty_worktree_fails() -> None:
    fake_client = FakeGitHubClient()
    fake_client.get_issue = lambda n: _make_blocked_issue(n)
    responses = {
        ("git", "status", "--porcelain"): CommandResult(
            ("git", "status", "--porcelain"), 0, " M foo.py\n", ""
        ),
        ("git", "branch", "--show-current"): CommandResult(
            ("git", "branch", "--show-current"), 0, "issue-1\n", ""
        ),
    }
    runner = FakeProcessRunner(responses=responses)

    with pytest.raises(BlockedContinueError, match="uncommitted changes"):
        blocked_continue_issue(
            issue_number=1,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=fake_client,
            process_runner=runner,
        )


def test_blocked_continue_wrong_branch_fails() -> None:
    fake_client = FakeGitHubClient()
    fake_client.get_issue = lambda n: _make_blocked_issue(n)
    responses = {
        ("git", "status", "--porcelain"): CommandResult(
            ("git", "status", "--porcelain"), 0, "", ""
        ),
        ("git", "branch", "--show-current"): CommandResult(
            ("git", "branch", "--show-current"), 0, "main\n", ""
        ),
    }
    runner = FakeProcessRunner(responses=responses)

    with pytest.raises(BlockedContinueError, match="main"):
        blocked_continue_issue(
            issue_number=1,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=fake_client,
            process_runner=runner,
        )


def test_blocked_continue_already_claimed_returns_false() -> None:
    fake_client = FakeGitHubClient()
    # First get_issue returns blocked; later returns running+blocked (simulating another runner claimed it)
    call_count = 0

    def _get_issue(number: int) -> IssueSummary:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_blocked_issue(number)
        return IssueSummary(
            number=number,
            title=f"Issue #{number}",
            url=f"https://github.com/example/repo/issues/{number}",
            body="",
            labels=("agent/running", "agent/blocked"),
        )

    fake_client.get_issue = _get_issue
    responses = {
        ("git", "status", "--porcelain"): CommandResult(
            ("git", "status", "--porcelain"), 0, "", ""
        ),
        ("git", "branch", "--show-current"): CommandResult(
            ("git", "branch", "--show-current"), 0, "issue-99\n", ""
        ),
        ("git", "rev-parse", "HEAD"): CommandResult(
            ("git", "rev-parse", "HEAD"), 0, "abc123\n", ""
        ),
    }
    runner = FakeProcessRunner(responses=responses)

    result = blocked_continue_issue(
        issue_number=99,
        repo_path=Path("."),
        config=AppConfig(),
        agent="auto",
        github_client=fake_client,
        process_runner=runner,
    )
    assert result is False


def test_extract_blocked_paths_from_comments() -> None:
    body = format_blocked_failure_comment(
        RuntimeError("Refusing to publish forbidden paths: .env, secrets/key"),
        issue_number=1,
    )
    fake_client = FakeGitHubClient()
    fake_client.comment_issue(1, body)
    issue = _make_blocked_issue(1)

    paths = _extract_blocked_paths_from_comments(issue, fake_client)
    assert ".env" in paths
    assert "secrets/key" in paths
