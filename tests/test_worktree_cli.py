"""End-to-end tests for the built-in ``iar worktree`` subcommand group.

These tests exercise the CLI through real ``git`` subprocesses inside
temporary repositories, asserting on actual filesystem state. The goal is
to lock the contract that ``create`` and ``path`` always agree on the
absolute worktree location, so the historical
``PosixPath(... not found)`` regression cannot return.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.api.cli import main
from backend.core.shared.models.agent_runner import (
    AppConfig,
    IssueSummary,
    WorktreeConfig,
)
from backend.core.use_cases.run_agent_once import create_or_reuse_worktree
from backend.infrastructure.git.worktree import WorktreeManager, WORKTREE_DIR_NAME
from backend.infrastructure.process_runner import SubprocessRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _run_git(repo_path: Path, *git_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *git_args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _init_git_repository(tmp_path: Path, name: str) -> Path:
    """Create a throwaway Git repository with an initial commit on main."""
    repo_path = tmp_path / name
    repo_path.mkdir()
    _run_git(repo_path, "init", "--initial-branch=main")
    _run_git(repo_path, "config", "user.email", "test@example.com")
    _run_git(repo_path, "config", "user.name", "Test User")
    (repo_path / "README.md").write_text("placeholder", encoding="utf-8")
    (repo_path / ".iar.toml").write_text(
        '[agent_runner.repository]\nid = "test-repo"\n',
        encoding="utf-8",
    )
    _run_git(repo_path, "add", "README.md", ".iar.toml")
    _run_git(repo_path, "commit", "-m", "init")
    return repo_path


def test_worktree_path_returns_consistent_layout(tmp_path: Path) -> None:
    """``worktree_path`` must produce the canonical location regardless of state."""
    repo_path = _init_git_repository(tmp_path, "target")
    manager = WorktreeManager(repo_path, SubprocessRunner())
    expected = (repo_path / WORKTREE_DIR_NAME / "issue-7").resolve()
    assert manager.worktree_path("issue-7") == expected
    # Repeat the call: no side effects, always the same answer.
    assert manager.worktree_path("issue-7") == expected


def test_worktree_create_creates_directory(tmp_path: Path) -> None:
    """``create`` materializes the worktree directory and registers it with git."""
    repo_path = _init_git_repository(tmp_path, "target")
    manager = WorktreeManager(repo_path, SubprocessRunner())
    target = manager.create(branch="issue-9", base_branch="main")
    assert target.exists()
    assert target.is_dir()
    list_result = _run_git(repo_path, "worktree", "list")
    assert str(target) in list_result.stdout
    # The new branch must actually point at the worktree's HEAD.
    rev_parse = _run_git(repo_path, "rev-parse", "issue-9")
    head_in_worktree = _run_git(target, "rev-parse", "HEAD")
    assert rev_parse.stdout.strip() == head_in_worktree.stdout.strip()


def test_worktree_create_excludes_worktree_dir_from_index(tmp_path: Path) -> None:
    """``create`` writes info/exclude so ``git add -A`` cannot stage worktrees.

    Regression guard: without the exclusion, a repository-wide ``git add -A``
    stages the embedded worktree as a gitlink and git emits the
    ``adding embedded git repository`` warning.
    """
    repo_path = _init_git_repository(tmp_path, "target")
    manager = WorktreeManager(repo_path, SubprocessRunner())
    manager.create(branch="issue-9", base_branch="main")

    exclude_path = repo_path / ".git" / "info" / "exclude"
    assert (
        f"/{WORKTREE_DIR_NAME}/"
        in exclude_path.read_text(encoding="utf-8").splitlines()
    )

    _run_git(repo_path, "add", "-A")
    staged_result = _run_git(repo_path, "diff", "--cached", "--name-only")
    assert WORKTREE_DIR_NAME not in staged_result.stdout

    # Idempotent: a second create for another branch adds no duplicate line.
    manager.create(branch="issue-10", base_branch="main")
    exclude_lines = exclude_path.read_text(encoding="utf-8").splitlines()
    assert exclude_lines.count(f"/{WORKTREE_DIR_NAME}/") == 1


def test_worktree_remove_cleans_up(tmp_path: Path) -> None:
    """``remove`` deletes the directory and prunes git's metadata."""
    repo_path = _init_git_repository(tmp_path, "target")
    manager = WorktreeManager(repo_path, SubprocessRunner())
    target = manager.create(branch="issue-3", base_branch="main")
    assert target.exists()
    manager.remove(branch="issue-3")
    assert not target.exists()
    # The worktree entry must be gone from git's bookkeeping.
    list_result = _run_git(repo_path, "worktree", "list")
    assert str(target) not in list_result.stdout


def test_iar_worktree_create_cli_real_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``iar worktree create`` subcommand must succeed against a real repo."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    exit_code = main(
        ["worktree", "create", "--branch", "cli-1", "--base-branch", "main"]
    )
    assert exit_code == 0
    worktree_path = repo_path / WORKTREE_DIR_NAME / "cli-1"
    assert worktree_path.exists()
    list_result = _run_git(repo_path, "worktree", "list")
    assert str(worktree_path) in list_result.stdout


def test_iar_worktree_path_cli_real_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``iar worktree path`` must print the canonical absolute path."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)
    exit_code = main(["worktree", "path", "--branch", "cli-2"])
    assert exit_code == 0
    captured = capsys.readouterr()
    expected = str((repo_path / WORKTREE_DIR_NAME / "cli-2").resolve())
    assert captured.out.strip() == expected


def test_iar_worktree_create_cli_copies_missing_env_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``iar worktree create`` must carry gitignored ``.env*`` files over.

    ``git worktree add`` only materializes tracked files, so without the
    copy step every fresh worktree is born without ``.env`` and agent
    commands that read worktree-local env files break.
    """
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / ".gitignore").write_text(".env*\n!.env*.example\n", encoding="utf-8")
    (repo_path / ".env.example").write_text("EXAMPLE=tracked\n", encoding="utf-8")
    _run_git(repo_path, "add", ".gitignore", ".env.example")
    _run_git(repo_path, "commit", "-m", "track env example")
    (repo_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    nested_env_path = repo_path / "tests" / "playwright-e2e" / ".env"
    nested_env_path.parent.mkdir(parents=True)
    nested_env_path.write_text("E2E=1\n", encoding="utf-8")

    monkeypatch.chdir(repo_path)
    exit_code = main(
        ["worktree", "create", "--branch", "cli-env", "--base-branch", "main"]
    )

    assert exit_code == 0
    worktree_path = repo_path / WORKTREE_DIR_NAME / "cli-env"
    assert (worktree_path / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
    assert (worktree_path / "tests" / "playwright-e2e" / ".env").read_text(
        encoding="utf-8"
    ) == "E2E=1\n"
    # The tracked example file comes from git itself, not the copy step.
    assert (worktree_path / ".env.example").read_text(
        encoding="utf-8"
    ) == "EXAMPLE=tracked\n"
    # Copied env files are gitignored, so the worktree must stay clean —
    # otherwise `iar worktree cleanup` would refuse to delete it later.
    status_result = _run_git(worktree_path, "status", "--porcelain")
    assert status_result.stdout.strip() == ""


def test_create_or_reuse_worktree_heals_env_files_on_reuse(
    tmp_path: Path,
) -> None:
    """Reusing an existing env-less worktree backfills missing env files.

    Worktrees created before the env-copy fix exist on disk without
    ``.env``; the next ``iar run`` that reuses them must heal the gap
    instead of leaving them broken forever.
    """
    repo_path = _init_git_repository(tmp_path, "target")
    (repo_path / ".gitignore").write_text(".env*\n", encoding="utf-8")
    _run_git(repo_path, "add", ".gitignore")
    _run_git(repo_path, "commit", "-m", "ignore env files")
    (repo_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    manager = WorktreeManager(repo_path, SubprocessRunner())
    worktree_path = manager.create(branch="issue-42", base_branch="main")
    assert not (worktree_path / ".env").exists()

    config = AppConfig(
        worktree=WorktreeConfig(
            create_command="false",
            reuse_command="true",
            path_command=f"echo {worktree_path}",
            base_branch="main",
        )
    )
    issue = IssueSummary(
        number=42,
        title="demo",
        url="https://example/issues/42",
        body="",
        labels=(),
    )
    resolved_worktree_path = create_or_reuse_worktree(
        repo_path, issue, config, SubprocessRunner()
    )

    assert resolved_worktree_path == worktree_path.resolve()
    assert (worktree_path / ".env").read_text(encoding="utf-8") == "SECRET=1\n"


def test_create_or_reuse_worktree_anchors_relative_path_output_to_repo(
    tmp_path: Path,
) -> None:
    """A relative ``path_command`` output must anchor to the repo, not cwd.

    ``path_command`` runs with ``cwd=repo_path``; before the fix a relative
    stdout was resolved against the daemon process cwd, pointing at a
    directory that does not exist (or worse, the wrong repository).
    """
    repo_path = _init_git_repository(tmp_path, "target")
    manager = WorktreeManager(repo_path, SubprocessRunner())
    worktree_path = manager.create(branch="issue-77", base_branch="main")

    config = AppConfig(
        worktree=WorktreeConfig(
            create_command="false",
            reuse_command="true",
            path_command=f"echo {WORKTREE_DIR_NAME}/issue-77",
            base_branch="main",
        )
    )
    issue = IssueSummary(
        number=77,
        title="demo",
        url="https://example/issues/77",
        body="",
        labels=(),
    )

    resolved_worktree_path = create_or_reuse_worktree(
        repo_path, issue, config, SubprocessRunner()
    )

    assert resolved_worktree_path == worktree_path.resolve()


def test_create_or_reuse_worktree_fails_fast_on_missing_path(
    tmp_path: Path,
) -> None:
    """``create_or_reuse_worktree`` must raise when path_command points nowhere.

    This guards against the historical ``PosixPath(... not found)`` bug:
    if the path_command output does not correspond to a real directory,
    the function raises FileNotFoundError with a structured message that
    names all three commands' return codes — not the opaque path error
    that previously hid the root cause.
    """
    repo_path = _init_git_repository(tmp_path, "target")
    bogus_path = tmp_path / "this-directory-truly-does-not-exist-issue-1"
    config = AppConfig(
        worktree=WorktreeConfig(
            create_command="true",
            reuse_command="true",
            path_command=f"echo {bogus_path}",
            base_branch="main",
        )
    )
    issue = IssueSummary(
        number=1,
        title="demo",
        url="https://example/issues/1",
        body="",
        labels=(),
    )
    with pytest.raises(FileNotFoundError) as exc_info:
        create_or_reuse_worktree(repo_path, issue, config, SubprocessRunner())
    message = str(exc_info.value)
    assert "worktree path does not exist" in message
    assert str(bogus_path) in message
    assert "create_command return_code" in message
    assert "path_command return_code" in message
