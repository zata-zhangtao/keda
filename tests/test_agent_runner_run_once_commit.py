"""Tests for the ``run_once`` uncommitted-changes commit proxy path.

Covers the runner committing on the agent's behalf, staged verification
recovery, agent command failures, validation gating and commit failures."""

from __future__ import annotations

import logging
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
    write_complete_prd,
)


def test_run_once_uncommitted_changes_runner_commits(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """run_once should commit requested agent changes before publishing."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    write_commit_request(worktree_path, "agent: implement example")

    class _FallbackCommitRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._status_calls = 0

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
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self.calls.append(list(command))
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=f"{sha}\n",
                    stderr="",
                )
            if command_tuple == ("git", "status", "--porcelain"):
                self.calls.append(list(command))
                self._status_calls += 1
                status_stdout = (
                    " M file.txt\n?? .agent-runner/commit-request.json\n"
                    if self._status_calls == 1
                    else " M file.txt\n"
                    if self._status_calls < 4
                    else ""
                )
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=status_stdout,
                    stderr="",
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
                label=label,
            )

    fake_runner = _FallbackCommitRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        ("git", "branch", "--show-current"): CommandResult(
            command=("git", "branch", "--show-current"),
            return_code=0,
            stdout="issue-123\n",
            stderr="",
        ),
        git_remote_command(): git_remote_result("origin"),
    }
    config = config_with_review_disabled(worktree_path, "npm test")
    caplog.set_level(logging.WARNING, logger="backend.core.use_cases.run_agent_once")

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
    commit_command = (
        "git",
        "commit",
        "-m",
        "agent: implement example",
    )
    validation_indices = [
        index
        for index, command in enumerate(commands)
        if is_bash_wrapped_verification_call(command, ("npm", "test"))
    ]
    add_index = commands.index(("git", "add", "-A"))
    commit_index = commands.index(commit_command)
    head_indices = [
        index for index, command in enumerate(commands) if command == ("git", "rev-parse", "HEAD")
    ]
    assert len(validation_indices) == 2
    assert validation_indices[0] < add_index < validation_indices[1] < commit_index
    assert commit_index < head_indices[-1]
    assert not any(
        is_bash_wrapped_verification_call(command, ("just", "test")) for command in commands
    )
    assert not (worktree_path / ".agent-runner" / "commit-request.json").exists()
    assert ("git", "push", "-u", "origin", "issue-123") in commands
    assert (
        "Agent left uncommitted changes for Issue #123; "
        "runner processing commit request." in caplog.text
    )
    review_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels" and config.labels.review in c.get("add", [])
    ]
    assert len(review_calls) == 1
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels" and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 0


def test_run_once_recovers_after_staged_verification_failure(
    tmp_path: Path,
) -> None:
    """run_once should ask the agent to fix failures found after git add."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    write_commit_request(worktree_path, "agent: initial attempt")

    class _StagedRecoveryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._test_calls = 0
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
                    write_commit_request(worktree_path, "agent: recovered fix")
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
            if command_tuple == ("just", "test") or is_bash_wrapped_verification_call(
                command, ("just", "test")
            ):
                self._test_calls += 1
                if self._test_calls == 2:
                    return CommandResult(
                        command_tuple,
                        1,
                        "staged stdout\n",
                        "staged stderr\n",
                    )
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "commit", "-m", "agent: recovered fix"):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _StagedRecoveryRunner()
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
    add_indices = [
        index for index, command in enumerate(commands) if command == ("git", "add", "-A")
    ]
    test_indices = [
        index
        for index, command in enumerate(commands)
        if is_bash_wrapped_verification_call(command, ("just", "test"))
    ]
    reset_index = commands.index(("git", "reset", "--mixed"))
    recovery_prompt = [command[-1] for command in commands if command[:1] == ("codex",)][2]
    assert len(add_indices) == 2
    # With the Fix Agent layer, the runner runs an extra post-fix verification
    # before falling back to the full recovery agent.
    assert len(test_indices) == 5
    assert add_indices[0] < test_indices[1] < reset_index
    assert reset_index < add_indices[1] < test_indices[4]
    assert "Verification after runner staged changes with git add -A failed" in recovery_prompt
    assert "staged stdout" in recovery_prompt
    assert "staged stderr" in recovery_prompt
    assert ("git", "commit", "-m", "agent: recovered fix") in commands


def test_run_once_recovers_after_agent_command_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_once should retry when the agent CLI exits before verification."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _AgentCommandRecoveryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._agent_calls = 0
            self._sha_calls = 0
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
                return self.responses[command_tuple]
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                if self._agent_calls == 1:
                    raise RuntimeError("API Error: unknown provider failure")
                prompt = command_tuple[-1]
                assert "Recovery attempt: 1/2" in prompt
                assert "API Error: unknown provider failure" in prompt
                write_commit_request(worktree_path, "agent: recovered after api error")
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
            if command_tuple == (
                "git",
                "commit",
                "-m",
                "agent: recovered after api error",
            ):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _AgentCommandRecoveryRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        git_remote_command(): git_remote_result("origin"),
    }
    sleep_calls: list[int] = []
    monkeypatch.setattr(
        "backend.core.use_cases.run_agent_once.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )
    config = config_with_review_disabled(worktree_path, recovery_retry_delay_seconds=7)
    caplog.set_level(logging.WARNING, logger="backend.core.use_cases.run_agent_once")

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

    commands = [tuple(command) for command in fake_runner.calls]
    agent_commands = [command for command in commands if command[:1] == ("codex",)]
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels" and config.labels.failed in c.get("add", [])
    ]
    assert exit_code == 0
    assert len(agent_commands) == 2
    assert sleep_calls == [7]
    assert ("git", "commit", "-m", "agent: recovered after api error") in commands
    assert len(failed_calls) == 0
    assert "Agent command failed for Issue #123" in caplog.text


def test_run_once_uncommitted_changes_validation_failure_does_not_stage(
    tmp_path: Path,
) -> None:
    """run_once should not stage fallback changes when validation fails."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    write_commit_request(worktree_path, "agent: implement example")
    write_complete_prd(worktree_path)

    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner = FakeProcessRunner(
        responses={
            path_command: path_result,
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="before-sha\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M file.txt\n",
                stderr="",
            ),
            ("just", "test"): CommandResult(
                command=("just", "test"),
                return_code=1,
                stdout="",
                stderr="tests failed\n",
            ),
            git_remote_command(): git_remote_result("origin"),
        }
    )
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

    assert exit_code == 1
    commands = [tuple(command) for command in fake_runner.calls]
    just_test_count = sum(
        1 for command in commands if is_bash_wrapped_verification_call(command, ("just", "test"))
    )
    assert just_test_count == 3
    assert ("git", "add", "-A") not in commands
    assert ("git", "commit", "-m", "[Agent] Issue #123: Example") not in commands
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels" and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    history_comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "<!-- iar-attempt-history -->" in c.get("body", "")
    ]
    assert len(history_comment_calls) >= 1
    failure_comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue"
        and "Command failed" in c.get("body", "")
        and "<!-- iar-attempt-history -->" not in c.get("body", "")
    ]
    assert len(failure_comment_calls) == 1


def test_run_once_uncommitted_changes_missing_request_fails(tmp_path: Path) -> None:
    """run_once should not commit changes without an agent commit request."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    write_complete_prd(worktree_path)

    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner = FakeProcessRunner(
        responses={
            path_command: path_result,
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="before-sha\n",
                stderr="",
            ),
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-123\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M file.txt\n",
                stderr="",
            ),
            git_remote_command(): git_remote_result("origin"),
        }
    )
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

    assert exit_code == 1
    commands = [tuple(command) for command in fake_runner.calls]
    # 正常 commit proxy 仍拒绝在缺少 commit-request 时按 agent 的 message 提交；
    # 失败前的 WIP checkpoint（--no-verify）是另一条路径，用于跨 claim 保留进度。
    proxy_commits = [
        command
        for command in commands
        if command[:2] == ("git", "commit") and "--no-verify" not in command
    ]
    assert proxy_commits == []
    history_comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "<!-- iar-attempt-history -->" in c.get("body", "")
    ]
    assert len(history_comment_calls) >= 1
    failure_comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue"
        and "commit request" in c.get("body", "")
        and "<!-- iar-attempt-history -->" not in c.get("body", "")
    ]
    assert len(failure_comment_calls) == 1


def test_run_once_uncommitted_changes_commit_failure_fails(tmp_path: Path) -> None:
    """run_once should fail if the runner fallback commit fails."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    write_commit_request(worktree_path, "[Agent] Issue #123: Example")
    write_complete_prd(worktree_path)

    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner = FakeProcessRunner(
        responses={
            path_command: path_result,
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="before-sha\n",
                stderr="",
            ),
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-123\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M file.txt\n",
                stderr="",
            ),
            ("git", "commit", "-m", "[Agent] Issue #123: Example"): CommandResult(
                command=("git", "commit", "-m", "[Agent] Issue #123: Example"),
                return_code=1,
                stdout="",
                stderr="commit failed\n",
            ),
            git_remote_command(): git_remote_result("origin"),
        }
    )
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

    assert exit_code == 1
    commands = [tuple(command) for command in fake_runner.calls]
    commit_command = ("git", "commit", "-m", "[Agent] Issue #123: Example")
    test_indices = [
        index
        for index, command in enumerate(commands)
        if is_bash_wrapped_verification_call(command, ("just", "test"))
    ]
    add_index = commands.index(("git", "add", "-A"))
    assert len(test_indices) == 2
    assert test_indices[0] < add_index < test_indices[1]
    assert test_indices[1] < commands.index(commit_command)
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels" and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    history_comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "<!-- iar-attempt-history -->" in c.get("body", "")
    ]
    assert len(history_comment_calls) >= 1
    failure_comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue"
        and "Command failed" in c.get("body", "")
        and "<!-- iar-attempt-history -->" not in c.get("body", "")
    ]
    assert len(failure_comment_calls) == 1
