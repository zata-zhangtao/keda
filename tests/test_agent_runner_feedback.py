"""Tests for agent runner feedback prompts."""

from __future__ import annotations

from pathlib import Path

from backend.core.shared.models.agent_runner import CommandResult, IssueSummary
from backend.core.use_cases.agent_runner_feedback import (
    build_fix_prompt,
    build_progress_continuation_prompt,
    build_prompt,
    build_recovery_prompt,
)


def _make_issue(number: int = 42) -> IssueSummary:
    return IssueSummary(
        number=number,
        title="Example Issue",
        url=f"https://github.com/example/repo/issues/{number}",
        body="Example body",
        labels=(),
    )


def test_build_fix_prompt_includes_verification_failure() -> None:
    """Fix prompt should contain the failed command and output."""
    issue = _make_issue()
    worktree_path = Path("/worktree")
    verification_results = [
        CommandResult(
            command=("just", "lint"),
            return_code=1,
            stdout="lint failed\n",
            stderr="lint stderr\n",
        )
    ]

    prompt = build_fix_prompt(issue, worktree_path, verification_results=verification_results)

    assert "Fix the verification failure" in prompt
    assert "just lint" in prompt
    assert "lint failed" in prompt
    assert "lint stderr" in prompt


def test_build_fix_prompt_forbids_global_deliverables() -> None:
    """Fix prompt must tell the agent not to touch evidence/PRD/commit request."""
    issue = _make_issue()
    worktree_path = Path("/worktree")
    verification_results = [
        CommandResult(
            command=("ruff", "check"),
            return_code=1,
            stdout="E501\n",
            stderr="",
        )
    ]

    prompt = build_fix_prompt(issue, worktree_path, verification_results=verification_results)

    assert "Do not update evidence files" in prompt
    assert "PRD Acceptance Checklists" in prompt
    assert "commit requests" in prompt


def test_build_fix_prompt_does_not_include_prd_closeout() -> None:
    """Fix prompt should not ask the agent to archive PRDs or update checklists."""
    issue = _make_issue()
    worktree_path = Path("/worktree")
    verification_results = [
        CommandResult(
            command=("ruff", "check"),
            return_code=1,
            stdout="E501\n",
            stderr="",
        )
    ]

    prompt = build_fix_prompt(issue, worktree_path, verification_results=verification_results)

    assert "move the PRD" not in prompt.lower()
    assert "tasks/pending" not in prompt
    assert "tasks/archive" not in prompt
    assert "update the PRD" not in prompt


def test_build_fix_prompt_includes_verification_commands_summary() -> None:
    """Fix prompt should list all verification commands when summary is provided."""
    issue = _make_issue()
    worktree_path = Path("/worktree")
    verification_results = [
        CommandResult(
            command=("ruff", "check"),
            return_code=1,
            stdout="E501\n",
            stderr="",
        )
    ]

    prompt = build_fix_prompt(
        issue,
        worktree_path,
        verification_results=verification_results,
        verification_commands_summary="- `ruff check`\n- `pytest -q`",
    )

    assert "ruff check" in prompt
    assert "pytest -q" in prompt
    assert "first failure stops the chain" in prompt
    assert "project conventions" in prompt


def test_build_recovery_prompt_still_includes_prd_closeout() -> None:
    """Recovery prompt should retain global deliverable responsibilities."""
    issue = _make_issue()
    worktree_path = Path("/worktree")

    prompt = build_recovery_prompt(
        issue,
        worktree_path,
        recovery_attempt=1,
        max_recovery_attempts=2,
        failure_summary="something failed",
    )

    assert "Repair GitHub Issue" in prompt
    assert "something failed" in prompt


def test_build_recovery_prompt_includes_verification_results() -> None:
    """Recovery prompt should include raw verification failures when provided."""
    issue = _make_issue()
    worktree_path = Path("/worktree")
    verification_results = [
        CommandResult(
            command=("pytest", "-q"),
            return_code=1,
            stdout="1 failed\n",
            stderr="",
        )
    ]

    prompt = build_recovery_prompt(
        issue,
        worktree_path,
        recovery_attempt=1,
        max_recovery_attempts=2,
        failure_summary="tests failed",
        verification_results=verification_results,
    )

    assert "pytest -q" in prompt
    assert "1 failed" in prompt
    assert "first failure stops the chain" in prompt
    assert "project conventions" in prompt


def test_build_prompt_includes_verification_commands_summary() -> None:
    """Initial execution prompt should list verification commands and project conventions reminder."""
    issue = _make_issue()
    worktree_path = Path("/worktree")

    class _FakePromptConfig:
        phases = {}

    prompt = build_prompt(
        issue,
        worktree_path,
        _FakePromptConfig(),  # type: ignore[arg-type]
        verification_commands_summary="- `just test`\n- `git diff --check`",
    )

    assert "Verification commands the runner will run before committing" in prompt
    assert "just test" in prompt
    assert "git diff --check" in prompt
    assert "project conventions" in prompt


def test_build_progress_continuation_prompt_includes_failure_context() -> None:
    """Continuation prompt should include previous failure and verification output."""
    issue = _make_issue()
    worktree_path = Path("/worktree")
    verification_results = [
        CommandResult(
            command=("ruff", "check"),
            return_code=1,
            stdout="E501 line too long\n",
            stderr="",
        )
    ]

    prompt = build_progress_continuation_prompt(
        issue,
        worktree_path,
        failure_summary="verification failed",
        verification_results=verification_results,
    )

    assert "Continue GitHub Issue" in prompt
    assert "verification failed" in prompt
    assert "ruff check" in prompt
    assert "E501 line too long" in prompt
    assert "project conventions" in prompt
