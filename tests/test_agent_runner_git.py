"""Real-git tests for agent runner git utilities.

``list_changed_paths`` feeds the forbidden-path safety gate
(``validate_safe_changes``), so these tests use real ``git`` subprocesses:
the bug class being locked down here is git's ``core.quotePath`` C-quoting
of non-ASCII paths, which mock-based tests cannot reproduce.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import AppConfig
from backend.core.use_cases.agent_runner_git import list_changed_paths
from backend.core.use_cases.agent_runner_publish import validate_safe_changes
from backend.infrastructure.process_runner import SubprocessRunner


def _run_git(repo_path: Path, *git_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *git_args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _init_git_repository(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _run_git(repo_path, "init", "--initial-branch=main")
    _run_git(repo_path, "config", "user.email", "test@example.com")
    _run_git(repo_path, "config", "user.name", "Test User")
    (repo_path / "README.md").write_text("placeholder\n", encoding="utf-8")
    _run_git(repo_path, "add", "README.md")
    _run_git(repo_path, "commit", "-m", "init")
    return repo_path


def test_list_changed_paths_returns_non_ascii_paths_verbatim(
    tmp_path: Path,
) -> None:
    """Non-ASCII paths must come back unquoted.

    With default ``core.quotePath=true``, plain ``--porcelain`` output
    C-quotes such paths (``"secrets/\\345\\257\\206..."``); the quoted text
    would never match forbidden-path patterns.
    """
    repo_path = _init_git_repository(tmp_path)
    secret_file_path = repo_path / "secrets" / "密钥.txt"
    secret_file_path.parent.mkdir()
    secret_file_path.write_text("token\n", encoding="utf-8")
    _run_git(repo_path, "add", "secrets")

    changed_paths = list_changed_paths(repo_path, SubprocessRunner())

    assert "secrets/密钥.txt" in changed_paths
    assert all('"' not in changed_path for changed_path in changed_paths)


def test_list_changed_paths_includes_rename_source_and_target(
    tmp_path: Path,
) -> None:
    """A staged rename must report both the new and the original path."""
    repo_path = _init_git_repository(tmp_path)
    _run_git(repo_path, "mv", "README.md", "说明.md")

    changed_paths = list_changed_paths(repo_path, SubprocessRunner())

    assert "说明.md" in changed_paths
    assert "README.md" in changed_paths


def test_validate_safe_changes_blocks_non_ascii_forbidden_path(
    tmp_path: Path,
) -> None:
    """The forbidden-path gate must catch non-ASCII paths under secrets/*."""
    repo_path = _init_git_repository(tmp_path)
    secret_file_path = repo_path / "secrets" / "密钥.txt"
    secret_file_path.parent.mkdir()
    secret_file_path.write_text("token\n", encoding="utf-8")
    _run_git(repo_path, "add", "secrets")

    with pytest.raises(RuntimeError, match="Refusing to publish forbidden paths"):
        validate_safe_changes(repo_path, AppConfig(), SubprocessRunner())
