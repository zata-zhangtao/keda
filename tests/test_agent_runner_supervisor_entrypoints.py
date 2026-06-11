"""Tests for fail-closed post-PR supervisor entry points."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.shared.models.agent_runner import (
    AgentCommitResult,
    AppConfig,
    IssueSummary,
    PostPrSupervisorConfig,
    PullRequestContext,
    ReviewEventMarker,
    SupervisorActionResult,
)
from backend.core.use_cases import (
    agent_runner_orchestrate,
    agent_runner_publication,
    agent_runner_supervisor,
)
from backend.core.use_cases.agent_runner_events import format_event_marker
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def _make_issue() -> IssueSummary:
    """Return an Issue in the post-PR workflow."""
    return IssueSummary(
        number=1, title="T", url="U", body="B", labels=("agent/running",)
    )


@pytest.mark.parametrize(
    "finish_function_name",
    ("_finish_implementation_publication", "_finish_existing_commit_publication"),
)
def test_publication_defers_supervisor_without_full_pr_context(
    finish_function_name: str,
    tmp_path: Path,
) -> None:
    """Publishing must not supervise an open branch from incomplete PR state."""
    github_client = FakeGitHubClient()
    config = AppConfig()
    finish_publication = getattr(agent_runner_publication, finish_function_name)

    with (
        patch.object(
            agent_runner_publication,
            "run_pre_push_review",
            return_value=("publish-sha", []),
        ),
        patch.object(
            agent_runner_publication,
            "publish_changes",
            return_value=("issue-1", "https://github.com/example/repo/pull/1"),
        ),
        patch.object(
            agent_runner_publication, "get_head_sha", return_value="publish-sha"
        ),
        patch.object(
            agent_runner_supervisor, "_run_supervisor_with_repair_loop"
        ) as run_supervisor,
    ):
        finish_publication(
            issue=_make_issue(),
            worktree_path=tmp_path,
            config=config,
            selected_agent="codex",
            github_client=github_client,
            process_runner=FakeProcessRunner(),
            expected_branch="issue-1",
            commit_result=AgentCommitResult([], []),
        )

    run_supervisor.assert_not_called()
    label_calls = [
        call for call in github_client.calls if call["method"] == "edit_issue_labels"
    ]
    assert any(config.labels.supervising in call["add"] for call in label_calls)
    assert not any(config.labels.review in call["add"] for call in label_calls)


def _make_rework_marker() -> ReviewEventMarker:
    """Return a queued rebase marker."""
    return ReviewEventMarker(
        version=1,
        phase="post_pr_rework_requested",
        cycle=1,
        head_sha="before-sha",
        pr_branch="issue-1",
        action="rebase_pr_branch",
    )


def test_running_rework_guard_finds_rework_marker_hidden_by_later_marker() -> None:
    """A later observer marker must not mask a pending rework request."""
    github_client = FakeGitHubClient()
    config = AppConfig()
    issue = _make_issue()
    github_client._open_prs["issue-1"] = "https://github.com/example/repo/pull/1"
    github_client._pr_contexts["issue-1"] = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="base-sha",
    )
    github_client._issue_comments[issue.number] = [
        format_event_marker(
            phase="post_pr_rework_requested",
            cycle=1,
            head_sha="abc123",
            pr_branch="issue-1",
            action="repair_pr_branch",
        ),
        format_event_marker(
            phase="post_pr_supervisor",
            cycle=2,
            head_sha="abc123",
            checks_state="FAILURE",
            mergeable=True,
        ),
    ]

    is_rework, marker = agent_runner_orchestrate._guard_running_issue_is_rework(
        issue, config, github_client
    )

    assert is_rework is True
    assert marker is not None
    assert marker.phase == "post_pr_rework_requested"
    assert marker.action == "repair_pr_branch"


def test_running_rework_guard_ignores_completed_rework_marker() -> None:
    """A completion marker should consume the earlier rework request."""
    github_client = FakeGitHubClient()
    config = AppConfig()
    issue = _make_issue()
    github_client._open_prs["issue-1"] = "https://github.com/example/repo/pull/1"
    github_client._pr_contexts["issue-1"] = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="def456",
        base_sha="base-sha",
    )
    github_client._issue_comments[issue.number] = [
        format_event_marker(
            phase="post_pr_rework_requested",
            cycle=1,
            head_sha="abc123",
            pr_branch="issue-1",
            action="rebase_pr_branch",
        ),
        format_event_marker(
            phase="rebase_repair_complete",
            cycle=1,
            head_sha="def456",
        ),
        format_event_marker(
            phase="post_pr_supervisor",
            cycle=2,
            head_sha="def456",
        ),
    ]

    is_rework, marker = agent_runner_orchestrate._guard_running_issue_is_rework(
        issue, config, github_client
    )

    assert is_rework is False
    assert marker is None


def test_running_rework_defers_supervisor_without_full_pr_context(
    tmp_path: Path,
) -> None:
    """A completed rebase must wait for readable PR state before supervision."""
    github_client = FakeGitHubClient()
    config = AppConfig()

    with (
        patch.object(
            agent_runner_orchestrate,
            "_find_worktree_path_for_issue",
            return_value=tmp_path,
        ),
        patch.object(
            agent_runner_orchestrate,
            "get_current_branch",
            return_value="issue-1",
        ),
        patch.object(agent_runner_orchestrate, "choose_agent", return_value="codex"),
        patch.object(agent_runner_orchestrate, "execute_rebase", return_value=[]),
        patch.object(
            agent_runner_orchestrate, "get_head_sha", return_value="after-sha"
        ),
        patch.object(
            agent_runner_orchestrate, "_run_supervisor_with_repair_loop"
        ) as run_supervisor,
    ):
        agent_runner_orchestrate._process_running_rework(
            issue=_make_issue(),
            repo_path=tmp_path,
            config=config,
            agent="auto",
            github_client=github_client,
            process_runner=FakeProcessRunner(),
            marker=_make_rework_marker(),
        )

    run_supervisor.assert_not_called()
    assert any(
        call["method"] == "get_pull_request_context" for call in github_client.calls
    )
    assert not any(
        call["method"] == "edit_issue_labels" and config.labels.review in call["add"]
        for call in github_client.calls
    )


def test_running_rework_without_supervisor_does_not_require_pr_context(
    tmp_path: Path,
) -> None:
    """Disabling supervisor should preserve the direct transition to review."""
    github_client = FakeGitHubClient()
    config = AppConfig(post_pr_supervisor=PostPrSupervisorConfig(enabled=False))

    with (
        patch.object(
            agent_runner_orchestrate,
            "_find_worktree_path_for_issue",
            return_value=tmp_path,
        ),
        patch.object(
            agent_runner_orchestrate,
            "get_current_branch",
            return_value="issue-1",
        ),
        patch.object(agent_runner_orchestrate, "choose_agent", return_value="codex"),
        patch.object(agent_runner_orchestrate, "execute_rebase", return_value=[]),
        patch.object(
            agent_runner_orchestrate, "get_head_sha", return_value="after-sha"
        ),
    ):
        agent_runner_orchestrate._process_running_rework(
            issue=_make_issue(),
            repo_path=tmp_path,
            config=config,
            agent="auto",
            github_client=github_client,
            process_runner=FakeProcessRunner(),
            marker=_make_rework_marker(),
        )

    assert not any(
        call["method"] == "get_pull_request_context" for call in github_client.calls
    )
    assert any(
        call["method"] == "edit_issue_labels" and config.labels.review in call["add"]
        for call in github_client.calls
    )


@pytest.mark.parametrize("action", ("repair_pr_branch", "rebase_pr_branch"))
def test_supervisor_loop_defers_after_rework_when_pr_context_refresh_fails(
    action: str,
    tmp_path: Path,
) -> None:
    """A follow-up cycle must not approve after losing complete PR state."""
    github_client = FakeGitHubClient()
    config = AppConfig(post_pr_supervisor=PostPrSupervisorConfig(max_repair_attempts=1))
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="before-sha",
        base_sha="base-sha",
    )

    with (
        patch.object(
            agent_runner_supervisor,
            "run_post_pr_supervisor_cycle",
            return_value=SupervisorActionResult(action=action),
        ) as supervisor_cycle,
        patch.object(agent_runner_supervisor, "execute_repair", return_value=[]),
        patch.object(agent_runner_supervisor, "execute_rebase", return_value=[]),
        patch.object(agent_runner_supervisor, "get_head_sha", return_value="after-sha"),
    ):
        agent_runner_supervisor._run_supervisor_with_repair_loop(
            issue=_make_issue(),
            worktree_path=tmp_path,
            config=config,
            github_client=github_client,
            process_runner=FakeProcessRunner(),
            pr_context=pr_context,
            supervisor_agent="codex",
        )

    assert supervisor_cycle.call_count == 1
    assert any(
        call["method"] == "get_pull_request_context" for call in github_client.calls
    )
    assert not any(
        call["method"] == "edit_issue_labels" and config.labels.review in call["add"]
        for call in github_client.calls
    )
    comment_bodies = [
        call["body"]
        for call in github_client.calls
        if call["method"] == "comment_issue"
    ]
    assert any("phase=post_pr_rework_requested" in body for body in comment_bodies)
    assert any("phase=rebase_repair_complete" in body for body in comment_bodies)
