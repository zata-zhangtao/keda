"""Tests for the ``run_once`` issue lifecycle.

Covers dry runs, remote preflight, success and failure label transitions,
the no-new-commits guard, and the PRD archive step that must land before
the commit."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PullRequestContext,
    RunnerConfig,
    WorktreeConfig,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner
from tests.support.agent_runner import (
    config_with_review_disabled,
    git_remote_command,
    git_remote_result,
    make_prd_issue,
    make_ready_issue,
    worktree_path_response,
    write_commit_request,
    write_complete_prd,
    write_incomplete_prd,
)


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

    from backend.core.use_cases.agent_runner_orchestrate import run_once

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
            git_remote_command(): git_remote_result("zata"),
        }
    )
    caplog.set_level(logging.ERROR, logger="backend.core.use_cases.run_agent_once")

    from backend.core.use_cases.agent_runner_orchestrate import run_once

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


def test_run_once_no_new_commits_fails() -> None:
    """run_once should fail when the agent produces no new commits."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
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
            git_remote_command(): git_remote_result("origin"),
        }
    )
    config = AppConfig(runner=RunnerConfig(recovery_retry_delay_seconds=0))

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
        and "no git commits" in c.get("body", "")
        and "<!-- iar-attempt-history -->" not in c.get("body", "")
    ]
    assert len(failure_comment_calls) == 1


def test_run_once_success(tmp_path: Path) -> None:
    """run_once should succeed through pre-PR review and supervisor approval."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    fake_client._pr_contexts["issue-123"] = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-123",
        head_sha="after-sha",
        base_sha="before-sha",
    )
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _SuccessRunner(FakeProcessRunner):
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
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(
                    command=command_tuple, return_code=0, stdout=f"{sha}\n", stderr=""
                )
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                # Agent 1: implementation, Agent 2: pre-PR review, Agent 3: supervisor
                if self._agent_calls == 2:
                    return CommandResult(
                        command=command_tuple,
                        return_code=0,
                        stdout='{"verdict": "approved", "summary": "LGTM"}'
                        if capture_output
                        else "",
                        stderr="",
                    )
                if self._agent_calls == 3:
                    return CommandResult(
                        command=command_tuple,
                        return_code=0,
                        stdout=('{"action": "approve_for_human_review", "summary": "LGTM"}')
                        if capture_output
                        else "",
                        stderr="",
                    )
                return CommandResult(command=command_tuple, return_code=0, stdout="", stderr="")
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            return CommandResult(command=command_tuple, return_code=0, stdout="", stderr="")

    fake_runner = _SuccessRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
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
        git_remote_command(): git_remote_result("origin"),
    }
    config = AppConfig(worktree=WorktreeConfig(path_command=f"echo {worktree_path}"))

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=tmp_path,
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 0
    # Labels: ready -> running -> supervising -> review
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    added_labels = [label for c in label_calls for label in c.get("add", [])]
    assert config.labels.review in added_labels
    assert config.labels.supervising in added_labels
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    bodies = [c["body"] for c in comment_calls]
    assert any("Implementation Complete" in b for b in bodies)
    assert any("Pre-PR Review" in b for b in bodies)
    assert any("Draft PR Created" in b for b in bodies)
    assert any("Post-PR Supervisor" in b for b in bodies)


def test_run_once_failure_removes_supervising_label(tmp_path: Path) -> None:
    """Failure after Draft PR creation should not leave supervising with failed."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    fake_client._pr_contexts["issue-123"] = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/1",
        branch="issue-123",
        head_sha="after-sha",
        base_sha="before-sha",
    )
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _SupervisorFailureRunner(FakeProcessRunner):
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
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = "after-sha" if self._sha_calls > 1 else "before-sha"
                return CommandResult(
                    command=command_tuple, return_code=0, stdout=f"{sha}\n", stderr=""
                )
            if command_tuple[:1] == ("codex",):
                self._agent_calls += 1
                if self._agent_calls == 2:
                    return CommandResult(
                        command=command_tuple,
                        return_code=0,
                        stdout='{"verdict": "approved", "summary": "LGTM"}',
                        stderr="",
                    )
                if self._agent_calls == 3:
                    raise RuntimeError("supervisor crashed")
                return CommandResult(command=command_tuple, return_code=0, stdout="", stderr="")
            if command_tuple in self.responses:
                result = self.responses[command_tuple]
                if check and result.return_code != 0:
                    raise RuntimeError(f"Command failed: {command}")
                return result
            return CommandResult(command=command_tuple, return_code=0, stdout="", stderr="")

    fake_runner = _SupervisorFailureRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
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
        git_remote_command(): git_remote_result("origin"),
    }
    config = AppConfig(worktree=WorktreeConfig(path_command=f"echo {worktree_path}"))

    from backend.core.use_cases.agent_runner_orchestrate import run_once

    exit_code = run_once(
        repo_path=tmp_path,
        config=config,
        dry_run=False,
        agent="auto",
        max_issues=1,
        github_client=fake_client,
        process_runner=fake_runner,
    )

    assert exit_code == 1
    failed_calls = [
        call
        for call in fake_client.calls
        if call["method"] == "edit_issue_labels" and config.labels.failed in call.get("add", [])
    ]
    assert len(failed_calls) == 1
    assert config.labels.supervising in failed_calls[0]["remove"]
    assert config.labels.agent_labels["codex"] not in failed_calls[0]["remove"]
    # After failure the Issue should be left with exactly one workflow label.
    final_labels = set(fake_client._issue_labels[issue.number])
    workflow_labels = {
        config.labels.ready,
        config.labels.running,
        config.labels.supervising,
        config.labels.review,
        config.labels.failed,
        config.labels.blocked,
    }
    assert final_labels.intersection(workflow_labels) == {config.labels.failed}


def test_run_once_git_mv_prd_before_commit(tmp_path: Path) -> None:
    """run_once should git mv a complete pending PRD before staging and committing."""
    fake_client = FakeGitHubClient()
    issue = make_prd_issue("tasks/pending/example.md")
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    write_commit_request(worktree_path, "agent: implement example")
    write_complete_prd(worktree_path, "tasks/pending/example.md")
    (worktree_path / "tasks" / "archive").mkdir(parents=True, exist_ok=True)
    src_file = worktree_path / "src" / "file.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("# code\n", encoding="utf-8")

    class _PrdSuccessRunner(FakeProcessRunner):
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
                    " M src/file.py\n?? .agent-runner/commit-request.json\n"
                    if self._status_calls == 1
                    else " M src/file.py\nR  tasks/pending/example.md -> tasks/archive/example.md\n"
                    if self._status_calls == 2
                    else ""
                )
                return CommandResult(
                    command=command_tuple,
                    return_code=0,
                    stdout=status_stdout,
                    stderr="",
                )
            if command_tuple == (
                "git",
                "mv",
                "tasks/pending/example.md",
                "tasks/archive/example.md",
            ):
                self.calls.append(list(command))
                pending_path = Path(cwd) / "tasks" / "pending" / "example.md"
                archive_path = Path(cwd) / "tasks" / "archive" / "example.md"
                if pending_path.exists():
                    archive_path.parent.mkdir(parents=True, exist_ok=True)
                    pending_path.rename(archive_path)
                return CommandResult(command=command_tuple, return_code=0, stdout="", stderr="")
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
                label=label,
            )

    fake_runner = _PrdSuccessRunner()
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
    mv_index = commands.index(("git", "mv", "tasks/pending/example.md", "tasks/archive/example.md"))
    add_index = commands.index(("git", "add", "-A"))
    commit_index = commands.index(("git", "commit", "-m", "agent: implement example"))
    assert mv_index < add_index < commit_index
    pr_calls = [c for c in fake_client.calls if c["method"] == "create_draft_pr"]
    assert len(pr_calls) == 1


def test_run_once_recovers_after_prd_delivery_failure(tmp_path: Path) -> None:
    """run_once should recover when the pending PRD checklist is initially incomplete."""
    fake_client = FakeGitHubClient()
    issue = make_prd_issue("tasks/pending/example.md")
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    write_incomplete_prd(worktree_path, "tasks/pending/example.md")
    (worktree_path / "tasks" / "archive").mkdir(parents=True, exist_ok=True)

    class _PrdRecoveryRunner(FakeProcessRunner):
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
                prompt = command_tuple[-1]
                if "Recovery attempt: 1/2" in prompt:
                    assert "PRD delivery check failed" in prompt
                    assert "unchecked items" in prompt
                    write_commit_request(worktree_path, "agent: recovered fix")
                    write_complete_prd(worktree_path, "tasks/pending/example.md")
                    recovery_prd_path = worktree_path / "tasks/pending/example.md"
                    recovery_prd_path.write_text(
                        recovery_prd_path.read_text(encoding="utf-8")
                        + "\n## Change Log\n\n"
                        + "### 2026-07-14 · Recovery update\n"
                        + "- 类型：验收状态更新\n"
                        + "- 原文：验收项未完成\n"
                        + "- 变更后：验收项已完成\n"
                        + "- 原因：已执行缺失验证\n"
                        + "- 影响：交付状态更新\n"
                        + "- 审核：runner 门禁待验证\n",
                        encoding="utf-8",
                    )
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
            if command_tuple == ("git", "commit", "-m", "agent: recovered fix"):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == (
                "git",
                "mv",
                "tasks/pending/example.md",
                "tasks/archive/example.md",
            ):
                pending_path = Path(cwd) / "tasks" / "pending" / "example.md"
                archive_path = Path(cwd) / "tasks" / "archive" / "example.md"
                if pending_path.exists():
                    archive_path.parent.mkdir(parents=True, exist_ok=True)
                    pending_path.rename(archive_path)
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _PrdRecoveryRunner()
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
        git_remote_command(): git_remote_result("origin"),
    }
    config = config_with_review_disabled(worktree_path)

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
    agent_commands = [command for command in commands if command[:1] == ("codex",)]
    assert len(agent_commands) == 2
    mv_index = commands.index(("git", "mv", "tasks/pending/example.md", "tasks/archive/example.md"))
    add_index = commands.index(("git", "add", "-A"))
    commit_index = commands.index(("git", "commit", "-m", "agent: recovered fix"))
    assert mv_index < add_index < commit_index
    failed_calls = [
        c
        for c in fake_client.calls
        if c["method"] == "edit_issue_labels" and config.labels.failed in c.get("add", [])
    ]
    assert len(failed_calls) == 0
