"""Tests for roadmap state resolver."""

from __future__ import annotations

from backend.core.shared.models.agent_runner import AppConfig
from backend.core.shared.models.roadmap import (
    RoadmapPrd,
    RoadmapPrdState,
)
from backend.core.use_cases.roadmap_state_resolver import resolve_roadmap_states
from tests.conftest import FakeGitHubClient


def _make_prd(
    prd_path: str,
    issue_number: int | None = None,
    status: str = "pending",
) -> RoadmapPrd:
    return RoadmapPrd(
        prd_path=prd_path,
        title="Test",
        status=status,
        priority="P1",
        issue_url=None,
        issue_number=issue_number,
        state=RoadmapPrdState.NOT_STARTED,
        acceptance_total=0,
        acceptance_checked=0,
        delivery_dependencies=(),
        updated_at="2026-01-01T00:00:00+00:00",
        block_reason=None,
        next_action=None,
    )


def test_no_issue_stays_not_started() -> None:
    """PRDs without issues should remain not_started."""
    prd = _make_prd("tasks/pending/P1-FEAT-20260101-a.md")
    client = FakeGitHubClient()
    resolved = resolve_roadmap_states([prd], client, AppConfig(), {})
    assert len(resolved) == 1
    assert resolved[0].state == RoadmapPrdState.NOT_STARTED
    assert resolved[0].next_action is not None
    assert resolved[0].next_action["label"] == "开始"


def test_ready_label_maps_to_ready_state() -> None:
    """Issue with agent/ready label should map to ready state."""
    prd = _make_prd("tasks/pending/P1-FEAT-20260101-a.md", issue_number=1)
    client = FakeGitHubClient()
    client._issue_labels[1] = ("agent/ready",)
    resolved = resolve_roadmap_states([prd], client, AppConfig(), {})
    assert resolved[0].state == RoadmapPrdState.READY


def test_review_label_maps_to_review_state() -> None:
    """Issue with agent/review label should map to review state."""
    prd = _make_prd("tasks/pending/P1-FEAT-20260101-a.md", issue_number=2)
    client = FakeGitHubClient()
    client._issue_labels[2] = ("agent/review",)
    client._issue_comments[2] = ["PR Branch: `issue-2`"]
    client._pr_contexts["issue-2"] = type(
        "FakePrContext",
        (),
        {"pr_url": "https://github.com/org/repo/pull/5", "branch": "issue-2"},
    )()
    resolved = resolve_roadmap_states([prd], client, AppConfig(), {})
    assert resolved[0].state == RoadmapPrdState.REVIEW
    assert resolved[0].next_action is not None


def test_closed_issue_with_merged_pr_maps_to_merged() -> None:
    """Closed issue with merged PR should map to merged state."""
    prd = _make_prd("tasks/pending/P1-FEAT-20260101-a.md", issue_number=3)
    client = FakeGitHubClient()
    client._issue_states[3] = "CLOSED"
    client._issue_comments[3] = [
        "<!-- iar:event version=1 phase=draft_pr_created cycle=1 pr_branch=issue-3 -->"
    ]
    client._merged_prs["issue-3"] = "https://github.com/org/repo/pull/9"
    resolved = resolve_roadmap_states([prd], client, AppConfig(), {})
    assert resolved[0].state == RoadmapPrdState.MERGED
    assert resolved[0].next_action is not None
    assert resolved[0].next_action["label"] == "开始下一个"


def test_archived_prd_is_archived() -> None:
    """PRDs in archive directory should be archived."""
    prd = _make_prd(
        "tasks/archive/P1-FEAT-20260101-a.md",
        issue_number=4,
        status="archived",
    )
    client = FakeGitHubClient()
    resolved = resolve_roadmap_states([prd], client, AppConfig(), {})
    assert resolved[0].state == RoadmapPrdState.ARCHIVED


def test_block_reason_overrides_state_to_waiting() -> None:
    """Dependency blocker should set state to waiting."""
    prd = _make_prd("tasks/pending/P1-FEAT-20260101-a.md", issue_number=5)
    client = FakeGitHubClient()
    client._issue_labels[5] = ("agent/ready",)
    block_reasons = {prd.prd_path: "等待上游 PRD"}
    resolved = resolve_roadmap_states([prd], client, AppConfig(), block_reasons)
    assert resolved[0].state == RoadmapPrdState.WAITING
    assert resolved[0].block_reason == "等待上游 PRD"
