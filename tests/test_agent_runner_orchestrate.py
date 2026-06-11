"""Tests for agent runner orchestration, focusing on dependency gate filtering."""

from __future__ import annotations

from pathlib import Path

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
