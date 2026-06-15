"""Tests for agent runner orchestration, focusing on dependency gate filtering."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    AttemptResult,
    FailureType,
    IssueSummary,
)
from backend.core.use_cases.agent_runner_events import format_event_marker
from backend.core.use_cases.agent_runner_failure import (
    ForbiddenBlockedError,
)
from backend.core.use_cases.agent_runner_orchestrate import (
    _READY_DISCOVERY_LIMIT,
    _guard_blocked_issue_has_resolution,
    _mark_issue_blocked,
    _mark_issue_failed,
    run_once,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def _make_ready_issue(number: int, body: str, labels: tuple[str, ...]) -> IssueSummary:
    return IssueSummary(
        number=number,
        title=f"Issue #{number}",
        url=f"https://github.com/example/repo/issues/{number}",
        body=body,
        labels=labels,
    )


def _make_blocked_issue(number: int) -> IssueSummary:
    return _make_ready_issue(number, "", ("agent/blocked",))


def test_mark_issue_failed_comment_includes_recovery_guidance() -> None:
    """The failure comment posted to GitHub must tell the operator how to recover."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue(53, "PRD path: `tasks/example.md`", ("agent/running",))

    _mark_issue_failed(
        issue=issue,
        config=AppConfig(),
        github_client=fake_client,
        exc=RuntimeError("Pre-push review did not approve after 2 attempt(s)"),
    )

    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 1
    assert "agent/failed" in label_calls[0]["add"]
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    body = comment_calls[0]["body"]
    assert "## Agent Runner Failed" in body
    assert "### How To Recover" in body
    assert (
        "gh issue edit 53 --add-label agent/ready --remove-label agent/failed" in body
    )


def test_mark_issue_failed_falls_back_to_minimal_comment_on_rejection() -> None:
    """When GitHub rejects the full report, a minimal comment must still post.

    Regression for Issue #84: the full failure comment was rejected with a
    400, the best-effort handler swallowed it, and the failure reason never
    reached the Issue.
    """

    class _FirstCommentFailsClient(FakeGitHubClient):
        def __init__(self) -> None:
            super().__init__()
            self.comment_attempts = 0

        def comment_issue(self, issue_number: int, body: str) -> None:
            self.comment_attempts += 1
            if self.comment_attempts == 1:
                raise RuntimeError("non-200 OK status code: 400 Bad Request")
            super().comment_issue(issue_number, body)

    fake_client = _FirstCommentFailsClient()
    issue = _make_ready_issue(84, "PRD path: `tasks/example.md`", ("agent/running",))

    _mark_issue_failed(
        issue=issue,
        config=AppConfig(),
        github_client=fake_client,
        exc=RuntimeError("Failed after 6 attempts."),
    )

    # Two attempts: the rejected full report, then the minimal fallback.
    assert fake_client.comment_attempts == 2
    posted = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(posted) == 1
    body = posted[0]["body"]
    assert "## Agent Runner Failed" in body
    assert "Failed after 6 attempts." in body
    assert (
        "gh issue edit 84 --add-label agent/ready --remove-label agent/failed" in body
    )
    # The Issue is still transitioned to the failed state.
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert any("agent/failed" in c["add"] for c in label_calls)


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


def test_run_once_dry_run_continues_after_dependency_blocked_ready_issue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Blocked ready Issues must not starve later actionable ready Issues."""
    fake_client = FakeGitHubClient()
    ready_query_limits: list[int] = []

    def list_ready_issues(ready_label: str, limit: int) -> list[IssueSummary]:
        ready_query_limits.append(limit)
        return [
            _make_ready_issue(
                5,
                "<!-- iar:depends-on #3 -->",
                ("agent/ready",),
            ),
            _make_ready_issue(
                4,
                "PRD path: `tasks/example.md`",
                ("agent/ready",),
            ),
        ]

    fake_client.list_ready_issues = list_ready_issues
    fake_client._issue_states[3] = "OPEN"
    fake_runner = FakeProcessRunner()
    caplog.set_level(
        logging.INFO,
        logger="backend.core.use_cases.agent_runner_orchestrate",
    )

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
    assert ready_query_limits == [_READY_DISCOVERY_LIMIT]
    assert "Issue #5 blocked by dependencies" in caplog.text
    assert "would process Issue #4 (ready)" in caplog.text


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


def test_mark_issue_blocked_sets_blocked_label() -> None:
    """Forbidden path failures must mark the Issue as agent/blocked."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue(42, "", ("agent/running",))
    exc = ForbiddenBlockedError(
        "Refusing to publish forbidden paths: .env.example",
        [
            AttemptResult(
                attempt_number=1,
                failure_type=FailureType.FORBIDDEN_BLOCKED,
                recovered=False,
                detail="forbidden",
            )
        ],
    )

    _mark_issue_blocked(
        issue=issue,
        config=AppConfig(),
        github_client=fake_client,
        exc=exc,
    )

    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 1
    assert "agent/blocked" in label_calls[0]["add"]
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    body = comment_calls[0]["body"]
    assert "## Agent Runner Blocked" in body
    assert "blocked_forbidden" in body
    assert ".env.example" in body
    assert "blocked-continue" in body


def test_guard_blocked_issue_has_resolution_finds_marker() -> None:
    """A blocked issue with a blocked_resolution_requested marker is detected."""
    fake_client = FakeGitHubClient()
    marker = format_event_marker(
        phase="blocked_resolution_requested", cycle=1, blocked_paths=(".env.example",)
    )
    fake_client.comment_issue(7, marker)
    issue = _make_blocked_issue(7)

    result = _guard_blocked_issue_has_resolution(issue, fake_client)
    assert result is not None
    assert result.phase == "blocked_resolution_requested"
    assert result.blocked_paths == (".env.example",)


def test_guard_blocked_issue_has_resolution_skips_without_marker() -> None:
    """A blocked issue without the marker returns None."""
    fake_client = FakeGitHubClient()
    issue = _make_blocked_issue(8)

    result = _guard_blocked_issue_has_resolution(issue, fake_client)
    assert result is None


def test_run_once_dry_run_lists_blocked_resolution() -> None:
    """Dry run should list a blocked issue with a resolution marker."""
    fake_client = FakeGitHubClient()
    fake_client.list_review_candidate_issues = lambda labels, limit: [
        _make_blocked_issue(5)
    ]
    marker = format_event_marker(
        phase="blocked_resolution_requested", cycle=1, blocked_paths=(".env.example",)
    )
    fake_client.comment_issue(5, marker)
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
    # No actual label edits in dry run
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 0


def test_acquire_blocked_claim_lock_atomic() -> None:
    """Only one process can acquire the blocked claim lock."""
    import tempfile

    from backend.core.use_cases.agent_runner_orchestrate import (
        _acquire_blocked_claim_lock,
        _release_blocked_claim_lock,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        lock_path = Path(temp_dir) / "blocked-claim.lock"

        # First acquire should succeed
        _acquire_blocked_claim_lock(lock_path, 99)
        assert lock_path.exists()

        # Second acquire from same process should succeed (re-entrant not needed but harmless)
        _release_blocked_claim_lock(lock_path)
        assert not lock_path.exists()

        # Acquire then simulate another process by writing a different PID
        _acquire_blocked_claim_lock(lock_path, 99)
        lock_path.write_text("999999\n", encoding="utf-8")

        # 999999 is almost certainly not running — lock should be stolen
        _acquire_blocked_claim_lock(lock_path, 99)
        assert "999999" not in lock_path.read_text(encoding="utf-8")

        _release_blocked_claim_lock(lock_path)


def test_worktree_needs_rebase_recovery_detects_rebase_and_detached(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Mid-rebase or detached-HEAD running worktrees must be flagged recoverable."""
    import backend.core.use_cases.agent_runner_orchestrate as orchestrate

    issue = _make_ready_issue(85, "", ("agent/running",))
    monkeypatch.setattr(
        orchestrate, "_find_worktree_path_for_issue", lambda *a, **k: tmp_path
    )

    def _probe() -> bool:
        return orchestrate._worktree_needs_rebase_recovery(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            process_runner=FakeProcessRunner(),
        )

    # Active rebase metadata present → recoverable.
    monkeypatch.setattr(orchestrate, "has_rebase_metadata", lambda *a, **k: True)
    monkeypatch.setattr(orchestrate, "is_detached_head", lambda *a, **k: False)
    assert _probe() is True

    # Detached HEAD without rebase metadata → still recoverable.
    monkeypatch.setattr(orchestrate, "has_rebase_metadata", lambda *a, **k: False)
    monkeypatch.setattr(orchestrate, "is_detached_head", lambda *a, **k: True)
    assert _probe() is True

    # Healthy worktree on its branch → not a recovery candidate.
    monkeypatch.setattr(orchestrate, "has_rebase_metadata", lambda *a, **k: False)
    monkeypatch.setattr(orchestrate, "is_detached_head", lambda *a, **k: False)
    assert _probe() is False


def test_worktree_needs_rebase_recovery_missing_worktree_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing worktree must not be treated as a rebase-recovery candidate."""
    import backend.core.use_cases.agent_runner_orchestrate as orchestrate

    issue = _make_ready_issue(85, "", ("agent/running",))

    def _raise(*_args: object, **_kwargs: object) -> Path:
        raise FileNotFoundError("worktree path does not exist")

    monkeypatch.setattr(orchestrate, "_find_worktree_path_for_issue", _raise)

    assert (
        orchestrate._worktree_needs_rebase_recovery(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            process_runner=FakeProcessRunner(),
        )
        is False
    )


def test_run_once_routes_mid_rebase_running_issue_to_recovery(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A running Issue stuck mid-rebase must enter publish-recovery, not be skipped.

    Reproduces the daemon-died-mid-rebase bug: the clean-local-commit probe
    fails for a half-finished rebase (HEAD detached on base, dirty worktree),
    so without explicit rebase/detached detection the Issue would be skipped on
    every poll and never re-claimed.
    """
    import backend.core.use_cases.agent_runner_orchestrate as orchestrate

    running_label = AppConfig().labels.running
    running_issue = _make_ready_issue(85, "PRD path: `tasks/x.md`", (running_label,))
    fake_client = FakeGitHubClient()
    fake_client.list_ready_issues = lambda ready_label, limit: []
    fake_client.list_review_candidate_issues = (
        lambda labels, limit: [running_issue] if running_label in labels else []
    )

    monkeypatch.setattr(
        orchestrate,
        "_has_existing_local_commit_ready_for_publish",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        orchestrate, "_worktree_needs_rebase_recovery", lambda **_kwargs: True
    )
    caplog.set_level(
        logging.INFO, logger="backend.core.use_cases.agent_runner_orchestrate"
    )

    exit_code = run_once(
        repo_path=Path("."),
        config=AppConfig(),
        dry_run=True,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=FakeProcessRunner(),
    )

    assert exit_code == 0
    assert "would process Issue #85 (running_publish_recovery)" in caplog.text
    assert "Skipping Issue #85" not in caplog.text


def test_run_once_skips_running_issue_without_recoverable_state(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A running Issue with no commit and no rebase/detached state stays skipped."""
    import backend.core.use_cases.agent_runner_orchestrate as orchestrate

    running_label = AppConfig().labels.running
    running_issue = _make_ready_issue(85, "PRD path: `tasks/x.md`", (running_label,))
    fake_client = FakeGitHubClient()
    fake_client.list_ready_issues = lambda ready_label, limit: []
    fake_client.list_review_candidate_issues = (
        lambda labels, limit: [running_issue] if running_label in labels else []
    )

    monkeypatch.setattr(
        orchestrate,
        "_has_existing_local_commit_ready_for_publish",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        orchestrate, "_worktree_needs_rebase_recovery", lambda **_kwargs: False
    )
    caplog.set_level(
        logging.INFO, logger="backend.core.use_cases.agent_runner_orchestrate"
    )

    exit_code = run_once(
        repo_path=Path("."),
        config=AppConfig(),
        dry_run=True,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=FakeProcessRunner(),
    )

    assert exit_code == 0
    assert "Skipping Issue #85" in caplog.text
    assert "would process Issue #85" not in caplog.text


def test_running_publish_recovery_holds_worktree_lock_around_heal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recovery must acquire the worktree lock before healing and release it after.

    Guards against two runners concurrently rebasing/publishing the same
    worktree once mid-rebase Issues become reachable by the recovery path.
    """
    import backend.core.use_cases.agent_runner_orchestrate as orchestrate

    events: list[str] = []
    issue = _make_ready_issue(85, "", (AppConfig().labels.running,))

    monkeypatch.setattr(orchestrate, "choose_agent", lambda *a, **k: "claude")
    monkeypatch.setattr(
        orchestrate, "_find_worktree_path_for_issue", lambda *a, **k: tmp_path
    )
    monkeypatch.setattr(
        orchestrate,
        "_acquire_blocked_claim_lock",
        lambda lock_path, number: events.append("acquire"),
    )
    monkeypatch.setattr(
        orchestrate,
        "_release_blocked_claim_lock",
        lambda lock_path: events.append("release"),
    )
    monkeypatch.setattr(
        orchestrate,
        "_ensure_worktree_branch",
        lambda *a, **k: events.append("heal"),
    )
    monkeypatch.setattr(
        orchestrate, "_reuse_existing_local_commit", lambda *a, **k: object()
    )
    monkeypatch.setattr(
        orchestrate,
        "_finish_existing_commit_publication",
        lambda **k: events.append("publish"),
    )

    orchestrate._process_running_publish_recovery(
        issue=issue,
        repo_path=Path("."),
        config=AppConfig(),
        agent="auto",
        github_client=FakeGitHubClient(),
        process_runner=FakeProcessRunner(),
    )

    # Heal and publish must happen strictly between acquire and release.
    assert events == ["acquire", "heal", "publish", "release"]
