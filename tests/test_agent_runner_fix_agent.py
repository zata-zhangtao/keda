"""Tests for the ``run_agent_until_committed`` escalation ladder.

Covers the Fix Agent / Recovery Agent escalation and the timeout
budget each stage runs under."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    FailureType,
    IssueSummary,
    RunnerConfig,
)
from backend.core.use_cases.run_agent_once import (
    MaxRetriesExceededError,
    run_agent_until_committed,
)
from tests.conftest import FakeProcessRunner
from tests.support.agent_runner import (
    is_bash_wrapped_verification_call,
    write_commit_request,
)


def test_run_agent_until_committed_enters_recovery_after_timeout(
    tmp_path: Path,
) -> None:
    """TimeoutExpired during agent execution should be caught and retried."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="https://github.com/example/repo/issues/1",
        body="Body",
        labels=(),
    )

    class _TimeoutRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            inactivity_timeout=None,
            capture_output=True,
            input_text=None,
            label=None,
        ):
            self.attempts += 1
            raise subprocess.TimeoutExpired(cmd=list(command), timeout=timeout)

    fake_runner = _TimeoutRunner()
    config = AppConfig(
        runner=RunnerConfig(
            max_recovery_attempts=1,
            timeout_seconds=5,
            inactivity_timeout_seconds=1,
            recovery_retry_delay_seconds=0,
        )
    )

    with pytest.raises(MaxRetriesExceededError):
        run_agent_until_committed(
            selected_agent="claude",
            issue=issue,
            worktree_path=tmp_path,
            config=config,
            process_runner=fake_runner,
            before_sha="abc123",
            expected_branch="issue-1",
        )

    assert fake_runner.attempts == 2


def test_run_fix_agent_uses_fix_timeout_seconds(tmp_path: Path) -> None:
    """run_fix_agent should pass fix_timeout_seconds to the process runner."""
    from backend.core.use_cases.run_agent_once import run_fix_agent

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_runner = FakeProcessRunner()
    config = AppConfig(
        runner=RunnerConfig(
            timeout_seconds=100,
            fix_timeout_seconds=30,
            agent_fallback_order=(),
        )
    )

    run_fix_agent(
        "claude",
        issue,
        tmp_path,
        config,
        fake_runner,
        verification_results=[
            CommandResult(("just", "lint"), 1, "fail", ""),
        ],
    )

    assert fake_runner.timeouts == [30]


def test_run_fix_agent_falls_back_to_timeout_seconds(tmp_path: Path) -> None:
    """run_fix_agent should fall back to timeout_seconds when fix timeout is unset."""
    from backend.core.use_cases.run_agent_once import run_fix_agent

    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    fake_runner = FakeProcessRunner()
    config = AppConfig(
        runner=RunnerConfig(
            timeout_seconds=100,
            fix_timeout_seconds=None,
            agent_fallback_order=(),
        )
    )

    run_fix_agent(
        "claude",
        issue,
        tmp_path,
        config,
        fake_runner,
        verification_results=[
            CommandResult(("just", "lint"), 1, "fail", ""),
        ],
    )

    assert fake_runner.timeouts == [100]


def test_run_agent_until_committed_calls_fix_agent_before_recovery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Staged verification failure should trigger Fix Agent before full recovery."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="https://github.com/example/repo/issues/1",
        body="Body",
        labels=(),
    )
    worktree_path = tmp_path / "issue-1"
    worktree_path.mkdir()
    write_commit_request(worktree_path, "agent: initial")

    class _FixThenRecoveryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._agent_calls = 0
            self._verification_calls = 0
            self._committed = False

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            inactivity_timeout=None,
            capture_output=True,
            input_text=None,
            label=None,
        ):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("claude",):
                self._agent_calls += 1
                self.timeouts.append(timeout)
                self.inactivity_timeouts.append(inactivity_timeout)
                prompt = command_tuple[-1]
                if self._agent_calls == 1:
                    # Normal agent: leave changes and a failing commit-request.
                    return CommandResult(command_tuple, 0, "", "")
                if self._agent_calls == 2:
                    # Fix Agent: assert it received a focused prompt.
                    assert "Fix the verification failure" in prompt
                    assert "just lint" in prompt
                    assert "Do not update evidence files" in prompt
                    write_commit_request(worktree_path, "agent: fixed")
                    return CommandResult(command_tuple, 0, "", "")
                # Recovery agent should not be reached.
                raise AssertionError("Recovery agent should not be invoked")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                return CommandResult(
                    command_tuple,
                    0,
                    "after-sha\n" if self._committed else "before-sha\n",
                    "",
                )
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-1\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                stdout = "" if self._committed else " M file.txt\n"
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("just", "lint") or is_bash_wrapped_verification_call(
                command, ("just", "lint")
            ):
                self._verification_calls += 1
                # Pass pre-staging verification (call 1), fail staged verification
                # inside commit proxy (call 2), then pass after Fix Agent (call 3).
                if self._verification_calls == 2:
                    return CommandResult(command_tuple, 1, "lint stdout\n", "lint stderr\n")
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "commit", "-m", "agent: fixed"):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _FixThenRecoveryRunner()
    config = AppConfig(
        runner=RunnerConfig(
            max_recovery_attempts=1,
            recovery_retry_delay_seconds=0,
            timeout_seconds=100,
            fix_timeout_seconds=30,
            verification_commands=("just lint",),
            agent_fallback_order=(),
        )
    )

    with caplog.at_level(logging.INFO, logger="backend.core.use_cases.run_agent_once"):
        result = run_agent_until_committed(
            selected_agent="claude",
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=fake_runner,
            before_sha="before-sha",
            expected_branch="issue-1",
        )

    assert result.attempt_results[-1].failure_type == FailureType.SUCCESS
    agent_commands = [c for c in fake_runner.calls if tuple(c)[:1] == ("claude",)]
    assert len(agent_commands) == 2
    assert fake_runner.timeouts[1] == 30
    assert "Starting Fix Agent for Issue #1 (timeout=30s)." in caplog.text
    assert "Fix Agent repaired staged verification failure for Issue #1" in caplog.text


def test_run_agent_until_committed_skips_fix_agent_when_disabled(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """fix_agent_enabled=False should escalate staged failures without a Fix Agent."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="https://github.com/example/repo/issues/1",
        body="Body",
        labels=(),
    )
    worktree_path = tmp_path / "issue-1"
    worktree_path.mkdir()
    write_commit_request(worktree_path, "agent: initial")

    class _StagedFailureRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._verification_calls = 0

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            inactivity_timeout=None,
            capture_output=True,
            input_text=None,
            label=None,
        ):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple[:1] == ("claude",):
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                return CommandResult(command_tuple, 0, "before-sha\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-1\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, " M file.txt\n", "")
            if command_tuple == ("just", "lint") or is_bash_wrapped_verification_call(
                command, ("just", "lint")
            ):
                self._verification_calls += 1
                # Pass pre-staging verification (call 1), fail staged
                # verification inside the commit proxy (call 2).
                if self._verification_calls == 2:
                    return CommandResult(command_tuple, 1, "lint stdout\n", "")
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _StagedFailureRunner()
    config = AppConfig(
        runner=RunnerConfig(
            max_recovery_attempts=0,
            recovery_retry_delay_seconds=0,
            timeout_seconds=100,
            fix_agent_enabled=False,
            fix_timeout_seconds=30,
            verification_commands=("just lint",),
            agent_fallback_order=(),
        )
    )

    with caplog.at_level(logging.INFO, logger="backend.core.use_cases.run_agent_once"):
        with pytest.raises(MaxRetriesExceededError):
            run_agent_until_committed(
                selected_agent="claude",
                issue=issue,
                worktree_path=worktree_path,
                config=config,
                process_runner=fake_runner,
                before_sha="before-sha",
                expected_branch="issue-1",
            )

    agent_commands = [c for c in fake_runner.calls if tuple(c)[:1] == ("claude",)]
    assert len(agent_commands) == 1
    assert all("Fix the verification failure" not in tuple(c)[-1] for c in agent_commands)
    assert "Fix Agent disabled for Issue #1" in caplog.text
    assert "Starting Fix Agent" not in caplog.text


def test_run_agent_until_committed_recovery_uses_recovery_timeout_seconds(
    tmp_path: Path,
) -> None:
    """Recovery agent invocations should use recovery_timeout_seconds."""
    issue = IssueSummary(
        number=1,
        title="T",
        url="https://github.com/example/repo/issues/1",
        body="Body",
        labels=(),
    )
    worktree_path = tmp_path / "issue-1"
    worktree_path.mkdir()

    class _RecoveryTimeoutRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._agent_calls = 0

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            inactivity_timeout=None,
            capture_output=True,
            input_text=None,
            label=None,
        ):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("claude",):
                self._agent_calls += 1
                self.timeouts.append(timeout)
                self.inactivity_timeouts.append(inactivity_timeout)
                if self._agent_calls == 1:
                    return CommandResult(command_tuple, 0, "", "")
                # Recovery attempt.
                assert "Recovery attempt: 1/1" in command_tuple[-1]
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                return CommandResult(command_tuple, 0, "same-sha\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _RecoveryTimeoutRunner()
    config = AppConfig(
        runner=RunnerConfig(
            max_recovery_attempts=1,
            recovery_retry_delay_seconds=0,
            timeout_seconds=100,
            recovery_timeout_seconds=60,
            verification_commands=("git diff --check",),
            agent_fallback_order=(),
        )
    )

    with pytest.raises(MaxRetriesExceededError):
        run_agent_until_committed(
            selected_agent="claude",
            issue=issue,
            worktree_path=worktree_path,
            config=config,
            process_runner=fake_runner,
            before_sha="same-sha",
            expected_branch="issue-1",
        )

    claude_indices = [
        index for index, call in enumerate(fake_runner.calls) if tuple(call)[:1] == ("claude",)
    ]
    assert len(claude_indices) == 2
    assert fake_runner.timeouts[1] == 60
