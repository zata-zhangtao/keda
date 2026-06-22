"""Tests for the post-PR supervisor cycle."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PostPrSupervisorConfig,
    PullRequestContext,
    RunnerConfig,
    SupervisorActionResult,
)
from backend.core.use_cases.agent_runner_events import parse_latest_event_marker
from backend.core.use_cases.pr_supervisor import (
    _ensure_rebase_context_matches_pr_branch,
    build_conflict_resolution_prompt,
    build_rework_intent_comment,
    build_supervisor_prompt,
    contains_supervisor_decision,
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


def test_parse_supervisor_action_invalid_marks_failed() -> None:
    """Invalid action should fail closed instead of requesting vague human input."""
    text = '{"action": "unknown_action"}'
    result = parse_supervisor_action(text)
    assert result.action == "mark_failed"
    assert "unknown or missing action" in result.summary


def test_parse_supervisor_action_unparseable_marks_failed() -> None:
    """Unparseable supervisor output should not become an empty blocked request."""
    result = parse_supervisor_action("not json")
    assert result.action == "mark_failed"
    assert "not parseable JSON" in result.summary


def test_parse_supervisor_action_empty_human_input_marks_failed() -> None:
    """Human-input requests need an actionable reason before blocking an Issue."""
    text = '{"action": "request_human_input", "summary": ""}'
    result = parse_supervisor_action(text)
    assert result.action == "mark_failed"
    assert "without a summary" in result.summary


def test_contains_supervisor_decision_detects_json_block() -> None:
    """A markdown JSON block with an action field counts as a decision."""
    text = '```json\n{"action": "approve_for_human_review", "summary": "ok"}\n```'
    assert contains_supervisor_decision(text) is True


def test_contains_supervisor_decision_detects_bare_json() -> None:
    """A bare JSON object with an action field counts as a decision."""
    text = 'Some preamble {"action": "mark_failed", "summary": "bad"} trailing'
    assert contains_supervisor_decision(text) is True


def test_contains_supervisor_decision_rejects_crash_output() -> None:
    """Infrastructure crash output without JSON must not count as a decision."""
    assert contains_supervisor_decision("API Error: 400 Invalid request Error") is False
    assert contains_supervisor_decision("") is False
    assert contains_supervisor_decision('{"action": broken json') is False


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


def test_supervisor_action_gate_rewrites_human_input_for_sign_off_only() -> None:
    """A conservative human-input request must become approval when only the
    Realistic Validation sign-off gate is failing (real case: Issue #72 / PR #76)."""
    action_result = SupervisorActionResult(
        action="request_human_input",
        summary="Checks are failing; needs a human to look.",
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/76",
        branch="issue-72",
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
    assert "sign-off gate guard" in gated_result.summary
    assert "Checks are failing; needs a human to look." in gated_result.summary


def test_supervisor_action_gate_keeps_human_input_with_other_failed_checks() -> None:
    """Human-input requests must survive when a real check fails alongside the gate."""
    action_result = SupervisorActionResult(
        action="request_human_input",
        summary="Lint is broken and the fix is unclear.",
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

    assert gated_result.action == "request_human_input"
    assert gated_result.summary == "Lint is broken and the fix is unclear."


def test_supervisor_action_gate_rebases_human_input_when_not_mergeable() -> None:
    """A conflicting PR must be rebased, not parked in blocked where the
    review poll never scans it again (real case: Issue #53 / PR #70)."""
    action_result = SupervisorActionResult(
        action="request_human_input",
        summary="PR has conflicts that need a decision.",
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/70",
        branch="issue-53",
        head_sha="abc123",
        base_sha="def456",
        mergeable=False,
        checks_state="FAILURE",
        checks_summary=(
            "Realistic Validation sign-off (status=COMPLETED, conclusion=FAILURE)",
        ),
    )

    gated_result = guard_supervisor_action_for_pr_state(action_result, pr_context)

    assert gated_result.action == "rebase_pr_branch"
    assert "mergeability gate" in gated_result.summary
    assert "PR has conflicts that need a decision." in gated_result.summary


def test_supervisor_action_gate_rebases_wait_for_checks_when_not_mergeable() -> None:
    """Waiting on checks cannot resolve a conflict; the context-unchanged skip
    would otherwise leave the Issue in supervising forever."""
    action_result = SupervisorActionResult(
        action="wait_for_checks",
        summary="Checks still running.",
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
        mergeable=False,
        checks_state="PENDING",
        checks_summary=("ci/build (status=IN_PROGRESS)",),
    )

    gated_result = guard_supervisor_action_for_pr_state(action_result, pr_context)

    assert gated_result.action == "rebase_pr_branch"
    assert "mergeability gate" in gated_result.summary


def test_supervisor_action_gate_keeps_mark_failed_when_not_mergeable() -> None:
    """mark_failed is a terminal escalation (also the infra-crash fallback);
    rewriting it to rebase would create pointless rework during outages."""
    action_result = SupervisorActionResult(
        action="mark_failed",
        summary="Supervisor agent infrastructure failure: exit 1.",
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
        mergeable=False,
    )

    gated_result = guard_supervisor_action_for_pr_state(action_result, pr_context)

    assert gated_result.action == "mark_failed"
    assert gated_result.summary == "Supervisor agent infrastructure failure: exit 1."


def test_supervisor_action_gate_keeps_repair_actions_when_not_mergeable() -> None:
    """Repair actions already address the conflict and must pass through."""
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
        mergeable=False,
    )

    for action in ("repair_pr_branch", "rebase_pr_branch", "resolve_conflict"):
        action_result = SupervisorActionResult(action=action, summary="Fixing.")
        gated_result = guard_supervisor_action_for_pr_state(action_result, pr_context)
        assert gated_result.action == action
        assert gated_result.summary == "Fixing."


def test_supervisor_action_gate_keeps_human_input_when_checks_pass() -> None:
    """When checks pass, a human-input request is about something else; keep it."""
    action_result = SupervisorActionResult(
        action="request_human_input",
        summary="Requirement ambiguity needs a product decision.",
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
        mergeable=True,
        checks_state="SUCCESS",
        checks_summary=(),
    )

    gated_result = guard_supervisor_action_for_pr_state(action_result, pr_context)

    assert gated_result.action == "request_human_input"
    assert gated_result.summary == "Requirement ambiguity needs a product decision."


def test_supervisor_action_gate_defers_approval_when_checks_pending() -> None:
    """Pending checks must not be approved into human review."""
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
        checks_state="PENDING",
        checks_summary=("ci/build (status=IN_PROGRESS)",),
    )

    gated_result = guard_supervisor_action_for_pr_state(action_result, pr_context)

    assert gated_result.action == "wait_for_checks"
    assert "pending" in gated_result.summary.lower()


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
    assert "wait_for_checks" in prompt


def test_build_supervisor_prompt_explains_sign_off_gate() -> None:
    """Prompt must tell the model the sign-off check is an expected manual gate."""
    issue = IssueSummary(
        number=72,
        title="Test",
        url="https://github.com/example/repo/issues/72",
        body="Do something.",
        labels=(),
    )
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/76",
        branch="issue-72",
        head_sha="abc123",
        base_sha="def456",
        checks_state="FAILURE",
        checks_summary=(
            "Realistic Validation sign-off (status=COMPLETED, conclusion=FAILURE)",
        ),
    )
    prompt = build_supervisor_prompt(
        issue=issue,
        pr_context=pr_context,
        config=AppConfig(),
        process_runner=FakeProcessRunner(),
        worktree_path=Path("."),
        issue_comments=[],
        pr_comments=[],
        base_sha_remote="remote-sha",
    )
    assert "intentional manual gate" in prompt
    assert "approve_for_human_review instead of request_human_input" in prompt


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
    # 空 stdout 触发 fail-closed；最终 action 必须随 marker 记录，
    # 供下一轮 review pass 识别 mark_failed 结局
    assert marker.action == "mark_failed"


def test_run_post_pr_supervisor_cycle_parses_action() -> None:
    """Supervisor cycle should parse agent output into an action result."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()

    class _ActionRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self.agent_capture_output: list[bool] = []

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


class _CrashingAgentRunner(FakeProcessRunner):
    """Fake runner whose agent invocations crash a configurable number of times."""

    def __init__(
        self,
        *,
        crash_count: int,
        crash_stdout: str = "API Error: 400 Invalid request Error",
        success_stdout: str = (
            '{"action": "approve_for_human_review", "summary": "LGTM"}'
        ),
    ) -> None:
        super().__init__()
        self.crash_count = crash_count
        self.crash_stdout = crash_stdout
        self.success_stdout = success_stdout
        self.agent_attempts = 0

    def run(
        self, command, *, cwd, check=True, timeout=None, capture_output=True, label=None
    ):
        if tuple(command)[:1] == ("codex",):
            self.calls.append(list(command))
            self.agent_attempts += 1
            if self.agent_attempts <= self.crash_count:
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=list(command),
                    output=self.crash_stdout,
                    stderr="",
                )
            return CommandResult(
                command=tuple(command),
                return_code=0,
                stdout=self.success_stdout if capture_output else "",
                stderr="",
            )
        return super().run(
            command,
            cwd=cwd,
            check=check,
            timeout=timeout,
            capture_output=capture_output,
        )


def _make_supervised_pr_context() -> PullRequestContext:
    return PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
    )


def _patch_supervisor_sleep(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the supervisor backoff sleep and record requested delays."""
    sleep_delays: list[int] = []
    monkeypatch.setattr(
        "backend.core.use_cases.pr_supervisor.time.sleep",
        lambda seconds: sleep_delays.append(seconds),
    )
    return sleep_delays


def test_run_post_pr_supervisor_cycle_retries_after_agent_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent crash without a JSON decision should retry within the cycle."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    fake_runner = _CrashingAgentRunner(crash_count=1)
    sleep_delays = _patch_supervisor_sleep(monkeypatch)

    result = run_post_pr_supervisor_cycle(
        issue=issue,
        worktree_path=Path("."),
        config=AppConfig(),
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=_make_supervised_pr_context(),
        supervisor_agent="codex",
        cycle=1,
    )

    assert result.action == "approve_for_human_review"
    assert result.summary == "LGTM"
    assert fake_runner.agent_attempts == 2
    assert sleep_delays == [30]


def test_run_post_pr_supervisor_cycle_marks_failed_after_crash_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausted crash retries should mark failed with an infrastructure reason."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    fake_runner = _CrashingAgentRunner(crash_count=10)
    sleep_delays = _patch_supervisor_sleep(monkeypatch)

    result = run_post_pr_supervisor_cycle(
        issue=issue,
        worktree_path=Path("."),
        config=AppConfig(),
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=_make_supervised_pr_context(),
        supervisor_agent="codex",
        cycle=1,
    )

    assert result.action == "mark_failed"
    assert "infrastructure failure" in result.summary
    # 默认 max_agent_crash_retries=5：首次执行 + 5 次重试 = 6 次尝试
    assert fake_runner.agent_attempts == 6
    # 指数退避从 30s 翻倍；最后一次尝试失败后不再等待
    assert sleep_delays == [30, 60, 120, 240, 480]


def test_run_post_pr_supervisor_cycle_crash_backoff_caps_at_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backoff delays must double from the initial value and cap at the maximum."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    fake_runner = _CrashingAgentRunner(crash_count=10)
    sleep_delays = _patch_supervisor_sleep(monkeypatch)
    config = AppConfig()
    config = replace(
        config,
        post_pr_supervisor=replace(
            config.post_pr_supervisor,
            max_agent_crash_retries=7,
        ),
    )

    result = run_post_pr_supervisor_cycle(
        issue=issue,
        worktree_path=Path("."),
        config=config,
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=_make_supervised_pr_context(),
        supervisor_agent="codex",
        cycle=1,
    )

    assert result.action == "mark_failed"
    assert fake_runner.agent_attempts == 8
    # 30 * 2**6 = 1920 超出上限，被封顶为 600
    assert sleep_delays == [30, 60, 120, 240, 480, 600, 600]


def test_run_post_pr_supervisor_cycle_uses_decision_from_crashed_agent() -> None:
    """A non-zero exit that still printed a JSON decision must use it, no retry."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    fake_runner = _CrashingAgentRunner(
        crash_count=10,
        crash_stdout=(
            '```json\n{"action": "wait_for_checks", "summary": "checks pending"}\n```'
        ),
    )

    result = run_post_pr_supervisor_cycle(
        issue=issue,
        worktree_path=Path("."),
        config=AppConfig(),
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=_make_supervised_pr_context(),
        supervisor_agent="codex",
        cycle=1,
    )

    assert result.action == "wait_for_checks"
    assert fake_runner.agent_attempts == 1


def test_run_post_pr_supervisor_cycle_clean_exit_garbage_fails_without_retry() -> None:
    """A clean agent exit with unparseable output keeps fail-closed, no retry."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_client = FakeGitHubClient()
    fake_runner = _CrashingAgentRunner(
        crash_count=0,
        success_stdout="I could not decide, sorry.",
    )

    result = run_post_pr_supervisor_cycle(
        issue=issue,
        worktree_path=Path("."),
        config=AppConfig(),
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=_make_supervised_pr_context(),
        supervisor_agent="codex",
        cycle=1,
    )

    assert result.action == "mark_failed"
    assert "not parseable JSON" in result.summary
    assert fake_runner.agent_attempts == 1


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
            ("git", "-c", "core.editor=true", "rebase", "--continue"): CommandResult(
                command=("git", "-c", "core.editor=true", "rebase", "--continue"),
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
    assert ("git", "-c", "core.editor=true", "rebase", "--continue") in commands
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
            ("git", "-c", "core.editor=true", "rebase", "--continue"): CommandResult(
                command=("git", "-c", "core.editor=true", "rebase", "--continue"),
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
    assert (
        commands.count(("git", "-c", "core.editor=true", "rebase", "--continue")) == 2
    )


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
    assert ("git", "-c", "core.editor=true", "rebase", "--continue") not in commands


def test_dirty_worktree_before_supervisor_stash_fails_blocked(tmp_path: Path) -> None:
    """When auto-stash fails, dirty worktree still moves the Issue to blocked."""
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
            (
                "git",
                "stash",
                "push",
                "-u",
                "-m",
                "iar: auto-stash before supervisor cycle 1",
            ): CommandResult(
                command=(
                    "git",
                    "stash",
                    "push",
                    "-u",
                    "-m",
                    "iar: auto-stash before supervisor cycle 1",
                ),
                return_code=1,
                stdout="",
                stderr="stash failed",
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
    fake_client._issue_labels[issue.number] = ("agent/supervising",)

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


def test_dirty_worktree_before_supervisor_auto_stash_and_approve(
    tmp_path: Path,
) -> None:
    """Dirty worktree is auto-stashed, supervisor approves, and changes are restored."""
    from backend.core.use_cases.agent_runner_supervisor import (
        _run_supervisor_with_repair_loop,
    )

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()

    class _StashThenApproveRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._status_calls = 0

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
            self.calls.append(list(command))
            if command_tuple == ("git", "status", "--porcelain"):
                self._status_calls += 1
                # Before stash: dirty; after stash (and after pop): clean
                stdout = " M file.py\n" if self._status_calls == 1 else ""
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("git", "stash", "pop"):
                return CommandResult(command_tuple, 0, "", "")
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

    fake_runner = _StashThenApproveRunner()
    fake_client = FakeGitHubClient()
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
    )
    config = AppConfig()
    fake_client._issue_labels[issue.number] = ("agent/supervising",)

    _run_supervisor_with_repair_loop(
        issue=issue,
        worktree_path=worktree_path,
        config=config,
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=pr_context,
        supervisor_agent="codex",
    )

    review_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.review in c.get("add", [])
    ]
    assert len(review_calls) == 1
    assert config.labels.supervising in review_calls[0]["remove"]

    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert any("dirty_worktree_auto_stashed" in c["body"] for c in comment_calls)
    assert any("approve_for_human_review" in c["body"] for c in comment_calls)

    # Stash and pop should both have been invoked
    commands = [tuple(c) for c in fake_runner.calls]
    assert (
        "git",
        "stash",
        "push",
        "-u",
        "-m",
        "iar: auto-stash before supervisor cycle 1",
    ) in commands
    assert ("git", "stash", "pop") in commands


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
    fake_client._issue_labels[issue.number] = ("agent/supervising",)

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


def test_supervisor_loop_waits_for_pending_checks_once(tmp_path: Path) -> None:
    """Pending checks should stay supervising and write one audit comment."""
    from backend.core.use_cases.agent_runner_supervisor import (
        _run_supervisor_with_repair_loop,
    )

    issue = IssueSummary(
        number=1,
        title="T",
        url="U",
        body="B",
        labels=("agent/supervising",),
    )
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()

    class _ApproveRunner(FakeProcessRunner):
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
            self.calls.append(list(command))
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, "", "")
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

    fake_runner = _ApproveRunner()
    fake_client = FakeGitHubClient()
    fake_client._issue_labels[issue.number] = issue.labels
    pr_context = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-1",
        head_sha="abc123",
        base_sha="def456",
        checks_state="PENDING",
        checks_summary=("ci/build (status=IN_PROGRESS)",),
    )

    _run_supervisor_with_repair_loop(
        issue=issue,
        worktree_path=worktree_path,
        config=AppConfig(),
        github_client=fake_client,
        process_runner=fake_runner,
        pr_context=pr_context,
        supervisor_agent="codex",
    )

    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    assert len(comment_calls) == 1
    assert "Action: wait_for_checks" in comment_calls[0]["body"]
    assert "ci/build" in comment_calls[0]["body"]

    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert label_calls == []


def test_execute_rebase_allows_detached_head_when_active_rebase_target_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detached HEAD during rebase is allowed when metadata confirms target branch."""
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

    rebase_merge_dir = worktree_path / ".git" / "rebase-merge"
    rebase_merge_dir.mkdir(parents=True, exist_ok=True)
    head_name_path = rebase_merge_dir / "head-name"
    head_name_path.write_text("refs/heads/issue-1", encoding="utf-8")

    class _DetachedHeadRunner(FakeProcessRunner):
        def __init__(self, worktree_path: Path, responses: dict) -> None:
            super().__init__(responses=responses)
            self._worktree_path = worktree_path
            self._branch_show_current_calls = 0

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            input_text=None,
        ):
            command_tuple = tuple(command)
            if command_tuple == ("git", "branch", "--show-current"):
                self._branch_show_current_calls += 1
                self.calls.append(list(command))
                self.input_texts.append(input_text)
                if self._branch_show_current_calls == 1:
                    return CommandResult(command_tuple, 0, "issue-1\n", "")
                return CommandResult(command_tuple, 0, "", "")

            if command_tuple == (
                "git",
                "rev-parse",
                "--git-path",
                "rebase-merge/head-name",
            ):
                self.calls.append(list(command))
                self.input_texts.append(input_text)
                head_name_path = (
                    self._worktree_path / ".git" / "rebase-merge" / "head-name"
                )
                return CommandResult(command_tuple, 0, str(head_name_path) + "\n", "")

            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
                input_text=input_text,
            )

    fake_runner = _DetachedHeadRunner(
        worktree_path=worktree_path,
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
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
            ("git", "add", "-A"): CommandResult(
                command=("git", "add", "-A"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "-c", "core.editor=true", "rebase", "--continue"): CommandResult(
                command=("git", "-c", "core.editor=true", "rebase", "--continue"),
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
        },
    )

    def _noop_run_agent(
        agent_name,
        prompt,
        worktree_path,
        process_runner,
        *,
        capture_output=False,
        timeout_seconds=None,
    ):
        return CommandResult(command=("noop",), return_code=0, stdout="", stderr="")

    monkeypatch.setattr(
        "backend.core.use_cases.pr_supervisor.run_agent_with_prompt",
        _noop_run_agent,
    )

    config = AppConfig(
        post_pr_supervisor=PostPrSupervisorConfig(max_repair_attempts=1),
        runner=RunnerConfig(verification_commands=()),
    )
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
    assert ("git", "-c", "core.editor=true", "rebase", "--continue") in commands
    assert ("git", "push", "--force-with-lease", "origin", "issue-1") in commands
    assert ("git", "commit", "-m", "resolve conflict") not in commands
    assert ("git", "rebase", "--abort") not in commands


def test_execute_rebase_rejects_mismatched_active_rebase_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detached HEAD with mismatched rebase metadata must raise immediately."""
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

    rebase_merge_dir = worktree_path / ".git" / "rebase-merge"
    rebase_merge_dir.mkdir(parents=True, exist_ok=True)
    head_name_path = rebase_merge_dir / "head-name"
    head_name_path.write_text("refs/heads/issue-99", encoding="utf-8")

    class _MismatchedRunner(FakeProcessRunner):
        def __init__(self, worktree_path: Path, responses: dict) -> None:
            super().__init__(responses=responses)
            self._worktree_path = worktree_path
            self._branch_show_current_calls = 0

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            input_text=None,
        ):
            command_tuple = tuple(command)
            if command_tuple == ("git", "branch", "--show-current"):
                self._branch_show_current_calls += 1
                self.calls.append(list(command))
                self.input_texts.append(input_text)
                if self._branch_show_current_calls == 1:
                    return CommandResult(command_tuple, 0, "issue-1\n", "")
                return CommandResult(command_tuple, 0, "", "")

            if command_tuple == (
                "git",
                "rev-parse",
                "--git-path",
                "rebase-merge/head-name",
            ):
                self.calls.append(list(command))
                self.input_texts.append(input_text)
                head_name_path = (
                    self._worktree_path / ".git" / "rebase-merge" / "head-name"
                )
                return CommandResult(command_tuple, 0, str(head_name_path) + "\n", "")

            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
                input_text=input_text,
            )

    fake_runner = _MismatchedRunner(
        worktree_path=worktree_path,
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
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
            ("git", "diff", "--name-only", "--diff-filter=U"): CommandResult(
                command=("git", "diff", "--name-only", "--diff-filter=U"),
                return_code=0,
                stdout="file.py\n",
                stderr="",
            ),
        },
    )

    def _noop_run_agent(
        agent_name,
        prompt,
        worktree_path,
        process_runner,
        *,
        capture_output=False,
        timeout_seconds=None,
    ):
        return CommandResult(command=("noop",), return_code=0, stdout="", stderr="")

    monkeypatch.setattr(
        "backend.core.use_cases.pr_supervisor.run_agent_with_prompt",
        _noop_run_agent,
    )

    config = AppConfig(
        post_pr_supervisor=PostPrSupervisorConfig(max_repair_attempts=1),
        runner=RunnerConfig(verification_commands=()),
    )
    with pytest.raises(
        RuntimeError,
        match="active rebase target 'issue-99' does not match expected PR branch 'issue-1'",
    ):
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
    assert ("git", "add", "-A") not in commands
    assert ("git", "-c", "core.editor=true", "rebase", "--continue") not in commands
    assert ("git", "push", "--force-with-lease", "origin", "issue-1") not in commands
    assert ("git", "rebase", "--abort") not in commands


def test_execute_rebase_rejects_unknown_active_rebase_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detached HEAD with no rebase metadata must raise immediately."""
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

    class _UnknownTargetRunner(FakeProcessRunner):
        def __init__(self, worktree_path: Path, responses: dict) -> None:
            super().__init__(responses=responses)
            self._worktree_path = worktree_path
            self._branch_show_current_calls = 0

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            input_text=None,
        ):
            command_tuple = tuple(command)
            if command_tuple == ("git", "branch", "--show-current"):
                self._branch_show_current_calls += 1
                self.calls.append(list(command))
                self.input_texts.append(input_text)
                if self._branch_show_current_calls == 1:
                    return CommandResult(command_tuple, 0, "issue-1\n", "")
                return CommandResult(command_tuple, 0, "", "")

            if command_tuple == (
                "git",
                "rev-parse",
                "--git-path",
                "rebase-merge/head-name",
            ):
                self.calls.append(list(command))
                self.input_texts.append(input_text)
                nonexistent_path = (
                    self._worktree_path / ".git" / "rebase-merge" / "head-name"
                )
                return CommandResult(command_tuple, 0, str(nonexistent_path) + "\n", "")

            if command_tuple == (
                "git",
                "rev-parse",
                "--git-path",
                "rebase-apply/head-name",
            ):
                self.calls.append(list(command))
                self.input_texts.append(input_text)
                nonexistent_path = (
                    self._worktree_path / ".git" / "rebase-apply" / "head-name"
                )
                return CommandResult(command_tuple, 0, str(nonexistent_path) + "\n", "")

            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
                input_text=input_text,
            )

    fake_runner = _UnknownTargetRunner(
        worktree_path=worktree_path,
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123\n",
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
            ("git", "diff", "--name-only", "--diff-filter=U"): CommandResult(
                command=("git", "diff", "--name-only", "--diff-filter=U"),
                return_code=0,
                stdout="file.py\n",
                stderr="",
            ),
        },
    )

    def _noop_run_agent(
        agent_name,
        prompt,
        worktree_path,
        process_runner,
        *,
        capture_output=False,
        timeout_seconds=None,
    ):
        return CommandResult(command=("noop",), return_code=0, stdout="", stderr="")

    monkeypatch.setattr(
        "backend.core.use_cases.pr_supervisor.run_agent_with_prompt",
        _noop_run_agent,
    )

    config = AppConfig(
        post_pr_supervisor=PostPrSupervisorConfig(max_repair_attempts=1),
        runner=RunnerConfig(verification_commands=()),
    )
    with pytest.raises(
        RuntimeError,
        match="current branch is empty and active rebase target cannot be confirmed",
    ):
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
    assert ("git", "add", "-A") not in commands
    assert ("git", "-c", "core.editor=true", "rebase", "--continue") not in commands
    assert ("git", "push", "--force-with-lease", "origin", "issue-1") not in commands
    assert ("git", "rebase", "--abort") not in commands


def test_execute_rebase_conflict_path_does_not_run_git_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebase conflict resolution must use --continue, never git commit."""
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
            ("git", "add", "-A"): CommandResult(
                command=("git", "add", "-A"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "-c", "core.editor=true", "rebase", "--continue"): CommandResult(
                command=("git", "-c", "core.editor=true", "rebase", "--continue"),
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

    def _noop_run_agent(
        agent_name,
        prompt,
        worktree_path,
        process_runner,
        *,
        capture_output=False,
        timeout_seconds=None,
    ):
        return CommandResult(command=("noop",), return_code=0, stdout="", stderr="")

    monkeypatch.setattr(
        "backend.core.use_cases.pr_supervisor.run_agent_with_prompt",
        _noop_run_agent,
    )

    config = AppConfig(
        post_pr_supervisor=PostPrSupervisorConfig(max_repair_attempts=1),
        runner=RunnerConfig(verification_commands=()),
    )
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
    assert ("git", "-c", "core.editor=true", "rebase", "--continue") in commands
    assert ("git", "push", "--force-with-lease", "origin", "issue-1") in commands
    assert not any(c[:2] == ("git", "commit") for c in commands)


def test_execute_rebase_real_git_conflict_allows_detached_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real git rebase conflict with detached HEAD resolves successfully."""
    import json
    import subprocess

    from backend.infrastructure.process_runner import SubprocessRunner

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    # CI environments may not have an editor configured; rebase --continue
    # should reuse the original commit message without prompting.
    subprocess.run(
        ["git", "config", "core.editor", "true"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    shared_file = repo_path / "shared.txt"
    shared_file.write_text("base content", encoding="utf-8")
    subprocess.run(
        ["git", "add", "shared.txt"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "checkout", "-b", "issue-73"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    shared_file.write_text("pr content", encoding="utf-8")
    subprocess.run(
        ["git", "add", "shared.txt"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "pr commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "checkout", "main"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    shared_file.write_text("main updated content", encoding="utf-8")
    subprocess.run(
        ["git", "add", "shared.txt"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "main update"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "checkout", "issue-73"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    rebase_result = subprocess.run(
        ["git", "rebase", "main"],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rebase_result.returncode != 0, "Expected rebase to conflict"

    shared_file.write_text("resolved content", encoding="utf-8")
    subprocess.run(
        ["git", "add", "shared.txt"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    branch_result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch_result.stdout.strip() == "", "Expected detached HEAD during rebase"

    request_path = repo_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps({"commit_message": "resolve conflict"}),
        encoding="utf-8",
    )

    head_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    expected_head = head_result.stdout.strip()

    def _noop_run_agent(
        agent_name,
        prompt,
        worktree_path,
        process_runner,
        *,
        capture_output=False,
        timeout_seconds=None,
    ):
        return CommandResult(command=("noop",), return_code=0, stdout="", stderr="")

    monkeypatch.setattr(
        "backend.core.use_cases.pr_supervisor.run_agent_with_prompt",
        _noop_run_agent,
    )

    class _NoOpFetchRebasePushRunner:
        def __init__(self, delegate: SubprocessRunner) -> None:
            self._delegate = delegate

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            capture_output=True,
            input_text=None,
        ):
            cmd = list(command)
            if (
                len(cmd) >= 4
                and cmd[0] == "git"
                and cmd[1] == "fetch"
                and cmd[2] == "origin"
                and cmd[3] == "main"
            ):
                return CommandResult(tuple(cmd), 0, "", "")
            # The real rebase state is already set up manually above.
            # execute_rebase must not run a second 'git rebase origin/main'
            # while a rebase is in progress, because Git aborts the active
            # rebase on some versions/platforms when the upstream is invalid.
            # Return a synthetic conflict so the conflict recovery loop runs.
            if (
                len(cmd) >= 3
                and cmd[0] == "git"
                and cmd[1] == "rebase"
                and cmd[2] == "origin/main"
            ):
                return CommandResult(
                    tuple(cmd),
                    1,
                    "CONFLICT (content): Merge conflict in shared.txt",
                    "",
                )
            if (
                len(cmd) >= 5
                and cmd[0] == "git"
                and cmd[1] == "push"
                and cmd[2] == "--force-with-lease"
                and cmd[3] == "origin"
                and cmd[4] == "issue-73"
            ):
                return CommandResult(tuple(cmd), 0, "", "")
            return self._delegate.run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
                input_text=input_text,
            )

    process_runner = _NoOpFetchRebasePushRunner(SubprocessRunner())

    config = AppConfig(
        post_pr_supervisor=PostPrSupervisorConfig(max_repair_attempts=1),
        runner=RunnerConfig(verification_commands=("true",)),
    )

    issue = IssueSummary(number=73, title="T", url="U", body="B", labels=())
    execute_rebase(
        issue=issue,
        worktree_path=repo_path,
        config=config,
        process_runner=process_runner,
        pr_branch="issue-73",
        expected_head=expected_head,
        supervisor_agent="codex",
    )

    assert shared_file.read_text(encoding="utf-8") == "resolved content"

    branch_after = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch_after.stdout.strip() == "issue-73"

    log_result = subprocess.run(
        ["git", "log", "--oneline", "issue-73"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "main update" in log_result.stdout
    assert "pr commit" in log_result.stdout


def test_ensure_rebase_context_rejects_mismatched_target(tmp_path: Path) -> None:
    """_ensure_rebase_context_matches_pr_branch should raise for wrong target."""
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()
    rebase_merge_dir = worktree_path / ".git" / "rebase-merge"
    rebase_merge_dir.mkdir(parents=True, exist_ok=True)
    head_name_path = rebase_merge_dir / "head-name"
    head_name_path.write_text("refs/heads/issue-99", encoding="utf-8")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            (
                "git",
                "rev-parse",
                "--git-path",
                "rebase-merge/head-name",
            ): CommandResult(
                command=("git", "rev-parse", "--git-path", "rebase-merge/head-name"),
                return_code=0,
                stdout=str(head_name_path) + "\n",
                stderr="",
            ),
        }
    )
    with pytest.raises(
        RuntimeError,
        match="active rebase target 'issue-99' does not match expected PR branch 'issue-1'",
    ):
        _ensure_rebase_context_matches_pr_branch(
            worktree_path=worktree_path,
            process_runner=fake_runner,
            pr_branch="issue-1",
        )
