"""Tests for keeping the worktree on its issue branch.

Covers ``ensure_worktree_branch`` detached-head healing, rebase target
guards, agent-driven conflict resolution, and the detached-head rejection
in the commit proxy."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    IssueSummary,
    PostPrSupervisorConfig,
    PrePrReviewConfig,
    PullRequestContext,
    RunnerConfig,
    WorktreeConfig,
)
from backend.core.use_cases.run_agent_once import (
    commit_requested_changes,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner
from tests.support.agent_runner import (
    create_commit,
    git_remote_command,
    git_remote_result,
    init_git_repo,
    worktree_path_response,
)


def test_ensure_worktree_branch_creates_branch_for_detached_head_real_git(
    tmp_path: Path,
) -> None:
    """Detached HEAD without an existing branch is healed by creating it."""
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "issue-123"
    repo = init_git_repo(repo_path)
    create_commit(repo, "initial")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    create_commit(repo, "on branch")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(worktree_path), "issue-123"],
        check=True,
        capture_output=True,
    )
    detached_sha = create_commit(worktree_path, "detached commit")
    subprocess.run(
        ["git", "-C", str(worktree_path), "checkout", detached_sha],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "-D", "issue-123"],
        check=True,
        capture_output=True,
    )

    from backend.infrastructure.process_runner import SubprocessRunner
    from backend.core.use_cases.run_agent_once import _ensure_worktree_branch

    _ensure_worktree_branch(
        worktree_path,
        "issue-123",
        IssueSummary(number=123, title="T", url="U", body="B", labels=()),
        AppConfig(),
        SubprocessRunner(),
    )

    current_branch = subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_branch == "issue-123"
    head_sha = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_sha == detached_sha


def test_ensure_worktree_branch_fast_forwards_branch_real_git(tmp_path: Path) -> None:
    """Detached HEAD ahead of the expected branch is healed by fast-forward."""
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "issue-123"
    repo = init_git_repo(repo_path)
    create_commit(repo, "initial")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    create_commit(repo, "on branch")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(worktree_path), "issue-123"],
        check=True,
        capture_output=True,
    )
    detached_sha = create_commit(worktree_path, "detached commit")
    subprocess.run(
        ["git", "-C", str(worktree_path), "checkout", detached_sha],
        check=True,
        capture_output=True,
    )

    from backend.infrastructure.process_runner import SubprocessRunner
    from backend.core.use_cases.run_agent_once import _ensure_worktree_branch

    _ensure_worktree_branch(
        worktree_path,
        "issue-123",
        IssueSummary(number=123, title="T", url="U", body="B", labels=()),
        AppConfig(),
        SubprocessRunner(),
    )

    current_branch = subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_branch == "issue-123"
    head_sha = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_sha == detached_sha


def test_ensure_worktree_branch_raises_when_branch_diverged_real_git(
    tmp_path: Path,
) -> None:
    """Detached HEAD that diverged from the expected branch cannot be healed."""
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "issue-123"
    repo = init_git_repo(repo_path)
    create_commit(repo, "initial")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    create_commit(repo, "on branch")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(worktree_path), "issue-123"],
        check=True,
        capture_output=True,
    )
    detached_sha = create_commit(worktree_path, "detached commit")
    subprocess.run(
        ["git", "-C", str(worktree_path), "checkout", detached_sha],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "issue-123"],
        check=True,
        capture_output=True,
    )
    create_commit(repo, "branch moved on")

    from backend.infrastructure.process_runner import SubprocessRunner
    from backend.core.use_cases.run_agent_once import _ensure_worktree_branch

    with pytest.raises(RuntimeError, match="diverged"):
        _ensure_worktree_branch(
            worktree_path,
            "issue-123",
            IssueSummary(number=123, title="T", url="U", body="B", labels=()),
            AppConfig(),
            SubprocessRunner(),
        )


def test_ensure_worktree_branch_aborts_conflicted_rebase_after_agent_fails_real_git(
    tmp_path: Path,
) -> None:
    """Active rebase with conflicts falls back to abort when agent resolution fails."""
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "issue-123"
    repo = init_git_repo(repo_path)
    create_commit(repo, "initial")
    file_path = repo / "file.txt"
    file_path.write_text("main content\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", str(file_path)],
        check=True,
        capture_output=True,
    )
    create_commit(repo, "main commit")

    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    file_path.write_text("issue content\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", str(file_path)],
        check=True,
        capture_output=True,
    )
    create_commit(repo, "issue commit")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(worktree_path), "issue-123"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(worktree_path), "rebase", "main"],
        check=False,
        capture_output=True,
    )

    from backend.infrastructure.process_runner import SubprocessRunner
    from backend.core.use_cases.run_agent_once import _ensure_worktree_branch

    _ensure_worktree_branch(
        worktree_path,
        "issue-123",
        IssueSummary(number=123, title="T", url="U", body="B", labels=()),
        AppConfig(),
        SubprocessRunner(),
    )

    current_branch = subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_branch == "issue-123"
    assert not (worktree_path / ".git" / "rebase-merge").exists()
    assert not (worktree_path / ".git" / "rebase-apply").exists()


def test_ensure_worktree_branch_rejects_mismatched_rebase_target(
    tmp_path: Path,
) -> None:
    """Active rebase for a different branch is left for manual recovery."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    rebase_merge = worktree_path / ".git" / "rebase-merge"
    rebase_merge.mkdir(parents=True)
    head_name = rebase_merge / "head-name"
    head_name.write_text("refs/heads/issue-999", encoding="utf-8")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): CommandResult(
                command=("git", "rev-parse", "--abbrev-ref", "HEAD"),
                return_code=0,
                stdout="HEAD\n",
                stderr="",
            ),
            ("git", "rev-parse", "--git-path", "rebase-merge/head-name"): CommandResult(
                command=("git", "rev-parse", "--git-path", "rebase-merge/head-name"),
                return_code=0,
                stdout=f"{head_name}\n",
                stderr="",
            ),
        }
    )

    from backend.core.use_cases.run_agent_once import _ensure_worktree_branch

    with pytest.raises(
        RuntimeError,
        match="active rebase for branch 'issue-999'.*expects 'issue-123'",
    ):
        _ensure_worktree_branch(
            worktree_path,
            "issue-123",
            IssueSummary(number=123, title="T", url="U", body="B", labels=()),
            AppConfig(),
            fake_runner,
        )

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "-c", "core.editor=true", "rebase", "--continue") not in commands
    assert ("git", "rebase", "--abort") not in commands
    assert ("git", "checkout", "issue-999") not in commands


def test_ensure_worktree_branch_rejects_unconfirmed_rebase_target(
    tmp_path: Path,
) -> None:
    """Active rebase with unreadable target is not treated as plain detached HEAD."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    rebase_merge = worktree_path / ".git" / "rebase-merge"
    rebase_merge.mkdir(parents=True)

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): CommandResult(
                command=("git", "rev-parse", "--abbrev-ref", "HEAD"),
                return_code=0,
                stdout="HEAD\n",
                stderr="",
            ),
            ("git", "rev-parse", "--git-path", "rebase-merge/head-name"): CommandResult(
                command=("git", "rev-parse", "--git-path", "rebase-merge/head-name"),
                return_code=0,
                stdout=f"{rebase_merge / 'missing-head-name'}\n",
                stderr="",
            ),
            ("git", "rev-parse", "--git-path", "rebase-apply/head-name"): CommandResult(
                command=("git", "rev-parse", "--git-path", "rebase-apply/head-name"),
                return_code=0,
                stdout=f"{worktree_path / '.git' / 'rebase-apply' / 'head-name'}\n",
                stderr="",
            ),
            ("git", "rev-parse", "--git-path", "rebase-merge"): CommandResult(
                command=("git", "rev-parse", "--git-path", "rebase-merge"),
                return_code=0,
                stdout=f"{rebase_merge}\n",
                stderr="",
            ),
        }
    )

    from backend.core.use_cases.run_agent_once import _ensure_worktree_branch

    with pytest.raises(RuntimeError, match="target branch cannot be confirmed"):
        _ensure_worktree_branch(
            worktree_path,
            "issue-123",
            IssueSummary(number=123, title="T", url="U", body="B", labels=()),
            AppConfig(),
            fake_runner,
        )

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "checkout", "issue-123") not in commands
    assert ("git", "checkout", "-b", "issue-123") not in commands
    assert ("git", "rebase", "--abort") not in commands


def test_ensure_worktree_branch_resolves_conflicts_via_agent(tmp_path: Path) -> None:
    """Active rebase with conflicts is resolved by the configured agent."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    git_dir = worktree_path / ".git"
    git_dir.mkdir()
    rebase_merge = git_dir / "rebase-merge"
    rebase_merge.mkdir()
    head_name = rebase_merge / "head-name"
    head_name.write_text("refs/heads/issue-123", encoding="utf-8")

    class _AgentWritingRunner(FakeProcessRunner):
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
            if command[0] in ("claude", "kimi", "codex"):
                request_path = cwd / ".agent-runner" / "commit-request.json"
                request_path.parent.mkdir(parents=True, exist_ok=True)
                request_path.write_text(
                    '{"commit_message": "agent: resolve rebase conflicts"}',
                    encoding="utf-8",
                )
                (cwd / "file.txt").write_text("resolved\n", encoding="utf-8")
                return CommandResult(tuple(command), 0, "", "")
            if tuple(command) == ("git", "status", "--porcelain"):
                return CommandResult(tuple(command), 0, " M file.txt\n", "")
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
                input_text=input_text,
                label=label,
            )

    fake_runner = _AgentWritingRunner(
        responses={
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): CommandResult(
                command=("git", "rev-parse", "--abbrev-ref", "HEAD"),
                return_code=0,
                stdout="HEAD\n",
                stderr="",
            ),
            ("git", "rev-parse", "--git-path", "rebase-merge/head-name"): CommandResult(
                command=("git", "rev-parse", "--git-path", "rebase-merge/head-name"),
                return_code=0,
                stdout=f"{head_name}\n",
                stderr="",
            ),
            ("git", "diff", "--name-only", "--diff-filter", "U"): CommandResult(
                command=("git", "diff", "--name-only", "--diff-filter", "U"),
                return_code=0,
                stdout="file.txt\n",
                stderr="",
            ),
            ("git", "status", "--porcelain"): CommandResult(
                command=("git", "status", "--porcelain"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            ("git", "add", "-A"): CommandResult(
                command=("git", "add", "-A"),
                return_code=0,
                stdout="",
                stderr="",
            ),
            (
                "git",
                "commit",
                "-m",
                "agent: resolve rebase conflicts",
            ): CommandResult(
                command=("git", "commit", "-m", "agent: resolve rebase conflicts"),
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
            ("echo", "ok"): CommandResult(
                command=("echo", "ok"),
                return_code=0,
                stdout="ok\n",
                stderr="",
            ),
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="issue-123\n",
                stderr="",
            ),
        }
    )

    from backend.core.use_cases.run_agent_once import _ensure_worktree_branch

    issue = IssueSummary(number=123, title="T", url="U", body="B", labels=())
    config = AppConfig(
        runner=RunnerConfig(
            default_agent="claude",
            verification_commands=["echo ok"],
        )
    )

    _ensure_worktree_branch(worktree_path, "issue-123", issue, config, fake_runner)

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "-c", "core.editor=true", "rebase", "--continue") in commands
    assert ("git", "commit", "-m", "agent: resolve rebase conflicts") in commands


def test_commit_requested_changes_rejects_detached_head(tmp_path: Path) -> None:
    """Commit proxy must refuse to commit when the worktree is detached."""
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()
    commit_request_path = worktree_path / ".agent-runner" / "commit-request.json"
    commit_request_path.parent.mkdir(parents=True, exist_ok=True)
    commit_request_path.write_text('{"commit_message": "agent: commit"}', encoding="utf-8")
    file_path = worktree_path / "file.txt"
    file_path.write_text("change\n", encoding="utf-8")

    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): CommandResult(
                command=("git", "branch", "--show-current"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )
    config = AppConfig(
        worktree=WorktreeConfig(
            create_command="echo",
            reuse_command="echo",
            path_command="echo",
        )
    )

    with pytest.raises(RuntimeError, match="detached HEAD"):
        commit_requested_changes(
            IssueSummary(number=1, title="T", url="U", body="B", labels=()),
            worktree_path,
            config,
            fake_runner,
            expected_branch="issue-1",
        )


def test_run_once_rebase_conflict_detached_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending rework rebase_pr_branch should succeed even with detached HEAD."""
    import json

    from backend.core.use_cases.agent_runner_events import format_event_marker
    from backend.core.use_cases.agent_runner_orchestrate import run_once

    issue = IssueSummary(
        number=73,
        title="Rebase detached head test",
        url="https://github.com/example/repo/issues/73",
        body="",
        labels=("agent/running", "agent/codex"),
    )
    worktree_path = tmp_path / "issue-73"
    worktree_path.mkdir()

    marker = format_event_marker(
        phase="post_pr_rework_requested",
        cycle=1,
        head_sha="abc123",
        pr_branch="issue-73",
        action="rebase_pr_branch",
    )
    rework_comment = "\n".join(
        [
            marker,
            "",
            "## Agent Runner Post-PR Rework Requested",
            "",
            "- Action: rebase_pr_branch",
            "- PR Branch: `issue-73`",
            "- Head SHA: `abc123`",
        ]
    )

    fake_client = FakeGitHubClient()
    fake_client.list_review_candidate_issues = lambda labels, limit: (
        [issue] if "agent/running" in labels else []
    )
    fake_client.comment_issue(73, rework_comment)
    fake_client._open_prs["issue-73"] = "https://github.com/example/repo/pull/73"
    fake_client._pr_contexts["issue-73"] = PullRequestContext(
        pr_url="https://github.com/example/repo/pull/73",
        branch="issue-73",
        head_sha="abc123",
        base_sha="base123",
    )

    rebase_merge_dir = worktree_path / ".git" / "rebase-merge"
    rebase_merge_dir.mkdir(parents=True, exist_ok=True)
    head_name_path = rebase_merge_dir / "head-name"
    head_name_path.write_text("refs/heads/issue-73", encoding="utf-8")

    request_path = worktree_path / ".agent-runner" / "commit-request.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
        json.dumps({"commit_message": "resolve conflict"}),
        encoding="utf-8",
    )

    class _DetachedHeadReworkRunner(FakeProcessRunner):
        def __init__(self, worktree_path: Path) -> None:
            super().__init__()
            self._worktree_path = worktree_path
            self._branch_calls = 0

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
            if command_tuple == ("git", "branch", "--show-current"):
                self._branch_calls += 1
                self.calls.append(list(command))
                if self._branch_calls <= 2:
                    return CommandResult(command_tuple, 0, "issue-73\n", "")
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == (
                "git",
                "rev-parse",
                "--git-path",
                "rebase-merge/head-name",
            ):
                self.calls.append(list(command))
                head_name_path = self._worktree_path / ".git" / "rebase-merge" / "head-name"
                return CommandResult(command_tuple, 0, str(head_name_path) + "\n", "")
            return super().run(
                command,
                cwd=cwd,
                check=check,
                timeout=timeout,
                capture_output=capture_output,
                label=label,
            )

    fake_runner = _DetachedHeadReworkRunner(worktree_path)
    path_command, path_result = worktree_path_response(worktree_path)
    fake_runner.responses = {
        path_command: path_result,
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
        (
            "git",
            "push",
            "--force-with-lease",
            "origin",
            "issue-73",
        ): CommandResult(
            command=(
                "git",
                "push",
                "--force-with-lease",
                "origin",
                "issue-73",
            ),
            return_code=0,
            stdout="",
            stderr="",
        ),
        git_remote_command(): git_remote_result("origin"),
    }

    def _noop_run_agent(
        agent_name,
        prompt,
        worktree_path,
        process_runner,
        *,
        capture_output=False,
        timeout_seconds=None,
        issue=None,
    ):
        return CommandResult(command=("noop",), return_code=0, stdout="", stderr="")

    monkeypatch.setattr(
        "backend.core.use_cases.pr_supervisor.run_agent_with_prompt",
        _noop_run_agent,
    )

    config = AppConfig(
        runner=RunnerConfig(verification_commands=()),
        worktree=WorktreeConfig(path_command=f"echo {worktree_path}"),
        pre_pr_review=PrePrReviewConfig(enabled=False),
        post_pr_supervisor=PostPrSupervisorConfig(enabled=False),
    )

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
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "-c", "core.editor=true", "rebase", "--continue") in commands
    assert (
        "git",
        "push",
        "--force-with-lease",
        "origin",
        "issue-73",
    ) in commands
    assert not any(c[:2] == ("git", "commit") for c in commands)
