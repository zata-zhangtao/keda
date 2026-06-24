"""Tests for the pre-push AI review gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PrePrReviewConfig,
    RunnerConfig,
)
from backend.core.use_cases.agent_review import (
    build_pre_pr_review_result_comment,
    build_review_packet,
    parse_reviewer_decision,
    run_pre_pr_review,
)
from backend.core.use_cases.agent_runner_events import (
    format_event_marker,
    parse_latest_event_marker,
    parse_latest_pending_rework_marker,
)
from backend.core.use_cases.agent_runner_failure import ProviderCapacityError
from backend.infrastructure.process_runner import CommandFailedError
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
        phase="pre_pr_review",
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


def test_parse_pending_rework_marker_ignores_later_observer_marker() -> None:
    """A later supervisor marker must not hide a queued rework request."""
    comments = [
        format_event_marker(
            phase="post_pr_rework_requested",
            cycle=1,
            head_sha="abc123",
            pr_branch="issue-1",
            action="repair_pr_branch",
        ),
        format_event_marker(
            phase="post_pr_supervisor",
            cycle=2,
            head_sha="abc123",
            checks_state="FAILURE",
            mergeable=True,
        ),
    ]

    latest_marker = parse_latest_event_marker(comments)
    pending_marker = parse_latest_pending_rework_marker(comments)

    assert latest_marker is not None
    assert latest_marker.phase == "post_pr_supervisor"
    assert pending_marker is not None
    assert pending_marker.phase == "post_pr_rework_requested"
    assert pending_marker.pr_branch == "issue-1"
    assert pending_marker.action == "repair_pr_branch"


def test_parse_pending_rework_marker_stops_after_completion() -> None:
    """A completed rework request should not be reprocessed."""
    comments = [
        format_event_marker(
            phase="post_pr_rework_requested",
            cycle=1,
            head_sha="abc123",
            pr_branch="issue-1",
            action="rebase_pr_branch",
        ),
        format_event_marker(
            phase="rebase_repair_complete",
            cycle=1,
            head_sha="def456",
        ),
        format_event_marker(
            phase="post_pr_supervisor",
            cycle=2,
            head_sha="def456",
        ),
    ]

    assert parse_latest_pending_rework_marker(comments) is None


def test_parse_event_marker_with_new_fields() -> None:
    """Parser should extract new optional fields from marker."""
    comment = (
        "<!-- iar:event version=1 phase=post_pr_supervisor cycle=3 "
        "head=abc123 base=def456 checks_state=FAILURE mergeable=true "
        "issue_comments_count=5 pr_comments_count=2 -->"
    )
    from backend.core.shared.models.agent_runner import ReviewEventMarker

    marker = parse_latest_event_marker([comment])
    assert marker is not None
    assert isinstance(marker, ReviewEventMarker)
    assert marker.checks_state == "FAILURE"
    assert marker.mergeable is True
    assert marker.issue_comments_count == 5
    assert marker.pr_comments_count == 2


def test_parse_event_marker_backward_compatible() -> None:
    """Old markers without new fields should parse without error."""
    comment = (
        "<!-- iar:event version=1 phase=post_pr_supervisor cycle=1 head=abc123 -->"
    )
    from backend.core.shared.models.agent_runner import ReviewEventMarker

    marker = parse_latest_event_marker([comment])
    assert marker is not None
    assert isinstance(marker, ReviewEventMarker)
    assert marker.checks_state is None
    assert marker.mergeable is None
    assert marker.issue_comments_count is None
    assert marker.pr_comments_count is None


def test_format_event_marker_with_new_fields() -> None:
    """Formatter should include new fields when provided."""
    marker = format_event_marker(
        phase="post_pr_supervisor",
        cycle=1,
        checks_state="PENDING",
        mergeable=False,
        issue_comments_count=3,
        pr_comments_count=1,
    )
    assert "checks_state=PENDING" in marker
    assert "mergeable=false" in marker
    assert "issue_comments_count=3" in marker
    assert "pr_comments_count=1" in marker


def test_context_changed_wide_detects_all_dimensions() -> None:
    """_context_changed_wide should detect changes in all six dimensions."""
    from dataclasses import replace

    from backend.core.shared.models.agent_runner import (
        PullRequestContext,
        ReviewEventMarker,
    )
    from backend.core.use_cases.review_once import _context_changed_wide

    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
        checks_state="PENDING",
        mergeable=True,
    )
    marker = ReviewEventMarker(
        version=1,
        phase="post_pr_supervisor",
        cycle=1,
        head_sha="abc123",
        base_sha="def456",
        checks_state="PENDING",
        mergeable=True,
        issue_comments_count=2,
        pr_comments_count=1,
    )
    # No change
    assert _context_changed_wide(pr_context, marker, "def456", 2, 1) is False

    # head_sha changed
    assert (
        _context_changed_wide(
            pr_context, replace(marker, head_sha="different"), "def456", 2, 1
        )
        is True
    )

    # base_sha changed
    assert (
        _context_changed_wide(
            pr_context, replace(marker, base_sha="different"), "def456", 2, 1
        )
        is True
    )

    # checks_state changed
    assert (
        _context_changed_wide(
            pr_context, replace(marker, checks_state="FAILURE"), "def456", 2, 1
        )
        is True
    )

    # mergeable changed
    assert (
        _context_changed_wide(
            pr_context, replace(marker, mergeable=False), "def456", 2, 1
        )
        is True
    )

    # issue_comments_count changed
    assert _context_changed_wide(pr_context, marker, "def456", 3, 1) is True

    # pr_comments_count changed
    assert _context_changed_wide(pr_context, marker, "def456", 2, 2) is True


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
    assert "Pre-PR Review for Issue #1" in packet
    assert "tasks/test.md" in packet
    assert "+added line" in packet
    assert "M file.py" in packet
    assert "`just test`: exit 0" in packet
    assert "call the `code-reviewer` skill" in packet
    assert "Findings JSON schema" in packet


def test_build_review_packet_uses_configured_template() -> None:
    """When ``review_prompt_template`` is configured it overrides the default."""
    issue = IssueSummary(
        number=2,
        title="Custom",
        url="https://github.com/example/repo/issues/2",
        body="Just review me.",
        labels=(),
    )
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "diff", "main...deadbeef"): CommandResult(
                command=("git", "diff", "main...deadbeef"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "status", "--short"): CommandResult(
                command=("git", "status", "--short"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )
    config = AppConfig(
        pre_pr_review=PrePrReviewConfig(
            review_prompt_template=("Custom rule A", "Custom rule B"),
        )
    )
    packet = build_review_packet(
        issue=issue,
        worktree_path=Path("."),
        config=config,
        process_runner=fake_runner,
        verification_results=[],
        head_sha="deadbeef",
    )
    assert "Custom rule A" in packet
    assert "Custom rule B" in packet
    # The embedded default should NOT be appended when an override is present.
    assert "call the `code-reviewer` skill" not in packet


def test_build_pre_pr_review_result_comment_structure() -> None:
    """Result comment should contain marker and human-readable fields."""
    body = build_pre_pr_review_result_comment(
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
        findings_critical=0,
    )
    assert "<!-- iar:event" in body
    assert "phase=pre_pr_review" in body
    assert "Verdict: approved" in body
    assert "Reviewer: codex" in body
    assert "Head Before: `abc123`" in body
    assert "Head After: `def456`" in body
    assert "Verification: passed" in body
    assert "Findings: 0 critical, 1 high, 2 medium, 3 low" in body


def test_build_pre_pr_review_result_comment_renders_findings_table() -> None:
    """The findings markdown table must render every captured finding."""
    from backend.core.shared.models.agent_runner import ReviewFinding

    body = build_pre_pr_review_result_comment(
        verdict="changes requested",
        reviewer="codex",
        head_before="abc123",
        head_after="def456",
        verification_passed=False,
        findings_high=1,
        findings_medium=0,
        findings_low=0,
        findings_critical=1,
        action_summary="reviewer reported findings but produced no commit request",
        cycle=2,
        findings=(
            ReviewFinding(
                category="code",
                severity="critical",
                file="src/backend/foo.py",
                line=42,
                title="dangerous",
                description="boom",
                recommendation="fix it",
            ),
            ReviewFinding(
                category="docs",
                severity="high",
                title="outdated",
            ),
        ),
    )
    assert "### Findings" in body
    assert "| Severity | Category | File | Line | Title | Recommendation |" in body
    assert "critical" in body
    assert "src/backend/foo.py" in body
    assert "outdated" in body


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


def test_parse_reviewer_decision_with_string_array_findings() -> None:
    """Findings as string arrays must not break verdict parsing."""
    result = parse_reviewer_decision(
        "```json\n"
        "{\n"
        '"verdict": "approved",\n'
        '"summary": "ok",\n'
        '"findings_medium": [\n'
        '"first finding",\n'
        '"last finding without trailing comma"\n'
        "]\n"
        "}\n"
        "```"
    )

    assert result.verdict == "approved"
    assert result.parseable is True


def test_parse_reviewer_decision_falls_back_to_quoted_text_verdict() -> None:
    """Corrupted JSON with a quoted verdict should still be rescued."""
    result = parse_reviewer_decision(
        '```json{"verdict": "approved","findings_medium": ["only",]}```'
    )

    assert result.verdict == "approved"
    assert result.parseable is True


def test_parse_reviewer_decision_extracts_findings_array() -> None:
    """Findings JSON array is parsed into structured ``ReviewFinding`` objects."""
    result = parse_reviewer_decision(
        "```json\n"
        "{\n"
        '  "verdict": "changes_requested",\n'
        '  "summary": "needs work",\n'
        '  "findings": [\n'
        '    {"category": "code", "severity": "high", '
        '"file": "src/foo.py", "line": 12, '
        '"title": "off-by-one", '
        '"description": "loop bound", '
        '"recommendation": "use < len"},\n'
        '    {"category": "docs", "severity": "medium", '
        '"title": "missing comment"}\n'
        "  ]\n"
        "}\n"
        "```"
    )

    assert result.verdict == "changes_requested"
    assert len(result.findings) == 2
    assert result.findings[0].category == "code"
    assert result.findings[0].severity == "high"
    assert result.findings[0].file == "src/foo.py"
    assert result.findings[0].line == 12
    assert result.findings[1].category == "docs"
    # Counts must be derived from the parsed findings, not from the agent's
    # self-reported numbers.
    assert result.findings_high == 1
    assert result.findings_medium == 1
    assert result.findings_low == 0


def test_parse_reviewer_decision_overrides_approved_when_findings_present() -> None:
    """``verdict=approved`` with findings is downgraded to changes_requested."""
    result = parse_reviewer_decision(
        "```json\n"
        "{\n"
        '  "verdict": "approved",\n'
        '  "summary": "looks good",\n'
        '  "findings": [\n'
        '    {"severity": "low", "title": "minor nit"}'
        "  ]\n"
        "}\n"
        "```"
    )

    assert result.verdict == "changes_requested"
    assert len(result.findings) == 1
    assert result.findings_low == 1


def test_parse_reviewer_decision_counts_critical_findings() -> None:
    """Critical severity counts must be exposed for the comment and counts line."""
    result = parse_reviewer_decision(
        "```json\n"
        "{\n"
        '  "verdict": "changes_requested",\n'
        '  "findings": [\n'
        '    {"severity": "critical", "title": "data loss"},\n'
        '    {"severity": "critical", "title": "auth bypass"},'
        "  ]\n"
        "}\n"
        "```"
    )

    assert result.findings_critical == 2
    assert result.findings_high == 0


def test_run_pre_pr_review_skips_when_disabled() -> None:
    """If pre-PR review is disabled, return immediately."""

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner()
    config = AppConfig(pre_pr_review=PrePrReviewConfig(enabled=False))

    final_sha, verification = run_pre_pr_review(
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


def test_run_pre_pr_review_runs_agent_and_approves_when_no_changes(
    tmp_path: Path,
) -> None:
    """Pre-PR reviewer that makes no changes should write an approved comment."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _ApprovingRunner(FakeProcessRunner):
        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
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
    config = AppConfig(pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1))
    worktree_path = tmp_path / "issue-1"
    worktree_path.mkdir()

    final_sha, verification = run_pre_pr_review(
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


def test_run_pre_pr_review_commits_reviewer_changes(tmp_path: Path) -> None:
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

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
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
        pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=2),
        runner=RunnerConfig(verification_commands=("just test",)),
    )

    final_sha, verification = run_pre_pr_review(
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


class _PatchingReviewRunner(FakeProcessRunner):
    """Fake runner: reviewer returns a fixed verdict and git commands succeed."""

    def __init__(self, verdict: str) -> None:
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
        self._verdict = verdict

    def run(
        self, command, *, cwd, check=True, timeout=None, capture_output=True, label=None
    ):
        command_tuple = tuple(command)
        if command_tuple[:1] == ("codex",):
            self.calls.append(list(command))
            return CommandResult(
                command=command_tuple,
                return_code=0,
                stdout=f'{{"verdict": "{self._verdict}", "summary": "done"}}',
                stderr="",
            )
        return super().run(
            command,
            cwd=cwd,
            check=check,
            timeout=timeout,
            capture_output=capture_output,
        )


def _write_commit_request(worktree_path: Path) -> None:
    import json

    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps({"commit_message": "reviewer fix"}),
        encoding="utf-8",
    )


def test_run_pre_pr_review_approved_with_patch_converges(tmp_path: Path) -> None:
    """Approved verdict plus a committed patch must converge in the same cycle."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    worktree_path = tmp_path / "fake-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)
    _write_commit_request(worktree_path)

    fake_runner = _PatchingReviewRunner("approved")
    from backend.core.shared.models.agent_runner import RunnerConfig

    config = AppConfig(
        pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1),
        runner=RunnerConfig(verification_commands=("just test",)),
    )

    final_sha, _verification = run_pre_pr_review(
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
    assert len(comment_calls) == 1
    assert "Verdict: approved" in comment_calls[0]["body"]
    assert (
        "reviewer approved and runner committed follow-up patch"
        in comment_calls[0]["body"]
    )


def test_run_pre_pr_review_patched_soft_fail_reports_last_cycle_summary(
    tmp_path: Path,
) -> None:
    """Last cycle with a successful patch must commit and continue publish."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    worktree_path = tmp_path / "fake-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)
    _write_commit_request(worktree_path)

    fake_runner = _PatchingReviewRunner("changes_requested")
    from backend.core.shared.models.agent_runner import RunnerConfig

    config = AppConfig(
        pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1),
        runner=RunnerConfig(verification_commands=("just test",)),
    )

    final_sha, _verification = run_pre_pr_review(
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
    # Per the PRD: the last cycle accepts the reviewer-supplied final patch
    # instead of hard-failing, so the runner converges and publishes.
    assert final_sha == "after-sha"
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert (
        "reviewer patched and runner committed follow-up changes"
        in (comment_calls[0]["body"])
    )


def test_run_pre_pr_review_empty_commit_request_with_approval_converges(
    tmp_path: Path,
) -> None:
    """Reviewer writing a commit request with no diff must not hard-fail.

    A reviewer that signals a commit but produces no actual file changes is a
    benign no-op (e.g. the suggested change matches the current state). When the
    reviewer's real verdict is ``approved`` the gate should converge instead of
    raising ``Pre-PR review repair failed``.
    """
    import json

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    worktree_path = tmp_path / "issue-1"
    worktree_path.mkdir(parents=True, exist_ok=True)
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps({"commit_message": "noop"}),
        encoding="utf-8",
    )

    class _EmptyCommitApproveRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__(
                responses={
                    ("git", "branch", "--show-current"): CommandResult(
                        command=("git", "branch", "--show-current"),
                        return_code=0,
                        stdout="issue-1\n",
                        stderr="",
                    ),
                    # 空工作树：移除 commit-request 后没有任何改动可提交
                    ("git", "status", "--porcelain"): CommandResult(
                        command=("git", "status", "--porcelain"),
                        return_code=0,
                        stdout="",
                        stderr="",
                    ),
                }
            )

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout='{"verdict": "approved", "summary": "all good"}',
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    config = AppConfig(pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1))

    final_sha, _verification = run_pre_pr_review(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=fake_client,
        process_runner=_EmptyCommitApproveRunner(),
        selected_agent="codex",
        head_sha_before="before-sha",
        expected_branch="issue-1",
        verification_results=[],
    )

    # head 未推进，且循环正常收敛而非硬失败
    assert final_sha == "before-sha"
    # 残留的 commit-request 应被清理，避免污染后续轮次
    assert not request_path.exists()
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "Verdict: approved" in comment_calls[0]["body"]
    assert "empty commit request" in comment_calls[0]["body"]


def test_run_pre_pr_review_uses_commit_request_verdict_when_stdout_unparseable(
    tmp_path: Path,
) -> None:
    """Commit-request verdict metadata should recover unparseable reviewer stdout."""
    import json

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    worktree_path = tmp_path / "issue-1"
    worktree_path.mkdir(parents=True, exist_ok=True)
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps(
            {
                "commit_message": "noop",
                "verdict": "approved",
                "summary": "approved via request metadata",
            }
        ),
        encoding="utf-8",
    )

    class _UnparseableApproveRunner(FakeProcessRunner):
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
                        stdout="",
                        stderr="",
                    ),
                }
            )

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout="I wrote a no-op commit request.",
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    config = AppConfig(pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1))

    final_sha, _verification = run_pre_pr_review(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=fake_client,
        process_runner=_UnparseableApproveRunner(),
        selected_agent="codex",
        head_sha_before="before-sha",
        expected_branch="issue-1",
        verification_results=[],
    )

    assert final_sha == "before-sha"
    assert not request_path.exists()
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "Verdict: approved" in comment_calls[0]["body"]
    assert "empty commit request" in comment_calls[0]["body"]


def test_run_pre_pr_review_empty_commit_request_changes_requested_soft_fails(
    tmp_path: Path,
) -> None:
    """An empty commit request with a changes_requested verdict soft-fails.

    The runner must NOT raise the hard ``Pre-PR review repair failed`` error
    (the bug seen on Issue #5); it should fall through to the regular
    "did not approve after N attempts" path with an accurate action summary.
    """
    import json

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    worktree_path = tmp_path / "issue-1"
    worktree_path.mkdir(parents=True, exist_ok=True)
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps({"commit_message": "noop"}),
        encoding="utf-8",
    )

    class _EmptyCommitChangesRunner(FakeProcessRunner):
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
                        stdout="",
                        stderr="",
                    ),
                }
            )

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=('{"verdict": "changes_requested", "summary": "still off"}'),
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    config = AppConfig(pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1))

    with pytest.raises(RuntimeError, match="did not approve") as exc_info:
        run_pre_pr_review(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            github_client=fake_client,
            process_runner=_EmptyCommitChangesRunner(),
            selected_agent="codex",
            head_sha_before="before-sha",
            expected_branch="issue-1",
            verification_results=[],
        )

    # 关键：不能是 "repair failed" 硬失败
    assert "repair failed" not in str(exc_info.value)
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "Verdict: changes requested" in comment_calls[0]["body"]
    assert "produced no committable diff" in comment_calls[0]["body"]


def test_run_pre_pr_review_rejects_changes_requested_without_commit_request(
    tmp_path: Path,
) -> None:
    """Changes requested in stdout without a commit request should not pass review."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _ChangesRequestedRunner(FakeProcessRunner):
        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
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

    config = AppConfig(pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1))

    with pytest.raises(RuntimeError, match="did not approve"):
        run_pre_pr_review(
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


def test_run_pre_pr_review_passes_configured_timeout(tmp_path: Path) -> None:
    """Pre-PR review should pass its configured timeout to the reviewer agent."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _RecordingTimeoutRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self.agent_timeouts: list[int | None] = []

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                self.agent_timeouts.append(timeout)
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout='{"verdict": "approved", "summary": "ok"}',
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _RecordingTimeoutRunner()
    config = AppConfig(
        pre_pr_review=PrePrReviewConfig(
            enabled=True,
            max_attempts=1,
            timeout_seconds=42,
        )
    )

    final_sha, _verification = run_pre_pr_review(
        issue=issue,
        worktree_path=tmp_path,
        config=config,
        github_client=fake_client,
        process_runner=fake_runner,
        selected_agent="codex",
        head_sha_before="before-sha",
        expected_branch="issue-1",
        verification_results=[],
    )

    assert final_sha == "before-sha"
    assert fake_runner.agent_timeouts == [42]


def test_run_pre_pr_review_soft_fails_with_findings_on_last_cycle(
    tmp_path: Path,
) -> None:
    """Final cycle with findings and no commit request soft-fails with findings rendered."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _FindingsOnlyRunner(FakeProcessRunner):
        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=(
                        "```json\n"
                        "{\n"
                        '  "verdict": "changes_requested",\n'
                        '  "summary": "needs work",\n'
                        '  "findings": [\n'
                        '    {"severity": "high", '
                        '"category": "code", '
                        '"title": "missing tests"}\n'
                        "  ]\n"
                        "}\n"
                        "```"
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
        pre_pr_review=PrePrReviewConfig(
            enabled=True, max_attempts=1, commit_request_reminder_attempts=0
        )
    )

    with pytest.raises(RuntimeError, match="did not approve"):
        run_pre_pr_review(
            issue=issue,
            worktree_path=tmp_path,
            config=config,
            github_client=fake_client,
            process_runner=_FindingsOnlyRunner(),
            selected_agent="codex",
            head_sha_before="before-sha",
            expected_branch="issue-1",
            verification_results=[],
        )

    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    body = comment_calls[0]["body"]
    assert "Verdict: changes requested" in body
    assert "missing tests" in body
    assert "reviewer reported findings but produced no commit request" in body


def test_run_pre_pr_review_reminds_reviewer_then_commits(
    tmp_path: Path,
) -> None:
    """Findings without commit request trigger a reminder; a follow-up patch commits."""
    import json

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    worktree_path = tmp_path / "fake-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)

    class _ReminderThenPatchRunner(FakeProcessRunner):
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
            self.last_prompt = ""

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                self._review_calls += 1
                self.last_prompt = " ".join(command)
                if self._review_calls == 1:
                    return CommandResult(
                        command=command_tuple,
                        return_code=0,
                        stdout=(
                            "```json\n"
                            "{\n"
                            '  "verdict": "changes_requested",\n'
                            '  "summary": "needs work",\n'
                            '  "findings": [\n'
                            '    {"severity": "high", "category": "code", '
                            '"title": "missing tests"}\n'
                            "  ]\n"
                            "}\n"
                            "```"
                        ),
                        stderr="",
                    )
                # Second call: write commit request and return approved.
                request_path = cwd / ".agent-runner" / "commit-request.json"
                request_path.parent.mkdir(parents=True, exist_ok=True)
                request_path.write_text(
                    json.dumps({"commit_message": "reviewer fix"}),
                    encoding="utf-8",
                )
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout='{"verdict": "approved", "summary": "fixed"}',
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _ReminderThenPatchRunner()
    from backend.core.shared.models.agent_runner import RunnerConfig

    config = AppConfig(
        pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1),
        runner=RunnerConfig(verification_commands=("just test",)),
    )

    final_sha, _verification = run_pre_pr_review(
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
    assert fake_runner._review_calls == 2
    assert "REMINDER #1" in fake_runner.last_prompt
    assert "missing tests" in fake_runner.last_prompt
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "Verdict: approved" in comment_calls[0]["body"]
    assert (
        "reviewer approved and runner committed follow-up patch"
        in comment_calls[0]["body"]
    )


def test_run_pre_pr_review_zero_reminder_attempts_fails_fast(
    tmp_path: Path,
) -> None:
    """commit_request_reminder_attempts=0 disables the inner re-prompt."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _FindingsOnlyNoRetryRunner(FakeProcessRunner):
        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=(
                        "```json\n"
                        "{\n"
                        '  "verdict": "changes_requested",\n'
                        '  "summary": "needs work",\n'
                        '  "findings": [\n'
                        '    {"severity": "high", "category": "code", '
                        '"title": "missing tests"}\n'
                        "  ]\n"
                        "}\n"
                        "```"
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

    fake_runner = _FindingsOnlyNoRetryRunner()
    config = AppConfig(
        pre_pr_review=PrePrReviewConfig(
            enabled=True, max_attempts=1, commit_request_reminder_attempts=0
        )
    )

    with pytest.raises(RuntimeError, match="did not approve"):
        run_pre_pr_review(
            issue=issue,
            worktree_path=tmp_path,
            config=config,
            github_client=fake_client,
            process_runner=fake_runner,
            selected_agent="codex",
            head_sha_before="before-sha",
            expected_branch="issue-1",
            verification_results=[],
        )

    codex_calls = [c for c in fake_runner.calls if c and c[0] == "codex"]
    assert len(codex_calls) == 1
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert (
        "reviewer reported findings but produced no commit request"
        in comment_calls[0]["body"]
    )


def test_run_pre_pr_review_last_cycle_final_patch_is_accepted(
    tmp_path: Path,
) -> None:
    """Final cycle with findings + commit request must commit and continue publish."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    worktree_path = tmp_path / "fake-worktree"
    worktree_path.mkdir(parents=True, exist_ok=True)
    _write_commit_request(worktree_path)

    class _FinalPatchReviewer(FakeProcessRunner):
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

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=(
                        "```json\n"
                        "{\n"
                        '  "verdict": "changes_requested",\n'
                        '  "findings": [\n'
                        '    {"severity": "medium", '
                        '"title": "naming"}\n'
                        "  ]\n"
                        "}\n"
                        "```"
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

    config = AppConfig(pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1))

    final_sha, _verification = run_pre_pr_review(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=fake_client,
        process_runner=_FinalPatchReviewer(),
        selected_agent="codex",
        head_sha_before="before-sha",
        expected_branch="issue-1",
        verification_results=[],
    )
    assert final_sha == "after-sha"
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert (
        "reviewer patched and runner committed follow-up changes"
        in (comment_calls[0]["body"])
    )


# ---------------------------------------------------------------------------
# Pre-PR review 错误韧性：瞬时重试（Level 1）与供应商容量升级（Level 2 触发）
# ---------------------------------------------------------------------------


def _socket_command_error(command: list[str]) -> CommandFailedError:
    return CommandFailedError(
        1,
        command,
        output=(
            "[agent error] API Error: The socket connection was closed " "unexpectedly."
        ),
        stderr="",
    )


def test_run_pre_pr_review_retries_transient_reviewer_error(tmp_path: Path) -> None:
    """A transient reviewer error is retried in place and then approves.

    Regression guard for the Issue #15 failure mode: a single socket drop during
    review must no longer fail the Issue.
    """
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _TransientThenApproveRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._reviewer_calls = 0

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                self._reviewer_calls += 1
                if self._reviewer_calls == 1:
                    raise _socket_command_error(list(command))
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

    fake_runner = _TransientThenApproveRunner()
    config = AppConfig(
        pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1),
        runner=RunnerConfig(
            transient_retry_attempts=2, transient_retry_delay_seconds=0
        ),
    )
    worktree_path = tmp_path / "issue-1"
    worktree_path.mkdir()

    final_sha, _verification = run_pre_pr_review(
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
    reviewer_calls = [c for c in fake_runner.calls if c[:1] == ["codex"]]
    assert len(reviewer_calls) == 2  # one transient failure + one success
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert "Verdict: approved" in comment_calls[0]["body"]


def test_run_pre_pr_review_escalates_provider_capacity(tmp_path: Path) -> None:
    """A reviewer provider-capacity error escalates so the chain can switch."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _CapacityRunner(FakeProcessRunner):
        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            if command_tuple[:1] == ("codex",):
                self.calls.append(list(command))
                raise CommandFailedError(
                    1,
                    list(command),
                    output="API Error: Request rejected (429) usage limit reached",
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _CapacityRunner()
    config = AppConfig(
        pre_pr_review=PrePrReviewConfig(enabled=True, max_attempts=1),
        runner=RunnerConfig(
            transient_retry_attempts=2, transient_retry_delay_seconds=0
        ),
    )
    worktree_path = tmp_path / "issue-1"
    worktree_path.mkdir()

    with pytest.raises(ProviderCapacityError):
        run_pre_pr_review(
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

    # Capacity is not retried in place: exactly one reviewer invocation.
    reviewer_calls = [c for c in fake_runner.calls if c[:1] == ["codex"]]
    assert len(reviewer_calls) == 1
