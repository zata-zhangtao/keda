"""Tests for review_once post-PR review polling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    PullRequestContext,
)
from backend.core.use_cases.review_once import (
    _process_review_candidate,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def _marker_comment(
    *,
    head_sha: str = "abc123",
    base_sha: str = "def456",
    checks_state: str = "PENDING",
    mergeable: bool = True,
    issue_comments_count: int = 1,
    pr_comments_count: int = 0,
) -> str:
    return (
        "<!-- iar:event version=1 phase=post_pr_supervisor cycle=1 "
        f"head={head_sha} base={base_sha} pr_branch=issue-1 "
        f"checks_state={checks_state} mergeable={'true' if mergeable else 'false'} "
        f"issue_comments_count={issue_comments_count} pr_comments_count={pr_comments_count} -->"
    )


def _make_pr_context(**kwargs: object) -> PullRequestContext:
    defaults: dict[str, object] = {
        "pr_url": "https://github.com/example/repo/pull/1",
        "branch": "issue-1",
        "head_sha": "abc123",
        "base_sha": "def456",
        "checks_state": "PENDING",
        "mergeable": True,
    }
    defaults.update(kwargs)
    return PullRequestContext(**defaults)  # type: ignore[arg-type]


def _supervisor_approve() -> MagicMock:
    """Return a mock supervisor action result for approve."""
    mock = MagicMock()
    mock.action = "approve_for_human_review"
    mock.summary = "LGTM"
    mock.findings_counts = {}
    mock.verification_status = ""
    mock.head_sha = None
    return mock


def test_review_once_detects_checks_state_change_and_triggers_supervisor() -> None:
    """checks_state change should trigger supervisor cycle."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/supervising",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_comments[1] = [
        _marker_comment(
            checks_state="PENDING", issue_comments_count=1, pr_comments_count=0
        )
    ]
    client._pr_contexts["issue-1"] = _make_pr_context(checks_state="FAILURE")

    with (
        patch(
            "backend.core.use_cases.review_once.create_or_reuse_worktree",
            return_value=Path("."),
        ),
        patch(
            "backend.core.use_cases.review_once.choose_agent",
            return_value="codex",
        ),
        patch(
            "backend.core.use_cases.review_once.run_post_pr_supervisor_cycle",
            return_value=_supervisor_approve(),
        ) as mock_cycle,
    ):
        _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert mock_cycle.called is True
    label_calls = [c for c in client.calls if c["method"] == "edit_issue_labels"]
    # supervisor approve moves supervising -> review
    assert len(label_calls) == 1
    assert label_calls[0]["add"] == ["agent/review"]
    assert label_calls[0]["remove"] == ["agent/supervising"]


def test_review_once_detects_new_issue_comments_and_triggers_supervisor() -> None:
    """New issue comments should trigger supervisor cycle."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/supervising",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_comments[1] = [
        _marker_comment(issue_comments_count=1, pr_comments_count=0),
        "new comment",
    ]
    client._pr_contexts["issue-1"] = _make_pr_context()

    with (
        patch(
            "backend.core.use_cases.review_once.create_or_reuse_worktree",
            return_value=Path("."),
        ),
        patch(
            "backend.core.use_cases.review_once.choose_agent",
            return_value="codex",
        ),
        patch(
            "backend.core.use_cases.review_once.run_post_pr_supervisor_cycle",
            return_value=_supervisor_approve(),
        ) as mock_cycle,
    ):
        _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert mock_cycle.called is True


def test_review_once_detects_new_pr_comments_and_triggers_supervisor() -> None:
    """New PR comments should trigger supervisor cycle."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/supervising",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_comments[1] = [
        _marker_comment(issue_comments_count=1, pr_comments_count=1)
    ]
    client._pr_contexts["issue-1"] = _make_pr_context()
    client._pr_comments[1] = ["new pr comment", "another"]

    with (
        patch(
            "backend.core.use_cases.review_once.create_or_reuse_worktree",
            return_value=Path("."),
        ),
        patch(
            "backend.core.use_cases.review_once.choose_agent",
            return_value="codex",
        ),
        patch(
            "backend.core.use_cases.review_once.run_post_pr_supervisor_cycle",
            return_value=_supervisor_approve(),
        ) as mock_cycle,
    ):
        _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert mock_cycle.called is True


def test_review_once_detects_mergeable_change_and_triggers_supervisor() -> None:
    """mergeable change should trigger supervisor cycle."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/supervising",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_comments[1] = [
        _marker_comment(mergeable=True, issue_comments_count=1, pr_comments_count=0)
    ]
    client._pr_contexts["issue-1"] = _make_pr_context(mergeable=False)

    with (
        patch(
            "backend.core.use_cases.review_once.create_or_reuse_worktree",
            return_value=Path("."),
        ),
        patch(
            "backend.core.use_cases.review_once.choose_agent",
            return_value="codex",
        ),
        patch(
            "backend.core.use_cases.review_once.run_post_pr_supervisor_cycle",
            return_value=_supervisor_approve(),
        ) as mock_cycle,
    ):
        _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert mock_cycle.called is True


def test_review_once_skips_when_no_context_change() -> None:
    """When context is unchanged, supervisor cycle should not run."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/supervising",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_comments[1] = [
        _marker_comment(issue_comments_count=1, pr_comments_count=1)
    ]
    client._pr_contexts["issue-1"] = _make_pr_context()
    client._pr_comments[1] = ["one"]

    with (
        patch(
            "backend.core.use_cases.review_once.create_or_reuse_worktree",
            return_value=Path("."),
        ),
        patch(
            "backend.core.use_cases.review_once.choose_agent",
            return_value="codex",
        ),
        patch(
            "backend.core.use_cases.review_once.run_post_pr_supervisor_cycle",
            return_value=_supervisor_approve(),
        ) as mock_cycle,
    ):
        _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert mock_cycle.called is False
    label_calls = [c for c in client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 0


def test_review_once_moves_review_label_to_supervising_on_change() -> None:
    """Issue with review label should be moved to supervising before supervisor runs."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/review",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_comments[1] = [
        _marker_comment(issue_comments_count=1, pr_comments_count=0)
    ]
    client._pr_contexts["issue-1"] = _make_pr_context(checks_state="FAILURE")

    with (
        patch(
            "backend.core.use_cases.review_once.create_or_reuse_worktree",
            return_value=Path("."),
        ),
        patch(
            "backend.core.use_cases.review_once.choose_agent",
            return_value="codex",
        ),
        patch(
            "backend.core.use_cases.review_once.run_post_pr_supervisor_cycle",
            return_value=_supervisor_approve(),
        ) as mock_cycle,
    ):
        _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert mock_cycle.called is True
    label_calls = [c for c in client.calls if c["method"] == "edit_issue_labels"]
    # First label call must be review -> supervising (before supervisor)
    assert label_calls[0]["add"] == ["agent/supervising"]
    assert label_calls[0]["remove"] == ["agent/review"]
