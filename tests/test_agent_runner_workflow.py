"""Tests for agent runner workflow helpers."""

from __future__ import annotations

from backend.core.shared.models.agent_runner import AppConfig
from backend.core.use_cases.agent_runner_events import format_event_marker
from backend.core.use_cases.agent_runner_workflow import (
    build_transition_labels,
    claim_blocked_issue,
    find_latest_unconsumed_marker,
    workflow_state_labels,
)
from tests.conftest import FakeGitHubClient


def test_workflow_state_labels_includes_all_durable_states() -> None:
    config = AppConfig()
    labels = workflow_state_labels(config)
    assert config.labels.ready in labels
    assert config.labels.running in labels
    assert config.labels.supervising in labels
    assert config.labels.review in labels
    assert config.labels.failed in labels
    assert config.labels.blocked in labels


def test_build_transition_labels_removes_other_workflow_labels() -> None:
    config = AppConfig()
    current = (
        "agent/running",
        "agent/review",
        "agent/codex",
        "task-group/foo",
    )
    result = build_transition_labels(current, config, config.labels.supervising)
    assert config.labels.supervising in result
    assert config.labels.running not in result
    assert config.labels.review not in result
    assert "agent/codex" in result
    assert "task-group/foo" in result


def test_find_latest_unconsumed_marker_finds_pending() -> None:
    comments = [
        format_event_marker(phase="post_pr_rework_requested", cycle=1),
        "some normal comment",
    ]
    result = find_latest_unconsumed_marker(
        comments,
        phase="post_pr_rework_requested",
        completion_phases={"implementation_complete"},
    )
    assert result is not None
    assert result.phase == "post_pr_rework_requested"


def test_find_latest_unconsumed_marker_respects_completion() -> None:
    comments = [
        format_event_marker(phase="post_pr_rework_requested", cycle=1),
        format_event_marker(phase="implementation_complete", cycle=1),
    ]
    result = find_latest_unconsumed_marker(
        comments,
        phase="post_pr_rework_requested",
        completion_phases={"implementation_complete"},
    )
    assert result is None


def test_find_latest_unconsumed_marker_blocked_resolution() -> None:
    comments = [
        format_event_marker(
            phase="blocked_resolution_requested",
            cycle=1,
            blocked_paths=(".env",),
        ),
    ]
    result = find_latest_unconsumed_marker(
        comments,
        phase="blocked_resolution_requested",
        completion_phases={"blocked_resolution_complete"},
    )
    assert result is not None
    assert result.blocked_paths == (".env",)


def test_claim_blocked_issue_success() -> None:
    fake_client = FakeGitHubClient()
    call_count = 0

    def _get_issue_with_blocked(number: int):
        nonlocal call_count
        call_count += 1
        from backend.core.shared.models.agent_runner import IssueSummary

        if call_count <= 2:
            return IssueSummary(
                number=number,
                title=f"Issue #{number}",
                url=f"https://github.com/example/repo/issues/{number}",
                body="",
                labels=("agent/blocked", "agent/codex"),
            )
        return IssueSummary(
            number=number,
            title=f"Issue #{number}",
            url=f"https://github.com/example/repo/issues/{number}",
            body="",
            labels=("agent/running", "agent/codex"),
        )

    fake_client.get_issue = _get_issue_with_blocked
    config = AppConfig()

    claimed = claim_blocked_issue(fake_client, 1, config)
    assert claimed is True

    # Verify label transition happened
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) >= 1
    last_call = label_calls[-1]
    assert config.labels.running in last_call["add"]
    assert config.labels.blocked in last_call["remove"]


def test_claim_blocked_issue_fails_when_not_blocked() -> None:
    fake_client = FakeGitHubClient()

    def _get_issue_running(number: int):
        from backend.core.shared.models.agent_runner import IssueSummary

        return IssueSummary(
            number=number,
            title=f"Issue #{number}",
            url=f"https://github.com/example/repo/issues/{number}",
            body="",
            labels=("agent/running",),
        )

    fake_client.get_issue = _get_issue_running
    config = AppConfig()

    claimed = claim_blocked_issue(fake_client, 1, config)
    assert claimed is False
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 0
