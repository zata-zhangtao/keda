"""Tests for roadmap dependency evaluation."""

from __future__ import annotations

from backend.core.shared.models.agent_runner import LabelConfig
from backend.core.shared.models.roadmap import (
    RoadmapDependency,
    RoadmapDependencyKind,
    RoadmapPrd,
    RoadmapPrdState,
)
from backend.core.use_cases.roadmap_dependencies import (
    _detect_cycles,
    evaluate_roadmap_dependencies,
)
from tests.conftest import FakeGitHubClient


def _make_prd(
    prd_path: str,
    state: RoadmapPrdState = RoadmapPrdState.NOT_STARTED,
    dependencies: tuple[RoadmapDependency, ...] = (),
) -> RoadmapPrd:
    return RoadmapPrd(
        prd_path=prd_path,
        title="Test",
        status="pending",
        priority="P1",
        issue_url=None,
        issue_number=None,
        state=state,
        acceptance_total=0,
        acceptance_checked=0,
        delivery_dependencies=dependencies,
        updated_at="2026-01-01T00:00:00+00:00",
        block_reason=None,
        next_action=None,
    )


def test_unblocked_prd_has_no_block_reason() -> None:
    """A PRD with no dependencies should be unblocked."""
    prd = _make_prd("tasks/pending/P1-FEAT-20260101-a.md")
    client = FakeGitHubClient()
    blockers = evaluate_roadmap_dependencies([prd], client, LabelConfig())
    assert blockers[prd.prd_path] is None


def test_issue_dependency_blocks_until_closed() -> None:
    """An open upstream Issue should block the downstream PRD."""
    prd = _make_prd(
        "tasks/pending/P1-FEAT-20260101-a.md",
        dependencies=(
            RoadmapDependency(
                from_path="tasks/pending/P1-FEAT-20260101-a.md",
                to_path="#7",
                kind=RoadmapDependencyKind.ISSUE,
            ),
        ),
    )
    client = FakeGitHubClient()
    client._issue_states[7] = "OPEN"
    blockers = evaluate_roadmap_dependencies([prd], client, LabelConfig())
    assert "未关闭" in (blockers[prd.prd_path] or "")

    client._issue_states[7] = "CLOSED"
    blockers = evaluate_roadmap_dependencies([prd], client, LabelConfig())
    assert blockers[prd.prd_path] is None


def test_prd_dependency_blocks_until_upstream_merged() -> None:
    """A downstream PRD should wait until upstream is merged/archived."""
    upstream = _make_prd(
        "tasks/pending/P1-FEAT-20260101-upstream.md",
        state=RoadmapPrdState.NOT_STARTED,
    )
    downstream = _make_prd(
        "tasks/pending/P1-FEAT-20260101-downstream.md",
        dependencies=(
            RoadmapDependency(
                from_path="tasks/pending/P1-FEAT-20260101-downstream.md",
                to_path="tasks/pending/P1-FEAT-20260101-upstream.md",
                kind=RoadmapDependencyKind.PRD,
            ),
        ),
    )
    client = FakeGitHubClient()
    blockers = evaluate_roadmap_dependencies(
        [upstream, downstream], client, LabelConfig()
    )
    assert "等待上游" in (blockers[downstream.prd_path] or "")

    upstream_merged = _make_prd(
        upstream.prd_path,
        state=RoadmapPrdState.MERGED,
    )
    blockers = evaluate_roadmap_dependencies(
        [upstream_merged, downstream], client, LabelConfig()
    )
    assert blockers[downstream.prd_path] is None


def test_cycle_is_detected() -> None:
    """A dependency cycle should be reported as a block reason."""
    a = _make_prd(
        "tasks/pending/P1-FEAT-20260101-a.md",
        dependencies=(
            RoadmapDependency(
                from_path="tasks/pending/P1-FEAT-20260101-a.md",
                to_path="tasks/pending/P1-FEAT-20260101-b.md",
                kind=RoadmapDependencyKind.PRD,
            ),
        ),
    )
    b = _make_prd(
        "tasks/pending/P1-FEAT-20260101-b.md",
        dependencies=(
            RoadmapDependency(
                from_path="tasks/pending/P1-FEAT-20260101-b.md",
                to_path="tasks/pending/P1-FEAT-20260101-a.md",
                kind=RoadmapDependencyKind.PRD,
            ),
        ),
    )
    assert _detect_cycles([a, b]) == {a.prd_path, b.prd_path}


def test_unresolved_dependency_blocks() -> None:
    """Unresolved PRD refs should produce a block reason."""
    prd = _make_prd(
        "tasks/pending/P1-FEAT-20260101-a.md",
        dependencies=(
            RoadmapDependency(
                from_path="tasks/pending/P1-FEAT-20260101-a.md",
                to_path="missing.md",
                kind=RoadmapDependencyKind.UNRESOLVED,
                detail="无法解析",
            ),
        ),
    )
    client = FakeGitHubClient()
    blockers = evaluate_roadmap_dependencies([prd], client, LabelConfig())
    assert "无法解析" in (blockers[prd.prd_path] or "")
