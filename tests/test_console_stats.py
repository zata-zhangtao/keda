"""Tests for real-time completion stats aggregation."""

from __future__ import annotations

from pathlib import Path

from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    RepositoryRunContext,
)
from backend.core.use_cases.console_stats import build_completion_stats


class FakeStatsGitHubClient:
    """IGitHubClient stub returning canned label query results."""

    def __init__(self, issues_by_label: dict[str, list[IssueSummary]]) -> None:
        self._issues_by_label = issues_by_label
        self.requested_states: list[str] = []

    def list_issues_by_label(self, label, limit, state="all"):
        self.requested_states.append(state)
        return self._issues_by_label.get(label, [])


def _issue(number: int, labels: tuple[str, ...], state: str) -> IssueSummary:
    return IssueSummary(
        number=number,
        title=f"Issue {number}",
        url=f"https://example.test/{number}",
        body="",
        labels=labels,
        state=state,
    )


def _make_context() -> RepositoryRunContext:
    return RepositoryRunContext(
        repo_id="keda-main",
        display_name="Keda Main",
        repo_path=Path("/tmp/repo"),
        config=AppConfig(),
    )


def test_completion_stats_partitions_outcomes() -> None:
    """Closed/failed/blocked/open issues should be counted correctly."""
    config = AppConfig()
    labels = config.labels
    issues_by_label = {
        # closed 且无 failed/blocked → completed
        labels.review: [_issue(1, (labels.review,), "CLOSED")],
        # closed 但带 failed → failed，不计 completed
        labels.failed: [
            _issue(2, (labels.failed,), "CLOSED"),
            _issue(3, (labels.failed,), "OPEN"),
        ],
        # open blocked
        labels.blocked: [_issue(4, (labels.blocked,), "OPEN")],
        # open 进行中
        labels.running: [_issue(5, (labels.running,), "OPEN")],
    }
    github_client = FakeStatsGitHubClient(issues_by_label)
    stats = build_completion_stats(context=_make_context(), github_client=github_client)

    assert stats.total_tracked == 5
    assert stats.completed == 1
    assert stats.failed == 2
    assert stats.blocked == 1
    assert stats.open_in_pipeline == 1  # 仅 #5；#3/#4 是 open failed/blocked。
    assert stats.completion_rate == 1 / 5
    assert stats.truncated is False
    # 必须用 state="all" 查询，否则 closed Issue 进不了统计。
    assert set(github_client.requested_states) == {"all"}


def test_completion_stats_dedupes_multi_label_issues() -> None:
    """An issue carrying two workflow labels must be counted once."""
    config = AppConfig()
    labels = config.labels
    shared_issue = _issue(7, (labels.supervising, labels.review), "OPEN")
    github_client = FakeStatsGitHubClient(
        {
            labels.supervising: [shared_issue],
            labels.review: [shared_issue],
        }
    )
    stats = build_completion_stats(context=_make_context(), github_client=github_client)
    assert stats.total_tracked == 1
    assert stats.open_in_pipeline == 1


def test_completion_stats_empty_repo() -> None:
    """No tracked issues → completion_rate is None, not a division error."""
    stats = build_completion_stats(
        context=_make_context(), github_client=FakeStatsGitHubClient({})
    )
    assert stats.total_tracked == 0
    assert stats.completion_rate is None


def test_completion_stats_isolates_github_failure() -> None:
    """A GitHub query failure must degrade to an error entry, not raise."""

    class ExplodingClient:
        def list_issues_by_label(self, label, limit, state="all"):
            raise RuntimeError("gh exploded")

    stats = build_completion_stats(
        context=_make_context(), github_client=ExplodingClient()
    )
    assert stats.error is not None
    assert stats.total_tracked == 0
