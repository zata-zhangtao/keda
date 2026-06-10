"""Tests for the post-PR supervisor cycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PullRequestContext,
    SupervisorActionResult,
)
from backend.core.use_cases.agent_runner_events import parse_latest_event_marker
from backend.core.use_cases.pr_supervisor import (
    build_conflict_resolution_prompt,
    build_rework_intent_comment,
    build_supervisor_prompt,
    execute_rebase,
    execute_repair,
    guard_supervisor_action_for_pr_state,
    parse_supervisor_action,
    run_post_pr_supervisor_cycle,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def test_parse_supervisor_action_approve() -> None:
    """Parser should extract approve action from JSON."""
    text = (
        '```json\n{"action": "approve_for_human_review", "summary": "looks good"}\n```'
    )
    result = parse_supervisor_action(text)
    assert result.action == "approve_for_human_review"
    assert result.summary == "looks good"


def test_parse_supervisor_action_repair() -> None:
    """Parser should extract repair action."""
    text = '{"action": "repair_pr_branch", "summary": "fix typo", "findings_high": 1}'
    result = parse_supervisor_action(text)
    assert result.action == "repair_pr_branch"
    assert result.findings_counts.get("high") == 1


def test_parse_supervisor_action_invalid_defaults_to_human_input() -> None:
    """Invalid action should default to request_human_input."""
    text = '{"action": "unknown_action"}'
    result = parse_supervisor_action(text)
    assert result.action == "request_human_input"


def test_supervisor_action_gate_blocks_conflicting_pr_approval() -> None:
    """Conflicting PRs should request rebase instead of human review."""
    action_result = SupervisorActionResult(
        action="approve_for_human_review",
        summary="LGTM",
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
        mergeable=False,
    )

    gated_result = guard_supervisor_action_for_pr_state(action_result, pr_context)

    assert gated_result.action == "rebase_pr_branch"
    assert "mergeability gate" in gated_result.summary


def test_supervisor_action_gate_blocks_failed_check_approval() -> None:
    """Failed checks should request repair instead of human review."""
    action_result = SupervisorActionResult(
        action="approve_for_human_review",
        summary="LGTM",
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
        mergeable=True,
        checks_state="FAILURE",
        checks_summary=("lint (conclusion=FAILURE)",),
    )

    gated_result = guard_supervisor_action_for_pr_state(action_result, pr_context)

    assert gated_result.action == "repair_pr_branch"
    assert "lint" in gated_result.summary


def test_supervisor_action_gate_allows_approval_for_validation_sign_off_only() -> None:
    """The intentional manual Realistic Validation gate alone must not block approval."""
    action_result = SupervisorActionResult(
        action="approve_for_human_review",
        summary="LGTM",
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
        mergeable=True,
        checks_state="FAILURE",
        checks_summary=(
            "Realistic Validation sign-off (status=COMPLETED, conclusion=FAILURE)",
        ),
    )

    gated_result = guard_supervisor_action_for_pr_state(action_result, pr_context)

    assert gated_result.action == "approve_for_human_review"
    assert gated_result.summary == "LGTM"


def test_supervisor_action_gate_blocks_when_validation_and_other_checks_fail() -> None:
    """If other checks fail alongside the validation gate, repair is still required."""
    action_result = SupervisorActionResult(
        action="approve_for_human_review",
        summary="LGTM",
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
        mergeable=True,
        checks_state="FAILURE",
        checks_summary=(
            "Realistic Validation sign-off (status=COMPLETED, conclusion=FAILURE)",
            "lint (conclusion=FAILURE)",
        ),
    )

    gated_result = guard_supervisor_action_for_pr_state(action_result, pr_context)

    assert gated_result.action == "repair_pr_branch"
    assert "lint" in gated_result.summary


def test_build_rework_intent_comment_has_marker() -> None:
    """Rework intent comment should include an iar:event marker."""
    body = build_rework_intent_comment(
        action="repair_pr_branch",
        pr_branch="issue-42",
        head_sha="abc123",
    )
    assert "<!-- iar:event" in body
    assert "phase=post_pr_rework_requested" in body
    assert "pr_branch=issue-42" in body
    assert "head=abc123" in body
    assert "Action: repair_pr_branch" in body


def test_build_supervisor_prompt_includes_context() -> None:
    """Supervisor prompt should include PR context and verification."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Do something.",
        labels=(),
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
    )
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "diff", "main...abc123"): CommandResult(
                command=("git", "diff", "main...abc123"),
                return_code=0,
                stdout="+line\n",
                stderr="",
            ),
        }
    )
    config = AppConfig()
    prompt = build_supervisor_prompt(
        issue=issue,
        pr_context=pr_context,
        config=config,
        process_runner=fake_runner,
        worktree_path=Path("."),
        issue_comments=[],
        pr_comments=[],
        base_sha_remote="remote-sha",
    )
    assert "Post-PR Supervisor Review for Issue #1" in prompt
    assert "issue-1" in prompt
    assert "abc123" in prompt
    assert "remote-sha" in prompt
    assert "+line" in prompt


def test_execute_rebase_safety_checks() -> None:
    """Rebase should validate branch and HEAD before proceeding."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="wrong-sha\n",
                stderr="",
            ),
        }
    )
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    config = AppConfig()
    with pytest.raises(RuntimeError, match="HEAD wrong-sha does not match expected"):
        execute_rebase(
            issue=issue,
            worktree_path=Path("."),
            config=config,
            process_runner=fake_runner,
            pr_branch="issue-1",
            expected_head="abc123",
            supervisor_agent="codex",
        )


def test_execute_rebase_fetches_and_rebases_safely() -> None:
    """Rebase should fetch, rebase, verify, and force-with-lease push."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
                stderr="",
            ),
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "fetch", "origin", "main"): CommandResult(
                command=("git", "fetch", "origin", "main"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "rebase", "origin/main"): CommandResult(
                command=("git", "rebase", "origin/main"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "push", "--force-with-lease", "origin", "issue-1"): CommandResult(
                command=("git", "push", "--force-with-lease", "origin", "issue-1"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    config = AppConfig()
    execute_rebase(
        issue=issue,
        worktree_path=Path("."),
        config=config,
        process_runner=fake_runner,
        pr_branch="issue-1",
        expected_head="abc123",
        supervisor_agent="codex",
    )
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "fetch", "origin", "main") in commands
    assert ("git", "rebase", "origin/main") in commands
    assert ("git", "push", "--force-with-lease", "origin", "issue-1") in commands
    # Should NOT push the base branch
    assert ("git", "push", "origin", "main") not in commands


def test_execute_rebase_aborts_on_conflict() -> None:
    """Rebase should abort and raise when conflicts occur."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
                stderr="",
            ),
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "fetch", "origin", "main"): CommandResult(
                command=("git", "fetch", "origin", "main"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "rebase", "origin/main"): CommandResult(
                command=("git", "rebase", "origin/main"),
                return_code=1,
                stdout="",
                stderr="CONFLICT (content): Merge conflict in file.py\n",
            ),
            ("git", "rebase", "--abort"): CommandResult(
                command=("git", "rebase", "--abort"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )
    from backend.core.shared.models.agent_runner import PostPrSupervisorConfig

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    config = AppConfig(post_pr_supervisor=PostPrSupervisorConfig(max_repair_attempts=0))
    with pytest.raises(RuntimeError, match="exhausted"):
        execute_rebase(
            issue=issue,
            worktree_path=Path("."),
            config=config,
            process_runner=fake_runner,
            pr_branch="issue-1",
            expected_head="abc123",
            supervisor_agent="codex",
        )
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "rebase", "--abort") in commands


def test_execute_repair_runs_agent_and_commits() -> None:
    """Repair should run agent, commit changes, verify, and push."""
    import json

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    worktree_path = Path("/tmp/fake-repair")
    worktree_path.mkdir(parents=True, exist_ok=True)
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps({"commit_message": "repair commit"}),
        encoding="utf-8",
    )

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
                stderr="",
            ),
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
            ("git", "add", "-A"): CommandResult(
                command=("git", "add", "-A"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "commit", "-m", "repair commit"): CommandResult(
                command=("git", "commit", "-m", "repair commit"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "push", "origin", "issue-1"): CommandResult(
                command=("git", "push", "origin", "issue-1"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )
    from backend.core.shared.models.agent_runner import RunnerConfig

    config = AppConfig(
        runner=RunnerConfig(verification_commands=("just test",)),
    )

    execute_repair(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        process_runner=fake_runner,
        pr_branch="issue-1",
        expected_head="abc123",
        supervisor_agent="codex",
    )
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "commit", "-m", "repair commit") in commands
    assert ("git", "push", "origin", "issue-1") in commands


def test_execute_repair_rejects_uncommitted_changes_without_request(
    tmp_path: Path,
) -> None:
    """Repair must not push old HEAD when the agent edits without a commit request."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    worktree_path = tmp_path / "fake-repair"
    worktree_path.mkdir()
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
                stderr="",
            ),
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
        }
    )

    with pytest.raises(RuntimeError, match="without writing"):
        execute_repair(
            issue=issue,
            worktree_path=worktree_path,
            config=AppConfig(),
            process_runner=fake_runner,
            pr_branch="issue-1",
            expected_head="abc123",
            supervisor_agent="codex",
        )

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "push", "origin", "issue-1") not in commands


def test_run_post_pr_supervisor_cycle_writes_comment() -> None:
    """Supervisor cycle should write a result comment to the Issue."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    fake_client._remote_base_sha = "def456"
    fake_runner = FakeProcessRunner()
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
    )

    run_post_pr_supervisor_cycle(
        issue=issue,
        worktree_path=Path("."),
        config=AppConfig(),
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=pr_context,
        supervisor_agent="codex",
        cycle=1,
    )

    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "<!-- iar:event" in comment_calls[0]["body"]
    assert "phase=post_pr_supervisor" in comment_calls[0]["body"]
    marker = parse_latest_event_marker([comment_calls[0]["body"]])
    assert marker is not None
    assert marker.base_sha == "def456"
    assert marker.issue_comments_count == 1


def test_run_post_pr_supervisor_cycle_parses_action() -> None:
    """Supervisor cycle should parse agent output into an action result."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _ActionRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self.agent_capture_output: list[bool] = []

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            if tuple(command)[:1] == ("codex",):
                self.calls.append(list(command))
                self.agent_capture_output.append(capture_output)
                return CommandResult(
                    command=tuple(command),
                    return_code=0,
                    stdout=(
                        '{"action": "approve_for_human_review", ' '"summary": "LGTM"}'
                    )
                    if capture_output
                    else "",
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _ActionRunner()
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
    )

    result = run_post_pr_supervisor_cycle(
        issue=issue,
        worktree_path=Path("."),
        config=AppConfig(),
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=pr_context,
        supervisor_agent="codex",
        cycle=1,
    )

    assert result.action == "approve_for_human_review"
    assert result.summary == "LGTM"
    assert fake_runner.agent_capture_output == [True]


def test_build_conflict_resolution_prompt_includes_context() -> None:
    """Conflict resolution prompt should include issue, branch, head, and files."""
    issue = IssueSummary(
        number=42,
        title="Fix bug",
        url="https://github.com/example/repo/issues/42",
        body="B",
        labels=(),
    )
    prompt = build_conflict_resolution_prompt(
        issue=issue,
        pr_branch="issue-42",
        expected_head="abc123",
        conflicted_files=["src/a.py", "src/b.py"],
    )
    assert "Issue #42: Fix bug" in prompt
    assert "issue-42" in prompt
    assert "abc123" in prompt
    assert "src/a.py" in prompt
    assert "src/b.py" in prompt
    assert ".agent-runner/commit-request.json" in prompt


def test_execute_rebase_resolves_conflict_via_agent(tmp_path: Path) -> None:
    """Rebase conflict should be resolved by agent, then continue, verify, and push."""
    import json

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps({"commit_message": "resolve conflict"}),
        encoding="utf-8",
    )

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
                stderr="",
            ),
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "fetch", "origin", "main"): CommandResult(
                command=("git", "fetch", "origin", "main"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "rebase", "origin/main"): CommandResult(
                command=("git", "rebase", "origin/main"),
                return_code=1,
                stdout="",
                stderr="CONFLICT",
            ),
            ("git", "diff", "--name-only", "--diff-filter=U"): CommandResult(
                command=("git", "diff", "--name-only", "--diff-filter=U"),
                return_code=0,
                stdout="file.py\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M file.py\n",
                stderr="",
            ),
            ("git", "rebase", "--continue"): CommandResult(
                command=("git", "rebase", "--continue"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "push", "--force-with-lease", "origin", "issue-1"): CommandResult(
                command=("git", "push", "--force-with-lease", "origin", "issue-1"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )
    config = AppConfig()
    execute_rebase(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        process_runner=fake_runner,
        pr_branch="issue-1",
        expected_head="abc123",
        supervisor_agent="codex",
    )
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "rebase", "--continue") in commands
    assert ("git", "push", "--force-with-lease", "origin", "issue-1") in commands


def test_execute_rebase_conflict_exhaustion(tmp_path: Path) -> None:
    """Rebase conflict resolution should exhaust and abort after max attempts."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
                stderr="",
            ),
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "fetch", "origin", "main"): CommandResult(
                command=("git", "fetch", "origin", "main"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "rebase", "origin/main"): CommandResult(
                command=("git", "rebase", "origin/main"),
                return_code=1,
                stdout="",
                stderr="CONFLICT",
            ),
            ("git", "diff", "--name-only", "--diff-filter=U"): CommandResult(
                command=("git", "diff", "--name-only", "--diff-filter=U"),
                return_code=0,
                stdout="file.py\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "rebase", "--continue"): CommandResult(
                command=("git", "rebase", "--continue"),
                return_code=1,
                stdout="",
                stderr="still conflicted",
            ),
            ("git", "rebase", "--abort"): CommandResult(
                command=("git", "rebase", "--abort"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )
    from backend.core.shared.models.agent_runner import PostPrSupervisorConfig

    config = AppConfig(post_pr_supervisor=PostPrSupervisorConfig(max_repair_attempts=2))
    with pytest.raises(RuntimeError, match="exhausted"):
        execute_rebase(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=fake_runner,
            pr_branch="issue-1",
            expected_head="abc123",
            supervisor_agent="codex",
        )
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "rebase", "--abort") in commands
    assert commands.count(("git", "rebase", "--continue")) == 2


def test_execute_rebase_conflict_agent_no_commit_request(tmp_path: Path) -> None:
    """Agent that changes files without writing commit request should raise immediately."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
                stderr="",
            ),
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-1\n",
                stderr="",
            ),
            ("git", "fetch", "origin", "main"): CommandResult(
                command=("git", "fetch", "origin", "main"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "rebase", "origin/main"): CommandResult(
                command=("git", "rebase", "origin/main"),
                return_code=1,
                stdout="",
                stderr="CONFLICT",
            ),
            ("git", "diff", "--name-only", "--diff-filter=U"): CommandResult(
                command=("git", "diff", "--name-only", "--diff-filter=U"),
                return_code=0,
                stdout="file.py\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M file.py\n",
                stderr="",
            ),
        }
    )
    config = AppConfig()
    with pytest.raises(RuntimeError, match="without writing"):
        execute_rebase(
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=fake_runner,
            pr_branch="issue-1",
            expected_head="abc123",
            supervisor_agent="codex",
        )
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "rebase", "--abort") not in commands
    assert ("git", "rebase", "--continue") not in commands


def test_dirty_worktree_before_supervisor_blocks_approval(tmp_path: Path) -> None:
    """Read-only supervisor must not approve when worktree is dirty before cycle."""
    from backend.core.use_cases.agent_runner_supervisor import (
        _run_supervisor_with_repair_loop,
    )

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M file.py\n",
                stderr="",
            ),
        }
    )
    fake_client = FakeGitHubClient()
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
    )
    config = AppConfig()

    _run_supervisor_with_repair_loop(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=pr_context,
        supervisor_agent="codex",
    )

    # Should move to blocked, not review
    blocked_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.blocked in c.get("add", [])
    ]
    assert len(blocked_calls) == 1
    assert config.labels.supervising in blocked_calls[0]["remove"]

    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "dirty_worktree_before_supervisor" in comment_calls[0]["body"]


def test_dirty_worktree_after_approve_blocks_review(tmp_path: Path) -> None:
    """Read-only supervisor leaving uncommitted changes must not enter review."""
    from backend.core.use_cases.agent_runner_supervisor import (
        _run_supervisor_with_repair_loop,
    )

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()

    class _DirtyAfterApproveRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._status_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple == ("git", "status", "--porcelain"):
                self._status_calls += 1
                # First call (before supervisor): clean
                # Second call (after approve): dirty
                stdout = "" if self._status_calls == 1 else " M file.py\n"
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple[:1] == ("codex",):
                return CommandResult(
                    command_tuple,
                    0,
                    '{"action": "approve_for_human_review", "summary": "LGTM"}',
                    "",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _DirtyAfterApproveRunner()
    fake_client = FakeGitHubClient()
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
    )
    config = AppConfig()

    _run_supervisor_with_repair_loop(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=pr_context,
        supervisor_agent="codex",
    )

    # Should move to blocked, not review
    blocked_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.blocked in c.get("add", [])
    ]
    assert len(blocked_calls) == 1
    assert config.labels.supervising in blocked_calls[0]["remove"]

    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 2  # supervisor result + dirty guard
    assert any("dirty_read_only_supervisor" in c["body"] for c in comment_calls)
