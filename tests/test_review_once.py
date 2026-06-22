"""Tests for review_once post-PR review polling."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PullRequestContext,
)
from backend.core.use_cases.agent_runner_events import format_event_marker
from backend.core.use_cases.agent_runner_workflow import workflow_state_labels
from backend.core.use_cases.pr_supervisor import build_supervisor_result_comment
from backend.core.use_cases.review_once import (
    _process_review_candidate,
    review_once,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def _marker_comment(
    *,
    head_sha: str = "abc123",
    base_sha: str = "def456",
    action: str | None = None,
    checks_state: str = "PENDING",
    mergeable: bool = True,
    issue_comments_count: int = 1,
    pr_comments_count: int = 0,
) -> str:
    action_part = f"action={action} " if action else ""
    return (
        "<!-- iar:event version=1 phase=post_pr_supervisor cycle=1 "
        f"head={head_sha} base={base_sha} pr_branch=issue-1 "
        f"{action_part}"
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
    client._issue_labels[issue.number] = issue.labels
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
    # Failed checks block approval and request branch repair.
    assert len(label_calls) == 1
    assert label_calls[0]["add"] == ["agent/running"]
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
    client._issue_labels[issue.number] = issue.labels
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
    client._issue_labels[issue.number] = issue.labels
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
    client._issue_labels[issue.number] = issue.labels
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


def test_review_once_blocks_conflicting_pr_approval() -> None:
    """Conflicting PRs must not be moved into human review after approval."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/review",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_labels[issue.number] = issue.labels
    client._issue_comments[1] = [
        _marker_comment(mergeable=True, issue_comments_count=1, pr_comments_count=0)
    ]
    client._pr_contexts["issue-1"] = _make_pr_context(mergeable=False)
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
                stderr="",
            )
        }
    )

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
        ),
    ):
        outcome = _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=fake_runner,
        )

    assert outcome == "queued_rebase_pr_branch"
    label_calls = [c for c in client.calls if c["method"] == "edit_issue_labels"]
    assert label_calls[-1]["add"] == ["agent/running"]
    assert label_calls[-1]["remove"] == ["agent/supervising"]
    assert not any(call["add"] == ["agent/review"] for call in label_calls)
    comment_calls = [c for c in client.calls if c["method"] == "comment_issue"]
    assert "Action: rebase_pr_branch" in comment_calls[-1]["body"]


def test_review_once_defers_when_full_open_pr_context_is_unavailable() -> None:
    """Open PR lookup without full context must not permit supervisor approval."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/review",),
    )
    client = FakeGitHubClient()
    client._issue_comments[1] = [
        format_event_marker(
            phase="draft_pr_created",
            cycle=1,
            head_sha="abc123",
            pr_branch="issue-1",
        )
    ]
    client._pr_contexts["issue-1"] = None
    client._open_prs["issue-1"] = "https://github.com/example/repo/pull/1"

    with patch(
        "backend.core.use_cases.review_once.run_post_pr_supervisor_cycle",
        return_value=_supervisor_approve(),
    ) as mock_cycle:
        outcome = _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert outcome == "deferred_pr_context_unavailable"
    assert mock_cycle.called is False
    assert not any(call["method"] == "edit_issue_labels" for call in client.calls)


def test_review_once_logs_queued_rebase_outcome(caplog) -> None:
    """Polling logs should distinguish queued rebase from completed review."""
    config = AppConfig()
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=(config.labels.review,),
    )
    client = FakeGitHubClient()
    client.list_review_candidate_issues = lambda labels, limit: [issue]
    client._remote_base_sha = "def456"
    client._issue_comments[1] = [
        _marker_comment(mergeable=True, issue_comments_count=1, pr_comments_count=0)
    ]
    client._pr_contexts["issue-1"] = _make_pr_context(mergeable=False)
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
                stderr="",
            )
        }
    )

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
        ),
        caplog.at_level(logging.INFO),
    ):
        exit_code = review_once(
            repo_path=Path("."),
            config=config,
            dry_run=False,
            agent="auto",
            max_issues=1,
            github_client=client,
            process_runner=fake_runner,
        )

    assert exit_code == 0
    assert "queued_rebase_pr_branch" in caplog.text
    assert "Reviewed Issue" not in caplog.text


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
    client._issue_labels[issue.number] = issue.labels
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


def test_review_once_skips_after_supervisor_writes_its_own_comment() -> None:
    """Supervisor result comments must not retrigger the next review pass."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/review",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_labels[issue.number] = issue.labels
    client._issue_comments[1] = [
        "\n".join(
            [
                format_event_marker(
                    phase="draft_pr_created",
                    cycle=1,
                    head_sha="abc123",
                    pr_branch="issue-1",
                ),
                "",
                "## Agent Runner Draft PR Created",
                "",
                "- Branch: `issue-1`",
            ]
        ),
        build_supervisor_result_comment(
            action="approve_for_human_review",
            supervisor="codex",
            summary="LGTM",
            findings_counts={},
            verification_status="passed",
            head_sha="abc123",
            cycle=1,
            base_sha="def456",
            checks_state="PENDING",
            mergeable=True,
            issue_comments_count=2,
            pr_comments_count=1,
        ),
    ]
    client._pr_contexts["issue-1"] = _make_pr_context()
    client._pr_comments[1] = ["one"]

    with patch(
        "backend.core.use_cases.review_once.run_post_pr_supervisor_cycle",
        return_value=_supervisor_approve(),
    ) as mock_cycle:
        outcome = _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert outcome == "skipped_context_unchanged"
    assert mock_cycle.called is False
    label_calls = [c for c in client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 0


def test_review_once_reruns_supervisor_after_mark_failed_marker() -> None:
    """A mark_failed marker must not suppress re-supervision of the same context.

    mark_failed 没有产出有效评审结论（如 agent 基础设施崩溃重试耗尽）；
    人工把 label 拨回 supervising 即是明确的重试请求。
    """
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/supervising",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_labels[issue.number] = issue.labels
    client._issue_comments[1] = [
        _marker_comment(
            action="mark_failed", issue_comments_count=1, pr_comments_count=0
        )
    ]
    # PR 上下文与 marker 完全一致：没有 mark_failed 放行的话本应被去重跳过
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
        outcome = _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert outcome != "skipped_context_unchanged"
    assert mock_cycle.called is True


def test_review_once_still_skips_unchanged_context_with_approve_marker() -> None:
    """Markers now record action; a non-failed action must keep the dedup skip."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/supervising",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_labels[issue.number] = issue.labels
    client._issue_comments[1] = [
        _marker_comment(
            action="approve_for_human_review",
            issue_comments_count=1,
            pr_comments_count=0,
        )
    ]
    client._pr_contexts["issue-1"] = _make_pr_context()

    with patch(
        "backend.core.use_cases.review_once.run_post_pr_supervisor_cycle",
        return_value=_supervisor_approve(),
    ) as mock_cycle:
        outcome = _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert outcome == "skipped_context_unchanged"
    assert mock_cycle.called is False


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
    client._issue_labels[issue.number] = issue.labels
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


def test_review_once_recovers_branch_from_draft_pr_comment(tmp_path: Path) -> None:
    """Review polling should use the original Draft PR branch comment."""
    config = AppConfig()
    issue = IssueSummary(
        number=23,
        title="Feature",
        url="https://github.com/example/repo/issues/23",
        body="Do the work.",
        labels=(config.labels.review,),
    )
    fake_client = FakeGitHubClient()
    fake_client.list_review_candidate_issues = lambda labels, limit: [issue]
    fake_client._issue_labels[issue.number] = issue.labels
    fake_client._issue_comments[issue.number] = [
        "\n".join(
            [
                format_event_marker(
                    phase="draft_pr_created",
                    cycle=1,
                    head_sha="abc123",
                    pr_branch="issue-23",
                ),
                "",
                "## Agent Runner Draft PR Created",
                "",
                "- Branch: `issue-23`",
                "- Draft PR: https://github.com/example/repo/pull/23",
                "- Head SHA: `abc123`",
            ]
        ),
        "\n".join(
            [
                format_event_marker(
                    phase="post_pr_supervisor",
                    cycle=2,
                    head_sha="abc123",
                    base_sha="remote-base-sha",
                ),
                "",
                "## Agent Runner Post-PR Supervisor",
                "",
                "- Action: approve_for_human_review",
            ]
        ),
    ]
    fake_client._pr_contexts["issue-23"] = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/23",
        branch="issue-23",
        head_sha="abc123",
        base_sha="remote-base-sha",
    )

    exit_code = review_once(
        repo_path=tmp_path,
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=FakeProcessRunner(),
    )

    assert exit_code == 0
    assert {
        "method": "get_pull_request_context",
        "branch": "issue-23",
    } in fake_client.calls
    assert not any(
        call["method"] == "find_open_pr_by_head" for call in fake_client.calls
    )


def test_review_once_cleans_dirty_workflow_labels() -> None:
    """Transition helper must leave exactly one durable workflow label."""
    config = AppConfig()
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=(
            config.labels.review,
            config.labels.running,
            config.labels.failed,
            config.labels.agent_labels["codex"],
        ),
    )
    client = FakeGitHubClient()
    client._issue_labels[issue.number] = issue.labels
    client._remote_base_sha = "def456"
    client._issue_comments[1] = [
        _marker_comment(issue_comments_count=1, pr_comments_count=0)
    ]
    client._pr_contexts["issue-1"] = _make_pr_context(checks_state="SUCCESS")
    client._pr_comments[1] = ["new pr comment"]

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
        ),
    ):
        _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=config,
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    final_labels = set(client._issue_labels[issue.number])
    workflow_labels = set(workflow_state_labels(config))
    assert final_labels.intersection(workflow_labels) == {config.labels.review}
    assert config.labels.agent_labels["codex"] in final_labels


def test_review_once_waits_for_pending_checks() -> None:
    """Pending checks must keep the Issue in supervising, not move to review."""
    config = AppConfig()
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=(config.labels.supervising,),
    )
    client = FakeGitHubClient()
    client._issue_labels[issue.number] = issue.labels
    client._remote_base_sha = "def456"
    client._issue_comments[1] = [
        _marker_comment(
            checks_state="PENDING", issue_comments_count=1, pr_comments_count=0
        )
    ]
    client._pr_contexts["issue-1"] = _make_pr_context(checks_state="PENDING")
    client._pr_comments[1] = ["new pr comment"]

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
        ),
    ):
        outcome = _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=config,
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert outcome == "waiting_for_checks"
    final_labels = set(client._issue_labels[issue.number])
    workflow_labels = set(workflow_state_labels(config))
    assert final_labels.intersection(workflow_labels) == {config.labels.supervising}


def test_review_once_skips_running_issue_with_pending_rework() -> None:
    """Review must not overwrite a pending rework marker on a running Issue."""
    config = AppConfig()
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=(config.labels.running, config.labels.review),
    )
    client = FakeGitHubClient()
    client._issue_labels[issue.number] = issue.labels
    client._remote_base_sha = "def456"
    client._issue_comments[1] = [
        "\n".join(
            [
                format_event_marker(
                    phase="post_pr_rework_requested",
                    cycle=1,
                    head_sha="abc123",
                    pr_branch="issue-1",
                    action="repair_pr_branch",
                ),
                "",
                "## Agent Runner Post-PR Rework Requested",
            ]
        )
    ]
    client._pr_contexts["issue-1"] = _make_pr_context()

    with patch(
        "backend.core.use_cases.review_once.run_post_pr_supervisor_cycle",
        return_value=_supervisor_approve(),
    ) as mock_cycle:
        outcome = _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=config,
            agent="auto",
            github_client=client,
            process_runner=FakeProcessRunner(),
        )

    assert outcome == "skipped_pending_rework"
    assert mock_cycle.called is False
    assert not any(call["method"] == "edit_issue_labels" for call in client.calls)


def test_review_once_auto_stashes_dirty_worktree_and_approves() -> None:
    """Dirty worktree is auto-stashed before review supervisor and restored on approve."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/supervising",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_labels[issue.number] = issue.labels
    client._issue_comments[1] = [
        _marker_comment(issue_comments_count=1, pr_comments_count=0),
        "another comment",
    ]
    client._pr_contexts["issue-1"] = _make_pr_context(checks_state="SUCCESS")

    class _StashThenApproveRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._status_calls = 0

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple == ("git", "status", "--porcelain"):
                self._status_calls += 1
                # Before stash: dirty; after stash (and after pop): clean
                stdout = " M file.py\n" if self._status_calls == 1 else ""
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("git", "stash", "pop"):
                return CommandResult(command_tuple, 0, "", "")
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _StashThenApproveRunner()

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
        outcome = _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=fake_runner,
        )

    assert outcome == "approved_for_human_review"
    assert mock_cycle.called is True
    commands = [tuple(c) for c in fake_runner.calls]
    assert (
        "git",
        "stash",
        "push",
        "-u",
        "-m",
        "iar: auto-stash before supervisor cycle 2",
    ) in commands
    assert ("git", "stash", "pop") in commands
    label_calls = [c for c in client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 1
    assert label_calls[0]["add"] == ["agent/review"]


def test_review_once_dirty_worktree_stash_fails_blocked() -> None:
    """When auto-stash fails, review still blocks the Issue."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/supervising",),
    )
    client = FakeGitHubClient()
    client._remote_base_sha = "def456"
    client._issue_labels[issue.number] = issue.labels
    client._issue_comments[1] = [
        _marker_comment(issue_comments_count=1, pr_comments_count=0),
        "another comment",
    ]
    client._pr_contexts["issue-1"] = _make_pr_context(checks_state="SUCCESS")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M file.py\n",
                stderr="",
            ),
            (
                "git",
                "stash",
                "push",
                "-u",
                "-m",
                "iar: auto-stash before supervisor cycle 2",
            ): CommandResult(
                command=(
                    "git",
                    "stash",
                    "push",
                    "-u",
                    "-m",
                    "iar: auto-stash before supervisor cycle 2",
                ),
                return_code=1,
                stdout="",
                stderr="stash failed",
            ),
        }
    )

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
        outcome = _process_review_candidate(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            agent="auto",
            github_client=client,
            process_runner=fake_runner,
        )

    assert outcome == "blocked_dirty_worktree_before_supervisor"
    assert mock_cycle.called is False
    label_calls = [c for c in client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 1
    assert label_calls[0]["add"] == ["agent/blocked"]
    comment_calls = [c for c in client.calls if c["method"] == "comment_issue"]
    assert any("Could not auto-stash" in c["body"] for c in comment_calls)
