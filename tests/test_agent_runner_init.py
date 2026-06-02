"""Tests for repository-local IAR initialization."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.api.cli import main

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
    repo_path = tmp_path / name
    repo_path.mkdir()
    _run_git(repo_path, "init")
    _run_git(repo_path, "checkout", "-b", "main")
    _run_git(repo_path, "remote", "add", "origin", "git@github.com:example/target.git")
    return repo_path


def test_iar_init_dry_run_real_entry(tmp_path: Path) -> None:
    """uv run iar init --dry-run should print TOML and not write .iar.toml."""
    repo_path = _init_git_repository(tmp_path, "target")
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPOSITORY_ROOT),
            "iar",
            "init",
            "--dry-run",
        ],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "[agent_runner.repository]" in completed.stdout
    assert 'id = "target"' in completed.stdout
    assert not (repo_path / ".iar.toml").exists()


def test_iar_init_writes_protects_and_force_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """iar init should write once, protect existing files, and honor --force."""
    repo_path = _init_git_repository(tmp_path, "target")
    monkeypatch.chdir(repo_path)

    first_exit_code = main(["init"])
    config_path = repo_path / ".iar.toml"
    first_config_text = config_path.read_text(encoding="utf-8")

    # iAR-owned worktree commands must be present and the legacy
    # `just worktree` formula must be gone, so the historical
    # `PosixPath not found` regression cannot return.
    assert "iar worktree create --branch issue-{issue_number}" in first_config_text
    assert "iar worktree path --branch issue-{issue_number}" in first_config_text
    assert "just worktree" not in first_config_text

    second_exit_code = main(["init"])
    protected_config_text = config_path.read_text(encoding="utf-8")

    force_exit_code = main(
        [
            "init",
            "--force",
            "--id",
            "replacement",
            "--display-name",
            "Replacement",
            "--remote",
            "upstream",
            "--base-branch",
            "develop",
        ]
    )
    overwritten_config_text = config_path.read_text(encoding="utf-8")

    assert first_exit_code == 0
    assert "[agent_runner.repository]" in first_config_text
    assert "[agent_runner.git]" in first_config_text
    assert "[agent_runner.runner]" in first_config_text
    assert second_exit_code == 1
    assert protected_config_text == first_config_text
    assert force_exit_code == 0
    assert 'id = "replacement"' in overwritten_config_text
    assert 'display_name = "Replacement"' in overwritten_config_text
    assert 'remote = "upstream"' in overwritten_config_text
    assert 'base_branch = "develop"' in overwritten_config_text
