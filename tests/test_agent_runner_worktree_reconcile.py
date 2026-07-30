"""Tests for reconciling a worktree with its remote branch (fake runner).

Covers ``_reconcile_worktree_with_remote_branch`` fast-forward, dirty,
diverged and no-op paths, plus how ``create_or_reuse_worktree`` and
``run_once`` drive it."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    GitConfig,
    IssueSummary,
    WorktreeConfig,
)
from backend.core.use_cases.run_agent_once import (
    create_or_reuse_worktree,
    _reconcile_worktree_with_remote_branch,
)
from tests.conftest import FakeGitHubClient, FakeProcessRunner
from tests.support.agent_runner import (
    config_with_review_disabled,
    git_remote_result,
    make_ready_issue,
    write_commit_request,
)


def _worktree_reconcile_branch_result(branch: str = "issue-123") -> CommandResult:
    return CommandResult(
        command=("git", "branch", "--show-current"),
        return_code=0,
        stdout=f"{branch}\n",
        stderr="",
    )


def _worktree_reconcile_ls_remote_result(
    *, exists: bool, remote: str = "origin", branch: str = "issue-123"
) -> tuple[tuple[str, ...], CommandResult]:
    stdout = f"abc123\trefs/heads/{branch}\n" if exists else ""
    command = ("git", "ls-remote", "--heads", remote, branch)
    return command, CommandResult(command=command, return_code=0, stdout=stdout, stderr="")


def _worktree_reconcile_rev_parse_result(
    ref: str, sha: str
) -> tuple[tuple[str, ...], CommandResult]:
    command = ("git", "rev-parse", ref)
    return command, CommandResult(command=command, return_code=0, stdout=f"{sha}\n", stderr="")


def _worktree_reconcile_ancestor_result(
    ancestor: str, descendant: str, is_ancestor: bool
) -> tuple[tuple[str, ...], CommandResult]:
    command = ("git", "merge-base", "--is-ancestor", ancestor, descendant)
    return command, CommandResult(
        command=command, return_code=0 if is_ancestor else 1, stdout="", stderr=""
    )


def _worktree_reconcile_status_result(dirty: bool) -> CommandResult:
    return CommandResult(
        command=("git", "status", "--porcelain"),
        return_code=0,
        stdout=" M file.txt\n" if dirty else "",
        stderr="",
    )


def test_worktree_reconcile_remote_ahead_clean_fast_forwards(tmp_path: Path) -> None:
    """Clean local-behind worktree fast-forwards to the fetched remote branch."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _worktree_reconcile_branch_result(),
            _worktree_reconcile_ls_remote_result(exists=True)[
                0
            ]: _worktree_reconcile_ls_remote_result(exists=True)[1],
            _worktree_reconcile_rev_parse_result("HEAD", "local-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("HEAD", "local-sha")[1],
            _worktree_reconcile_rev_parse_result("origin/issue-123", "remote-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("origin/issue-123", "remote-sha")[1],
            _worktree_reconcile_ancestor_result("local-sha", "origin/issue-123", True)[
                0
            ]: _worktree_reconcile_ancestor_result("local-sha", "origin/issue-123", True)[1],
            _worktree_reconcile_ancestor_result("remote-sha", "HEAD", False)[
                0
            ]: _worktree_reconcile_ancestor_result("remote-sha", "HEAD", False)[1],
            ("git", "status", "--porcelain"): _worktree_reconcile_status_result(dirty=False),
            ("git", "merge", "--ff-only", "origin/issue-123"): CommandResult(
                command=("git", "merge", "--ff-only", "origin/issue-123"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )

    _reconcile_worktree_with_remote_branch(tmp_path, AppConfig(), fake_runner)

    commands = [tuple(c) for c in fake_runner.calls]
    assert (
        "git",
        "fetch",
        "origin",
        "+issue-123:refs/remotes/origin/issue-123",
    ) in commands
    assert ("git", "merge", "--ff-only", "origin/issue-123") in commands


def test_worktree_reconcile_remote_ahead_dirty_fails(tmp_path: Path) -> None:
    """Dirty local-behind worktree fails instead of resetting local changes."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _worktree_reconcile_branch_result(),
            _worktree_reconcile_ls_remote_result(exists=True)[
                0
            ]: _worktree_reconcile_ls_remote_result(exists=True)[1],
            _worktree_reconcile_rev_parse_result("HEAD", "local-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("HEAD", "local-sha")[1],
            _worktree_reconcile_rev_parse_result("origin/issue-123", "remote-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("origin/issue-123", "remote-sha")[1],
            _worktree_reconcile_ancestor_result("local-sha", "origin/issue-123", True)[
                0
            ]: _worktree_reconcile_ancestor_result("local-sha", "origin/issue-123", True)[1],
            _worktree_reconcile_ancestor_result("remote-sha", "HEAD", False)[
                0
            ]: _worktree_reconcile_ancestor_result("remote-sha", "HEAD", False)[1],
            ("git", "status", "--porcelain"): _worktree_reconcile_status_result(dirty=True),
        }
    )

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        _reconcile_worktree_with_remote_branch(tmp_path, AppConfig(), fake_runner)

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "merge", "--ff-only", "origin/issue-123") not in commands
    assert ("git", "reset", "--hard") not in commands


def test_worktree_reconcile_local_ahead_preserved(tmp_path: Path) -> None:
    """Local-ahead worktree is left untouched to support publish recovery."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _worktree_reconcile_branch_result(),
            _worktree_reconcile_ls_remote_result(exists=True)[
                0
            ]: _worktree_reconcile_ls_remote_result(exists=True)[1],
            _worktree_reconcile_rev_parse_result("HEAD", "local-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("HEAD", "local-sha")[1],
            _worktree_reconcile_rev_parse_result("origin/issue-123", "remote-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("origin/issue-123", "remote-sha")[1],
            _worktree_reconcile_ancestor_result("local-sha", "origin/issue-123", False)[
                0
            ]: _worktree_reconcile_ancestor_result("local-sha", "origin/issue-123", False)[1],
            _worktree_reconcile_ancestor_result("remote-sha", "HEAD", True)[
                0
            ]: _worktree_reconcile_ancestor_result("remote-sha", "HEAD", True)[1],
        }
    )

    _reconcile_worktree_with_remote_branch(tmp_path, AppConfig(), fake_runner)

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "merge", "--ff-only", "origin/issue-123") not in commands
    assert ("git", "reset", "--hard") not in commands


def test_worktree_reconcile_diverged_fails(tmp_path: Path) -> None:
    """Diverged branch fails and requests manual reconciliation."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _worktree_reconcile_branch_result(),
            _worktree_reconcile_ls_remote_result(exists=True)[
                0
            ]: _worktree_reconcile_ls_remote_result(exists=True)[1],
            _worktree_reconcile_rev_parse_result("HEAD", "local-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("HEAD", "local-sha")[1],
            _worktree_reconcile_rev_parse_result("origin/issue-123", "remote-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("origin/issue-123", "remote-sha")[1],
            _worktree_reconcile_ancestor_result("local-sha", "origin/issue-123", False)[
                0
            ]: _worktree_reconcile_ancestor_result("local-sha", "origin/issue-123", False)[1],
            _worktree_reconcile_ancestor_result("remote-sha", "HEAD", False)[
                0
            ]: _worktree_reconcile_ancestor_result("remote-sha", "HEAD", False)[1],
        }
    )

    with pytest.raises(RuntimeError, match="diverged"):
        _reconcile_worktree_with_remote_branch(tmp_path, AppConfig(), fake_runner)

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "merge") not in commands
    assert ("git", "rebase") not in commands
    assert ("git", "reset") not in commands


def test_worktree_reconcile_missing_remote_branch_noops(tmp_path: Path) -> None:
    """No-op when the configured remote does not have the current branch."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _worktree_reconcile_branch_result(),
            _worktree_reconcile_ls_remote_result(exists=False)[
                0
            ]: _worktree_reconcile_ls_remote_result(exists=False)[1],
        }
    )

    _reconcile_worktree_with_remote_branch(tmp_path, AppConfig(), fake_runner)

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "fetch") not in commands


def test_worktree_reconcile_already_synced_noops(tmp_path: Path) -> None:
    """No-op when local HEAD equals the fetched remote branch."""
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _worktree_reconcile_branch_result(),
            _worktree_reconcile_ls_remote_result(exists=True)[
                0
            ]: _worktree_reconcile_ls_remote_result(exists=True)[1],
            _worktree_reconcile_rev_parse_result("HEAD", "same-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("HEAD", "same-sha")[1],
            _worktree_reconcile_rev_parse_result("origin/issue-123", "same-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("origin/issue-123", "same-sha")[1],
        }
    )

    _reconcile_worktree_with_remote_branch(tmp_path, AppConfig(), fake_runner)

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "merge", "--ff-only", "origin/issue-123") not in commands


def test_worktree_reconcile_uses_configured_remote(tmp_path: Path) -> None:
    """Reconcile uses config.git.remote instead of hard-coded origin."""
    config = AppConfig(git=GitConfig(remote="zata"))
    fake_runner = FakeProcessRunner(
        responses={
            ("git", "branch", "--show-current"): _worktree_reconcile_branch_result(),
            _worktree_reconcile_ls_remote_result(exists=True, remote="zata")[
                0
            ]: _worktree_reconcile_ls_remote_result(exists=True, remote="zata")[1],
            _worktree_reconcile_rev_parse_result("HEAD", "local-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("HEAD", "local-sha")[1],
            _worktree_reconcile_rev_parse_result("zata/issue-123", "remote-sha")[
                0
            ]: _worktree_reconcile_rev_parse_result("zata/issue-123", "remote-sha")[1],
            _worktree_reconcile_ancestor_result("local-sha", "zata/issue-123", True)[
                0
            ]: _worktree_reconcile_ancestor_result("local-sha", "zata/issue-123", True)[1],
            _worktree_reconcile_ancestor_result("remote-sha", "HEAD", False)[
                0
            ]: _worktree_reconcile_ancestor_result("remote-sha", "HEAD", False)[1],
            ("git", "status", "--porcelain"): _worktree_reconcile_status_result(dirty=False),
            ("git", "merge", "--ff-only", "zata/issue-123"): CommandResult(
                command=("git", "merge", "--ff-only", "zata/issue-123"),
                return_code=0,
                stdout="",
                stderr="",
            ),
        }
    )

    _reconcile_worktree_with_remote_branch(tmp_path, config, fake_runner)

    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "ls-remote", "--heads", "zata", "issue-123") in commands
    assert (
        "git",
        "fetch",
        "zata",
        "+issue-123:refs/remotes/zata/issue-123",
    ) in commands
    assert ("git", "merge", "--ff-only", "zata/issue-123") in commands


def test_create_or_reuse_worktree_calls_reconcile(tmp_path: Path) -> None:
    """create_or_reuse_worktree should invoke remote branch reconciliation."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    path_command = ("echo", str(worktree_path))
    fake_runner = FakeProcessRunner(
        responses={
            path_command: CommandResult(
                command=path_command,
                return_code=0,
                stdout=f"{worktree_path}\n",
                stderr="",
            ),
            ("git", "branch", "--show-current"): _worktree_reconcile_branch_result(),
            _worktree_reconcile_ls_remote_result(exists=False)[
                0
            ]: _worktree_reconcile_ls_remote_result(exists=False)[1],
        }
    )
    config = AppConfig(
        worktree=WorktreeConfig(
            create_command=f"echo created {worktree_path}",
            reuse_command=f"echo reused {worktree_path}",
            path_command=f"echo {worktree_path}",
        )
    )

    result_path = create_or_reuse_worktree(
        tmp_path,
        IssueSummary(number=123, title="T", url="U", body="B", labels=()),
        config,
        fake_runner,
    )

    assert result_path == worktree_path.resolve()
    commands = [tuple(c) for c in fake_runner.calls]
    assert ("git", "branch", "--show-current") in commands


def test_create_or_reuse_worktree_heals_detached_before_reconcile(
    tmp_path: Path,
) -> None:
    """Detached worktree is reattached before remote branch reconciliation."""
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    path_command = ("echo", str(worktree_path))

    class _DetachedThenBranchRunner(FakeProcessRunner):
        def __init__(self) -> None:
            super().__init__(
                responses={
                    path_command: CommandResult(
                        command=path_command,
                        return_code=0,
                        stdout=f"{worktree_path}\n",
                        stderr="",
                    ),
                    ("git", "rev-parse", "--git-path", "info/exclude"): CommandResult(
                        command=("git", "rev-parse", "--git-path", "info/exclude"),
                        return_code=1,
                        stdout="",
                        stderr="",
                    ),
                    ("git", "rev-parse", "--abbrev-ref", "HEAD"): CommandResult(
                        command=("git", "rev-parse", "--abbrev-ref", "HEAD"),
                        return_code=0,
                        stdout="HEAD\n",
                        stderr="",
                    ),
                    (
                        "git",
                        "rev-parse",
                        "--git-path",
                        "rebase-merge/head-name",
                    ): CommandResult(
                        command=(
                            "git",
                            "rev-parse",
                            "--git-path",
                            "rebase-merge/head-name",
                        ),
                        return_code=0,
                        stdout="missing-rebase-merge-head-name\n",
                        stderr="",
                    ),
                    (
                        "git",
                        "rev-parse",
                        "--git-path",
                        "rebase-apply/head-name",
                    ): CommandResult(
                        command=(
                            "git",
                            "rev-parse",
                            "--git-path",
                            "rebase-apply/head-name",
                        ),
                        return_code=0,
                        stdout="missing-rebase-apply-head-name\n",
                        stderr="",
                    ),
                    ("git", "rev-parse", "--git-path", "rebase-merge"): CommandResult(
                        command=("git", "rev-parse", "--git-path", "rebase-merge"),
                        return_code=0,
                        stdout="missing-rebase-merge\n",
                        stderr="",
                    ),
                    ("git", "rev-parse", "--git-path", "rebase-apply"): CommandResult(
                        command=("git", "rev-parse", "--git-path", "rebase-apply"),
                        return_code=0,
                        stdout="missing-rebase-apply\n",
                        stderr="",
                    ),
                    ("git", "rev-parse", "HEAD"): CommandResult(
                        command=("git", "rev-parse", "HEAD"),
                        return_code=0,
                        stdout="detached-sha\n",
                        stderr="",
                    ),
                    (
                        "git",
                        "show-ref",
                        "--verify",
                        "--quiet",
                        "refs/heads/issue-123",
                    ): CommandResult(
                        command=(
                            "git",
                            "show-ref",
                            "--verify",
                            "--quiet",
                            "refs/heads/issue-123",
                        ),
                        return_code=0,
                        stdout="",
                        stderr="",
                    ),
                    ("git", "rev-parse", "refs/heads/issue-123"): CommandResult(
                        command=("git", "rev-parse", "refs/heads/issue-123"),
                        return_code=0,
                        stdout="detached-sha\n",
                        stderr="",
                    ),
                    ("git", "checkout", "issue-123"): CommandResult(
                        command=("git", "checkout", "issue-123"),
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
                    _worktree_reconcile_ls_remote_result(exists=False)[
                        0
                    ]: _worktree_reconcile_ls_remote_result(exists=False)[1],
                }
            )

    fake_runner = _DetachedThenBranchRunner()
    config = AppConfig(
        worktree=WorktreeConfig(
            create_command=f"echo created {worktree_path}",
            reuse_command=f"echo reused {worktree_path}",
            path_command=f"echo {worktree_path}",
        )
    )

    result_path = create_or_reuse_worktree(
        tmp_path,
        IssueSummary(number=123, title="T", url="U", body="B", labels=()),
        config,
        fake_runner,
    )

    assert result_path == worktree_path.resolve()
    commands = [tuple(c) for c in fake_runner.calls]
    checkout_index = commands.index(("git", "checkout", "issue-123"))
    ls_remote_index = commands.index(("git", "ls-remote", "--heads", "origin", "issue-123"))
    assert checkout_index < ls_remote_index
    assert ("git", "ls-remote", "--heads", "origin", "") not in commands


def test_worktree_reconcile_run_once(tmp_path: Path) -> None:
    """run_once issues reconcile commands before invoking the agent."""
    fake_client = FakeGitHubClient()
    issue = make_ready_issue()
    fake_client.list_ready_issues = lambda ready_label, limit: [issue]
    worktree_path = tmp_path / "issue-123"
    worktree_path.mkdir()
    write_commit_request(worktree_path, "agent: run once reconcile")

    class _ReconcileOrderRunner(FakeProcessRunner):
        def __init__(self, wt_path: Path) -> None:
            super().__init__()
            self._wt_path = wt_path
            self._sha_calls = 0
            self._status_calls = 0
            self._committed = False
            self.responses = {
                ("git", "remote"): git_remote_result("origin"),
                (
                    "git",
                    "ls-remote",
                    "--heads",
                    "origin",
                    "refs/heads/iar-evidence/*",
                ): CommandResult(
                    command=(
                        "git",
                        "ls-remote",
                        "--heads",
                        "origin",
                        "refs/heads/iar-evidence/*",
                    ),
                    return_code=0,
                    stdout="",
                    stderr="",
                ),
                (
                    "git",
                    "branch",
                    "--show-current",
                ): _worktree_reconcile_branch_result(),
                _worktree_reconcile_ls_remote_result(exists=True)[
                    0
                ]: _worktree_reconcile_ls_remote_result(exists=True)[1],
                _worktree_reconcile_rev_parse_result("origin/issue-123", "remote-sha")[
                    0
                ]: _worktree_reconcile_rev_parse_result("origin/issue-123", "remote-sha")[1],
                _worktree_reconcile_ancestor_result("local-sha", "origin/issue-123", True)[
                    0
                ]: _worktree_reconcile_ancestor_result("local-sha", "origin/issue-123", True)[1],
                _worktree_reconcile_ancestor_result("remote-sha", "HEAD", False)[
                    0
                ]: _worktree_reconcile_ancestor_result("remote-sha", "HEAD", False)[1],
                ("git", "merge", "--ff-only", "origin/issue-123"): CommandResult(
                    command=("git", "merge", "--ff-only", "origin/issue-123"),
                    return_code=0,
                    stdout="",
                    stderr="",
                ),
                ("git", "rev-parse", "--git-path", "info/exclude"): CommandResult(
                    command=("git", "rev-parse", "--git-path", "info/exclude"),
                    return_code=0,
                    stdout=".git/info/exclude\n",
                    stderr="",
                ),
                ("git", "rev-list", "--count", "origin/main..HEAD"): CommandResult(
                    command=("git", "rev-list", "--count", "origin/main..HEAD"),
                    return_code=0,
                    stdout="0\n",
                    stderr="",
                ),
                ("git", "push", "-u", "origin", "issue-123"): CommandResult(
                    command=("git", "push", "-u", "origin", "issue-123"),
                    return_code=0,
                    stdout="",
                    stderr="",
                ),
            }

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
            if command_tuple == ("git", "rev-parse", "HEAD"):
                self._sha_calls += 1
                sha = (
                    "after-sha"
                    if self._committed
                    else "remote-sha"
                    if self._sha_calls > 1
                    else "local-sha"
                )
                return CommandResult(command_tuple, 0, f"{sha}\n", "")
            if command_tuple == ("git", "status", "--porcelain"):
                self._status_calls += 1
                status_stdout = (
                    ""
                    if self._status_calls == 1 or self._committed
                    else " M file.txt\n?? .agent-runner/commit-request.json\n"
                )
                return CommandResult(command_tuple, 0, status_stdout, "")
            if command_tuple[:1] == ("codex",):
                (self._wt_path / "fake.txt").write_text("change\n", encoding="utf-8")
                return CommandResult(command_tuple, 0, "", "")
            if command_tuple == ("git", "commit", "-m", "agent: run once reconcile"):
                self._committed = True
                return CommandResult(command_tuple, 0, "", "")
            return CommandResult(command_tuple, 0, "", "")

    path_command = ("echo", str(worktree_path))
    fake_runner = _ReconcileOrderRunner(worktree_path)
    fake_runner.responses[path_command] = CommandResult(
        command=path_command, return_code=0, stdout=f"{worktree_path}\n", stderr=""
    )
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
    commands = [tuple(c) for c in fake_runner.calls]
    merge_index = commands.index(("git", "merge", "--ff-only", "origin/issue-123"))
    codex_indices = [i for i, c in enumerate(commands) if c[:1] == ("codex",)]
    assert codex_indices
    assert all(merge_index < idx for idx in codex_indices)
