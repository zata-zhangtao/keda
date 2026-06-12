"""Tests for stale iAR issue worktree cleanup."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.api.cli import main
from backend.core.use_cases.worktree_cleanup import (
    WorktreeCleanupRequest,
    WorktreeCleanupStatus,
    cleanup_iar_worktrees,
)
from backend.infrastructure.git.worktree import WORKTREE_DIR_NAME
from backend.infrastructure.process_runner import SubprocessRunner
from tests.conftest import FakeGitHubClient


def _run_git(repo_path: Path, *git_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *git_args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _init_remote_backed_repository(tmp_path: Path) -> Path:
    remote_path = tmp_path / "origin.git"
    repo_path = tmp_path / "target"
    _run_git(tmp_path, "init", "--bare", str(remote_path))
    repo_path.mkdir()
    _run_git(repo_path, "init", "--initial-branch=main")
    _run_git(repo_path, "config", "user.email", "test@example.com")
    _run_git(repo_path, "config", "user.name", "Test User")
    (repo_path / "README.md").write_text("placeholder", encoding="utf-8")
    _run_git(repo_path, "add", "README.md")
    _run_git(repo_path, "commit", "-m", "init")
    _run_git(repo_path, "remote", "add", "origin", str(remote_path))
    _run_git(repo_path, "push", "-u", "origin", "main")
    return repo_path


def _create_issue_worktree(repo_path: Path, issue_number: int) -> Path:
    branch = f"issue-{issue_number}"
    worktree_path = repo_path / WORKTREE_DIR_NAME / branch
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(repo_path, "worktree", "add", "-b", branch, str(worktree_path), "main")
    return worktree_path


def _local_branch_names(repo_path: Path) -> set[str]:
    branch_result = _run_git(repo_path, "branch", "--format", "%(refname:short)")
    return {
        branch_line.strip()
        for branch_line in branch_result.stdout.splitlines()
        if branch_line.strip()
    }


def _closed_issue_client(*issue_numbers: int) -> FakeGitHubClient:
    github_client = FakeGitHubClient()
    for issue_number in issue_numbers:
        github_client._issue_states[issue_number] = "CLOSED"
    return github_client


def test_cleanup_dry_run_reports_closed_issue_with_deleted_remote_branch(
    tmp_path: Path,
) -> None:
    """Dry-run should report eligible stale branches without deleting them."""
    repo_path = _init_remote_backed_repository(tmp_path)
    worktree_path = _create_issue_worktree(repo_path, 7)
    github_client = _closed_issue_client(7)

    cleanup_result = cleanup_iar_worktrees(
        WorktreeCleanupRequest(repo_path=repo_path, dry_run=True),
        github_client=github_client,
        process_runner=SubprocessRunner(),
    )

    assert cleanup_result.would_delete_count == 1
    assert cleanup_result.branches[0].status is WorktreeCleanupStatus.WOULD_DELETE
    assert "issue-7" in _local_branch_names(repo_path)
    assert worktree_path.exists()


def test_cleanup_deletes_clean_merged_closed_issue_branch(tmp_path: Path) -> None:
    """Cleanup should remove both the managed worktree and local branch."""
    repo_path = _init_remote_backed_repository(tmp_path)
    worktree_path = _create_issue_worktree(repo_path, 8)
    github_client = _closed_issue_client(8)

    cleanup_result = cleanup_iar_worktrees(
        WorktreeCleanupRequest(repo_path=repo_path, dry_run=False),
        github_client=github_client,
        process_runner=SubprocessRunner(),
    )

    assert cleanup_result.deleted_count == 1
    assert cleanup_result.branches[0].status is WorktreeCleanupStatus.DELETED
    assert "issue-8" not in _local_branch_names(repo_path)
    assert not worktree_path.exists()


def test_cleanup_handles_manually_deleted_worktree_directory(
    tmp_path: Path,
) -> None:
    """A manually removed worktree directory must not crash the cleanup run.

    The stale entry still shows up in ``git worktree list --porcelain``
    (as prunable); running ``git status`` with the missing directory as
    cwd would raise OSError. Cleanup must instead treat it as clean,
    remove the stale registration, and delete the branch.
    """
    repo_path = _init_remote_backed_repository(tmp_path)
    worktree_path = _create_issue_worktree(repo_path, 31)
    shutil.rmtree(worktree_path)
    github_client = _closed_issue_client(31)

    cleanup_result = cleanup_iar_worktrees(
        WorktreeCleanupRequest(repo_path=repo_path, dry_run=False),
        github_client=github_client,
        process_runner=SubprocessRunner(),
    )

    assert cleanup_result.deleted_count == 1
    assert cleanup_result.branches[0].status is WorktreeCleanupStatus.DELETED
    assert "issue-31" not in _local_branch_names(repo_path)
    worktree_list_result = _run_git(repo_path, "worktree", "list", "--porcelain")
    assert str(worktree_path) not in worktree_list_result.stdout


def test_cleanup_skips_dirty_worktree_by_default(tmp_path: Path) -> None:
    """Dirty managed worktrees should survive cleanup unless force is used."""
    repo_path = _init_remote_backed_repository(tmp_path)
    worktree_path = _create_issue_worktree(repo_path, 9)
    (worktree_path / "dirty.txt").write_text("local notes", encoding="utf-8")
    github_client = _closed_issue_client(9)

    cleanup_result = cleanup_iar_worktrees(
        WorktreeCleanupRequest(repo_path=repo_path, dry_run=False),
        github_client=github_client,
        process_runner=SubprocessRunner(),
    )

    branch_result = cleanup_result.branches[0]
    assert branch_result.status is WorktreeCleanupStatus.SKIPPED
    assert "uncommitted changes" in branch_result.reason
    assert "issue-9" in _local_branch_names(repo_path)
    assert worktree_path.exists()


def test_cleanup_skips_when_remote_branch_still_exists(tmp_path: Path) -> None:
    """A closed Issue is not enough; the remote branch must be gone too."""
    repo_path = _init_remote_backed_repository(tmp_path)
    worktree_path = _create_issue_worktree(repo_path, 10)
    _run_git(repo_path, "push", "origin", "issue-10")
    github_client = _closed_issue_client(10)

    cleanup_result = cleanup_iar_worktrees(
        WorktreeCleanupRequest(repo_path=repo_path, dry_run=False),
        github_client=github_client,
        process_runner=SubprocessRunner(),
    )

    branch_result = cleanup_result.branches[0]
    assert branch_result.status is WorktreeCleanupStatus.SKIPPED
    assert "remote branch origin/issue-10 still exists" == branch_result.reason
    assert "issue-10" in _local_branch_names(repo_path)
    assert worktree_path.exists()


def test_cleanup_skips_unmerged_branch_by_default(tmp_path: Path) -> None:
    """Closed Issues with unmerged local commits require explicit force."""
    repo_path = _init_remote_backed_repository(tmp_path)
    worktree_path = _create_issue_worktree(repo_path, 11)
    (worktree_path / "feature.txt").write_text("feature", encoding="utf-8")
    _run_git(worktree_path, "add", "feature.txt")
    _run_git(worktree_path, "commit", "-m", "feature")
    github_client = _closed_issue_client(11)

    cleanup_result = cleanup_iar_worktrees(
        WorktreeCleanupRequest(repo_path=repo_path, dry_run=False),
        github_client=github_client,
        process_runner=SubprocessRunner(),
    )

    branch_result = cleanup_result.branches[0]
    assert branch_result.status is WorktreeCleanupStatus.SKIPPED
    assert "not merged into origin/main" in branch_result.reason
    assert "no merged PR was found" in branch_result.reason
    assert "issue-11" in _local_branch_names(repo_path)
    assert worktree_path.exists()


def test_cleanup_deletes_squash_merged_branch_with_merged_pr(
    tmp_path: Path,
) -> None:
    """Squash/rebase merges are not git ancestors; rely on merged PR state."""
    repo_path = _init_remote_backed_repository(tmp_path)
    worktree_path = _create_issue_worktree(repo_path, 13)
    (worktree_path / "feature.txt").write_text("feature", encoding="utf-8")
    _run_git(worktree_path, "add", "feature.txt")
    _run_git(worktree_path, "commit", "-m", "feature")
    github_client = _closed_issue_client(13)
    github_client._merged_prs["issue-13"] = "https://github.com/example/repo/pull/13"

    cleanup_result = cleanup_iar_worktrees(
        WorktreeCleanupRequest(repo_path=repo_path, dry_run=False),
        github_client=github_client,
        process_runner=SubprocessRunner(),
    )

    branch_result = cleanup_result.branches[0]
    assert branch_result.status is WorktreeCleanupStatus.DELETED
    assert "issue-13" not in _local_branch_names(repo_path)
    assert not worktree_path.exists()


def test_cleanup_dry_run_reports_squash_merged_branch_with_merged_pr(
    tmp_path: Path,
) -> None:
    """Dry-run should report squash-merged branches as would-delete."""
    repo_path = _init_remote_backed_repository(tmp_path)
    worktree_path = _create_issue_worktree(repo_path, 14)
    (worktree_path / "feature.txt").write_text("feature", encoding="utf-8")
    _run_git(worktree_path, "add", "feature.txt")
    _run_git(worktree_path, "commit", "-m", "feature")
    github_client = _closed_issue_client(14)
    github_client._merged_prs["issue-14"] = "https://github.com/example/repo/pull/14"

    cleanup_result = cleanup_iar_worktrees(
        WorktreeCleanupRequest(repo_path=repo_path, dry_run=True),
        github_client=github_client,
        process_runner=SubprocessRunner(),
    )

    branch_result = cleanup_result.branches[0]
    assert branch_result.status is WorktreeCleanupStatus.WOULD_DELETE
    assert "issue-14" in _local_branch_names(repo_path)
    assert worktree_path.exists()


def test_iar_worktree_cleanup_yes_deletes_eligible_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real CLI entry should wire cleanup to the GitHub client factory."""
    repo_path = _init_remote_backed_repository(tmp_path)
    (repo_path / ".iar.toml").write_text(
        """
[agent_runner.repository]
id = "target"

[agent_runner.git]
remote = "origin"
base_branch = "main"
""".lstrip(),
        encoding="utf-8",
    )
    worktree_path = _create_issue_worktree(repo_path, 12)
    github_client = _closed_issue_client(12)
    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("IAR_SKIP_GH_AUTH_CHECK", "1")

    with patch("backend.api.cli.create_github_client", return_value=github_client):
        exit_code = main(["worktree", "cleanup", "--yes"])

    assert exit_code == 0
    assert "issue-12" not in _local_branch_names(repo_path)
    assert not worktree_path.exists()
