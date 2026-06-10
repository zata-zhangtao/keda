"""Tests for agent runner orchestration, focusing on dependency gate filtering."""

from __future__ import annotations

from pathlib import Path


from backend.core.shared.models.agent_runner import AppConfig, IssueSummary
from backend.core.use_cases.agent_runner_orchestrate import run_once
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def _make_ready_issue(number: int, body: str, labels: tuple[str, ...]) -> IssueSummary:
    return IssueSummary(
        number=number,
        title=f"Issue #{number}",
        url=f"https://github.com/example/repo/issues/{number}",
        body=body,
        labels=labels,
    )


def test_run_once_dry_run_skips_blocked_ready_issue() -> None:
    """Blocked ready Issues should be skipped and reported in dry-run mode."""
    fake_client = FakeGitHubClient()
    fake_client.list_ready_issues = lambda ready_label, limit: [
        _make_ready_issue(
            2,
            "<!-- iar:depends-on #1 -->",
            ("agent/ready",),
        )
    ]
    fake_client._issue_states[1] = "OPEN"
    fake_runner = FakeProcessRunner()

    exit_code = run_once(
        repo_path=Path("."),
        config=AppConfig(),
        dry_run=True,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    process_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(process_calls) == 0


def test_run_once_dry_run_processes_unblocked_ready_issue() -> None:
    """Ready Issues with satisfied dependencies should enter the process list."""
    fake_client = FakeGitHubClient()
    fake_client.list_ready_issues = lambda ready_label, limit: [
        _make_ready_issue(
            2,
            "<!-- iar:depends-on #1 -->",
            ("agent/ready", "agent/waiting"),
        )
    ]
    fake_client._issue_states[1] = "CLOSED"
    fake_runner = FakeProcessRunner()

    exit_code = run_once(
        repo_path=Path("."),
        config=AppConfig(),
        dry_run=True,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    # The only label mutation in dry-run should be the would-remove waiting log,
    # not an actual edit_issue_labels call.
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 0


def test_run_once_no_marker_issue_unchanged() -> None:
    """Ready Issues without dependency markers should proceed as before."""
    fake_client = FakeGitHubClient()
    fake_client.list_ready_issues = lambda ready_label, limit: [
        _make_ready_issue(
            3,
            "PRD path: `tasks/example.md`",
            ("agent/ready",),
        )
    ]
    fake_runner = FakeProcessRunner()

    exit_code = run_once(
        repo_path=Path("."),
        config=AppConfig(),
        dry_run=True,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 0
