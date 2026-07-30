"""Real Git integration tests for worktree/remote reconciliation.

Drives ``_reconcile_worktree_with_remote_branch`` and
``create_or_reuse_worktree`` against on-disk repositories instead of a
fake process runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import (
    AppConfig,
    GitConfig,
    IssueSummary,
    WorktreeConfig,
)
from backend.core.use_cases.run_agent_once import (
    create_or_reuse_worktree,
    _reconcile_worktree_with_remote_branch,
)
from tests.support.agent_runner import (
    create_commit,
    init_bare_git_repo,
    init_git_repo,
)


def test_worktree_reconcile_remote_ahead_real_git_fast_forwards(tmp_path: Path) -> None:
    """Reused clean worktree fast-forwards when remote branch is ahead."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    init_bare_git_repo(remote_path)
    local_repo = init_git_repo(local_path)
    subprocess.run(
        ["git", "-C", str(local_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "issue start")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "issue-123"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    worktree_path = tmp_path / "issue-123"
    subprocess.run(
        [
            "git",
            "-C",
            str(local_repo),
            "worktree",
            "add",
            str(worktree_path),
            "issue-123",
        ],
        check=True,
        capture_output=True,
    )
    local_head_before = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Advance remote issue-123 from another clone
    other_path = tmp_path / "other"
    other_repo = init_git_repo(other_path)
    subprocess.run(
        ["git", "-C", str(other_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(other_repo), "fetch", "origin"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(other_repo),
            "checkout",
            "-b",
            "issue-123",
            "--track",
            "origin/issue-123",
        ],
        check=True,
        capture_output=True,
    )
    remote_head = create_commit(other_repo, "remote advance")
    subprocess.run(
        ["git", "-C", str(other_repo), "push", "origin", "issue-123"],
        check=True,
        capture_output=True,
    )

    from backend.infrastructure.process_runner import SubprocessRunner

    _reconcile_worktree_with_remote_branch(worktree_path, AppConfig(), SubprocessRunner())

    local_head_after = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert local_head_before != remote_head
    assert local_head_after == remote_head


def test_worktree_reconcile_local_ahead_real_git_preserved(tmp_path: Path) -> None:
    """Local-ahead worktree keeps its commits when remote is behind."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    init_bare_git_repo(remote_path)
    local_repo = init_git_repo(local_path)
    subprocess.run(
        ["git", "-C", str(local_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "issue start")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "issue-123"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    worktree_path = tmp_path / "issue-123"
    subprocess.run(
        [
            "git",
            "-C",
            str(local_repo),
            "worktree",
            "add",
            str(worktree_path),
            "issue-123",
        ],
        check=True,
        capture_output=True,
    )

    local_ahead = create_commit(worktree_path, "local only")

    from backend.infrastructure.process_runner import SubprocessRunner

    _reconcile_worktree_with_remote_branch(worktree_path, AppConfig(), SubprocessRunner())

    local_head_after = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert local_head_after == local_ahead


def test_worktree_reconcile_dirty_real_git_fails(tmp_path: Path) -> None:
    """Dirty worktree behind remote fails without destructive reset."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    init_bare_git_repo(remote_path)
    local_repo = init_git_repo(local_path)
    subprocess.run(
        ["git", "-C", str(local_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    tracked = local_path / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(local_repo), "add", "tracked.txt"],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "issue start")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "issue-123"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    worktree_path = tmp_path / "issue-123"
    subprocess.run(
        [
            "git",
            "-C",
            str(local_repo),
            "worktree",
            "add",
            str(worktree_path),
            "issue-123",
        ],
        check=True,
        capture_output=True,
    )

    # Advance remote issue-123
    other_path = tmp_path / "other"
    other_repo = init_git_repo(other_path)
    subprocess.run(
        ["git", "-C", str(other_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(other_repo), "fetch", "origin"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(other_repo),
            "checkout",
            "-b",
            "issue-123",
            "--track",
            "origin/issue-123",
        ],
        check=True,
        capture_output=True,
    )
    create_commit(other_repo, "remote advance")
    subprocess.run(
        ["git", "-C", str(other_repo), "push", "origin", "issue-123"],
        check=True,
        capture_output=True,
    )

    # Make worktree dirty
    dirty_file = worktree_path / "tracked.txt"
    dirty_file.write_text("dirty\n", encoding="utf-8")

    from backend.infrastructure.process_runner import SubprocessRunner

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        _reconcile_worktree_with_remote_branch(worktree_path, AppConfig(), SubprocessRunner())

    status = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert "tracked.txt" in status


def test_worktree_reconcile_diverged_real_git_fails(tmp_path: Path) -> None:
    """Diverged worktree fails and does not rebase, merge, or reset."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    init_bare_git_repo(remote_path)
    local_repo = init_git_repo(local_path)
    subprocess.run(
        ["git", "-C", str(local_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "issue start")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "issue-123"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    worktree_path = tmp_path / "issue-123"
    subprocess.run(
        [
            "git",
            "-C",
            str(local_repo),
            "worktree",
            "add",
            str(worktree_path),
            "issue-123",
        ],
        check=True,
        capture_output=True,
    )

    # Local commit in worktree
    local_head = create_commit(worktree_path, "local advance")

    # Force-push a different history to remote
    other_path = tmp_path / "other"
    other_repo = init_git_repo(other_path)
    subprocess.run(
        ["git", "-C", str(other_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(other_repo), "fetch", "origin"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(other_repo),
            "checkout",
            "-b",
            "issue-123",
            "--track",
            "origin/issue-123",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(other_repo), "reset", "--hard", "origin/main"],
        check=True,
        capture_output=True,
    )
    _ = create_commit(other_repo, "diverged remote advance")
    subprocess.run(
        ["git", "-C", str(other_repo), "push", "--force", "origin", "issue-123"],
        check=True,
        capture_output=True,
    )

    from backend.infrastructure.process_runner import SubprocessRunner

    with pytest.raises(RuntimeError, match="diverged"):
        _reconcile_worktree_with_remote_branch(worktree_path, AppConfig(), SubprocessRunner())

    final_head = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert final_head == local_head


def test_worktree_reconcile_custom_remote_real_git(tmp_path: Path) -> None:
    """Reconcile fast-forwards using a non-origin remote name."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    init_bare_git_repo(remote_path)
    local_repo = init_git_repo(local_path)
    subprocess.run(
        ["git", "-C", str(local_repo), "remote", "add", "zata", str(remote_path)],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "zata", "main"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "issue start")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "zata", "issue-123"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    worktree_path = tmp_path / "issue-123"
    subprocess.run(
        [
            "git",
            "-C",
            str(local_repo),
            "worktree",
            "add",
            str(worktree_path),
            "issue-123",
        ],
        check=True,
        capture_output=True,
    )

    other_path = tmp_path / "other"
    other_repo = init_git_repo(other_path)
    subprocess.run(
        ["git", "-C", str(other_repo), "remote", "add", "zata", str(remote_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(other_repo), "fetch", "zata"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(other_repo),
            "checkout",
            "-b",
            "issue-123",
            "--track",
            "zata/issue-123",
        ],
        check=True,
        capture_output=True,
    )
    remote_head = create_commit(other_repo, "zata advance")
    subprocess.run(
        ["git", "-C", str(other_repo), "push", "zata", "issue-123"],
        check=True,
        capture_output=True,
    )

    from backend.infrastructure.process_runner import SubprocessRunner

    config = AppConfig(git=GitConfig(remote="zata"))
    _reconcile_worktree_with_remote_branch(worktree_path, config, SubprocessRunner())

    local_head_after = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert local_head_after == remote_head


def test_create_or_reuse_worktree_real_git_fast_forwards(tmp_path: Path) -> None:
    """create_or_reuse_worktree fast-forwards a reused clean worktree."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    init_bare_git_repo(remote_path)
    local_repo = init_git_repo(local_path)
    subprocess.run(
        ["git", "-C", str(local_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "issue start")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "issue-123"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    worktree_path = tmp_path / "issue-123"
    subprocess.run(
        [
            "git",
            "-C",
            str(local_repo),
            "worktree",
            "add",
            str(worktree_path),
            "issue-123",
        ],
        check=True,
        capture_output=True,
    )

    other_path = tmp_path / "other"
    other_repo = init_git_repo(other_path)
    subprocess.run(
        ["git", "-C", str(other_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(other_repo), "fetch", "origin"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(other_repo),
            "checkout",
            "-b",
            "issue-123",
            "--track",
            "origin/issue-123",
        ],
        check=True,
        capture_output=True,
    )
    remote_head = create_commit(other_repo, "remote advance")
    subprocess.run(
        ["git", "-C", str(other_repo), "push", "origin", "issue-123"],
        check=True,
        capture_output=True,
    )

    from backend.infrastructure.process_runner import SubprocessRunner

    config = AppConfig(
        worktree=WorktreeConfig(
            create_command="echo create-fail",
            reuse_command="echo reused",
            path_command=f"echo {worktree_path}",
        )
    )

    result_path = create_or_reuse_worktree(
        local_path,
        IssueSummary(number=123, title="T", url="U", body="B", labels=()),
        config,
        SubprocessRunner(),
    )

    assert result_path == worktree_path.resolve()
    local_head_after = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert local_head_after == remote_head


def test_create_or_reuse_worktree_heals_detached_head_real_git(tmp_path: Path) -> None:
    """create_or_reuse_worktree recovers a reused worktree in detached HEAD."""
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

    config = AppConfig(
        worktree=WorktreeConfig(
            create_command="echo create-fail",
            reuse_command="echo reused",
            path_command=f"echo {worktree_path}",
        )
    )

    result_path = create_or_reuse_worktree(
        repo_path,
        IssueSummary(number=123, title="T", url="U", body="B", labels=()),
        config,
        SubprocessRunner(),
    )

    assert result_path == worktree_path.resolve()
    current_branch = subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_branch == "issue-123"


def test_create_or_reuse_worktree_heals_detached_head_before_remote_reconcile_real_git(
    tmp_path: Path,
) -> None:
    """Detached reused worktree heals before remote reconciliation probes branch."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    init_bare_git_repo(remote_path)
    local_repo = init_git_repo(local_path)
    subprocess.run(
        ["git", "-C", str(local_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    create_commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "-b", "issue-123"],
        check=True,
        capture_output=True,
    )
    issue_sha = create_commit(local_repo, "issue start")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "issue-123"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local_repo), "checkout", "main"],
        check=True,
        capture_output=True,
    )

    worktree_path = tmp_path / "issue-123"
    subprocess.run(
        [
            "git",
            "-C",
            str(local_repo),
            "worktree",
            "add",
            str(worktree_path),
            "issue-123",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree_path), "checkout", "--detach", issue_sha],
        check=True,
        capture_output=True,
    )

    from backend.infrastructure.process_runner import SubprocessRunner

    config = AppConfig(
        worktree=WorktreeConfig(
            create_command="echo create-fail",
            reuse_command="echo reused",
            path_command=f"echo {worktree_path}",
        )
    )

    result_path = create_or_reuse_worktree(
        local_path,
        IssueSummary(number=123, title="T", url="U", body="B", labels=()),
        config,
        SubprocessRunner(),
    )

    assert result_path == worktree_path.resolve()
    current_branch = subprocess.run(
        ["git", "-C", str(worktree_path), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    current_head = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_branch == "issue-123"
    assert current_head == issue_sha
