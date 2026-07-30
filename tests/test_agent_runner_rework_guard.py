"""Tests for guarding post-PR rework claims on running issues.

Covers ``_guard_running_issue_is_rework`` stale-head rejection and
``_process_running_rework`` blocking when the rework worktree is gone."""

from __future__ import annotations

from pathlib import Path


from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PullRequestContext,
    ReviewEventMarker,
    WorktreeConfig,
)
from backend.core.use_cases.agent_runner_events import format_event_marker
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def test_guard_running_issue_is_rework_rejects_stale_head() -> None:
    """Rework marker whose head does not match the open PR must be ignored."""
    from backend.core.use_cases.agent_runner_orchestrate import (
        _guard_running_issue_is_rework,
    )

    config = AppConfig()
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=(config.labels.running,),
    )
    fake_client = FakeGitHubClient()
    fake_client._issue_comments[issue.number] = [
        "\n".join(
            [
                format_event_marker(
                    phase="post_pr_rework_requested",
                    cycle=1,
                    head_sha="old-sha",
                    pr_branch="issue-1",
                    action="repair_pr_branch",
                ),
                "",
                "## Agent Runner Post-PR Rework Requested",
            ]
        )
    ]
    fake_client._pr_contexts["issue-1"] = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="new-sha",
        base_sha="base-sha",
    )

    is_rework, marker = _guard_running_issue_is_rework(issue, config, fake_client)

    assert is_rework is False
    assert marker is None


def test_process_running_rework_blocks_when_worktree_missing(
    tmp_path: Path,
) -> None:
    """Missing rework worktree must transition Issue to blocked with recovery note."""
    from backend.core.use_cases.agent_runner_orchestrate import _process_running_rework

    config = AppConfig()
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=(config.labels.running,),
    )
    fake_client = FakeGitHubClient()
    fake_client._issue_labels[issue.number] = issue.labels
    missing_path = tmp_path / "missing-wt"
    fake_process_runner = FakeProcessRunner(
        responses={
            ("echo", str(missing_path)): CommandResult(
                command=("echo", str(missing_path)),
                return_code=0,
                stdout=f"{missing_path}\n",
                stderr="",
            )
        }
    )
    marker = ReviewEventMarker(
        version=1,
        phase="post_pr_rework_requested",
        cycle=1,
        head_sha="abc123",
        pr_branch="issue-1",
        action="repair_pr_branch",
    )

    _process_running_rework(
        issue=issue,
        repo_path=tmp_path,
        config=AppConfig(worktree=WorktreeConfig(path_command=f"echo {missing_path}")),
        agent="auto",
        github_client=fake_client,
        process_runner=fake_process_runner,
        marker=marker,
    )

    blocked_calls = [
        call
        for call in fake_client.calls
        if call["method"] == "edit_issue_labels" and config.labels.blocked in call.get("add", [])
    ]
    assert len(blocked_calls) == 1
    final_labels = set(fake_client._issue_labels[issue.number])
    workflow_labels = {
        config.labels.ready,
        config.labels.running,
        config.labels.supervising,
        config.labels.review,
        config.labels.failed,
        config.labels.blocked,
    }
    assert final_labels.intersection(workflow_labels) == {config.labels.blocked}
    comment_calls = [call for call in fake_client.calls if call["method"] == "comment_issue"]
    assert any(str(missing_path) in call["body"] for call in comment_calls)
