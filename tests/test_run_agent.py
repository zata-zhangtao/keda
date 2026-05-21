"""Tests for the local Issue runner."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    GitConfig,
    IssueSummary,
    RunnerConfig,
    WorktreeConfig,
)
from backend.core.use_cases.run_agent_once import (
    build_recovery_prompt,
    build_prompt,
    choose_agent,
    commit_requested_changes,
    format_command,
    get_head_sha,
    publish_changes,
    run_agent_with_prompt,
    validate_safe_changes,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner


def test_format_command_substitutes_issue_number() -> None:
    """Command templates should have {issue_number} replaced."""
    result = format_command("echo {issue_number}", issue_number=42)
    assert result == ["echo", "42"]


def test_choose_agent_override() -> None:
    """CLI override should take precedence."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    config = AppConfig()
    assert choose_agent(issue, config, "claude") == "claude"


def test_choose_agent_from_labels() -> None:
    """Issue labels should determine agent when override is auto."""
    issue = IssueSummary(
        number=1, title="T", url="U", body="B", labels=("agent/claude",)
    )
    config = AppConfig()
    assert choose_agent(issue, config, "auto") == "claude"


def test_choose_agent_defaults_to_codex() -> None:
    """Default agent should be codex when no signals are present."""
    issue = IssueSummary(number=1, title="T", url="U", body="B", labels=())
    config = AppConfig()
    assert choose_agent(issue, config, "auto") == "codex"


def test_run_agent_with_prompt_uses_claude_yolo_mode(tmp_path: Path) -> None:
    """Claude runner should bypass permission prompts for unattended execution."""
    fake_runner = FakeProcessRunner()

    run_agent_with_prompt("claude", "Implement the issue.", tmp_path, fake_runner)

    assert fake_runner.calls == [
        [
            "claude",
            "--dangerously-skip-permissions",
            "--verbose",
            "-p",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "Implement the issue.",
        ]
    ]


def test_run_once_dry_run() -> None:
    """Dry-run should list ready work without mutating labels."""
    fake_client = FakeGitHubClient()
    fake_client.list_ready_issues = lambda ready_label, limit: [
        IssueSummary(
            number=123,
            title="Example",
            url="https://github.com/example/repo/issues/123",
            body="PRD path: `tasks/example.md`",
            labels=("agent/ready", "agent/codex"),
        )
    ]
    fake_runner = FakeProcessRunner()
    config = AppConfig()

    from backend.core.use_cases.run_agent_once import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=config,
        dry_run=True,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    edit_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert len(edit_calls) == 0


def test_run_once_preflight_rejects_missing_configured_remote(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """run_once should fail before claiming work when configured remote is absent."""
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
        responses={
            _git_remote_command(): _git_remote_result("zata"),
        }
    )
    caplog.set_level(logging.ERROR, logger="backend.core.use_cases.run_agent_once")

    from backend.core.use_cases.run_agent_once import run_once

    exit_code = run_once(
        repo_path=Path("."),
        config=AppConfig(),
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    assert fake_client.calls == []
    assert fake_runner.calls == [["git", "remote"]]
    assert "Configured git remote 'origin' does not exist" in caplog.text
    assert "Available remotes: zata" in caplog.text


def test_build_prompt_uses_commit_request_proxy() -> None:
    """Prompt should route commit intent through the runner proxy."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    prompt = build_prompt(issue, Path("/worktree"))
    assert "Do not merge main, delete branches, push, or create PRs" in prompt
    assert "Do not run `git add` or `git commit`" in prompt
    assert ".agent-runner/commit-request.json" in prompt
    assert "commit_message" in prompt


def test_build_recovery_prompt_includes_failure_context() -> None:
    """Recovery prompt should give the agent enough detail to fix and retry."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    failure_summary = "\n".join(
        [
            "Verification after runner staged changes with git add -A failed.",
            "Command: `just test`",
            "stdout: failing stdout",
            "stderr: failing stderr",
        ]
    )

    prompt = build_recovery_prompt(
        issue,
        Path("/worktree"),
        recovery_attempt=1,
        max_recovery_attempts=2,
        failure_summary=failure_summary,
    )

    assert "Recovery attempt: 1/2" in prompt
    assert "Verification after runner staged changes with git add -A failed" in prompt
    assert "failing stdout" in prompt
    assert "failing stderr" in prompt
    assert "Do not run `git add` or `git commit`" in prompt
    assert ".agent-runner/commit-request.json" in prompt


def test_get_head_sha() -> None:
    """get_head_sha should return the HEAD SHA."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="abc123def456\n",
                stderr="",
            ),
        }
    )
    sha = get_head_sha(Path("."), fake_runner)
    assert sha == "abc123def456"


def test_publish_changes_no_git_commit() -> None:
    """publish_changes should not call git add or git commit."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
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
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    branch, pr_url = publish_changes(
        issue, Path("."), AppConfig(), fake_client, fake_runner
    )
    assert branch == "issue-1"
    assert pr_url == "https://github.com/example/repo/pull/1"
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "add", "-A") not in commands
    assert ("git", "commit", "-m", "agent: complete issue #1") not in commands
    assert ("git", "push", "-u", "origin", "issue-1") in commands


def test_publish_changes_rejects_missing_configured_remote() -> None:
    """publish_changes should fail instead of guessing another remote."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
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
            _git_remote_command(): _git_remote_result("zata", "upstream"),
        }
    )

    with pytest.raises(RuntimeError, match="Configured git remote 'origin'"):
        publish_changes(issue, Path("."), AppConfig(), fake_client, fake_runner)

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "push", "-u", "origin", "issue-1") not in commands
    assert ("git", "push", "-u", "zata", "issue-1") not in commands


def test_publish_changes_uses_configured_existing_remote() -> None:
    """publish_changes should push only to the configured remote."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_client = FakeGitHubClient()
    fake_runner = FakeProcessRunner(
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
            _git_remote_command(): _git_remote_result("origin", "zata"),
        }
    )
    config = AppConfig(git=GitConfig(remote="zata"))

    branch, _ = publish_changes(issue, Path("."), config, fake_client, fake_runner)

    assert branch == "issue-1"
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "push", "-u", "zata", "issue-1") in commands
    assert ("git", "push", "-u", "origin", "issue-1") not in commands


def test_publish_changes_rejects_branch_change() -> None:
    """publish_changes should refuse to push if the worktree branch changed."""
    issue = IssueSummary(
        number=1,
        title="Test",
        url="https://github.com/example/repo/issues/1",
        body="Test body",
        labels=(),
    )
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="main\n",
                stderr="",
            ),
        }
    )

    with pytest.raises(RuntimeError, match="unexpected branch: main"):
        publish_changes(
            issue,
            Path("."),
            AppConfig(),
            FakeGitHubClient(),
            fake_runner,
            expected_branch="issue-1",
        )

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "status", "--porcelain") not in commands
    assert ("git", "push", "-u", "origin", "main") not in commands


def test_validate_safe_changes_rejects_forbidden_path(tmp_path: Path) -> None:
    """Runner should not publish configured secret-like paths."""
    repo = tmp_path / "repo"
    repo.mkdir()
    from tests.test_create_issue_from_prd import _init_repo

    _init_repo(repo)
    (repo / ".env").write_text("SECRET=value\n", encoding="utf-8")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M .env\n",
                stderr="",
            ),
        }
    )

    with pytest.raises(RuntimeError, match="Refusing to publish forbidden paths: .env"):
        validate_safe_changes(repo, AppConfig(), fake_runner)


def _make_ready_issue() -> IssueSummary:
    return IssueSummary(
        number=123,
        title="Example",
        url="https://github.com/example/repo/issues/123",
        body="PRD path: `tasks/example.md`",
        labels=("agent/ready", "agent/codex"),
    )


def _config_for_worktree(
    worktree_path: Path,
    *verification_commands: str,
    max_recovery_attempts: int = 2,
    recovery_retry_delay_seconds: int = 0,
) -> AppConfig:
    commands = verification_commands or ("just test",)
    return AppConfig(
        runner=RunnerConfig(
            max_recovery_attempts=max_recovery_attempts,
            recovery_retry_delay_seconds=recovery_retry_delay_seconds,
            verification_commands=commands,
        ),
        worktree=WorktreeConfig(path_command=f"echo {worktree_path}"),
    )


def _worktree_path_response(
    worktree_path: Path,
) -> tuple[tuple[str, ...], CommandResult]:
    command = ("echo", str(worktree_path))
    return command, CommandResult(
        command=command,
        return_code=0,
        stdout=f"{worktree_path}\n",
        stderr="",
    )


def _git_remote_command() -> tuple[str, ...]:
    return ("git", "remote")


def _git_remote_result(*remote_names: str) -> CommandResult:
    command = _git_remote_command()
    return CommandResult(
        command=command,
        return_code=0,
        stdout="".join(f"{remote_name}\n" for remote_name in remote_names),
        stderr="",
    )


def _write_commit_request(worktree_path: Path, commit_message: str) -> None:
    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        f'{{"commit_message": "{commit_message}"}}\n',
        encoding="utf-8",
    )


def test_run_once_uncommitted_changes_runner_commits(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """run_once should commit requested agent changes before publishing."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    class _FallbackCommitRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._status_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
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
            )

    fake_runner = _FallbackCommitRunner()
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        ("git", "branch", "--show-current"): CommandResult(
            command=("git", "branch", "--show-current"),
            return_code=0,
            stdout="issue-123\n",
            stderr="",
        ),
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_for_worktree(worktree_path, "npm test")
    caplog.set_level(logging.WARNING, logger="backend.core.use_cases.run_agent_once")

    from backend.core.use_cases.run_agent_once import run_once

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
        index for index, command in enumerate(commands) if command == ("npm", "test")
    ]
    add_index = commands.index(("git", "add", "-A"))
    commit_index = commands.index(commit_command)
    head_indices = [
        index
        for index, command in enumerate(commands)
        if command == ("git", "rev-parse", "HEAD")
    ]
    assert len(validation_indices) == 2
    assert validation_indices[0] < add_index < validation_indices[1] < commit_index
    assert commit_index < head_indices[-1]
    assert ("just", "test") not in commands
    assert not (worktree_path / ".agent-runner" / "commit-request.json").exists()
    assert ("git", "push", "-u", "origin", "issue-123") in commands
    assert (
        "Agent left uncommitted changes for Issue #123; "
        "runner processing commit request." in caplog.text
    )
    review_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.review in c.get("add", [])
    ]
    assert len(review_calls) == 1
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 0


def test_run_once_recovers_after_staged_verification_failure(
    tmp_path: Path,
) -> None:
    """run_once should ask the agent to fix failures found after git add."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: initial attempt")

    class _StagedRecoveryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0
            self._test_calls = 0
            self._committed = False

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
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
                    _write_commit_request(worktree_path, "agent: recovered fix")
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
            if command_tuple == ("just", "test"):
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
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = _config_for_worktree(worktree_path)

    from backend.core.use_cases.run_agent_once import run_once

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
        index
        for index, command in enumerate(commands)
        if command == ("git", "add", "-A")
    ]
    test_indices = [
        index for index, command in enumerate(commands) if command == ("just", "test")
    ]
    reset_index = commands.index(("git", "reset", "--mixed"))
    recovery_prompt = [
        command[-1] for command in commands if command[:1] == ("codex",)
    ][1]
    assert len(add_indices) == 2
    assert len(test_indices) == 4
    assert add_indices[0] < test_indices[1] < reset_index
    assert reset_index < add_indices[1] < test_indices[3]
    assert (
        "Verification after runner staged changes with git add -A failed"
        in recovery_prompt
    )
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
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _AgentCommandRecoveryRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._agent_calls = 0
            self._sha_calls = 0
            self._committed = False

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            command_tuple = tuple(command)
            self.calls.append(list(command))
            if command_tuple in self.responses:
                return self.responses[command_tuple]
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                if self._agent_calls == 1:
                    raise RuntimeError("API Error: 400 Invalid request Error")
                prompt = command_tuple[-1]
                assert "Recovery attempt: 1/2" in prompt
                assert "API Error: 400 Invalid request Error" in prompt
                _write_commit_request(worktree_path, "agent: recovered after api error")
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
    path_command, path_result = _worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        _git_remote_command(): _git_remote_result("origin"),
    }
    sleep_calls: list[int] = []
    monkeypatch.setattr(
        "backend.core.use_cases.run_agent_once.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )
    config = _config_for_worktree(worktree_path, recovery_retry_delay_seconds=7)
    caplog.set_level(logging.WARNING, logger="backend.core.use_cases.run_agent_once")

    from backend.core.use_cases.run_agent_once import run_once

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
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
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
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")

    path_command, path_result = _worktree_path_response(worktree_path)
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
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    config = _config_for_worktree(worktree_path)

    from backend.core.use_cases.run_agent_once import run_once

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
    assert commands.count(("just", "test")) == 3
    assert ("git", "add", "-A") not in commands
    assert ("git", "commit", "-m", "[Agent] Issue #123: Example") not in commands
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "Command failed" in c.get("body", "")
    ]
    assert len(comment_calls) == 1


def test_run_once_uncommitted_changes_missing_request_fails(tmp_path: Path) -> None:
    """run_once should not commit changes without an agent commit request."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    path_command, path_result = _worktree_path_response(worktree_path)
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
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    config = _config_for_worktree(worktree_path)

    from backend.core.use_cases.run_agent_once import run_once

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
    assert ("git", "add", "-A") not in commands
    assert ("git", "commit", "-m", "agent: implement example") not in commands
    comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "commit request" in c.get("body", "")
    ]
    assert len(comment_calls) == 1


def test_run_once_uncommitted_changes_commit_failure_fails(tmp_path: Path) -> None:
    """run_once should fail if the runner fallback commit fails."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "[Agent] Issue #123: Example")

    path_command, path_result = _worktree_path_response(worktree_path)
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
            ("git", "commit", "-m", "[Agent] Issue #123: Example"): CommandResult(
                command=("git", "commit", "-m", "[Agent] Issue #123: Example"),
                return_code=1,
                stdout="",
                stderr="commit failed\n",
            ),
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    config = _config_for_worktree(worktree_path)

    from backend.core.use_cases.run_agent_once import run_once

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
        index for index, command in enumerate(commands) if command == ("just", "test")
    ]
    add_index = commands.index(("git", "add", "-A"))
    assert len(test_indices) == 2
    assert test_indices[0] < add_index < test_indices[1]
    assert test_indices[1] < commands.index(commit_command)
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "Command failed" in c.get("body", "")
    ]
    assert len(comment_calls) == 1


def test_commit_requested_changes_rejects_branch_change(tmp_path: Path) -> None:
    """Commit proxy should only commit on the expected branch."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="main\n",
                stderr="",
            ),
        }
    )

    with pytest.raises(RuntimeError, match="unexpected branch: main"):
        commit_requested_changes(
            _make_ready_issue(),
            worktree_path,
            AppConfig(),
            fake_runner,
            expected_branch="issue-123",
        )

    commands = [tuple(command) for command in fake_runner.calls]
    assert ("git", "add", "-A") not in commands


def test_commit_requested_changes_rejects_forbidden_paths(tmp_path: Path) -> None:
    """Commit proxy should apply forbidden path checks before staging."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    _write_commit_request(worktree_path, "agent: implement example")
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-123\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout=" M .env\n",
                stderr="",
            ),
        }
    )

    with pytest.raises(RuntimeError, match="Refusing to publish forbidden paths"):
        commit_requested_changes(
            _make_ready_issue(),
            worktree_path,
            AppConfig(),
            fake_runner,
            expected_branch="issue-123",
        )

    commands = [tuple(command) for command in fake_runner.calls]
    assert ("git", "add", "-A") not in commands


def test_run_once_no_new_commits_fails() -> None:
    """run_once should fail when the agent produces no new commits."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "HEAD"): CommandResult(
                command=("git", "rev-parse", "HEAD"),
                return_code=0,
                stdout="same-sha\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            _git_remote_command(): _git_remote_result("origin"),
        }
    )
    config = AppConfig(runner=RunnerConfig(recovery_retry_delay_seconds=0))

    from backend.core.use_cases.run_agent_once import run_once

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
        if c["method"] == "edit_issue_labels"
        and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 1
    comment_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "comment_issue" and "no git commits" in c.get("body", "")
    ]
    assert len(comment_calls) == 1


def test_run_once_success() -> None:
    """run_once should succeed when the agent commits changes."""
    fake_client = FakeGitHubClient()
    issue = _make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]

    # _ShaSequenceRunner returns a different SHA on the second call to
    # simulate a new commit.
    class _ShaSequenceRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self._sha_calls = 0

        def run(self, command, *, cwd, check=True, timeout=None, capture_output=True):
            if tuple(command) == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(
                    command=tuple(command), return_code=0, stdout=f"{sha}\n", stderr=""
                )
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
            )

    fake_runner = _ShaSequenceRunner()
    fake_runner.responses = {
        ("git", "status", "--porcelain"): CommandResult(
            command=("git", "status", "--porcelain"),
            return_code=0,
            stdout="",
            stderr="",
        ),
        ("git", "branch", "--show-current"): CommandResult(
            command=("git", "branch", "--show-current"),
            return_code=0,
            stdout="issue-123\n",
            stderr="",
        ),
        _git_remote_command(): _git_remote_result("origin"),
    }
    config = AppConfig()

    from backend.core.use_cases.run_agent_once import run_once

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
    review_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels"
        and config.labels.review in c.get("add", [])
    ]
    assert len(review_calls) == 1
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1
