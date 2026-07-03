"""Tests for frontend ``node_modules`` propagation into worktrees.

``link_frontend_node_modules`` is pure filesystem logic, so its legacy symlink
fallback tests use plain temporary directories. ``ensure_frontend_node_modules``
needs a package manager install, so its tests use a fake process runner.

The ``create_or_reuse_worktree`` wiring is covered in ``test_worktree_cli.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.core.shared.models.agent_runner import CommandResult
from backend.core.use_cases.worktree_frontend import (
    ensure_frontend_node_modules,
    link_frontend_node_modules,
)
from tests.conftest import FakeProcessRunner


def _write_file(file_path: Path, content: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def _make_frontend_project(project_dir: Path) -> None:
    """Create a minimal frontend project with installed dependencies."""
    _write_file(project_dir / "package.json", "{}\n")
    _write_file(project_dir / "node_modules" / ".bin" / "vite", "#!/bin/sh\n")


def _make_fake_process_runner(*, return_code: int = 0, stderr: str = "") -> FakeProcessRunner:
    """Return a fake runner that makes every install command succeed (or fail)."""
    return FakeProcessRunner(
        responses={
            tuple(command): CommandResult(
                command=tuple(command),
                return_code=return_code,
                stdout="",
                stderr=stderr,
            )
            for command in [
                ["pnpm", "install", "--ignore-scripts"],
                ["npm", "ci", "--ignore-scripts"],
                ["npm", "install", "--ignore-scripts"],
                ["yarn", "install", "--ignore-scripts"],
                ["bun", "install", "--ignore-scripts"],
            ]
        }
    )


class TestEnsureFrontendNodeModules:
    """Install-first ``ensure_frontend_node_modules`` behavior."""

    def test_installs_when_pnpm_lockfile_present(self, tmp_path: Path) -> None:
        """A pnpm-lock.yaml triggers pnpm install in the worktree."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "pnpm-lock.yaml", "lockfile\n")
        process_runner = _make_fake_process_runner()

        installed_paths, linked_paths = ensure_frontend_node_modules(
            repo_path, worktree_path, process_runner
        )

        assert [str(path) for path in installed_paths] == ["frontend"]
        assert linked_paths == []
        assert not (worktree_path / "frontend" / "node_modules").is_symlink()
        assert process_runner.calls == [
            ["pnpm", "install", "--ignore-scripts"],
        ]
        assert process_runner.calls[0] == ["pnpm", "install", "--ignore-scripts"]
        assert process_runner.calls[0][0] == "pnpm"
        assert process_runner.calls[0][1] == "install"

    def test_installs_when_npm_lockfile_present(self, tmp_path: Path) -> None:
        """A package-lock.json triggers npm ci."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "package-lock.json", "{}\n")
        process_runner = _make_fake_process_runner()

        installed_paths, linked_paths = ensure_frontend_node_modules(
            repo_path, worktree_path, process_runner
        )

        assert [str(path) for path in installed_paths] == ["frontend"]
        assert linked_paths == []
        assert process_runner.calls == [["npm", "ci", "--ignore-scripts"]]

    def test_installs_when_yarn_lockfile_present(self, tmp_path: Path) -> None:
        """A yarn.lock triggers yarn install."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "yarn.lock", "{}\n")
        process_runner = _make_fake_process_runner()

        installed_paths, linked_paths = ensure_frontend_node_modules(
            repo_path, worktree_path, process_runner
        )

        assert [str(path) for path in installed_paths] == ["frontend"]
        assert linked_paths == []
        assert process_runner.calls == [["yarn", "install", "--ignore-scripts"]]

    def test_installs_when_bun_lockfile_present(self, tmp_path: Path) -> None:
        """A bun.lock triggers bun install."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "bun.lock", "{}\n")
        process_runner = _make_fake_process_runner()

        installed_paths, linked_paths = ensure_frontend_node_modules(
            repo_path, worktree_path, process_runner
        )

        assert [str(path) for path in installed_paths] == ["frontend"]
        assert linked_paths == []
        assert process_runner.calls == [["bun", "install", "--ignore-scripts"]]

    def test_installs_when_bun_lockb_present(self, tmp_path: Path) -> None:
        """The legacy bun.lockb also triggers bun install."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "bun.lockb", "{}\n")
        process_runner = _make_fake_process_runner()

        installed_paths, linked_paths = ensure_frontend_node_modules(
            repo_path, worktree_path, process_runner
        )

        assert [str(path) for path in installed_paths] == ["frontend"]
        assert linked_paths == []
        assert process_runner.calls == [["bun", "install", "--ignore-scripts"]]

    def test_falls_back_to_symlink_when_no_lockfile(self, tmp_path: Path) -> None:
        """Without a lockfile, the install is skipped and symlink fallback runs."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        process_runner = _make_fake_process_runner()

        installed_paths, linked_paths = ensure_frontend_node_modules(
            repo_path, worktree_path, process_runner
        )

        assert installed_paths == []
        assert [str(path) for path in linked_paths] == ["frontend"]
        assert process_runner.calls == []
        assert (worktree_path / "frontend" / "node_modules").is_symlink()

    def test_falls_back_to_symlink_when_install_fails(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed install logs a warning and falls back to symlink."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "pnpm-lock.yaml", "lockfile\n")
        process_runner = _make_fake_process_runner(return_code=1, stderr="network error")

        with caplog.at_level(logging.WARNING):
            installed_paths, linked_paths = ensure_frontend_node_modules(
                repo_path, worktree_path, process_runner
            )

        assert installed_paths == []
        assert [str(path) for path in linked_paths] == ["frontend"]
        assert (worktree_path / "frontend" / "node_modules").is_symlink()
        assert "pnpm install failed" in caplog.text
        assert "network error" in caplog.text

    def test_runs_install_in_project_directory(self, tmp_path: Path) -> None:
        """The install command is executed with the project directory as cwd."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "pnpm-lock.yaml", "lockfile\n")
        process_runner = _make_fake_process_runner()

        ensure_frontend_node_modules(repo_path, worktree_path, process_runner)

        assert process_runner.calls == [["pnpm", "install", "--ignore-scripts"]]
        assert process_runner.calls[0] == ["pnpm", "install", "--ignore-scripts"]

    def test_skips_existing_real_node_modules(self, tmp_path: Path) -> None:
        """An existing directory is left untouched and no install runs."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "pnpm-lock.yaml", "lockfile\n")
        _write_file(worktree_path / "frontend" / "node_modules" / "marker.txt", "local\n")
        process_runner = _make_fake_process_runner()

        installed_paths, linked_paths = ensure_frontend_node_modules(
            repo_path, worktree_path, process_runner
        )

        assert installed_paths == []
        assert linked_paths == []
        assert process_runner.calls == []
        assert not (worktree_path / "frontend" / "node_modules").is_symlink()

    def test_skips_existing_symlink(self, tmp_path: Path) -> None:
        """An existing symlink is left untouched and no install runs."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "pnpm-lock.yaml", "lockfile\n")
        dangling_target = tmp_path / "missing-deps"
        (worktree_path / "frontend" / "node_modules").symlink_to(dangling_target)
        process_runner = _make_fake_process_runner()

        installed_paths, linked_paths = ensure_frontend_node_modules(
            repo_path, worktree_path, process_runner
        )

        assert installed_paths == []
        assert linked_paths == []
        assert process_runner.calls == []
        assert (worktree_path / "frontend" / "node_modules").readlink() == dangling_target

    def test_prunes_vcs_cache_and_nested_node_modules(self, tmp_path: Path) -> None:
        """Manifests under pruned directories are ignored during install scan."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "pnpm-lock.yaml", "lockfile\n")
        _write_file(worktree_path / "node_modules" / "dep" / "package.json", "{}\n")
        _write_file(worktree_path / ".git" / "package.json", "{}\n")
        _write_file(
            worktree_path / ".iar-worktrees" / "issue-9" / "frontend" / "package.json",
            "{}\n",
        )
        process_runner = _make_fake_process_runner()

        installed_paths, _ = ensure_frontend_node_modules(repo_path, worktree_path, process_runner)

        assert [str(path) for path in installed_paths] == ["frontend"]
        assert not (worktree_path / "node_modules" / "dep" / "node_modules").exists()
        assert not (
            worktree_path / ".iar-worktrees" / "issue-9" / "frontend" / "node_modules"
        ).exists()

    def test_warns_when_install_and_symlink_both_unavailable(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No lockfile and no main-checkout deps results in a clear warning."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _write_file(repo_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        process_runner = _make_fake_process_runner()

        with caplog.at_level(logging.WARNING):
            installed_paths, linked_paths = ensure_frontend_node_modules(
                repo_path, worktree_path, process_runner
            )

        assert installed_paths == []
        assert linked_paths == []
        assert "has no node_modules" in caplog.text

    def test_returns_empty_when_worktree_is_repo_root(self, tmp_path: Path) -> None:
        """Operating on the repo root itself is a no-op."""
        repo_path = tmp_path / "repo"
        _make_frontend_project(repo_path / "frontend")
        process_runner = _make_fake_process_runner()

        assert ensure_frontend_node_modules(repo_path, repo_path, process_runner) == (
            [],
            [],
        )


class TestLinkFrontendNodeModules:
    """Legacy symlink-only fallback behavior."""

    def test_links_node_modules_for_root_and_nested_projects(self, tmp_path: Path) -> None:
        """Every frontend project in the worktree gets a symlink to main's deps."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        # Main checkout has installed dependencies for both frontends.
        _make_frontend_project(repo_path / "frontend")
        _make_frontend_project(repo_path / "frontend-admin")
        # Worktree has the manifests (materialized by git) but no node_modules.
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        _write_file(worktree_path / "frontend-admin" / "package.json", "{}\n")

        linked_relative_paths = link_frontend_node_modules(repo_path, worktree_path)

        assert sorted(str(path) for path in linked_relative_paths) == [
            "frontend",
            "frontend-admin",
        ]
        linked_vite_path = worktree_path / "frontend" / "node_modules" / ".bin" / "vite"
        assert (worktree_path / "frontend" / "node_modules").is_symlink()
        # Following the link reaches the real binary in the main checkout.
        assert linked_vite_path.exists()
        assert (
            linked_vite_path.resolve()
            == (repo_path / "frontend" / "node_modules" / ".bin" / "vite").resolve()
        )

    def test_never_overwrites_existing_worktree_node_modules(self, tmp_path: Path) -> None:
        """A real ``node_modules`` already installed in the worktree is untouched."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        # A previous `npm install` left a real directory in the worktree.
        _write_file(worktree_path / "frontend" / "node_modules" / "marker.txt", "local\n")

        linked_relative_paths = link_frontend_node_modules(repo_path, worktree_path)

        assert linked_relative_paths == []
        assert not (worktree_path / "frontend" / "node_modules").is_symlink()
        assert (worktree_path / "frontend" / "node_modules" / "marker.txt").read_text(
            encoding="utf-8"
        ) == "local\n"

    def test_does_not_clobber_existing_dangling_symlink(self, tmp_path: Path) -> None:
        """An existing (even broken) ``node_modules`` symlink is left in place."""
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _make_frontend_project(repo_path / "frontend")
        _write_file(worktree_path / "frontend" / "package.json", "{}\n")
        dangling_target = tmp_path / "missing-deps"
        (worktree_path / "frontend" / "node_modules").symlink_to(dangling_target)

        linked_relative_paths = link_frontend_node_modules(repo_path, worktree_path)

        assert linked_relative_paths == []
        assert (worktree_path / "frontend" / "node_modules").readlink() == dangling_target

    def test_returns_empty_when_no_frontend_projects(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repo"
        worktree_path = tmp_path / "worktree"
        _write_file(repo_path / "README.md", "no frontend here\n")
        _write_file(worktree_path / "README.md", "no frontend here\n")

        assert link_frontend_node_modules(repo_path, worktree_path) == []

    def test_returns_empty_when_worktree_is_repo_root(self, tmp_path: Path) -> None:
        repo_path = tmp_path / "repo"
        _make_frontend_project(repo_path / "frontend")

        assert link_frontend_node_modules(repo_path, repo_path) == []
