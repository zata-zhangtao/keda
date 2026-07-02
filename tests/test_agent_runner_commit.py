"""Tests for the agent runner commit proxy."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    RunnerConfig,
)
from backend.core.use_cases.agent_runner_commit import commit_requested_changes
from backend.core.use_cases.agent_runner_feedback import VerificationFailedError
from tests.conftest import FakeProcessRunner


def _make_issue(number: int = 123) -> IssueSummary:
    return IssueSummary(
        number=number,
        title="Example",
        url=f"https://github.com/example/repo/issues/{number}",
        body="Example body",
        labels=(),
    )


def _write_commit_request(worktree_path: Path, commit_message: str) -> None:
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        f'{{"commit_message": "{commit_message}"}}\n', encoding="utf-8"
    )


def test_commit_requested_changes_raises_on_verification_failure(
    tmp_path: Path,
) -> None:
    """Verification failures should raise VerificationFailedError."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                ("git", "branch", "--show-current"), 0, "issue-123\n", ""
            ),
            ("git", "status", "--porcelain"): CommandResult(
                ("git", "status", "--porcelain"), 0, " M src/example.py\n", ""
            ),
            ("ruff", "check"): CommandResult(
                ("ruff", "check"),
                1,
                "src/example.py:1:1: E501 Line too long\n",
                "",
            ),
        }
    )
    config = AppConfig(runner=RunnerConfig(verification_commands=("ruff check",)))

    with pytest.raises(VerificationFailedError):
        commit_requested_changes(
            _make_issue(),
            worktree_path,
            config,
            fake_runner,
            expected_branch="issue-123",
        )
