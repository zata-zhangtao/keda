"""Tests for worktree env file propagation.

``copy_missing_env_files`` is pure filesystem logic, so these tests use
plain temporary directories — no git repositories required. The CLI and
``create_or_reuse_worktree`` wiring is covered in ``test_worktree_cli.py``.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.use_cases.worktree_env import copy_missing_env_files


def _write_file(file_path: Path, content: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def test_copies_missing_env_files_preserving_relative_paths(
    tmp_path: Path,
) -> None:
    """Root and nested ``.env*`` files land at the same relative paths."""
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    _write_file(repo_path / ".env", "ROOT=1\n")
    _write_file(repo_path / ".env.local", "LOCAL=1\n")
    _write_file(repo_path / "tests" / "playwright-e2e" / ".env", "E2E=1\n")
    _write_file(repo_path / "README.md", "not an env file\n")

    copied_relative_paths = copy_missing_env_files(repo_path, worktree_path)

    assert sorted(str(path) for path in copied_relative_paths) == [
        ".env",
        ".env.local",
        "tests/playwright-e2e/.env",
    ]
    assert (worktree_path / ".env").read_text(encoding="utf-8") == "ROOT=1\n"
    assert (worktree_path / "tests" / "playwright-e2e" / ".env").read_text(
        encoding="utf-8"
    ) == "E2E=1\n"
    assert not (worktree_path / "README.md").exists()


def test_never_overwrites_existing_worktree_files(tmp_path: Path) -> None:
    """Files already present in the worktree are left untouched.

    This covers both tracked ``.env*.example`` files materialized by git
    and worktree-local ``.env`` edits from a previous agent run.
    """
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    _write_file(repo_path / ".env", "ROOT=new\n")
    _write_file(repo_path / ".env.example", "EXAMPLE=source\n")
    _write_file(worktree_path / ".env", "ROOT=worktree-local\n")
    _write_file(worktree_path / ".env.example", "EXAMPLE=tracked\n")

    copied_relative_paths = copy_missing_env_files(repo_path, worktree_path)

    assert copied_relative_paths == []
    assert (worktree_path / ".env").read_text(encoding="utf-8") == "ROOT=worktree-local\n"
    assert (worktree_path / ".env.example").read_text(encoding="utf-8") == "EXAMPLE=tracked\n"


def test_prunes_vcs_cache_and_worktree_container_dirs(tmp_path: Path) -> None:
    """Env files under pruned directories must never be copied.

    The ``.iar-worktrees`` prune is the critical one: without it, a
    sibling worktree's ``.env`` would leak into every new worktree.
    """
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    _write_file(repo_path / ".env", "ROOT=1\n")
    _write_file(repo_path / ".git" / ".env", "GIT=1\n")
    _write_file(repo_path / ".venv" / ".env", "VENV=1\n")
    _write_file(repo_path / "node_modules" / "pkg" / ".env", "NODE=1\n")
    _write_file(repo_path / ".iar-worktrees" / "issue-9" / ".env", "SIBLING=1\n")

    copied_relative_paths = copy_missing_env_files(repo_path, worktree_path)

    assert [str(path) for path in copied_relative_paths] == [".env"]
    assert not (worktree_path / ".git").exists()
    assert not (worktree_path / ".iar-worktrees").exists()


def test_skips_sources_inside_target_worktree_itself(tmp_path: Path) -> None:
    """A worktree nested in the repo under a custom name is not a source.

    Custom ``create_command`` configurations may place worktrees inside
    the repository under a directory name that is not in the prune list;
    the containment check must stop self-copying.
    """
    repo_path = tmp_path / "repo"
    worktree_path = repo_path / "custom-worktrees" / "issue-5"
    _write_file(repo_path / ".env", "ROOT=1\n")
    _write_file(worktree_path / "nested" / ".env", "WORKTREE=1\n")

    copied_relative_paths = copy_missing_env_files(repo_path, worktree_path)

    assert [str(path) for path in copied_relative_paths] == [".env"]
    assert (worktree_path / ".env").read_text(encoding="utf-8") == "ROOT=1\n"
    # The worktree's own nested env file must not be duplicated elsewhere.
    assert not (worktree_path / "nested" / "nested").exists()


def test_returns_empty_when_no_env_files_exist(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    _write_file(repo_path / "README.md", "no env here\n")
    worktree_path.mkdir()

    assert copy_missing_env_files(repo_path, worktree_path) == []


def test_returns_empty_when_worktree_is_repo_root(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _write_file(repo_path / ".env", "ROOT=1\n")

    assert copy_missing_env_files(repo_path, repo_path) == []


def test_broken_source_symlink_is_skipped_best_effort(tmp_path: Path) -> None:
    """A dangling ``.env`` symlink must not abort the copy of other files."""
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    _write_file(repo_path / ".env", "ROOT=1\n")
    (repo_path / "sub").mkdir()
    (repo_path / "sub" / ".env").symlink_to(tmp_path / "missing-target")

    copied_relative_paths = copy_missing_env_files(repo_path, worktree_path)

    assert [str(path) for path in copied_relative_paths] == [".env"]
    assert not (worktree_path / "sub" / ".env").exists()
