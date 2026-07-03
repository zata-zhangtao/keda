"""Tests for stale ``agent/running`` reclaim (L1 hard-kill recovery)."""

from __future__ import annotations

import dataclasses
import os

from backend.core.shared.models.agent_runner import AppConfig
from backend.core.use_cases.agent_runner_reclaim import (
    format_claim_marker,
    is_pid_alive,
    parse_claim_marker,
    reclaim_stale_running_issues,
)
from tests.conftest import FakeGitHubClient


def _seed_running_issue(
    github_client: FakeGitHubClient,
    config: AppConfig,
    *,
    issue_number: int,
    marker: str | None,
) -> None:
    """Seed an ``agent/running`` Issue, optionally with a claim marker comment."""
    github_client.edit_issue_labels(issue_number, add=[config.labels.running])
    if marker is not None:
        github_client.comment_issue(issue_number, f"## Agent Runner Claimed\n\n{marker}")
    github_client.set_list_issues_by_label_result([github_client.get_issue(issue_number)])


def test_claim_marker_round_trip() -> None:
    """A formatted claim marker parses back to the same host and pid."""
    marker = format_claim_marker("host-a", 4242)
    assert parse_claim_marker(f"## Claimed\n{marker}") == ("host-a", 4242)


def test_parse_claim_marker_picks_last() -> None:
    """When a comment carries several markers, the last one wins."""
    body = format_claim_marker("host-a", 1) + "\n" + format_claim_marker("host-b", 2)
    assert parse_claim_marker(body) == ("host-b", 2)


def test_parse_claim_marker_absent_returns_none() -> None:
    assert parse_claim_marker("no marker here") is None


def test_is_pid_alive_distinguishes_live_and_dead() -> None:
    """The current process is alive; an unused high PID is not."""
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(2**31 - 1) is False
    assert is_pid_alive(0) is False


def test_reclaim_flips_dead_local_run_to_ready() -> None:
    """A same-host, dead-PID run is returned to ready with an explanatory note."""
    config = AppConfig()
    github = FakeGitHubClient()
    _seed_running_issue(github, config, issue_number=5, marker=format_claim_marker("host-a", 4242))

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: False,
    )

    assert reclaimed == [5]
    labels = github.get_issue(5).labels
    assert config.labels.ready in labels
    assert config.labels.running not in labels
    assert any("Stale Run Reclaimed" in body for body in github.list_issue_comments(5))


def test_reclaim_skips_live_pid() -> None:
    """A run whose PID is still alive must never be reclaimed."""
    config = AppConfig()
    github = FakeGitHubClient()
    _seed_running_issue(github, config, issue_number=5, marker=format_claim_marker("host-a", 4242))

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: True,
    )

    assert reclaimed == []
    assert config.labels.running in github.get_issue(5).labels


def test_reclaim_skips_other_host() -> None:
    """A run claimed by a different machine is left untouched."""
    config = AppConfig()
    github = FakeGitHubClient()
    _seed_running_issue(github, config, issue_number=5, marker=format_claim_marker("host-b", 4242))

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: False,
    )

    assert reclaimed == []
    assert config.labels.running in github.get_issue(5).labels


def test_reclaim_skips_issue_without_claim_marker() -> None:
    """Without a claim marker we cannot prove death, so we skip conservatively."""
    config = AppConfig()
    github = FakeGitHubClient()
    _seed_running_issue(github, config, issue_number=5, marker=None)

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: False,
    )

    assert reclaimed == []
    assert config.labels.running in github.get_issue(5).labels


def test_reclaim_skips_closed_issue() -> None:
    """A closed Issue is never resurrected, even with a dead local claim."""
    config = AppConfig()
    github = FakeGitHubClient()
    github.edit_issue_labels(5, add=[config.labels.running])
    github.comment_issue(5, format_claim_marker("host-a", 4242))
    closed = dataclasses.replace(github.get_issue(5), state="CLOSED")
    github.set_list_issues_by_label_result([closed])

    reclaimed = reclaim_stale_running_issues(
        config=config,
        github_client=github,
        host="host-a",
        pid_alive=lambda _pid: False,
    )

    assert reclaimed == []
