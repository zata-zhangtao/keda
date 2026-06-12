"""Tests for the Agent Runner monitoring use case and API endpoints.

Covers PRD Issue #30 scenarios 1-8 (backend behaviour). Frontend Playwright
smoke is exercised separately.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.routes.agent_runner import (
    _serialize_monitoring,
)
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PullRequestContext,
)
from backend.core.use_cases.agent_runner_monitor import (
    Anomaly,
    AnomalyDetectionContext,
    EventTimelineEntry,
    build_issue_snapshot,
    build_overview,
    build_repository_overview,
    detect_anomalies,
    parse_event_timeline,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _marker(
    *,
    phase: str,
    cycle: int = 1,
    head: str = "abc123",
    pr_branch: str = "issue-10",
    action: str | None = None,
    checks_state: str | None = None,
    mergeable: bool | None = None,
) -> str:
    """Format an iar:event comment body matching the official schema."""
    parts = [
        "version=1",
        f"phase={phase}",
        f"cycle={cycle}",
    ]
    if head:
        parts.append(f"head={head}")
    if pr_branch:
        parts.append(f"pr_branch={pr_branch}")
    if action:
        parts.append(f"action={action}")
    if checks_state:
        parts.append(f"checks_state={checks_state}")
    if mergeable is not None:
        parts.append(f"mergeable={'true' if mergeable else 'false'}")
    return f"<!-- iar:event {' '.join(parts)} -->"


def _make_issue(
    number: int,
    *,
    title: str = "Test Issue",
    labels: tuple[str, ...] = (),
    url: str | None = None,
) -> IssueSummary:
    return IssueSummary(
        number=number,
        title=title,
        url=url or f"https://github.com/example/repo/issues/{number}",
        body="",
        labels=labels,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: parse_event_timeline
# ─────────────────────────────────────────────────────────────────────────────


def test_parse_event_timeline_orders_markers_oldest_first() -> None:
    """Timeline parser must surface all markers in chronological order."""
    comments = [
        _marker(phase="claimed"),
        "non-marker comment",
        _marker(phase="implementation_complete", cycle=2),
        _marker(phase="pre_push_review", cycle=3, checks_state="SUCCESS"),
        _marker(phase="draft_pr_created", cycle=4),
        _marker(phase="post_pr_supervisor", cycle=5, action="approve"),
    ]
    timeline = parse_event_timeline(comments)

    assert [entry.phase for entry in timeline] == [
        "claimed",
        "implementation_complete",
        "pre_push_review",
        "draft_pr_created",
        "post_pr_supervisor",
    ]
    assert [entry.cycle for entry in timeline] == [1, 2, 3, 4, 5]
    assert timeline[2].checks_state == "SUCCESS"
    assert timeline[4].action == "approve"


def test_parse_event_timeline_handles_multiple_markers_per_comment() -> None:
    """A single comment with two markers must produce two entries."""
    body = (
        "Lead text\n"
        + _marker(phase="claimed")
        + "\nMiddle\n"
        + _marker(phase="implementation_complete", cycle=2)
    )
    timeline = parse_event_timeline([body])
    assert [entry.phase for entry in timeline] == [
        "claimed",
        "implementation_complete",
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: anomaly detection rules
# ─────────────────────────────────────────────────────────────────────────────


def _ctx(
    *,
    primary_label: str = "agent/supervising",
    labels: tuple[str, ...] = (),
    pr_context: PullRequestContext | None = None,
    worktree_dirty: bool = False,
    worktree_exists: bool = True,
    latest_event_phase: str | None = None,
    latest_event_mergeable: bool | None = None,
) -> AnomalyDetectionContext:
    from backend.core.use_cases.agent_runner_monitor import WorktreeStatus

    worktree = WorktreeStatus(
        exists=worktree_exists,
        is_clean=not worktree_dirty,
        branch="issue-1",
        head_sha="deadbeef",
    )
    latest_event = None
    if latest_event_phase is not None:
        from backend.core.shared.models.agent_runner import ReviewEventMarker

        latest_event = ReviewEventMarker(
            version=1,
            phase=latest_event_phase,
            cycle=1,
            head_sha="deadbeef",
            mergeable=latest_event_mergeable,
        )
    return AnomalyDetectionContext(
        issue=_make_issue(1, labels=labels or (primary_label,)),
        labels=labels or (primary_label,),
        primary_label=primary_label,
        pr_context=pr_context,
        worktree=worktree,
        latest_event=latest_event,
        config=AppConfig(),
    )


def test_detect_anomalies_label_pr_mismatch() -> None:
    """PR exists but label is not a post-PR label."""
    anomalies = detect_anomalies(
        _ctx(
            primary_label="agent/ready",
            pr_context=PullRequestContext(
                pr_url="https://github.com/example/repo/pull/22",
                branch="issue-1",
                head_sha="x",
                base_sha="y",
                mergeable=True,
            ),
        )
    )
    types = [a.type for a in anomalies]
    assert "label_pr_mismatch" in types
    warning = next(a for a in anomalies if a.type == "label_pr_mismatch")
    assert warning.severity == "warning"
    assert "iar labels sync" in warning.suggested_cli


def test_detect_anomalies_pr_dirty_in_review() -> None:
    """PR mergeable=false while label is review must produce an error anomaly."""
    anomalies = detect_anomalies(
        _ctx(
            primary_label="agent/review",
            pr_context=PullRequestContext(
                pr_url="https://github.com/example/repo/pull/26",
                branch="issue-1",
                head_sha="x",
                base_sha="y",
                mergeable=False,
            ),
        )
    )
    error = next(a for a in anomalies if a.type == "pr_dirty_in_review")
    assert error.severity == "error"
    assert "iar review" in error.suggested_cli


def test_detect_anomalies_dirty_worktree_mismatch() -> None:
    """Dirty worktree with non-running label must be flagged."""
    anomalies = detect_anomalies(
        _ctx(
            primary_label="agent/supervising",
            worktree_dirty=True,
        )
    )
    warning = next(a for a in anomalies if a.type == "dirty_worktree_mismatch")
    assert warning.severity == "warning"
    assert "uncommitted" in warning.message.lower()


def test_detect_anomalies_event_label_mismatch() -> None:
    """Latest event phase implies supervising but label is review."""
    anomalies = detect_anomalies(
        _ctx(
            primary_label="agent/review",
            latest_event_phase="draft_pr_created",
        )
    )
    assert any(a.type == "event_label_mismatch" for a in anomalies)


def test_detect_anomalies_clean_state() -> None:
    """No anomalies when label/PR/event/worktree are all consistent."""
    anomalies = detect_anomalies(
        _ctx(
            primary_label="agent/supervising",
            pr_context=PullRequestContext(
                pr_url="https://github.com/example/repo/pull/10",
                branch="issue-1",
                head_sha="x",
                base_sha="y",
                mergeable=True,
            ),
            latest_event_phase="post_pr_supervisor",
        )
    )
    assert anomalies == ()


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: build_issue_snapshot
# ─────────────────────────────────────────────────────────────────────────────


def test_build_issue_snapshot_collects_timeline_pr_and_worktree() -> None:
    """Snapshot must merge comments, PR context, and worktree status."""
    worktree_path = Path("/tmp/wt-issue-19")
    worktree_path.mkdir()
    try:
        from backend.core.shared.models.agent_runner import WorktreeConfig

        config = AppConfig(
            worktree=WorktreeConfig(
                path_command=f"echo {worktree_path}",
            )
        )
        client = FakeGitHubClient()
        client._issue_comments[19] = [
            _marker(phase="claimed", cycle=1, pr_branch="issue-19"),
            _marker(phase="implementation_complete", cycle=2, pr_branch="issue-19"),
            _marker(
                phase="pre_push_review",
                cycle=3,
                pr_branch="issue-19",
                checks_state="SUCCESS",
            ),
            _marker(phase="draft_pr_created", cycle=4, pr_branch="issue-19"),
            _marker(phase="post_pr_supervisor", cycle=5, pr_branch="issue-19"),
            _marker(
                phase="post_pr_rework_requested",
                cycle=6,
                pr_branch="issue-19",
                action="rebase_pr_branch",
            ),
        ]
        client._pr_contexts["issue-19"] = PullRequestContext(
            pr_url="https://github.com/example/repo/pull/20",
            branch="issue-19",
            head_sha="abc123",
            base_sha="def456",
            mergeable=False,
        )

        runner = FakeProcessRunner(
            responses={
                ("echo", str(worktree_path)): CommandResult(
                    command=("echo", str(worktree_path)),
                    return_code=0,
                    stdout=f"{worktree_path}\n",
                    stderr="",
                ),
                ("git", "branch", "--show-current"): CommandResult(
                    command=("git", "branch", "--show-current"),
                    return_code=0,
                    stdout="issue-19\n",
                    stderr="",
                ),
                ("git", "rev-parse", "HEAD"): CommandResult(
                    command=("git", "rev-parse", "HEAD"),
                    return_code=0,
                    stdout="abc123\n",
                    stderr="",
                ),
                ("git", "status", "--porcelain"): CommandResult(
                    command=("git", "status", "--porcelain"),
                    return_code=0,
                    stdout="",
                    stderr="",
                ),
            }
        )
        issue = _make_issue(19, labels=("agent/review", "source/prd"))
        snapshot = build_issue_snapshot(
            issue=issue,
            config=config,
            github_client=client,
            process_runner=runner,
            repo_path=Path("/tmp/does-not-matter"),
        )
    finally:
        worktree_path.rmdir()

    assert [entry.phase for entry in snapshot.timeline] == [
        "claimed",
        "implementation_complete",
        "pre_push_review",
        "draft_pr_created",
        "post_pr_supervisor",
        "post_pr_rework_requested",
    ]
    assert snapshot.pr is not None
    assert snapshot.pr["number"] == 20
    assert snapshot.pr["mergeable"] is False
    assert snapshot.worktree.exists is True
    assert snapshot.worktree.branch == "issue-19"
    assert snapshot.worktree.is_clean is True
    types = {a.type for a in snapshot.anomalies}
    assert "pr_dirty_in_review" in types
    assert "event_label_mismatch" in types
    assert "iar labels sync" in snapshot.suggested_cli_commands
    assert "iar review" in snapshot.suggested_cli_commands


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: build_repository_overview aggregates anomaly counts
# ─────────────────────────────────────────────────────────────────────────────


def test_build_repository_overview_aggregates_anomaly_counts(
    tmp_path: Path,
) -> None:
    """Anomaly counts must roll up by severity across Issues."""
    from backend.core.shared.models.agent_runner import WorktreeConfig

    dirty_worktree = tmp_path / "issue-32"
    dirty_worktree.mkdir()
    (dirty_worktree / "M").write_text("foo", encoding="utf-8")
    try:
        config = AppConfig(
            worktree=WorktreeConfig(
                path_command=f"echo {dirty_worktree}",
            )
        )
        client = FakeGitHubClient()
        test_issues = [
            _make_issue(30, labels=("agent/failed",)),
            _make_issue(31, labels=("agent/review",)),
            _make_issue(32, labels=("agent/supervising",)),
            _make_issue(33, labels=("agent/running",)),
        ]
        # Patch the FakeGitHubClient to surface our prebuilt issues for any label.
        client.list_ready_issues = lambda ready_label, limit: list(test_issues)  # type: ignore[method-assign]
        client._pr_contexts["issue-30"] = PullRequestContext(
            pr_url="https://example/30",
            branch="issue-30",
            head_sha="x",
            base_sha="y",
            mergeable=True,
        )
        client._pr_contexts["issue-31"] = PullRequestContext(
            pr_url="https://example/31",
            branch="issue-31",
            head_sha="x",
            base_sha="y",
            mergeable=False,
        )
        client._pr_contexts["issue-32"] = PullRequestContext(
            pr_url="https://example/32",
            branch="issue-32",
            head_sha="x",
            base_sha="y",
            mergeable=True,
        )
        # 30 has draft_pr_created marker implying supervising.
        client._issue_comments[30] = [
            _marker(phase="draft_pr_created", cycle=1, pr_branch="issue-30")
        ]
        # 31 / 32 carry a non-marker comment with the PR branch so the
        # snapshot resolution can locate the PR context without setting
        # a latest iar:event marker (which would skew anomaly totals).
        client._issue_comments[31] = ["PR Branch: `issue-31`"]
        client._issue_comments[32] = ["PR Branch: `issue-32`"]

        runner = FakeProcessRunner(
            responses={
                ("echo", str(dirty_worktree)): CommandResult(
                    command=("echo", str(dirty_worktree)),
                    return_code=0,
                    stdout=f"{dirty_worktree}\n",
                    stderr="",
                ),
                ("git", "branch", "--show-current"): CommandResult(
                    command=("git", "branch", "--show-current"),
                    return_code=0,
                    stdout="issue-32\n",
                    stderr="",
                ),
                ("git", "rev-parse", "HEAD"): CommandResult(
                    command=("git", "rev-parse", "HEAD"),
                    return_code=0,
                    stdout="abc\n",
                    stderr="",
                ),
                ("git", "status", "--porcelain"): CommandResult(
                    command=("git", "status", "--porcelain"),
                    return_code=0,
                    stdout=" M tasks/pending/foo.md\n",
                    stderr="",
                ),
            }
        )
        overview = build_repository_overview(
            repo_id="zata/keda-test",
            display_name="Keda Test",
            enabled=True,
            config=config,
            github_client=client,
            process_runner=runner,
            repo_path=tmp_path,
        )
    finally:
        (dirty_worktree / "M").unlink()
        dirty_worktree.rmdir()

    assert overview.anomaly_count == 3
    assert overview.anomaly_summary == {"warning": 2, "error": 1}
    flagged = {issue.number for issue in overview.issues if issue.has_anomaly}
    assert flagged == {30, 31, 32}


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: build_overview iterates over configured repositories
# ─────────────────────────────────────────────────────────────────────────────


def test_build_overview_returns_repositories_collection(
    tmp_path: Path,
) -> None:
    """build_overview must produce a MonitoringResult for each repository context."""
    config = AppConfig()
    client = FakeGitHubClient()
    client._ready_issues = [_make_issue(11, labels=("agent/supervising",))]

    class _Repo:
        def __init__(self) -> None:
            self.repo_id = "r1"
            self.display_name = "Repo One"
            self.repo_path = tmp_path
            self.config = config

    overview = build_overview(
        repositories=[_Repo()],
        github_client_factory=lambda _path: client,
        process_runner=FakeProcessRunner(),
    )
    assert len(overview.repositories) == 1
    assert overview.repositories[0].repo_id == "r1"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: _serialize_monitoring round-trips dataclasses
# ─────────────────────────────────────────────────────────────────────────────


def test_serialize_monitoring_converts_nested_dataclasses() -> None:
    """JSON serializer must descend into nested dataclasses and tuples."""
    snapshot = _make_issue(7, labels=("agent/supervising",))
    payload = {
        "issue": snapshot,
        "timeline": (
            EventTimelineEntry(
                phase="claimed",
                cycle=1,
                comment_index=0,
                raw_marker="<!-- iar:event ... -->",
            ),
        ),
        "anomaly": Anomaly(
            type="label_pr_mismatch",
            severity="warning",
            message="boom",
            suggested_cli=("iar labels sync",),
        ),
    }
    result = _serialize_monitoring(payload)
    assert isinstance(result, dict)
    assert result["issue"]["number"] == 7
    assert result["timeline"][0]["phase"] == "claimed"
    assert result["anomaly"]["suggested_cli"] == ["iar labels sync"]


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: API endpoints delegate to the use case
# ─────────────────────────────────────────────────────────────────────────────


def test_api_overview_returns_serialized_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /overview endpoint must surface a structured monitoring payload."""
    from backend.api.routes import agent_runner as agent_runner_routes

    # The module warms a background cache on import; clear it so this test
    # actually exercises the patched _build_overview_response.
    agent_runner_routes._OVERVIEW_CACHE.clear()

    captured: dict[str, Any] = {}

    def _fake_build_overview_response() -> dict:
        captured["called"] = True
        return {
            "repositories": [
                {
                    "repo_id": "zata/keda-test",
                    "display_name": "Keda Test",
                    "enabled": True,
                    "base_branch": "main",
                    "remote": "origin",
                    "health": {
                        "gh_available": True,
                        "repo_path_exists": True,
                        "publish_remote_exists": True,
                    },
                    "queue_counts": {
                        "ready": 1,
                        "running": 0,
                        "supervising": 0,
                        "review": 0,
                        "failed": 0,
                        "blocked": 0,
                    },
                    "labels": {
                        "ready": "agent/ready",
                        "running": "agent/running",
                        "supervising": "agent/supervising",
                        "review": "agent/review",
                        "failed": "agent/failed",
                        "blocked": "agent/blocked",
                    },
                    "issues": [],
                    "anomaly_count": 0,
                    "anomaly_summary": {"warning": 0, "error": 0},
                    "scanned_at": "2026-05-24T00:00:00+00:00",
                }
            ],
            "scanned_at": "2026-05-24T00:00:00+00:00",
        }

    monkeypatch.setattr(
        "backend.api.routes.agent_runner._build_overview_response",
        _fake_build_overview_response,
    )
    from backend.api.app import app

    client = TestClient(app)
    response = client.get("/api/v1/agent-runner/overview")
    assert response.status_code == 200
    body = response.json()
    assert captured.get("called") is True
    assert body["repositories"][0]["repo_id"] == "zata/keda-test"


def test_api_issue_detail_returns_404_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown Issue numbers must return 404, not crash."""

    def _fake_build_issue_detail_response(issue_number: int) -> dict:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=404,
            detail=f"Issue #{issue_number} not found in monitored repositories.",
        )

    monkeypatch.setattr(
        "backend.api.routes.agent_runner._build_issue_detail_response",
        _fake_build_issue_detail_response,
    )
    from backend.api.app import app

    client = TestClient(app)
    response = client.get("/api/v1/agent-runner/issues/999999")
    assert response.status_code == 404


def test_api_issue_detail_rejects_invalid_number() -> None:
    """Non-positive issue numbers must be rejected with 400."""
    from backend.api.app import app

    client = TestClient(app)
    response = client.get("/api/v1/agent-runner/issues/0")
    assert response.status_code == 400
