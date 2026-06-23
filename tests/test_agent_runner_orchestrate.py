"""Tests for agent runner orchestration, focusing on dependency gate filtering."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    AttemptResult,
    FailureType,
    GeneratedContentConfig,
    IssueSummary,
    LabelConfig,
    WorktreeConfig,
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
    process_prd_rework_issues,
    run_once,
)
from backend.infrastructure.process_runner import SubprocessRunner
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def _make_ready_issue(
    number: int, title: str, body: str, labels: tuple[str, ...]
) -> IssueSummary:
    return IssueSummary(
        number=number,
        title=title,
        url=f"https://github.com/example/repo/issues/{number}",
        body=body,
        labels=labels,
    )


def _make_blocked_issue(number: int) -> IssueSummary:
    return _make_ready_issue(number, f"Issue #{number}", "", ("agent/blocked",))


def test_mark_issue_failed_comment_includes_recovery_guidance() -> None:
    """The failure comment posted to GitHub must tell the operator how to recover."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue(
        53, "Issue #53", "PRD path: `tasks/example.md`", ("agent/running",)
    )

    _mark_issue_failed(
        issue=issue,
        config=AppConfig(),
        github_client=fake_client,
        exc=RuntimeError("Pre-PR review did not approve after 2 attempt(s)"),
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
    issue = _make_ready_issue(
        84, "Issue #84", "PRD path: `tasks/example.md`", ("agent/running",)
    )

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


def test_mark_issue_failed_transition_failure_recovery_guidance() -> None:
    """A failed workflow transition to supervising should retry that transition."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue(
        104, "Issue #104", "PRD path: `tasks/example.md`", ("agent/running",)
    )
    exc = subprocess.CalledProcessError(
        returncode=1,
        cmd=[
            "gh",
            "issue",
            "edit",
            "104",
            "--add-label",
            "agent/supervising",
            "--remove-label",
            "agent/running",
        ],
    )

    _mark_issue_failed(
        issue=issue,
        config=AppConfig(),
        github_client=fake_client,
        exc=exc,
    )

    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    body = comment_calls[0]["body"]
    assert "## Agent Runner Failed" in body
    assert "### How To Recover" in body
    assert (
        "gh issue edit 104 --add-label agent/supervising --remove-label agent/failed"
        in body
    )
    assert "finished its work" in body
    assert "agent/ready" not in body
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert any("agent/failed" in c["add"] for c in label_calls)


def test_run_once_dry_run_skips_blocked_ready_issue() -> None:
    """Blocked ready Issues should be skipped and reported in dry-run mode."""
    fake_client = FakeGitHubClient()
    fake_client.list_ready_issues = lambda ready_label, limit: [
        _make_ready_issue(
            2,
            "Issue #2",
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
                "Issue #5",
                "<!-- iar:depends-on #3 -->",
                ("agent/ready",),
            ),
            _make_ready_issue(
                4,
                "Issue #4",
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
            "Issue #2",
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
            "Issue #3",
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
    issue = _make_ready_issue(42, "Issue #42", "", ("agent/running",))
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
    from backend.core.use_cases import agent_runner_worktree_probe as probe

    issue = _make_ready_issue(85, "Issue #85", "", ("agent/running",))
    monkeypatch.setattr(
        probe, "_find_worktree_path_for_issue", lambda *a, **k: tmp_path
    )

    def _probe() -> bool:
        return orchestrate._worktree_needs_rebase_recovery(
            issue=issue,
            repo_path=Path("."),
            config=AppConfig(),
            process_runner=FakeProcessRunner(),
        )

    # Active rebase metadata present → recoverable.
    monkeypatch.setattr(probe, "has_rebase_metadata", lambda *a, **k: True)
    monkeypatch.setattr(probe, "is_detached_head", lambda *a, **k: False)
    assert _probe() is True

    # Detached HEAD without rebase metadata → still recoverable.
    monkeypatch.setattr(probe, "has_rebase_metadata", lambda *a, **k: False)
    monkeypatch.setattr(probe, "is_detached_head", lambda *a, **k: True)
    assert _probe() is True

    # Healthy worktree on its branch → not a recovery candidate.
    monkeypatch.setattr(probe, "has_rebase_metadata", lambda *a, **k: False)
    monkeypatch.setattr(probe, "is_detached_head", lambda *a, **k: False)
    assert _probe() is False


def test_worktree_needs_rebase_recovery_missing_worktree_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing worktree must not be treated as a rebase-recovery candidate."""
    import backend.core.use_cases.agent_runner_orchestrate as orchestrate
    from backend.core.use_cases import agent_runner_worktree_probe as probe

    issue = _make_ready_issue(85, "Issue #85", "", ("agent/running",))

    def _raise(*_args: object, **_kwargs: object) -> Path:
        raise FileNotFoundError("worktree path does not exist")

    monkeypatch.setattr(probe, "_find_worktree_path_for_issue", _raise)

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
    running_issue = _make_ready_issue(
        85, "Issue #85", "PRD path: `tasks/x.md`", (running_label,)
    )
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
    running_issue = _make_ready_issue(
        85, "Issue #85", "PRD path: `tasks/x.md`", (running_label,)
    )
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
    issue = _make_ready_issue(85, "Issue #85", "", (AppConfig().labels.running,))

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


def _git(command: list[str], cwd: Path) -> None:
    """Run a git command in cwd, raising on failure."""
    subprocess.run(
        ["git", *command],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo_with_remote(repo_path: Path, remote_path: Path) -> None:
    """Initialize a git repo (with tasks/pending/) pushed to a bare remote."""
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], repo_path)
    _git(["config", "user.name", "Test"], repo_path)
    _git(["config", "user.email", "test@example.com"], repo_path)
    (repo_path / "README.md").write_text("# repo\n", encoding="utf-8")
    pending_dir = repo_path / "tasks" / "pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / ".gitkeep").write_text("", encoding="utf-8")
    archive_dir = repo_path / "tasks" / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / ".gitkeep").write_text("", encoding="utf-8")
    _git(["add", "-A"], repo_path)
    _git(["commit", "-m", "init"], repo_path)
    remote_path.mkdir(parents=True, exist_ok=True)
    _git(["init", "--bare"], remote_path)
    _git(["remote", "add", "origin", str(remote_path)], repo_path)
    _git(["push", "-u", "origin", "main"], repo_path)


def _rework_config(worktree_path: Path) -> AppConfig:
    """Build a rework-prd config that resolves to a pre-created worktree.

    create_command is a no-op (the worktree already exists); path_command echoes
    the pre-created worktree path so create_or_reuse_worktree resolves to it.
    Generated content is disabled so the fallback PRD is used (no agent needed).
    """
    return AppConfig(
        labels=LabelConfig(rework_prd="agent/rework-prd"),
        worktree=WorktreeConfig(
            create_command="true",
            reuse_command="true",
            path_command=f"echo {worktree_path}",
        ),
        generated_content=GeneratedContentConfig(enabled=False),
    )


def test_process_prd_rework_issues_lands_pr_in_worktree(tmp_path: Path) -> None:
    """Real entry point: PRD lands on the issue-N branch + draft PR, main tree clean.

    Exercises the real worktree/commit/push machinery (real git + bare remote,
    fake gh) to prove: the PRD is written inside the issue-87 worktree, committed
    to the issue-87 branch, published via create_draft_pr, the main repo working
    tree gains no untracked PRD, and the PRD is visible on the branch to any
    downstream worktree (queue_ready safety).
    """
    repo_path = tmp_path / "repo"
    remote_path = tmp_path / "remote.git"
    _init_repo_with_remote(repo_path, remote_path)
    worktree_path = tmp_path / "worktrees" / "issue-87"
    worktree_path.parent.mkdir(parents=True)
    _git(["worktree", "add", "-b", "issue-87", str(worktree_path), "main"], repo_path)

    issue = _make_ready_issue(
        87, "Generate PRD", "Need a feature.", ("agent/rework-prd",)
    )
    fake_client = FakeGitHubClient()
    fake_client.set_rework_prd_issues([issue])

    process_prd_rework_issues(
        repo_path=repo_path,
        config=_rework_config(worktree_path),
        github_client=fake_client,
        process_runner=SubprocessRunner(),
        content_generator=None,
        max_issues=1,
    )

    # PRD written inside the issue-87 worktree, not the main repo tree.
    prd_files = list((worktree_path / "tasks" / "pending").glob("*-prd-*.md"))
    assert len(prd_files) == 1
    assert prd_files[0].read_text(encoding="utf-8").startswith("# PRD: Generate PRD")
    relative_prd = prd_files[0].relative_to(worktree_path).as_posix()

    # Main repo working tree gains no untracked PRD file.
    main_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "tasks/pending" not in main_status.stdout

    # PRD is committed to the issue-87 branch (visible to any downstream worktree).
    branch_show = subprocess.run(
        ["git", "show", f"issue-87:{relative_prd}"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch_show.stdout.startswith("# PRD: Generate PRD")

    # Draft PR created via the GitHub client.
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1

    # Labels switched; agent/ready kept.
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(label_calls) == 1
    assert "source/prd" in label_calls[0]["add"]
    assert "agent/ready" in label_calls[0]["add"]
    assert "agent/rework-prd" in label_calls[0]["remove"]

    # Success comment includes the draft PR link.
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "Draft PR:" in comment_calls[0]["body"]


def test_process_prd_rework_issues_failure_rollback(tmp_path: Path) -> None:
    """Worktree provisioning failure marks the issue failed without writing main tree."""
    issue = _make_ready_issue(88, "Issue #88", "", ("agent/rework-prd",))
    fake_client = FakeGitHubClient()
    fake_client.set_rework_prd_issues([issue])
    # path_command echoes a path that does not exist → create_or_reuse_worktree
    # raises FileNotFoundError before any PRD is generated.
    config = AppConfig(
        labels=LabelConfig(rework_prd="agent/rework-prd"),
        worktree=WorktreeConfig(
            create_command="true",
            reuse_command="true",
            path_command=f"echo {tmp_path / 'missing-worktree'}",
        ),
    )

    process_prd_rework_issues(
        repo_path=tmp_path,
        config=config,
        github_client=fake_client,
        process_runner=SubprocessRunner(),
        content_generator=None,
        max_issues=1,
    )

    # No PRD written anywhere under the main repo tree.
    assert not list(tmp_path.rglob("*-prd-*.md"))
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    failed_label_calls = [c for c in label_calls if "agent/failed" in c["add"]]
    assert len(failed_label_calls) == 1
    assert "agent/rework-prd" in failed_label_calls[0]["remove"]
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "PRD generation failed" in comment_calls[0]["body"]
    assert "agent/rework-prd" in comment_calls[0]["body"]
