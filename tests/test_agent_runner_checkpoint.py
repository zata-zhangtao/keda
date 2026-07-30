"""Tests for progress checkpointing and cross-claim continuation.

进度落盘：失败时 checkpoint + 跨 claim 在已提交进度上续作。"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner
from tests.support.agent_runner import (
    config_with_review_disabled,
    git_remote_command,
    git_remote_result,
    make_prd_issue,
    make_ready_issue,
    worktree_path_response,
)


def _checkpoint_issue() -> IssueSummary:
    return IssueSummary(
        number=84,
        title="Big feature",
        url="https://github.com/example/repo/issues/84",
        body="PRD path: `tasks/pending/example.md`",
        labels=("agent/running",),
    )


def test_checkpoint_uncommitted_progress_commits_and_returns_sha(
    tmp_path: Path,
) -> None:
    """有未提交改动时应 git add -A + --no-verify commit，并返回新 SHA。"""
    from backend.core.use_cases.run_agent_once import checkpoint_uncommitted_progress

    runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): CommandResult(
                ("git", "status", "--porcelain"), 0, " M src/feature.py\n", ""
            ),
            ("git", "status", "--porcelain", "-z"): CommandResult(
                ("git", "status", "--porcelain", "-z"), 0, " M src/feature.py\0", ""
            ),
            ("git", "branch", "--show-current"): CommandResult(
                ("git", "branch", "--show-current"), 0, "issue-84\n", ""
            ),
            ("git", "rev-parse", "HEAD"): CommandResult(
                ("git", "rev-parse", "HEAD"), 0, "checkpoint-sha\n", ""
            ),
        }
    )

    result_sha = checkpoint_uncommitted_progress(
        _checkpoint_issue(),
        tmp_path,
        AppConfig(),
        runner,
        expected_branch="issue-84",
    )

    assert result_sha == "checkpoint-sha"
    commands = [tuple(call) for call in runner.calls]
    # Only the safe path is staged (explicitly), never a blanket ``git add -A``.
    assert ("git", "add", "--", "src/feature.py") in commands
    assert ("git", "add", "-A") not in commands
    commit_calls = [c for c in commands if c[:2] == ("git", "commit")]
    assert len(commit_calls) == 1
    assert "--no-verify" in commit_calls[0]
    assert any("[Agent][WIP] Issue #84 checkpoint" in token for token in commit_calls[0])


def test_checkpoint_uncommitted_progress_returns_none_when_clean(
    tmp_path: Path,
) -> None:
    """工作区干净时不提交、返回 None。"""
    from backend.core.use_cases.run_agent_once import checkpoint_uncommitted_progress

    runner = FakeProcessRunner()  # git status --porcelain → 空 → 无改动

    result_sha = checkpoint_uncommitted_progress(
        _checkpoint_issue(),
        tmp_path,
        AppConfig(),
        runner,
        expected_branch="issue-84",
    )

    assert result_sha is None
    assert not [c for c in runner.calls if tuple(c)[:2] == ("git", "commit")]


def test_checkpoint_uncommitted_progress_returns_none_on_branch_mismatch(
    tmp_path: Path,
) -> None:
    """分支与期望不符时拒绝提交（防止误提交到非 Issue 分支）。"""
    from backend.core.use_cases.run_agent_once import checkpoint_uncommitted_progress

    runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): CommandResult(
                ("git", "status", "--porcelain"), 0, " M src/feature.py\n", ""
            ),
            ("git", "branch", "--show-current"): CommandResult(
                ("git", "branch", "--show-current"), 0, "main\n", ""
            ),
        }
    )

    result_sha = checkpoint_uncommitted_progress(
        _checkpoint_issue(),
        tmp_path,
        AppConfig(),
        runner,
        expected_branch="issue-84",
    )

    assert result_sha is None
    assert not [c for c in runner.calls if tuple(c)[:2] == ("git", "commit")]


def test_checkpoint_uncommitted_progress_returns_none_when_all_forbidden(
    tmp_path: Path,
) -> None:
    """全是禁改路径时无可安全提交的内容,返回 None 且不提交、不抛错。"""
    from backend.core.use_cases.run_agent_once import checkpoint_uncommitted_progress

    runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): CommandResult(
                ("git", "status", "--porcelain"), 0, " M .env\n", ""
            ),
            ("git", "branch", "--show-current"): CommandResult(
                ("git", "branch", "--show-current"), 0, "issue-84\n", ""
            ),
            ("git", "status", "--porcelain", "-z"): CommandResult(
                ("git", "status", "--porcelain", "-z"), 0, " M .env\0", ""
            ),
        }
    )

    result_sha = checkpoint_uncommitted_progress(
        _checkpoint_issue(),
        tmp_path,
        AppConfig(),
        runner,
        expected_branch="issue-84",
    )

    assert result_sha is None
    assert not [c for c in runner.calls if tuple(c)[:2] == ("git", "commit")]


def test_checkpoint_uncommitted_progress_excludes_forbidden_but_keeps_safe(
    tmp_path: Path,
) -> None:
    """混合改动时只 checkpoint 安全文件,禁改文件被排除、绝不进历史。"""
    from backend.core.use_cases.run_agent_once import checkpoint_uncommitted_progress

    runner = FakeProcessRunner(
        responses={
            ("git", "status", "--porcelain"): CommandResult(
                ("git", "status", "--porcelain"), 0, " M src/feature.py\n M .env\n", ""
            ),
            ("git", "status", "--porcelain", "-z"): CommandResult(
                ("git", "status", "--porcelain", "-z"),
                0,
                " M src/feature.py\0 M .env\0",
                "",
            ),
            ("git", "branch", "--show-current"): CommandResult(
                ("git", "branch", "--show-current"), 0, "issue-84\n", ""
            ),
            ("git", "rev-parse", "HEAD"): CommandResult(
                ("git", "rev-parse", "HEAD"), 0, "checkpoint-sha\n", ""
            ),
        }
    )

    result_sha = checkpoint_uncommitted_progress(
        _checkpoint_issue(),
        tmp_path,
        AppConfig(),
        runner,
        expected_branch="issue-84",
    )

    assert result_sha == "checkpoint-sha"
    commands = [tuple(call) for call in runner.calls]
    # The safe path is staged; the forbidden path is never added.
    assert ("git", "add", "--", "src/feature.py") in commands
    add_calls = [c for c in commands if c[:2] == ("git", "add")]
    assert all(".env" not in token for call in add_calls for token in call)
    assert [c for c in commands if c[:2] == ("git", "commit")]


def test_process_ready_issue_continues_from_partial_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """已有部分提交但门禁未过时，应重跑 agent 续作（带 continuation prompt），不硬失败。"""
    from backend.core.shared.models.agent_runner import AgentCommitResult
    from backend.core.use_cases import agent_runner_orchestrate as orch
    from backend.core.use_cases import run_agent_once
    from backend.core.use_cases.run_agent_once import PrdDeliveryError

    issue = make_prd_issue("tasks/pending/example.md")
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    def _raise_not_ready(*_args: object, **_kwargs: object) -> None:
        raise PrdDeliveryError("Acceptance Checklist has unchecked items")

    monkeypatch.setattr(orch, "_reuse_existing_local_commit", _raise_not_ready)
    monkeypatch.setattr(orch, "create_or_reuse_worktree", lambda *a, **k: worktree_path)
    monkeypatch.setattr(orch, "get_head_sha", lambda *a, **k: "partial-sha")
    monkeypatch.setattr(orch, "get_current_branch", lambda *a, **k: "issue-123")

    captured: dict[str, object] = {}

    def _fake_run_agent_until_committed(**kwargs: object) -> AgentCommitResult:
        captured["called"] = True
        captured["prompt_override"] = kwargs.get("prompt_override")
        return AgentCommitResult(verification_results=[], attempt_results=[])

    monkeypatch.setattr(
        run_agent_once, "run_agent_until_committed", _fake_run_agent_until_committed
    )

    finish_called: dict[str, bool] = {}

    def _fake_finish(**_kwargs: object) -> None:
        finish_called["yes"] = True

    monkeypatch.setattr(orch, "_finish_implementation_publication", _fake_finish)

    orch._process_ready_issue(
        issue=issue,
        repo_path=Path("."),
        config=config_with_review_disabled(worktree_path),
        agent="auto",
        github_client=FakeGitHubClient(),
        process_runner=FakeProcessRunner(),
    )

    assert captured.get("called") is True
    continuation_prompt = captured.get("prompt_override")
    assert isinstance(continuation_prompt, str)
    assert "already contains committed progress" in continuation_prompt
    assert finish_called.get("yes") is True


def test_process_ready_issue_checkpoints_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """run_agent_until_committed 耗尽重试时，应 checkpoint 在途进度后再抛出。"""
    from backend.core.use_cases import agent_runner_orchestrate as orch
    from backend.core.use_cases import run_agent_once
    from backend.core.use_cases.run_agent_once import MaxRetriesExceededError

    issue = make_prd_issue("tasks/pending/example.md")
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    monkeypatch.setattr(orch, "_reuse_existing_local_commit", lambda *a, **k: None)
    monkeypatch.setattr(orch, "create_or_reuse_worktree", lambda *a, **k: worktree_path)
    monkeypatch.setattr(orch, "get_head_sha", lambda *a, **k: "before-sha")
    monkeypatch.setattr(orch, "get_current_branch", lambda *a, **k: "issue-123")

    def _raise_max(**_kwargs: object) -> None:
        raise MaxRetriesExceededError([])

    monkeypatch.setattr(run_agent_once, "run_agent_until_committed", _raise_max)

    checkpoint_called: dict[str, object] = {}

    def _fake_checkpoint(
        _issue: object,
        _worktree: object,
        _config: object,
        _runner: object,
        *,
        expected_branch: str,
    ) -> str:
        checkpoint_called["branch"] = expected_branch
        return "wip-sha"

    monkeypatch.setattr(run_agent_once, "checkpoint_uncommitted_progress", _fake_checkpoint)

    with pytest.raises(MaxRetriesExceededError):
        orch._process_ready_issue(
            issue=issue,
            repo_path=Path("."),
            config=config_with_review_disabled(worktree_path),
            agent="auto",
            github_client=FakeGitHubClient(),
            process_runner=FakeProcessRunner(),
        )

    assert checkpoint_called.get("branch") == "issue-123"


def test_run_once_reuses_existing_clean_local_commit(tmp_path: Path) -> None:
    """Runner should publish an existing clean commit without invoking the agent."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _ExistingCommitRunner(FakeProcessRunner):
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
                raise AssertionError("agent should not be invoked")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                return CommandResult(command_tuple, 0, "existing-sha\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == (
                "git",
                "rev-list",
                "--count",
                "origin/main..HEAD",
            ):
                return CommandResult(command_tuple, 0, "1\n", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _ExistingCommitRunner()
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
    assert not [command for command in commands if command[:1] == ("codex",)]
    assert ("git", "push", "-u", "origin", "issue-123") in commands
    comment_calls = [c for c in fake_client.calls if c["method"] == "comment_issue"]
    implementation_comment = [
        c for c in comment_calls if "Implementation Complete" in c.get("body", "")
    ]
    assert len(implementation_comment) == 1
    assert "Reused 1 existing local commit" in implementation_comment[0]["body"]


def test_run_once_recovers_running_issue_with_existing_local_commit(
    tmp_path: Path,
) -> None:
    """Running Issues with clean local commits should resume publish without agent."""
    fake_client = FakeGitHubClient()
    issue = IssueSummary(
        number=123,
        title="Example",
        url="https://github.com/example/repo/issues/123",
        body="Example body",
        labels=("agent/running", "agent/codex"),
    )
    fake_client.list_ready_issues = lambda ready_label, limit: []
    fake_client.list_review_candidate_issues = lambda labels, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()

    class _RunningRecoveryRunner(FakeProcessRunner):
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
                raise AssertionError("agent should not be invoked")
            if command_tuple == ("git", "rev-parse", "HEAD"):
                return CommandResult(command_tuple, 0, "existing-sha\n", "")
            if command_tuple == ("git", "branch", "--show-current"):
                return CommandResult(command_tuple, 0, "issue-123\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == (
                "git",
                "rev-list",
                "--count",
                "origin/main..HEAD",
            ):
                return CommandResult(command_tuple, 0, "1\n", "")
            return CommandResult(command_tuple, 0, "", "")

    fake_runner = _RunningRecoveryRunner()
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
    assert not [command for command in commands if command[:1] == ("codex",)]
    assert ("git", "push", "-u", "origin", "issue-123") in commands
    label_calls = [c for c in fake_client.calls if c["method"] == "edit_issue_labels"]
    assert any(config.labels.review in c.get("add", []) for c in label_calls)
