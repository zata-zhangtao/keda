"""Tests for the verification recovery loop driven by ``run_once``.

Covers retry-until-success, max-retries exhaustion, the pre-commit lint
recovery scenarios, attempt history reporting and the KeyboardInterrupt
checkpoint on the way out."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    CommandResult,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner
from tests.support.agent_runner import (
    config_with_review_disabled,
    git_remote_command,
    git_remote_result,
    is_bash_wrapped_verification_call,
    make_ready_issue,
    worktree_path_response,
    write_commit_request,
)


def test_recovery_loop_success_on_second_attempt(tmp_path: Path) -> None:
    """Runner should succeed when recovery agent fixes the issue on attempt 2."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _RecoverySuccessRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._agent_calls = 0
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
            label=None,
        ):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                if self._agent_calls == 1:
                    # First attempt: produce no commits
                    return CommandResult(command_tuple, 0, "", "")
                if self._agent_calls == 2:
                    # Recovery: write commit request and succeed
                    write_commit_request(worktree_path, "agent: recovered")
                    return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                stdout = "" if self._committed else " M file.txt\n"
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("git", "commit", "-m", "agent: recovered"):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _RecoverySuccessRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        git_remote_command(): git_remote_result("origin"),
    }
    config = config_with_review_disabled(worktree_path)

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    commands = [tuple(command) for command in fake_runner.calls]
    agent_commands = [command for command in commands if command[:1] == ("codex",)]
    assert len(agent_commands) == 2
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    implementation_comment = [
        c for c in comment_calls if "Implementation Complete" in c.get("body", "")
    ]
    assert len(implementation_comment) == 1
    assert "Attempt History" in implementation_comment[0]["body"]


def test_recovery_loop_exhausted_raises_max_retries(tmp_path: Path) -> None:
    """Runner should fail with MaxRetriesExceededError when all attempts fail."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _ExhaustedRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0

        def run(
            self,
            command,
            *,
            cwd,
            check=True,
            timeout=None,
            inactivity_timeout=None,
            capture_output=True,
            label=None,
        ):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                return self.responses[command_tuple]
            if command_tuple[:1] == ("codex",):
                # Always produce no commits
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                return CommandResult(command_tuple, 0, "same-sha\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _ExhaustedRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        git_remote_command(): git_remote_result("origin"),
    }
    config = config_with_review_disabled(
        worktree_path, max_recovery_attempts=1, recovery_retry_delay_seconds=0
    )

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels" and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    failure_comment = comment_calls[-1]
    assert "Attempt History" in failure_comment["body"]
    assert "Failed after 2 attempts" in failure_comment["body"]
    assert "no_commits" in failure_comment["body"]


def test_attempt_history_in_issue_comment(tmp_path: Path) -> None:
    """Successful run should include Attempt History in the implementation comment."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _HistoryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
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
            label=None,
        ):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                if self._agent_calls == 1:
                    # First attempt fails verification
                    return CommandResult(command_tuple, 0, "", "")
                if self._agent_calls == 2:
                    # Recovery succeeds
                    write_commit_request(worktree_path, "agent: fix")
                    return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                stdout = " M file.txt\n" if self._agent_calls < 2 else ""
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("git", "commit", "-m", "agent: fix"):
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _HistoryRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        git_remote_command(): git_remote_result("origin"),
    }
    config = config_with_review_disabled(worktree_path, "echo ok")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    implementation_comment = [
        c for c in comment_calls if "Implementation Complete" in c.get("body", "")
    ]
    assert len(implementation_comment) == 1
    body = implementation_comment[0]["body"]
    assert "Attempt History" in body
    assert "success" in body
    assert "| 1 |" in body
    assert "| 2 |" in body


def test_scenario_b_precommit_lint_failure_recovery(tmp_path: Path) -> None:
    """Scene B: Agent committed, just lint failed, recovery fixed, 2nd pass.

    Steps:
    1. Agent writes commit-request (runner will stage and commit on its behalf).
    2. Runner stages with ``git add -A``.
    3. ``just lint`` returns non-zero -> VERIFICATION_FAILED.
    4. Runner injects stderr into recovery prompt.
    5. Recovery agent fixes and writes new commit-request.
    6. Runner re-stages, re-runs ``just lint`` -> passes.
    7. Runner commits and publishes.
    """
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    write_commit_request(worktree_path, "agent: initial attempt")

    class _LintRecoveryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._lint_calls = 0
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
            label=None,
        ):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("codex",):
                prompt = command_tuple[-1]
                if "Recovery attempt: 1/2" in prompt:
                    assert (
                        "Verification after runner staged changes with git add -A failed" in prompt
                    )
                    assert "lint stdout" in prompt
                    assert "lint stderr" in prompt
                    write_commit_request(worktree_path, "agent: fix lint")
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                stdout = "" if self._committed else " M file.txt\n"
                return CommandResult(command_tuple, 0, stdout, "")
            if command_tuple == ("just", "lint") or is_bash_wrapped_verification_call(
                command, ("just", "lint")
            ):
                self._lint_calls += 1
                if self._lint_calls == 2:
                    return CommandResult(
                        command_tuple,
                        1,
                        "lint stdout\n",
                        "lint stderr\n",
                    )
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "commit", "-m", "agent: fix lint"):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _LintRecoveryRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        git_remote_command(): git_remote_result("origin"),
    }
    config = config_with_review_disabled(worktree_path, "just lint")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    commands = [tuple(command) for command in fake_runner.calls]
    add_indices = [
        index for index, command in enumerate(commands) if command == ("git", "add", "-A")
    ]
    lint_indices = [
        index
        for index, command in enumerate(commands)
        if is_bash_wrapped_verification_call(command, ("just", "lint"))
    ]
    reset_index = commands.index(("git", "reset", "--mixed"))
    recovery_prompt = [command[-1] for command in commands if command[:1] == ("codex",)][2]

    # Two staging rounds (initial + recovery)
    assert len(add_indices) == 2
    # With the Fix Agent layer there is one extra verification run after the
    # Fix Agent attempt before falling back to the full recovery agent.
    assert len(lint_indices) == 5
    assert add_indices[0] < lint_indices[1] < reset_index
    assert reset_index < add_indices[1] < lint_indices[4]
    assert "Verification after runner staged changes with git add -A failed" in recovery_prompt
    assert "lint stdout" in recovery_prompt
    assert "lint stderr" in recovery_prompt
    assert ("git", "commit", "-m", "agent: fix lint") in commands

    # Verify attempt history records the failed attempt then success.
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    implementation_comment = [
        c for c in comment_calls if "Implementation Complete" in c.get("body", "")
    ]
    assert len(implementation_comment) == 1
    body = implementation_comment[0]["body"]
    assert "Attempt History" in body
    assert "verification_failed" in body
    assert "success" in body


def test_scenario_e_lint_exhausted_max_retries(tmp_path: Path) -> None:
    """Scene E: staged verification fails on all 3 attempts, MaxRetriesExceededError.

    Steps:
    1. Attempt 0: Agent writes commit-request, runner stages, ``just lint`` fails.
    2. Attempt 1 (recovery): Agent fixes, runner re-stages, ``just lint`` still fails.
    3. Attempt 2 (recovery): Agent fixes again, runner re-stages, ``just lint`` still fails.
    4. All attempts exhausted → runner marks issue as failed.
    5. Issue comment contains Attempt History with 3 rows of ``verification_failed``.
    """
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _LintExhaustedRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
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
            label=None,
        ):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                write_commit_request(worktree_path, f"agent: attempt {self._agent_calls}")
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, " M file.txt\n", "")
            if command_tuple == ("git", "status", "--porcelain", "-z"):
                return CommandResult(command_tuple, 0, " M file.txt\0", "")
            if command_tuple == ("just", "lint") or is_bash_wrapped_verification_call(
                command, ("just", "lint")
            ):
                return CommandResult(command_tuple, 1, "lint stdout\n", "lint stderr\n")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _LintExhaustedRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        git_remote_command(): git_remote_result("origin"),
    }
    config = config_with_review_disabled(worktree_path, "just lint")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    commands = [tuple(command) for command in fake_runner.calls]
    lint_indices = [
        index
        for index, command in enumerate(commands)
        if is_bash_wrapped_verification_call(command, ("just", "lint"))
    ]
    add_indices = [
        index
        for index, command in enumerate(commands)
        if command == ("git", "add", "--", "file.txt")
    ]
    reset_indices = [
        index for index, command in enumerate(commands) if command == ("git", "reset", "--mixed")
    ]

    # just lint 在 3 次尝试的预 staging 验证都失败，正常流程从不进入 commit proxy。
    # 重试耗尽后，runner 把在途改动 checkpoint 成一个 WIP commit（只 stage 非禁改
    # 路径 git add -- file.txt + git commit --no-verify），供下次 claim 续作。
    assert len(lint_indices) == 3
    assert len(add_indices) == 1
    assert len(reset_indices) == 0
    checkpoint_commits = [
        command
        for command in commands
        if command[:2] == ("git", "commit") and "--no-verify" in command
    ]
    assert len(checkpoint_commits) == 1

    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels" and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    failure_comment = comment_calls[-1]
    assert "Attempt History" in failure_comment["body"]
    assert "Failed after 3 attempts" in failure_comment["body"]
    assert "verification_failed" in failure_comment["body"]
    assert "| 1 |" in failure_comment["body"]
    assert "| 2 |" in failure_comment["body"]
    assert "| 3 |" in failure_comment["body"]


def test_keyboard_interrupt_checkpoints_in_flight_work_before_exit(
    tmp_path: Path,
) -> None:
    """Ctrl-C (KeyboardInterrupt) during a run checkpoints the safe in-flight
    work, then propagates so the interrupt still exits the process."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _InterruptingRunner(FakeProcessRunner):
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
            output_sink=None,
        ):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            if command_tuple[:1] in {("codex",), ("claude",), ("kimi",)}:
                # The operator hits Ctrl-C while the agent is running.
                raise KeyboardInterrupt()
            if command_tuple == ("git", "rev-parse", "HEAD"):
                return CommandResult(command_tuple, 0, "before-sha\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, " M src/wip.py\n", "")
            if command_tuple == ("git", "status", "--porcelain", "-z"):
                return CommandResult(command_tuple, 0, " M src/wip.py\0", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _InterruptingRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        git_remote_command(): git_remote_result("origin"),
    }
    config = config_with_review_disabled(worktree_path, "just lint")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    with pytest.raises(KeyboardInterrupt):
        run_once(
            repo_path=Path("."),
            config=config,
            dry_run=False,
            agent="auto",
            max_issues=1,
            github_client=fake_client,
            process_runner=fake_runner,
        )

    commands = [tuple(command) for command in fake_runner.calls]
    # The safe in-flight file was checkpointed before the interrupt propagated.
    assert ("git", "add", "--", "src/wip.py") in commands
    checkpoint_commits = [
        command
        for command in commands
        if command[:2] == ("git", "commit") and "--no-verify" in command
    ]
    assert len(checkpoint_commits) == 1
