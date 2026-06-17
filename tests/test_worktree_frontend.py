"""Tests for frontend ``node_modules`` propagation into worktrees.

``link_frontend_node_modules`` is pure filesystem logic, so these tests use
plain temporary directories — no git repositories required. The
``create_or_reuse_worktree`` wiring is covered in ``test_worktree_cli.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend.core.use_cases.worktree_frontend import link_frontend_node_modules


def _write_file(file_path: Path, content: str) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def _make_frontend_project(project_dir: Path) -> None:
    """Create a minimal frontend project with installed dependencies."""
    _write_file(project_dir / "package.json", "{}\n")
    _write_file(project_dir / "node_modules" / ".bin" / "vite", "#!/bin/sh\n")


def test_links_node_modules_for_root_and_nested_projects(tmp_path: Path) -> None:
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


def test_never_overwrites_existing_worktree_node_modules(tmp_path: Path) -> None:
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


def test_does_not_clobber_existing_dangling_symlink(tmp_path: Path) -> None:
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


def test_prunes_vcs_cache_and_nested_node_modules(tmp_path: Path) -> None:
    """Manifests under pruned directories must never be treated as projects.

    The ``node_modules`` and ``.iar-worktrees`` prunes are the critical
    ones: a package's own ``package.json`` and a sibling worktree's frontend
    must not be discovered and linked.
    """
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    _make_frontend_project(repo_path / "frontend")
    _write_file(worktree_path / "frontend" / "package.json", "{}\n")
    # Decoys that must be ignored by the scan: a dependency's own manifest
    # inside node_modules, plus manifests under VCS / sibling-worktree dirs.
    _write_file(worktree_path / "node_modules" / "dep" / "package.json", "{}\n")
    _write_file(worktree_path / ".git" / "package.json", "{}\n")
    _write_file(
        worktree_path / ".iar-worktrees" / "issue-9" / "frontend" / "package.json",
        "{}\n",
    )

    linked_relative_paths = link_frontend_node_modules(repo_path, worktree_path)

    assert [str(path) for path in linked_relative_paths] == ["frontend"]
    # Pruned directories must not have been treated as linkable projects.
    assert not (worktree_path / "node_modules" / "dep" / "node_modules").exists()
    assert not (
        worktree_path / ".iar-worktrees" / "issue-9" / "frontend" / "node_modules"
    ).exists()


def test_warns_and_skips_when_main_lacks_node_modules(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A project missing deps in the main checkout is skipped with a warning.

    Silent skipping is exactly the legacy weakness this rewrite avoids: the
    operator must be able to trace a later ``vite: command not found`` back
    to an un-installed main checkout.
    """
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    # Main has the manifest but dependencies were never installed.
    _write_file(repo_path / "frontend" / "package.json", "{}\n")
    _write_file(worktree_path / "frontend" / "package.json", "{}\n")

    with caplog.at_level(logging.WARNING):
        linked_relative_paths = link_frontend_node_modules(repo_path, worktree_path)

    assert linked_relative_paths == []
    assert not (worktree_path / "frontend" / "node_modules").exists()
    assert "has no node_modules" in caplog.text


def test_returns_empty_when_no_frontend_projects(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    worktree_path = tmp_path / "worktree"
    _write_file(repo_path / "README.md", "no frontend here\n")
    _write_file(worktree_path / "README.md", "no frontend here\n")

    assert link_frontend_node_modules(repo_path, worktree_path) == []


def test_returns_empty_when_worktree_is_repo_root(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _make_frontend_project(repo_path / "frontend")

    assert link_frontend_node_modules(repo_path, repo_path) == []
