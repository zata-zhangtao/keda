"""Tests for scripts/worktree/create.sh remote sync behavior."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _run_create_script(
    cwd: Path,
    branch: str,
    *,
    extra_args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "worktree" / "create.sh"
    )
    env = os.environ.copy()
    env.update(extra_env or {})
    args = [str(script_path), branch]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def _git_init_bare(path: Path) -> Path:
    subprocess.run(
        ["git", "init", "--bare", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def _git_init(path: Path) -> Path:
    subprocess.run(
        ["git", "init", str(path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    return path


def _commit(path: Path, message: str) -> str:
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", message],
        check=True,
        capture_output=True,
    )
    sha_result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return sha_result.stdout.strip()


def test_new_worktree_uses_latest_remote_base(tmp_path: Path) -> None:
    """When local main is behind origin/main, new worktree starts at origin/main."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    _git_init_bare(remote_path)
    local_repo = _git_init(local_path)

    subprocess.run(
        ["git", "-C", str(local_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    _commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    # Advance remote main from another clone
    other_path = tmp_path / "other"
    other_repo = _git_init(other_path)
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
            "main",
            "--track",
            "origin/main",
        ],
        check=True,
        capture_output=True,
    )
    remote_sha = _commit(other_repo, "remote advance")
    subprocess.run(
        ["git", "-C", str(other_repo), "push", "origin", "main"],
        check=True,
        capture_output=True,
    )

    result = _run_create_script(
        local_repo, "feature-x", extra_env={"KODA_WORKTREE_BASE_BRANCH": "main"}
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    worktree_path = local_path.parent / "local-worktrees" / "feature-x"
    assert worktree_path.exists()

    wt_sha_result = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert wt_sha_result.stdout.strip() == remote_sha

    local_main_sha = subprocess.run(
        ["git", "-C", str(local_repo), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert local_main_sha.stdout.strip() != remote_sha


def test_local_base_branch_not_moved(tmp_path: Path) -> None:
    """Creating worktree from remote ref must not move local refs/heads/main."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    _git_init_bare(remote_path)
    local_repo = _git_init(local_path)

    subprocess.run(
        ["git", "-C", str(local_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    local_sha = _commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    # Advance remote
    other_path = tmp_path / "other"
    other_repo = _git_init(other_path)
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
            "main",
            "--track",
            "origin/main",
        ],
        check=True,
        capture_output=True,
    )
    _commit(other_repo, "remote advance")
    subprocess.run(
        ["git", "-C", str(other_repo), "push", "origin", "main"],
        check=True,
        capture_output=True,
    )

    _run_create_script(
        local_repo, "feature-a", extra_env={"KODA_WORKTREE_BASE_BRANCH": "main"}
    )

    local_main_after = subprocess.run(
        ["git", "-C", str(local_repo), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert local_main_after.stdout.strip() == local_sha


def test_sync_disabled_uses_local_base(tmp_path: Path) -> None:
    """KODA_WORKTREE_SYNC_BASE=false keeps old local-base behavior."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    _git_init_bare(remote_path)
    local_repo = _git_init(local_path)

    subprocess.run(
        ["git", "-C", str(local_repo), "remote", "add", "origin", str(remote_path)],
        check=True,
        capture_output=True,
    )
    local_sha = _commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
    )

    # Advance remote
    other_path = tmp_path / "other"
    other_repo = _git_init(other_path)
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
            "main",
            "--track",
            "origin/main",
        ],
        check=True,
        capture_output=True,
    )
    remote_sha = _commit(other_repo, "remote advance")
    subprocess.run(
        ["git", "-C", str(other_repo), "push", "origin", "main"],
        check=True,
        capture_output=True,
    )

    result = _run_create_script(
        local_repo,
        "feature-y",
        extra_env={
            "KODA_WORKTREE_BASE_BRANCH": "main",
            "KODA_WORKTREE_SYNC_BASE": "false",
        },
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    worktree_path = local_path.parent / "local-worktrees" / "feature-y"
    assert worktree_path.exists()

    wt_sha = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert wt_sha.stdout.strip() == local_sha
    assert wt_sha.stdout.strip() != remote_sha


def test_no_remote_with_local_base_creates_worktree(tmp_path: Path) -> None:
    """No-remote repos fallback to local base and succeed."""
    local_path = tmp_path / "local"
    local_repo = _git_init(local_path)
    local_sha = _commit(local_repo, "initial")

    result = _run_create_script(
        local_repo, "feature-z", extra_env={"KODA_WORKTREE_BASE_BRANCH": "main"}
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    worktree_path = tmp_path / "local-worktrees" / "feature-z"
    assert worktree_path.exists()

    wt_sha = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert wt_sha.stdout.strip() == local_sha


def test_fetch_failure_exits_before_creating_worktree(tmp_path: Path) -> None:
    """Remote exists but fetch fails: exit non-zero, no worktree left behind."""
    local_path = tmp_path / "local"
    local_repo = _git_init(local_path)
    _commit(local_repo, "initial")

    subprocess.run(
        [
            "git",
            "-C",
            str(local_repo),
            "remote",
            "add",
            "origin",
            "/nonexistent/path/to/remote",
        ],
        check=True,
        capture_output=True,
    )

    result = _run_create_script(
        local_repo, "feature-fail", extra_env={"KODA_WORKTREE_BASE_BRANCH": "main"}
    )

    assert result.returncode != 0
    worktree_path = tmp_path / "local-worktrees" / "feature-fail"
    assert not worktree_path.exists()


def test_custom_remote_via_env(tmp_path: Path) -> None:
    """KODA_WORKTREE_BASE_REMOTE overrides remote name."""
    remote_path = tmp_path / "remote.git"
    local_path = tmp_path / "local"

    _git_init_bare(remote_path)
    local_repo = _git_init(local_path)

    subprocess.run(
        [
            "git",
            "-C",
            str(local_repo),
            "remote",
            "add",
            "upstream",
            str(remote_path),
        ],
        check=True,
        capture_output=True,
    )
    _commit(local_repo, "initial")
    subprocess.run(
        ["git", "-C", str(local_repo), "push", "-u", "upstream", "main"],
        check=True,
        capture_output=True,
    )

    # Advance upstream main
    other_path = tmp_path / "other"
    other_repo = _git_init(other_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(other_repo),
            "remote",
            "add",
            "upstream",
            str(remote_path),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(other_repo), "fetch", "upstream"],
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
            "main",
            "--track",
            "upstream/main",
        ],
        check=True,
        capture_output=True,
    )
    remote_sha = _commit(other_repo, "upstream advance")
    subprocess.run(
        ["git", "-C", str(other_repo), "push", "upstream", "main"],
        check=True,
        capture_output=True,
    )

    result = _run_create_script(
        local_repo,
        "feature-custom",
        extra_env={
            "KODA_WORKTREE_BASE_BRANCH": "main",
            "KODA_WORKTREE_BASE_REMOTE": "upstream",
        },
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    worktree_path = local_path.parent / "local-worktrees" / "feature-custom"
    assert worktree_path.exists()

    wt_sha = subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert wt_sha.stdout.strip() == remote_sha
