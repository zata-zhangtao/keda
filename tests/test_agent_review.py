"""Tests for the pre-push AI review gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PrePushReviewConfig,
)
from backend.core.use_cases.agent_review import (
    build_pre_push_review_result_comment,
    build_review_packet,
    parse_reviewer_decision,
    run_pre_push_review,
)
from backend.core.use_cases.agent_runner_events import (
    format_event_marker,
    parse_latest_event_marker,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def test_format_event_marker_basic() -> None:
    """Marker should contain phase, cycle, and version."""
    marker = format_event_marker(phase="implementation_complete", cycle=1)
    assert "version=1" in marker
    assert "phase=implementation_complete" in marker
    assert "cycle=1" in marker


def test_format_event_marker_with_optional_fields() -> None:
    """Marker should include optional fields when provided."""
    marker = format_event_marker(
        phase="pre_push_review",
        cycle=2,
        head_sha="abc123",
        base_sha="def456",
        pr_branch="issue-42",
        action="repair_pr_branch",
    )
    assert "head=abc123" in marker
    assert "base=def456" in marker
    assert "pr_branch=issue-42" in marker
    assert "action=repair_pr_branch" in marker


def test_parse_latest_event_marker_finds_latest() -> None:
    """Parser should return the most recent marker."""
    comments = [
        "<!-- iar:event version=1 phase=old cycle=1 head=aaa -->",
        "some text",
        "<!-- iar:event version=1 phase=new cycle=2 head=bbb action=rebase_pr_branch -->",
    ]
    marker = parse_latest_event_marker(comments)
    assert marker is not None
    assert marker.phase == "new"
    assert marker.cycle == 2
    assert marker.head_sha == "bbb"
    assert marker.action == "rebase_pr_branch"


def test_parse_latest_event_marker_returns_none_when_missing() -> None:
    """Parser should return None when no marker exists."""
    assert parse_latest_event_marker(["no marker here"]) is None


def test_build_review_packet_includes_diff_and_verification() -> None:
    """Review packet should contain diff and verification results."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="PRD path: `tasks/test.md`\n\nDo something.",
        labels=(),
    )
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "diff", "main...abc123"): CommandResult(
                command=("git", "diff", "main...abc123"),
                return_code=0,
                stdout="+added line\n",
                stderr="",
            ),
            ("git", "status", "--short"): CommandResult(
                command=("git", "status", "--short"),
                return_code=0,
                stdout=" M file.py\n",
                stderr="",
            ),
        }
    )
    config = AppConfig()
    verification_results = [
        CommandResult(
            command=("just", "test"),
            return_code=0,
            stdout="ok",
            stderr="",
        )
    ]
    packet = build_review_packet(
        issue=issue,
        worktree_path=Path("."),
        config=config,
        process_runner=fake_runner,
        verification_results=verification_results,
        head_sha="abc123",
    )
    assert "Pre-Push Review for Issue #1" in packet
    assert "tasks/test.md" in packet
    assert "+added line" in packet
    assert "M file.py" in packet
    assert "`just test`: exit 0" in packet


def test_build_pre_push_review_result_comment_structure() -> None:
    """Result comment should contain marker and human-readable fields."""
    body = build_pre_push_review_result_comment(
        verdict="approved",
        reviewer="codex",
        head_before="abc123",
        head_after="def456",
        verification_passed=True,
        findings_high=1,
        findings_medium=2,
        findings_low=3,
        action_summary="reviewer approved without changes",
        cycle=1,
    )
    assert "<!-- iar:event" in body
    assert "phase=pre_push_review" in body
    assert "Verdict: approved" in body
    assert "Reviewer: codex" in body
    assert "Head Before: `abc123`" in body
    assert "Head After: `def456`" in body
    assert "Verification: passed" in body
    assert "Findings: 1 high, 2 medium, 3 low" in body


def test_parse_reviewer_decision_from_json() -> None:
    """Reviewer parser should extract verdict and finding counts."""
    result = parse_reviewer_decision(
        '```json\n{"verdict": "changes_requested", "summary": "fix", '
        '"findings_high": 1, "findings_medium": 2, "findings_low": 3}\n```'
    )

    assert result.verdict == "changes_requested"
    assert result.summary == "fix"
    assert result.findings_high == 1
    assert result.findings_medium == 2
    assert result.findings_low == 3


def test_run_pre_push_review_skips_when_disabled() -> None:
    """If pre-push review is disabled, return immediately."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner()
    config = AppConfig(pre_push_review=PrePushReviewConfig(enabled=False))

    final_sha, verification = run_pre_push_review(
        issue=issue,
        worktree_path=Path("."),
        config=config,
        github_client=fake_client,
        process_runner=fake_runner,
        selected_agent="codex",
        head_sha_before="abc123",
        expected_branch="issue-1",
        verification_results=[],
    )
    assert final_sha == "abc123"
    assert verification == []
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 0


def test_run_pre_push_review_runs_agent_and_approves_when_no_changes(
    tmp_path: Path,
) -> None:
    """Pre-push reviewer that makes no changes should write an approved comment."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _ApprovingRunner(FakeProcessRunner):
        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout='{"verdict": "approved", "summary": "LGTM"}',
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _ApprovingRunner()
    config = AppConfig(
        pre_push_review=PrePushReviewConfig(enabled=True, max_attempts=1)
    )
    worktree_path = tmp_path / "issue-1"
    worktree_path.mkdir()

    final_sha, verification = run_pre_push_review(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=fake_client,
        process_runner=fake_runner,
        selected_agent="codex",
        head_sha_before="abc123",
        expected_branch="issue-1",
        verification_results=[],
    )
    assert final_sha == "abc123"
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "Verdict: approved" in comment_calls[0]["body"]


def test_run_pre_push_review_commits_reviewer_changes(tmp_path: Path) -> None:
    """If reviewer writes a commit request, runner should commit and verify."""
    import json

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    worktree_path = tmp_path / "fake-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps({"commit_message": "reviewer fix"}),
        encoding="utf-8",
    )

    class _PatchThenApproveRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__(
                responses={
                    ("git", "branch", "--show-current"): CommandResult(
                        command=("git", "branch", "--show-current"),
                        return_code=0,
                        stdout="issue-1\n",
                        stderr="",
                    ),
                    ("git", "status", "--porcelain"): CommandResult(
                        command=("git", "status", "--porcelain"),
                        return_code=0,
                        stdout=" M file.py\n",
                        stderr="",
                    ),
                    ("git", "rev-parse", "HEAD"): CommandResult(
                        command=("git", "rev-parse", "HEAD"),
                        return_code=0,
                        stdout="after-sha\n",
                        stderr="",
                    ),
                }
            )
            self._review_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                self._review_calls += 1
                verdict = "changes_requested" if self._review_calls == 1 else "approved"
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=f'{{"verdict": "{verdict}", "summary": "done"}}',
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _PatchThenApproveRunner()
    from backend.core.shared.models.agent_runner import RunnerConfig

    config = AppConfig(
        pre_push_review=PrePushReviewConfig(enabled=True, max_attempts=2),
        runner=RunnerConfig(verification_commands=("just test",)),
    )

    final_sha, verification = run_pre_push_review(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=fake_client,
        process_runner=fake_runner,
        selected_agent="codex",
        head_sha_before="before-sha",
        expected_branch="issue-1",
        verification_results=[],
    )
    assert final_sha == "after-sha"
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 2
    assert "reviewer patched" in comment_calls[0]["body"]
    assert "Verdict: approved" in comment_calls[1]["body"]


def test_run_pre_push_review_rejects_changes_requested_without_commit_request(
    tmp_path: Path,
) -> None:
    """Changes requested in stdout without a commit request should not pass review."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _ChangesRequestedRunner(FakeProcessRunner):
        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=(
                        '{"verdict": "changes_requested", '
                        '"summary": "missing tests"}'
                    ),
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    config = AppConfig(
        pre_push_review=PrePushReviewConfig(enabled=True, max_attempts=1)
    )

    with pytest.raises(RuntimeError, match="did not approve"):
        run_pre_push_review(
            issue=issue,
            worktree_path=tmp_path,
            config=config,
            github_client=fake_client,
            process_runner=_ChangesRequestedRunner(),
            selected_agent="codex",
            head_sha_before="before-sha",
            expected_branch="issue-1",
            verification_results=[],
        )

    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "Verdict: changes requested" in comment_calls[0]["body"]
    assert (
        "reviewer requested changes without a commit request"
        in comment_calls[0]["body"]
    )
